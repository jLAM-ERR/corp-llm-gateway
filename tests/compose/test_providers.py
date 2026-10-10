"""The base compose stack runs one gateway per provider, picked by COMPOSE_PROFILES.

`anthropic`, `codex` and `corp` each start their own gateway container
(`gateway-anthropic` / `gateway-codex` / `gateway-corp`) from one shared
definition, with its own litellm config, bridge flag and loopback port. The
subscription bridges stay one per process (settings.FORWARD_AUTH_EXCLUSIVE_MESSAGE).

Two properties hold for every provider and neither is visible in one file alone:

  * a subscription token can only reach its own provider — the hook gates on the
    client-visible model alias, but litellm resolves the deployment after the hook
    runs, so the routing table of each config is the binding control;
  * no litellm master key, admin UI, gateway-held provider key or litellm
    database reaches a gateway: the developer's own token is the upstream
    credential, and litellm must not consume it as a virtual key.

`profile-guard` refuses `up` when COMPOSE_PROFILES names no provider: `postgres`
waits for it, so the stack fails loudly instead of starting with no gateway.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.compose.nginx_support import (
    COMPOSE,
    COMPOSE_DIR,
    MODE_A_ONLY_KEYS,
    REQUIRED_ENV,
    ROOT,
    bare_compose_env,
    compose_cli_available,
    render,
    skip_or_fail,
)

ENV_EXAMPLE = COMPOSE_DIR / ".env.example"
LITELLM_DIR = COMPOSE_DIR / "litellm"
ANTHROPIC_CONFIG = LITELLM_DIR / "config.anthropic.yaml"

LITELLM_CONFIG_TARGET = "/etc/litellm/config.yaml"
SHIM_TARGET = "/etc/litellm/corp_llm_gateway/bootstrap.py"
SHIM_SOURCE = Path("litellm/corp_llm_gateway/bootstrap.py")
GATEWAY_IMAGE = "ghcr.io/jlam-err/corp-llm-gateway:"
AUDIT_LABEL = "com.corp-llm-gateway.audit-source"

PROVIDERS = ("anthropic", "codex", "corp")
ALL_PROVIDERS = ",".join(PROVIDERS)
GATEWAYS = {provider: f"gateway-{provider}" for provider in PROVIDERS}
# (CORP_LLM_FORWARD_ANTHROPIC_AUTH, CORP_LLM_FORWARD_CHATGPT_AUTH) per provider.
BRIDGES = {"anthropic": ("1", "0"), "codex": ("0", "1"), "corp": ("0", "0")}
PORTS = {
    "anthropic": ("GATEWAY_ANTHROPIC_PORT", 4000),
    "codex": ("GATEWAY_CODEX_PORT", 4001),
    "corp": ("GATEWAY_CORP_PORT", 4002),
}
# Nothing a gateway-image service may be given: Mode A, the admin UI, a
# gateway-held provider key, litellm's own database.
FORBIDDEN_ENV = (
    *MODE_A_ONLY_KEYS,
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DATABASE_URL",
    "DIRECT_URL",
    "STORE_MODEL_IN_DB",
)
NO_PROVIDER_MESSAGE = "names none of anthropic, codex, corp"


needs_compose_cli = pytest.mark.skipif(
    not compose_cli_available(), reason="docker compose CLI not on PATH"
)


def _base_services() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def _anthropic_config() -> dict[str, Any]:
    return yaml.safe_load(ANTHROPIC_CONFIG.read_text())


def _rendered(tmp_path: Path, providers: str = ALL_PROVIDERS, **kwargs: Any) -> dict[str, Any]:
    return render(tmp_path, providers=providers, **kwargs).services


def _mounts(project_dir: Path, service: dict[str, Any]) -> dict[str, Path]:
    return {
        volume["target"]: Path(volume["source"]).resolve().relative_to(project_dir)
        for volume in service.get("volumes", [])
    }


def _gateway_image_services(services: dict[str, Any]) -> dict[str, Any]:
    return {
        name: service
        for name, service in services.items()
        if str(service.get("image", "")).startswith(GATEWAY_IMAGE)
    }


# --------------------------------------------------------------------------- #
# the routing table is the binding control
# --------------------------------------------------------------------------- #


def test_oauth_config_routes_only_native_anthropic_without_a_static_secret() -> None:
    deployments = _anthropic_config()["model_list"]

    assert len(deployments) == 1
    assert deployments[0]["model_name"] == "claude-*"
    params = deployments[0]["litellm_params"]
    assert params["model"] == "anthropic/claude-*"
    assert params["api_key"] == "oauth-passthrough-placeholder"
    # A placeholder shaped like a real OAuth token would select litellm's OAuth
    # branch at boot instead of failing upstream when the bridge is off.
    assert not params["api_key"].startswith("sk-ant-")


def test_oauth_config_has_no_wildcard_and_no_foreign_provider_route() -> None:
    for deployment in _anthropic_config()["model_list"]:
        assert deployment["model_name"] != "*"
        assert deployment["litellm_params"]["model"].startswith("anthropic/")


def test_oauth_config_never_reads_a_gateway_held_provider_key() -> None:
    # The developer's own token is the only upstream credential. An
    # `os.environ/ANTHROPIC_API_KEY` here would let a stale key serve traffic the
    # operator believes is subscription-billed. Asserted on the parsed values,
    # not the file text — the header comment names the variable to say it is NOT read.
    for deployment in _anthropic_config()["model_list"]:
        for value in deployment["litellm_params"].values():
            assert not str(value).startswith("os.environ/"), deployment["model_name"]


def test_oauth_config_uses_the_production_composition_root() -> None:
    # NOT _demo_guardrail: that one seeds an in-memory token store with
    # `demo-team-token` and has no audit chain.
    settings = _anthropic_config()["litellm_settings"]

    assert settings["callbacks"] == ["corp_llm_gateway.bootstrap.guardrail"]
    assert settings["drop_params"] is True
    assert settings["json_logs"] is True


# --------------------------------------------------------------------------- #
# one gateway per provider, from one definition
# --------------------------------------------------------------------------- #


def test_the_base_stack_hands_both_bridge_flags_to_the_container() -> None:
    # bootstrap.build_guardrail() reads both; each gateway sets both explicitly,
    # so no provider inherits another's bridge.
    services = _base_services()

    for provider, (anthropic, chatgpt) in BRIDGES.items():
        environment = services[GATEWAYS[provider]]["environment"]
        assert environment["CORP_LLM_FORWARD_ANTHROPIC_AUTH"] == anthropic, provider
        assert environment["CORP_LLM_FORWARD_CHATGPT_AUTH"] == chatgpt, provider


def test_each_gateway_is_switched_by_its_own_profile_only() -> None:
    services = _base_services()

    gateways = {name for name in services if name.startswith("gateway-")}
    assert gateways == set(GATEWAYS.values())
    for provider, name in GATEWAYS.items():
        assert services[name]["profiles"] == [provider]
    assert "profiles" not in services["profile-guard"]


def test_the_three_gateways_share_one_definition() -> None:
    services = _base_services()
    first, *others = (services[name] for name in GATEWAYS.values())

    for other in others:
        differing = {key for key in first.keys() | other.keys() if first.get(key) != other.get(key)}
        assert differing == {"profiles", "volumes", "environment", "ports"}, differing
        changed = {
            key
            for key in first["environment"].keys() | other["environment"].keys()
            if first["environment"].get(key) != other["environment"].get(key)
        }
        assert changed <= {"CORP_LLM_FORWARD_ANTHROPIC_AUTH", "CORP_LLM_FORWARD_CHATGPT_AUTH"}
        assert first["volumes"][1:] == other["volumes"][1:]
    assert "<<: *gateway" in COMPOSE.read_text()


@needs_compose_cli
@pytest.mark.parametrize("provider", PROVIDERS)
def test_each_provider_profile_alone_starts_exactly_its_gateway(
    tmp_path: Path, provider: str
) -> None:
    services = _rendered(tmp_path, providers=provider)

    assert {name for name in services if name.startswith("gateway-")} == {GATEWAYS[provider]}
    assert "profile-guard" in services


@needs_compose_cli
def test_all_three_provider_profiles_start_three_gateways(tmp_path: Path) -> None:
    services = _rendered(tmp_path)

    assert {name for name in services if name.startswith("gateway-")} == set(GATEWAYS.values())


@needs_compose_cli
@pytest.mark.parametrize("profiles", ["", "nginx"], ids=["unset", "nginx-only"])
def test_no_provider_profile_starts_no_gateway(tmp_path: Path, profiles: str) -> None:
    services = _rendered(tmp_path, providers="", profiles=profiles or None)

    assert not [name for name in services if name.startswith("gateway-")]
    assert "profile-guard" in services


@needs_compose_cli
@pytest.mark.parametrize("provider", PROVIDERS)
def test_each_gateway_carries_its_config_bridge_port_and_shim(
    tmp_path: Path, provider: str
) -> None:
    result = render(tmp_path, providers=ALL_PROVIDERS)
    service = result.services[GATEWAYS[provider]]
    mounts = _mounts(result.project_dir, service)
    environment = service["environment"]
    key, port = PORTS[provider]

    assert mounts[LITELLM_CONFIG_TARGET] == Path(f"litellm/config.{provider}.yaml")
    assert mounts[SHIM_TARGET] == SHIM_SOURCE
    assert (
        environment["CORP_LLM_FORWARD_ANTHROPIC_AUTH"],
        environment["CORP_LLM_FORWARD_CHATGPT_AUTH"],
    ) == (BRIDGES[provider])
    assert [(p["host_ip"], int(p["published"]), p["target"]) for p in service["ports"]] == [
        ("127.0.0.1", port, 4000)
    ]
    assert f"${{{key}:-{port}}}" in COMPOSE.read_text()


@needs_compose_cli
@pytest.mark.parametrize("provider", PROVIDERS)
def test_each_gateway_carries_the_audit_label_and_pinned_logging(
    tmp_path: Path, provider: str
) -> None:
    service = _rendered(tmp_path)[GATEWAYS[provider]]

    assert service["labels"] == {AUDIT_LABEL: "gateway-stdout"}
    assert service["logging"]["driver"] == "json-file"
    assert service["logging"]["options"] == {
        "labels": AUDIT_LABEL,
        "max-size": "100m",
        "max-file": "10",
        "compress": "false",
    }


@needs_compose_cli
def test_no_gateway_image_service_gets_a_master_key_ui_provider_key_or_litellm_database(
    tmp_path: Path,
) -> None:
    services = _gateway_image_services(_rendered(tmp_path))

    assert set(services) == {*GATEWAYS.values(), "profile-guard"}
    for name, service in services.items():
        leaked = set(service.get("environment") or {}) & set(FORBIDDEN_ENV)
        assert not leaked, (name, leaked)


def test_no_compose_file_names_a_mode_a_provider_key_or_litellm_database_switch() -> None:
    # DATABASE_URL itself stays: Langfuse needs its own.
    names = (*MODE_A_ONLY_KEYS, "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STORE_MODEL_IN_DB")
    for path in sorted(COMPOSE_DIR.glob("docker-compose*.yml")):
        text = path.read_text()
        for key in names:
            assert key not in text, (path.name, key)
    assert not (COMPOSE_DIR / "postgres" / "initdb" / "00-create-litellm-db.sh").exists()


@needs_compose_cli
def test_a_master_key_left_in_env_reaches_no_gateway(tmp_path: Path) -> None:
    # A leftover .env line from 1.0 must not reach a container: litellm would
    # consume the developer's bearer as a virtual key and 401 before pre_call.
    leftovers = "".join(f"{key}=left-over-{key.lower()}\n" for key in FORBIDDEN_ENV)
    services = _gateway_image_services(_rendered(tmp_path, env_extra=leftovers))

    for name, service in services.items():
        assert not set(service.get("environment") or {}) & set(FORBIDDEN_ENV), name


@needs_compose_cli
@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_oracle_defaults_off_on_every_gateway(tmp_path: Path, provider: str) -> None:
    environment = _rendered(tmp_path)[GATEWAYS[provider]]["environment"]

    assert environment["CORP_LLM_ORACLE_ENABLED"] == "0"


@needs_compose_cli
def test_the_merged_stack_mounts_the_oauth_config_as_the_only_litellm_config(
    tmp_path: Path,
) -> None:
    result = render(tmp_path, providers=ALL_PROVIDERS)

    for provider, name in GATEWAYS.items():
        sources = [
            Path(volume["source"]).resolve().relative_to(result.project_dir)
            for volume in result.services[name]["volumes"]
        ]
        configs = [source for source in sources if source.parent == Path("litellm")]
        assert configs == [Path(f"litellm/config.{provider}.yaml")], (name, configs)
    assert not (LITELLM_DIR / "config.yaml").exists()


@needs_compose_cli
def test_the_merged_stack_arms_the_bridge_and_injects_no_master_key(tmp_path: Path) -> None:
    environment = _rendered(tmp_path, providers="anthropic")[GATEWAYS["anthropic"]]["environment"]

    assert environment["CORP_LLM_FORWARD_ANTHROPIC_AUTH"] == "1"
    assert environment["CORP_LLM_FORWARD_CHATGPT_AUTH"] == "0"
    # Absent, not `null` and not empty: settings.master_key_conflict() counts an
    # empty value as set.
    for key in MODE_A_ONLY_KEYS:
        assert key not in environment, key


# --------------------------------------------------------------------------- #
# the no-provider refusal
# --------------------------------------------------------------------------- #


def test_the_profile_guard_runs_once_on_the_gateway_image_outside_the_audit_trail() -> None:
    guard = _base_services()["profile-guard"]

    assert guard["image"] == _base_services()["gateway-anthropic"]["image"]
    assert guard["restart"] == "no"
    assert guard["environment"] == ["COMPOSE_PROFILES=${COMPOSE_PROFILES:-}"]
    assert "labels" not in guard
    assert "ports" not in guard and "volumes" not in guard


def test_postgres_waits_for_the_profile_guard_and_every_gateway_for_postgres() -> None:
    services = _base_services()

    assert services["postgres"]["depends_on"] == {
        "profile-guard": {"condition": "service_completed_successfully"}
    }
    for name in GATEWAYS.values():
        assert services[name]["depends_on"]["postgres"] == {"condition": "service_healthy"}


@pytest.mark.parametrize("service", ["vector", "nginx", "nginx-ports"])
def test_the_audit_and_front_door_services_wait_for_the_gateways_only_if_enabled(
    service: str,
) -> None:
    depends_on = _base_services()[service]["depends_on"]

    for name in GATEWAYS.values():
        assert depends_on[name] == {"condition": "service_started", "required": False}


def _guard_script() -> str:
    (script,) = _base_services()["profile-guard"]["command"]
    # Compose turns `$$` into `$` before the shell sees it.
    return script.replace("$$", "$")


def _run_guard(profiles: str | None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ["PATH"]}
    if profiles is not None:
        env["COMPOSE_PROFILES"] = profiles
    return subprocess.run(
        ["/bin/sh", "-c", _guard_script()], env=env, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize(
    "profiles",
    ["anthropic", "codex", "corp", "anthropic,codex", "nginx,corp", "codex,nginx-ports"],
)
def test_the_profile_guard_accepts_a_provider_token(profiles: str) -> None:
    result = _run_guard(profiles)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


@pytest.mark.parametrize(
    "profiles",
    [None, "", "nginx", "nginx-ports", "corporate", "Anthropic", "anthropic codex", " corp", "*"],
    ids=["unset", "empty", "nginx", "nginx-ports", "corporate", "case", "space", "padded", "glob"],
)
def test_the_profile_guard_refuses_without_an_exact_provider_token(profiles: str | None) -> None:
    result = _run_guard(profiles)

    assert result.returncode != 0
    assert NO_PROVIDER_MESSAGE in result.stderr
    assert "COMPOSE_PROFILES" in result.stderr


# A small image with /bin/sh stands in for the gateway image (the guard script is
# POSIX sh) and for postgres, so the live test pulls nothing large.
STAND_IN_IMAGE = "nginx:1.27-alpine"
LIVE_OVERRIDE = f"""
services:
  profile-guard:
    image: {STAND_IN_IMAGE}
  postgres:
    image: {STAND_IN_IMAGE}
    entrypoint: ["sleep", "120"]
    volumes: !reset []
    healthcheck:
      disable: true
"""


def _docker(
    *args: str, cwd: Path | None = None, timeout: float = 180
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=bare_compose_env(),
        timeout=timeout,
        check=False,
    )


@pytest.fixture
def live_project(tmp_path: Path) -> Any:
    if not compose_cli_available() or _docker("version").returncode != 0:
        skip_or_fail("docker daemon not reachable — the live refusal runs real containers")
    if _docker("image", "inspect", STAND_IN_IMAGE).returncode != 0:
        pulled = _docker("pull", STAND_IN_IMAGE, timeout=300)
        if pulled.returncode != 0:
            skip_or_fail(f"cannot pull {STAND_IN_IMAGE}: {pulled.stderr.strip()[-300:]}")
    project_dir = tmp_path / "compose"
    shutil.copytree(COMPOSE_DIR, project_dir)
    (project_dir / "docker-compose.live-test.yml").write_text(LIVE_OVERRIDE)
    name = f"corp-guard-{uuid.uuid4().hex[:8]}"

    def up(profiles: str | None) -> tuple[subprocess.CompletedProcess[str], str]:
        lines = [f"{key}=render-fixture" for key in REQUIRED_ENV]
        if profiles is not None:
            lines.append(f"COMPOSE_PROFILES={profiles}")
        (project_dir / ".env").write_text("\n".join(lines) + "\n")
        files = ["-f", "docker-compose.yml", "-f", "docker-compose.live-test.yml"]
        result = _docker("compose", "-p", name, *files, "up", "-d", "postgres", cwd=project_dir)
        logs = _docker("compose", "-p", name, *files, "logs", "profile-guard", cwd=project_dir)
        return result, logs.stdout + logs.stderr

    try:
        yield up
    finally:
        _docker(
            "compose",
            "-p",
            name,
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.live-test.yml",
            "down",
            "-v",
            "--remove-orphans",
            cwd=project_dir,
        )


@pytest.mark.parametrize(
    "profiles", [None, "nginx", "corporate"], ids=["unset", "nginx-only", "corporate"]
)
def test_up_refuses_to_start_without_a_provider_profile(
    live_project: Any, profiles: str | None
) -> None:
    result, logs = live_project(profiles)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "profile-guard" in result.stderr
    assert NO_PROVIDER_MESSAGE in logs


def test_up_starts_once_a_provider_profile_is_set(live_project: Any) -> None:
    result, logs = live_project("corp")

    assert result.returncode == 0, result.stdout + result.stderr
    assert NO_PROVIDER_MESSAGE not in logs


# --------------------------------------------------------------------------- #
# .env.example and the boot-time unit
# --------------------------------------------------------------------------- #


def test_the_env_example_ships_the_provider_switch_commented() -> None:
    # Commented on purpose: a copied .env starts nothing until the operator picks
    # a provider, and profile-guard says so by name.
    lines = ENV_EXAMPLE.read_text().splitlines()

    assert "# COMPOSE_PROFILES=anthropic" in lines
    assert not [line for line in lines if line.startswith("COMPOSE_PROFILES=")]
    for provider in PROVIDERS:
        assert any(line.startswith(f"#   {provider} ") for line in lines), provider


def test_the_env_example_offers_each_gateway_port_and_no_mode_a_key() -> None:
    text = ENV_EXAMPLE.read_text()

    for key, port in PORTS.values():
        assert f"\n{key}={port}\n" in text, key
    for key in FORBIDDEN_ENV:
        assert f"\n{key}=" not in text and f"\n# {key}=" not in text, key
    assert "docker-compose.oauth.yml" not in text


def test_the_autostart_unit_runs_a_bare_compose_up_so_mode_b_needs_compose_file() -> None:
    # The bare command is what makes the .env decide: COMPOSE_PROFILES picks the
    # providers and COMPOSE_FILE the overlays. An explicit `-f` or `--profile`
    # here would pin the unit to one stack.
    unit = (ROOT / "scripts" / "deploy" / "corp-llm-gateway.service").read_text()
    exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))

    assert exec_start == "ExecStart=/usr/bin/docker compose up -d"
    assert " -f " not in exec_start

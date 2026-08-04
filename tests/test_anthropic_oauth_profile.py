from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from corp_llm_gateway import bootstrap, config, settings
from corp_llm_gateway.settings import ConfigError

ROOT = Path(__file__).resolve().parents[1]

PROFILE_PATH = ROOT / "docker/anthropic-oauth/litellm-config.yaml"
OVERLAY_PATH = ROOT / "docker-compose.anthropic-oauth.yml"
DEMO_COMPOSE_PATH = ROOT / "docker-compose.demo.yml"
CLIENT_ENV_PATH = ROOT / "docker/anthropic-oauth/claude-env.sh"
ENV_DEMO_EXAMPLE_PATH = ROOT / ".env.demo.example"

LITELLM_CONFIG_TARGET = "/etc/litellm/config.yaml"
ANTHROPIC_CONFIG_SOURCE = Path("docker/anthropic-oauth/litellm-config.yaml")
DEMO_CONFIG_SOURCE = Path("docker/demo-litellm/litellm-config.yaml")

# Obvious non-credential; only its presence in the merged render matters.
POISON_MASTER_KEY = "not-a-real-master-key-fixture"


def _profile() -> dict:
    return yaml.safe_load(PROFILE_PATH.read_text())


def test_anthropic_oauth_profile_routes_native_anthropic_without_static_secret() -> None:
    deployments = _profile()["model_list"]

    assert len(deployments) == 1
    params = deployments[0]["litellm_params"]
    assert deployments[0]["model_name"] == "claude-*"
    assert params["model"] == "anthropic/claude-*"
    assert params["api_key"] == "oauth-passthrough-placeholder"
    # A placeholder shaped like a real OAuth token would select litellm's OAuth
    # branch at boot instead of failing upstream when the bridge is off.
    assert not params["api_key"].startswith("sk-ant-")


def test_anthropic_oauth_profile_has_no_wildcard_and_no_foreign_provider_route() -> None:
    # The binding control behind the hook's alias gate: the gate reads the
    # client-visible model alias, but litellm resolves the deployment after the
    # hook runs. Only an Anthropic-only config keeps the subscription token from
    # reaching another provider.
    for deployment in _profile()["model_list"]:
        assert deployment["model_name"] != "*"
        assert deployment["litellm_params"]["model"].startswith("anthropic/")


def test_anthropic_oauth_profile_uses_the_demo_guardrail_callback() -> None:
    # bootstrap's make_auth_middleware() reads CORP_LLM_PG_DSN, which the demo
    # compose does not set — with it every X-Corp-Auth request 401s before the
    # OAuth bridge runs.
    settings = _profile()["litellm_settings"]

    assert settings["callbacks"] == ["corp_llm_gateway._demo_guardrail.guardrail"]
    assert settings["drop_params"] is True
    assert settings["json_logs"] is True


def test_anthropic_oauth_overlay_enables_the_bridge_and_mounts_its_own_config() -> None:
    overlay = yaml.safe_load(OVERLAY_PATH.read_text())
    litellm = overlay["services"]["litellm"]

    assert litellm["environment"]["CORP_LLM_FORWARD_ANTHROPIC_AUTH"] == "1"
    assert litellm["build"]["dockerfile"] == "docker/anthropic-oauth/Dockerfile"
    assert (
        "./docker/anthropic-oauth/litellm-config.yaml:/etc/litellm/config.yaml:ro"
        in litellm["volumes"]
    )


def test_anthropic_oauth_overlaid_stack_sets_no_master_key() -> None:
    # A master key makes litellm consume the inbound Authorization as a virtual
    # key and reject the request before pre_call ever sees the OAuth bearer. The
    # overlay is applied on top of the demo stack, so check both layers — and the
    # parsed environment, so a comment naming the key does not pass for setting it.
    for path in (ROOT / "docker-compose.demo.yml", OVERLAY_PATH):
        environment = yaml.safe_load(path.read_text())["services"]["litellm"].get("environment", {})
        assert "LITELLM_MASTER_KEY" not in environment

    for line in (ROOT / ".env.demo.example").read_text().splitlines():
        assert not line.strip().startswith("LITELLM_MASTER_KEY=")


def test_anthropic_oauth_overlay_does_not_enable_the_conflicting_bridge() -> None:
    # The two forward-auth bridges are mutually exclusive; both on refuses to boot.
    environment = yaml.safe_load(OVERLAY_PATH.read_text())["services"]["litellm"]["environment"]

    assert "CORP_LLM_FORWARD_CHATGPT_AUTH" not in environment


def test_anthropic_oauth_overlay_ships_no_credential() -> None:
    # The prefix itself is legitimately present (docs + the shape check); a real
    # token is the prefix followed by a long opaque body.
    token_shape = re.compile(r"sk-ant-[a-z]+[A-Za-z0-9_-]{12,}")
    for path in (PROFILE_PATH, OVERLAY_PATH, CLIENT_ENV_PATH):
        text = path.read_text()
        assert token_shape.search(text) is None
        assert "ANTHROPIC_API_KEY=" not in text


def test_anthropic_oauth_client_env_keeps_the_corp_header_in_one_place() -> None:
    text = CLIENT_ENV_PATH.read_text()

    assert text.count("X-Corp-Auth") == 1
    assert 'export ANTHROPIC_CUSTOM_HEADERS="X-Corp-Auth: $CORP_TEAM_TOKEN"' in text
    assert 'export ANTHROPIC_BASE_URL="$CORP_GATEWAY_URL"' in text
    # A stale API key in the shell would shadow the subscription token.
    assert "unset ANTHROPIC_API_KEY" in text


def test_demo_env_example_keeps_client_anthropic_vars_out_of_the_container() -> None:
    # litellm reads ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN from its own process
    # env; .env.demo is env_file for the litellm container, so an uncommented
    # assignment there self-loops the upstream call or installs a shared
    # server-side fallback credential.
    for line in (ROOT / ".env.demo.example").read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not stripped.startswith("ANTHROPIC_BASE_URL=")
        assert not stripped.startswith("ANTHROPIC_AUTH_TOKEN=")
        assert not stripped.startswith("ANTHROPIC_CUSTOM_HEADERS=")


# ── merged-render asserts ────────────────────────────────────────────────────
#
# The static asserts above read each compose file on its own, so they can only
# prove that the overlay MENTIONS its config. What has to hold is a property of
# the MERGED service: the overlay's config must be the SOLE mount at
# /etc/litellm/config.yaml. If the mounts ever merged instead of replacing, the
# demo's wildcard `"*"` → hosted_vllm route could win and hand the Anthropic
# subscription token to the corp vLLM. So render the stack the way it is run.


def test_install_doc_lists_the_compose_files_in_the_order_the_render_asserts() -> None:
    # Compose merges later files over earlier ones, so the overlay must come
    # second — reversed, the demo's wildcard config would win. The render below
    # fixes that order, so pin the documented command to the same one.
    text = (ROOT / "docs/ops/install.md").read_text()
    commands = [
        block
        for block in text.split("docker compose")[1:]
        if OVERLAY_PATH.name in block.split("```")[0]
    ]

    assert commands
    for block in commands:
        listed = re.findall(r"-f\s+(docker-compose[\w.-]*\.yml)", block.split("```")[0])
        assert listed == [DEMO_COMPOSE_PATH.name, OVERLAY_PATH.name]


def _compose_cli_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True, check=False
    )
    return probe.returncode == 0


needs_compose_cli = pytest.mark.skipif(
    not _compose_cli_available(), reason="docker compose CLI not on PATH"
)


class _Render:
    """A `docker compose config` render plus the project dir it was made in."""

    def __init__(self, project_dir: Path, doc: dict[str, Any]) -> None:
        self.project_dir = project_dir.resolve()
        self.doc = doc

    @property
    def litellm(self) -> dict[str, Any]:
        return self.doc["services"]["litellm"]

    def _relative(self, source: str) -> Path:
        return Path(source).resolve().relative_to(self.project_dir)

    def bind_sources(self) -> list[Path]:
        return [
            self._relative(volume["source"])
            for volume in self.litellm.get("volumes", [])
            if volume.get("type") == "bind"
        ]

    def mounts_at(self, target: str) -> list[Path]:
        return [
            self._relative(volume["source"])
            for volume in self.litellm.get("volumes", [])
            if volume.get("target") == target
        ]


def _interpolated_names() -> set[str]:
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")
    names: set[str] = set()
    for path in (DEMO_COMPOSE_PATH, OVERLAY_PATH):
        names |= set(pattern.findall(path.read_text()))
    return names


def _render(project_dir: Path, env_demo: str) -> _Render:
    for path in (DEMO_COMPOSE_PATH, OVERLAY_PATH):
        shutil.copy(path, project_dir / path.name)
    (project_dir / ".env.demo").write_text(env_demo)
    # Strip everything compose could interpolate from the developer's shell, so
    # the render is the template's and not this laptop's.
    interpolated = _interpolated_names()
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in interpolated and not key.startswith("COMPOSE_")
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            "corp-oauth-render",
            "-f",
            DEMO_COMPOSE_PATH.name,
            "-f",
            OVERLAY_PATH.name,
            "config",
        ],
        cwd=project_dir,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"docker compose config failed (exit {result.returncode}):\n{result.stderr}")
    return _Render(project_dir, yaml.safe_load(result.stdout))


@pytest.fixture(scope="module")
def merged_stack(tmp_path_factory: pytest.TempPathFactory) -> _Render:
    """The overlaid stack as `docs/ops/install.md` tells developers to run it."""
    return _render(tmp_path_factory.mktemp("clean"), ENV_DEMO_EXAMPLE_PATH.read_text())


@needs_compose_cli
def test_merged_stack_mounts_the_anthropic_config_as_the_only_litellm_config(
    merged_stack: _Render,
) -> None:
    mounted = merged_stack.mounts_at(LITELLM_CONFIG_TARGET)

    assert mounted == [ANTHROPIC_CONFIG_SOURCE]
    assert DEMO_CONFIG_SOURCE not in merged_stack.bind_sources()

    # The file that actually wins carries the Anthropic-only routing table.
    config_yaml = yaml.safe_load((ROOT / mounted[0]).read_text())
    deployments = config_yaml["model_list"]
    assert [d["model_name"] for d in deployments] == ["claude-*"]
    assert all(d["litellm_params"]["model"].startswith("anthropic/") for d in deployments)
    assert config_yaml["litellm_settings"]["callbacks"] == [
        "corp_llm_gateway._demo_guardrail.guardrail"
    ]


@needs_compose_cli
def test_merged_stack_arms_only_the_anthropic_bridge_and_sets_no_master_key(
    merged_stack: _Render,
) -> None:
    environment = merged_stack.litellm["environment"]

    assert environment["CORP_LLM_FORWARD_ANTHROPIC_AUTH"] == "1"
    assert "CORP_LLM_FORWARD_CHATGPT_AUTH" not in environment
    # env_file + both `environment:` maps, merged — the surface the container sees.
    assert "LITELLM_MASTER_KEY" not in environment
    assert merged_stack.litellm["image"] == "corp-llm-gateway-anthropic-oauth:local"
    assert merged_stack.litellm["build"]["dockerfile"] == "docker/anthropic-oauth/Dockerfile"


@needs_compose_cli
def test_a_master_key_in_env_demo_reaches_the_container_and_stops_the_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # .env.demo is developer-owned and git-ignored, and `cp -n` keeps an existing
    # one, so the clean-template render above does not cover this input.
    poisoned = _render(
        tmp_path,
        ENV_DEMO_EXAMPLE_PATH.read_text() + f"LITELLM_MASTER_KEY={POISON_MASTER_KEY}\n",
    )
    environment = poisoned.litellm["environment"]

    # The hole is real: compose injects it, no layer neutralises it.
    assert environment["LITELLM_MASTER_KEY"] == POISON_MASTER_KEY

    for name in settings.all_keys():
        monkeypatch.delenv(name, raising=False)
    empty = tmp_path / "hermetic-config.toml"
    empty.write_text("")
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(empty))
    for name, value in environment.items():
        monkeypatch.setenv(name, "" if value is None else str(value))
    config.reset_cache()
    try:
        with pytest.raises(ConfigError) as exc:
            bootstrap.build_guardrail()
    finally:
        config.reset_cache()

    assert settings.MASTER_KEY_VS_FORWARD_AUTH_MESSAGE in exc.value.problems
    # A boot diagnostic is a log line; it must not print the key it complains about.
    assert POISON_MASTER_KEY not in str(exc.value)

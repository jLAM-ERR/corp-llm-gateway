"""Developer token issuance on the compose stack: `docker-compose.issuance.yml`.

The team map is a TOML table, so it can only reach the gateway in a config file.
The overlay mounts `gateway/config.toml` on every provider's gateway and passes
the scalar keys by bare name; the base stack stays unchanged, so a deploy without
Keycloak is not affected.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from corp_llm_gateway import settings

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DIR = ROOT / "compose"
COMPOSE = COMPOSE_DIR / "docker-compose.yml"
OVERLAY = COMPOSE_DIR / "docker-compose.issuance.yml"
ENV_EXAMPLE = COMPOSE_DIR / ".env.example"
CONFIG_EXAMPLE = COMPOSE_DIR / "gateway" / "config.toml.example"

TARGET = "/etc/corp-llm-gateway/config.toml"
TEAM_MAP_KEY = "CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP"
ISSUE_SCALARS = tuple(
    key
    for key in settings.all_keys()
    if key.startswith("CORP_GATEWAY_ISSUE_") and key != TEAM_MAP_KEY
)


GATEWAYS = ("gateway-anthropic", "gateway-codex", "gateway-corp")


def _gateways(path: Path) -> dict[str, dict[str, Any]]:
    services = yaml.safe_load(path.read_text())["services"]
    return {name: services[name] for name in GATEWAYS}


def test_the_scalar_list_covers_every_issuance_key() -> None:
    assert len(ISSUE_SCALARS) == 12


def test_the_overlay_mounts_the_config_file_read_only_and_never_creates_it() -> None:
    # create_host_path: false — a missing file must fail `up`, not become an
    # empty directory that the loader skips (issuance silently off).
    for name, gateway in _gateways(OVERLAY).items():
        assert gateway["volumes"] == [
            {
                "type": "bind",
                "source": "./gateway/config.toml",
                "target": TARGET,
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        ], name


def test_the_overlay_points_the_loader_at_the_file_and_passes_the_scalars_by_bare_name() -> None:
    for name, gateway in _gateways(OVERLAY).items():
        environment = gateway["environment"]
        assert f"CORP_LLM_GATEWAY_CONFIG_FILE={TARGET}" in environment, name
        # Bare names: unset in .env means absent, so the file's value is not shadowed.
        for key in ISSUE_SCALARS:
            assert key in environment, (name, key)
        assert TEAM_MAP_KEY not in " ".join(environment), name


def test_the_base_stack_carries_no_issuance_wiring() -> None:
    for name, gateway in _gateways(COMPOSE).items():
        assert not [e for e in gateway["environment"] if "CORP_GATEWAY_ISSUE_" in e], name
        assert not [v for v in gateway["volumes"] if TARGET in str(v)], name
    assert "CORP_GATEWAY_ISSUE_" not in COMPOSE.read_text()


def test_the_env_example_lists_every_scalar_commented() -> None:
    text = ENV_EXAMPLE.read_text()

    assert "docker-compose.issuance.yml" in text
    for key in ISSUE_SCALARS:
        assert re.search(rf"^# {key}=", text, re.MULTILINE), key
        assert not re.search(rf"^{key}=", text, re.MULTILINE), key


def test_the_env_example_offers_the_compose_file_line_with_the_overlay() -> None:
    line = "# COMPOSE_FILE=docker-compose.yml:docker-compose.issuance.yml"

    assert line in ENV_EXAMPLE.read_text().splitlines()


def test_the_real_config_file_is_never_committed() -> None:
    assert "compose/gateway/config.toml" in (ROOT / ".gitignore").read_text().splitlines()


def test_the_example_config_carries_only_the_ordered_team_map() -> None:
    parsed = tomllib.loads(CONFIG_EXAMPLE.read_text())

    assert list(parsed) == [TEAM_MAP_KEY]
    assert list(parsed[TEAM_MAP_KEY].items()) == [
        ("/devs/payments", "payments"),
        ("/devs/core", "core"),
    ]
    for key in ISSUE_SCALARS:
        assert f"# {key} = " in CONFIG_EXAMPLE.read_text(), key


@pytest.mark.usefixtures("hermetic_gateway_config")
def test_the_example_config_resolves_through_the_gateway_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from corp_llm_gateway import config

    for key in (*ISSUE_SCALARS, "CORP_GATEWAY_OIDC_AUDIENCE"):
        monkeypatch.delenv(key, raising=False)
    target = tmp_path / "config.toml"
    target.write_text(CONFIG_EXAMPLE.read_text())
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(target))
    monkeypatch.setenv("CORP_ENV", "production")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_ISSUER", "https://keycloak.corp.lan/realms/dev")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_AUDIENCE", "corp-gateway-issuance")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID", "corp-gateway-cli")
    config.reset_cache()

    resolved = settings.issuance()

    assert resolved is not None
    assert resolved.team_map == (("/devs/payments", "payments"), ("/devs/core", "core"))


# --------------------------------------------------------------------------- #
# the merged render
# --------------------------------------------------------------------------- #


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

REQUIRED_ENV = (
    "POSTGRES_PASSWORD",
    "GATEWAY_IMAGE_TAG",
    "CORP_LANGFUSE_PUBLIC_KEY",
    "CORP_LANGFUSE_SECRET_KEY",
    "LANGFUSE_CLICKHOUSE_PASSWORD",
    "LANGFUSE_ENCRYPTION_KEY",
    "LANGFUSE_NEXTAUTH_SECRET",
    "LANGFUSE_POSTGRES_PASSWORD",
    "LANGFUSE_SALT",
    "MINIO_ROOT_PASSWORD",
)


def _render(
    tmp_path: Path, *files: Path, env_extra: str = "", provider: str = "anthropic"
) -> dict[str, Any]:
    project_dir = tmp_path / "compose"
    shutil.copytree(COMPOSE_DIR, project_dir)
    shutil.copy(CONFIG_EXAMPLE, project_dir / "gateway" / "config.toml")
    env_text = "".join(f"{key}=render-fixture\n" for key in REQUIRED_ENV)
    env_text += f"COMPOSE_PROFILES={provider}\n" + env_extra
    (project_dir / ".env").write_text(env_text)
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")
    interpolated: set[str] = set()
    for path in (COMPOSE, OVERLAY):
        interpolated |= set(pattern.findall(path.read_text()))
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in interpolated
        and key not in ISSUE_SCALARS
        and key != "LITELLM_MASTER_KEY"
        and not key.startswith("COMPOSE_")
    }
    argv = ["docker", "compose", "--project-name", "corp-issuance-render"]
    for path in files:
        argv += ["-f", path.name]
    argv.append("config")
    result = subprocess.run(
        argv, cwd=project_dir, capture_output=True, text=True, env=env, check=False
    )
    if result.returncode != 0:
        pytest.fail(f"docker compose config failed (exit {result.returncode}):\n{result.stderr}")
    gateway = yaml.safe_load(result.stdout)["services"][f"gateway-{provider}"]
    gateway["_project_dir"] = project_dir.resolve()
    return gateway


def _mounts_at(gateway: dict[str, Any], target: str) -> list[dict[str, Any]]:
    return [v for v in gateway.get("volumes", []) if v.get("target") == target]


@needs_compose_cli
def test_the_merged_stack_mounts_the_config_file_beside_the_oauth_config(
    tmp_path: Path,
) -> None:
    # (CORP_LLM_FORWARD_ANTHROPIC_AUTH, CORP_LLM_FORWARD_CHATGPT_AUTH) per provider.
    for provider, bridges in (
        ("anthropic", ("1", "0")),
        ("codex", ("0", "1")),
        ("corp", ("0", "0")),
    ):
        gateway = _render(tmp_path / provider, COMPOSE, OVERLAY, provider=provider)
        environment = gateway["environment"]

        (mount,) = _mounts_at(gateway, TARGET)
        assert Path(mount["source"]) == gateway["_project_dir"] / "gateway" / "config.toml"
        assert mount["read_only"] is True
        (config,) = _mounts_at(gateway, "/etc/litellm/config.yaml")
        assert Path(config["source"]).name == f"config.{provider}.yaml"
        assert (
            environment["CORP_LLM_FORWARD_ANTHROPIC_AUTH"],
            environment["CORP_LLM_FORWARD_CHATGPT_AUTH"],
        ) == bridges, provider


@needs_compose_cli
def test_the_merged_stack_leaves_unset_scalars_absent_and_passes_set_ones(
    tmp_path: Path,
) -> None:
    for provider in ("anthropic", "codex", "corp"):
        gateway = _render(
            tmp_path / provider,
            COMPOSE,
            OVERLAY,
            env_extra="CORP_GATEWAY_ISSUE_OIDC_ISSUER=https://keycloak.corp.lan/realms/dev\n",
            provider=provider,
        )
        environment = gateway["environment"]

        assert environment["CORP_LLM_GATEWAY_CONFIG_FILE"] == TARGET, provider
        assert environment["CORP_GATEWAY_ISSUE_OIDC_ISSUER"] == (
            "https://keycloak.corp.lan/realms/dev"
        ), provider
        # `null` is compose's rendering of an unresolved bare name: not set in the container.
        for key in ISSUE_SCALARS:
            if key != "CORP_GATEWAY_ISSUE_OIDC_ISSUER":
                assert environment[key] is None, (provider, key)


@needs_compose_cli
def test_the_stack_without_the_overlay_mounts_no_config_file(tmp_path: Path) -> None:
    gateway = _render(tmp_path, COMPOSE)

    assert _mounts_at(gateway, TARGET) == []
    assert "CORP_LLM_GATEWAY_CONFIG_FILE" not in gateway["environment"]

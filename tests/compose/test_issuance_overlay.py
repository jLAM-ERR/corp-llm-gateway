"""Developer token issuance on the compose stack: `docker-compose.issuance.yml`.

The team map is a TOML table, so it can only reach the gateway in a config file.
The overlay mounts `gateway/config.toml` and passes the scalar keys by bare name;
the base stack stays unchanged, so a subscription deploy without Keycloak is not
affected.
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
OAUTH = COMPOSE_DIR / "docker-compose.oauth.yml"
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


def _litellm(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())["services"]["litellm"]


def test_the_scalar_list_covers_every_issuance_key() -> None:
    assert len(ISSUE_SCALARS) == 12


def test_the_overlay_mounts_the_config_file_read_only_and_never_creates_it() -> None:
    # create_host_path: false — a missing file must fail `up`, not become an
    # empty directory that the loader skips (issuance silently off).
    assert _litellm(OVERLAY)["volumes"] == [
        {
            "type": "bind",
            "source": "./gateway/config.toml",
            "target": TARGET,
            "read_only": True,
            "bind": {"create_host_path": False},
        }
    ]


def test_the_overlay_points_the_loader_at_the_file_and_passes_the_scalars_by_bare_name() -> None:
    environment = _litellm(OVERLAY)["environment"]

    assert f"CORP_LLM_GATEWAY_CONFIG_FILE={TARGET}" in environment
    # Bare names: unset in .env means absent, so the file's value is not shadowed.
    for key in ISSUE_SCALARS:
        assert key in environment, key
    assert TEAM_MAP_KEY not in " ".join(environment)


def test_the_base_stack_carries_no_issuance_wiring() -> None:
    litellm = _litellm(COMPOSE)

    assert not [e for e in litellm["environment"] if "CORP_GATEWAY_ISSUE_" in e]
    assert not [v for v in litellm["volumes"] if TARGET in str(v)]
    assert "CORP_GATEWAY_ISSUE_" not in OAUTH.read_text()


def test_the_env_example_lists_every_scalar_commented() -> None:
    text = ENV_EXAMPLE.read_text()

    assert "docker-compose.issuance.yml" in text
    for key in ISSUE_SCALARS:
        assert re.search(rf"^# {key}=", text, re.MULTILINE), key
        assert not re.search(rf"^{key}=", text, re.MULTILINE), key


def test_the_env_example_offers_the_compose_file_line_with_the_overlay() -> None:
    line = "# COMPOSE_FILE=docker-compose.yml:docker-compose.oauth.yml:docker-compose.issuance.yml"

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


def _render(tmp_path: Path, *files: Path, env_extra: str = "") -> dict[str, Any]:
    project_dir = tmp_path / "compose"
    shutil.copytree(COMPOSE_DIR, project_dir)
    shutil.copy(CONFIG_EXAMPLE, project_dir / "gateway" / "config.toml")
    env_text = "".join(f"{key}=render-fixture\n" for key in REQUIRED_ENV) + env_extra
    (project_dir / ".env").write_text(env_text)
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")
    interpolated: set[str] = set()
    for path in (COMPOSE, OAUTH, OVERLAY):
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
    litellm = yaml.safe_load(result.stdout)["services"]["litellm"]
    litellm["_project_dir"] = project_dir.resolve()
    return litellm


def _mounts_at(litellm: dict[str, Any], target: str) -> list[dict[str, Any]]:
    return [v for v in litellm.get("volumes", []) if v.get("target") == target]


@needs_compose_cli
def test_the_merged_stack_mounts_the_config_file_beside_the_oauth_config(
    tmp_path: Path,
) -> None:
    litellm = _render(tmp_path, COMPOSE, OAUTH, OVERLAY)

    (mount,) = _mounts_at(litellm, TARGET)
    assert Path(mount["source"]) == litellm["_project_dir"] / "gateway" / "config.toml"
    assert mount["read_only"] is True
    (oauth,) = _mounts_at(litellm, "/etc/litellm/config.yaml")
    assert Path(oauth["source"]).name == "config.oauth.yaml"
    assert litellm["environment"]["CORP_LLM_FORWARD_ANTHROPIC_AUTH"] == "1"


@needs_compose_cli
def test_the_merged_stack_leaves_unset_scalars_absent_and_passes_set_ones(
    tmp_path: Path,
) -> None:
    litellm = _render(
        tmp_path,
        COMPOSE,
        OAUTH,
        OVERLAY,
        env_extra="CORP_GATEWAY_ISSUE_OIDC_ISSUER=https://keycloak.corp.lan/realms/dev\n",
    )
    environment = litellm["environment"]

    assert environment["CORP_LLM_GATEWAY_CONFIG_FILE"] == TARGET
    assert environment["CORP_GATEWAY_ISSUE_OIDC_ISSUER"] == "https://keycloak.corp.lan/realms/dev"
    # `null` is compose's rendering of an unresolved bare name: not set in the container.
    assert environment["CORP_GATEWAY_ISSUE_MAX_ACTIVE"] is None


@needs_compose_cli
def test_the_stack_without_the_overlay_mounts_no_config_file(tmp_path: Path) -> None:
    litellm = _render(tmp_path, COMPOSE, OAUTH)

    assert _mounts_at(litellm, TARGET) == []
    assert "CORP_LLM_GATEWAY_CONFIG_FILE" not in litellm["environment"]

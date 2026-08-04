"""Mode B (Anthropic subscription / OAuth) on the PRODUCTION compose stack.

`docker-compose.oauth.yml` + `litellm/config.oauth.yaml` are the production twin
of the demo-stack overlay pinned by `tests/test_anthropic_oauth_profile.py`. Two
properties have to hold and neither is visible in either file alone:

  * the subscription token can only ever reach api.anthropic.com — the hook
    gates on the client-visible model alias, but litellm resolves the actual
    deployment after the hook runs, so the routing table is the binding control;
  * `LITELLM_MASTER_KEY` must be ABSENT (not blank) from the merged render, or
    litellm consumes the developer's bearer as one of its own virtual keys and
    401s before `pre_call` ever runs.

The base file's `${LITELLM_MASTER_KEY:?...}` used to make the second impossible:
compose interpolates every `-f` file before merging them, so no overlay could
have rescued it. The requirement moved into the litellm entrypoint, where it can
tell the two modes apart — that move is pinned here too.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DIR = ROOT / "compose"
COMPOSE = COMPOSE_DIR / "docker-compose.yml"
OVERLAY = COMPOSE_DIR / "docker-compose.oauth.yml"
OAUTH_CONFIG = COMPOSE_DIR / "litellm" / "config.oauth.yaml"
ENV_EXAMPLE = COMPOSE_DIR / ".env.example"

LITELLM_CONFIG_TARGET = "/etc/litellm/config.yaml"
OAUTH_CONFIG_SOURCE = Path("litellm/config.oauth.yaml")
VIRTUAL_KEY_CONFIG_SOURCE = Path("litellm/config.yaml")

MODE_A_ONLY_KEYS = ("LITELLM_MASTER_KEY", "UI_USERNAME", "UI_PASSWORD")


def _service(path: Path, name: str = "litellm") -> dict[str, Any]:
    return yaml.safe_load(path.read_text())["services"][name]


def _base_environment() -> list[str]:
    return _service(COMPOSE)["environment"]


def _oauth_config() -> dict[str, Any]:
    return yaml.safe_load(OAUTH_CONFIG.read_text())


# --------------------------------------------------------------------------- #
# the routing table is the binding control
# --------------------------------------------------------------------------- #


def test_oauth_config_routes_only_native_anthropic_without_a_static_secret() -> None:
    deployments = _oauth_config()["model_list"]

    assert len(deployments) == 1
    assert deployments[0]["model_name"] == "claude-*"
    params = deployments[0]["litellm_params"]
    assert params["model"] == "anthropic/claude-*"
    assert params["api_key"] == "oauth-passthrough-placeholder"
    # A placeholder shaped like a real OAuth token would select litellm's OAuth
    # branch at boot instead of failing upstream when the bridge is off.
    assert not params["api_key"].startswith("sk-ant-")


def test_oauth_config_has_no_wildcard_and_no_foreign_provider_route() -> None:
    for deployment in _oauth_config()["model_list"]:
        assert deployment["model_name"] != "*"
        assert deployment["litellm_params"]["model"].startswith("anthropic/")


def test_oauth_config_never_reads_a_gateway_held_provider_key() -> None:
    # The point of the mode: the developer's own token is the only upstream
    # credential. An `os.environ/ANTHROPIC_API_KEY` here would let a stale key
    # in .env serve traffic that the operator believes is subscription-billed.
    # Asserted on the parsed values, not the file text — the header comment
    # names the variable precisely to say it is NOT read.
    for deployment in _oauth_config()["model_list"]:
        for value in deployment["litellm_params"].values():
            assert not str(value).startswith("os.environ/"), deployment["model_name"]


def test_oauth_config_uses_the_production_composition_root() -> None:
    # NOT _demo_guardrail: that one seeds an in-memory token store with
    # `demo-team-token` and has no audit chain. Mode B is a production mode, so
    # it gets the Postgres token store and the real audit sink.
    settings = _oauth_config()["litellm_settings"]

    assert settings["callbacks"] == ["corp_llm_gateway.bootstrap.guardrail"]
    assert settings["drop_params"] is True
    assert settings["json_logs"] is True


# --------------------------------------------------------------------------- #
# the master key: absent, not blank
# --------------------------------------------------------------------------- #


def test_the_base_stack_no_longer_makes_the_mode_a_keys_a_parse_time_requirement() -> None:
    # `${X:?}` in the base file aborts the whole `-f base -f overlay` render,
    # because compose interpolates each file before merging. Keeping it would
    # make Mode B undeployable on this stack.
    for key in MODE_A_ONLY_KEYS:
        entries = [line for line in _base_environment() if line.split("=")[0] == key]
        assert entries == [key], f"{key} must be a bare pass-through, got {entries!r}"


def test_the_entrypoint_still_requires_the_mode_a_keys_when_no_bridge_is_on() -> None:
    # The requirement did not disappear, it became mode-aware. Without a master
    # key litellm skips proxy auth entirely, so Mode A losing it is a silent
    # removal of the only per-developer credential that mode has.
    command = "\n".join(_service(COMPOSE)["command"])

    assert 'CORP_LLM_FORWARD_ANTHROPIC_AUTH:-0}" != "1"' in command
    assert 'CORP_LLM_FORWARD_CHATGPT_AUTH:-0}" != "1"' in command
    for key in MODE_A_ONLY_KEYS:
        assert f'-z "$${{{key}:-}}"' in command, f"{key} is not guarded in the entrypoint"
    assert "exit 1" in command


def test_the_base_stack_hands_both_bridge_flags_to_the_container() -> None:
    # The entrypoint guard and bootstrap.build_guardrail() both read them; an
    # unset flag would make the guard treat Mode B as a misconfigured Mode A.
    environment = _base_environment()

    assert "CORP_LLM_FORWARD_ANTHROPIC_AUTH=${CORP_LLM_FORWARD_ANTHROPIC_AUTH:-0}" in environment
    assert "CORP_LLM_FORWARD_CHATGPT_AUTH=${CORP_LLM_FORWARD_CHATGPT_AUTH:-0}" in environment


def test_the_overlay_arms_the_anthropic_bridge_and_swaps_the_config_mount() -> None:
    litellm = _service(OVERLAY)

    assert litellm["environment"]["CORP_LLM_FORWARD_ANTHROPIC_AUTH"] == "1"
    assert litellm["volumes"] == [f"./{OAUTH_CONFIG_SOURCE}:{LITELLM_CONFIG_TARGET}:ro"]
    # Setting one here would be self-defeating; the whole mode needs it absent.
    assert "LITELLM_MASTER_KEY" not in litellm["environment"]


def test_the_env_example_tells_mode_b_to_delete_the_line_rather_than_blank_it() -> None:
    text = ENV_EXAMPLE.read_text()

    assert "docker-compose.oauth.yml" in text
    assert re.search(r"DELETE ALL\s+# THREE LINES ENTIRELY", text) or "DELETE" in text
    assert "blank" in text.lower()


# --------------------------------------------------------------------------- #
# the merged render — the only place both halves are visible at once
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

# Every `${X:?...}` in the base file, minus the Mode A keys this mode omits.
# Values are obvious non-credentials; only their presence in the render matters.
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


class _Render:
    def __init__(self, project_dir: Path, doc: dict[str, Any]) -> None:
        self.project_dir = project_dir.resolve()
        self.doc = doc

    @property
    def litellm(self) -> dict[str, Any]:
        return self.doc["services"]["litellm"]

    def mounts_at(self, target: str) -> list[Path]:
        return [
            Path(volume["source"]).resolve().relative_to(self.project_dir)
            for volume in self.litellm.get("volumes", [])
            if volume.get("target") == target
        ]


def _interpolated_names() -> set[str]:
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")
    names: set[str] = set()
    for path in (COMPOSE, OVERLAY):
        names |= set(pattern.findall(path.read_text()))
    return names


def _render(tmp_path: Path, *files: Path, env_extra: str = "") -> _Render:
    project_dir = tmp_path / "compose"
    shutil.copytree(COMPOSE_DIR, project_dir)
    env_text = "".join(f"{key}=render-fixture\n" for key in REQUIRED_ENV) + env_extra
    (project_dir / ".env").write_text(env_text)
    # Strip everything compose could interpolate from the developer's shell, so
    # the render is the template's and not this laptop's. Load-bearing here: a
    # LITELLM_MASTER_KEY exported in the shell would otherwise be picked up by
    # the bare pass-through and quietly invalidate the assertion below.
    interpolated = _interpolated_names() | set(MODE_A_ONLY_KEYS)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in interpolated and not key.startswith("COMPOSE_")
    }
    argv = ["docker", "compose", "--project-name", "corp-oauth-prod-render"]
    for path in files:
        argv += ["-f", path.name]
    argv.append("config")
    result = subprocess.run(
        argv, cwd=project_dir, capture_output=True, text=True, env=env, check=False
    )
    if result.returncode != 0:
        pytest.fail(f"docker compose config failed (exit {result.returncode}):\n{result.stderr}")
    return _Render(project_dir, yaml.safe_load(result.stdout))


@needs_compose_cli
def test_the_merged_stack_mounts_the_oauth_config_as_the_only_litellm_config(
    tmp_path: Path,
) -> None:
    render = _render(tmp_path, COMPOSE, OVERLAY)

    assert render.mounts_at(LITELLM_CONFIG_TARGET) == [OAUTH_CONFIG_SOURCE]
    sources = [
        Path(volume["source"]).resolve().relative_to(render.project_dir)
        for volume in render.litellm["volumes"]
    ]
    assert VIRTUAL_KEY_CONFIG_SOURCE not in sources


@needs_compose_cli
def test_the_merged_stack_arms_the_bridge_and_injects_no_master_key(tmp_path: Path) -> None:
    environment = _render(tmp_path, COMPOSE, OVERLAY).litellm["environment"]

    assert environment["CORP_LLM_FORWARD_ANTHROPIC_AUTH"] == "1"
    assert environment["CORP_LLM_FORWARD_CHATGPT_AUTH"] == "0"
    # `null` is compose's rendering of an unresolved bare pass-through: the
    # variable is NOT set in the container. An empty string would be, and
    # settings.master_key_conflict() counts that as set.
    for key in MODE_A_ONLY_KEYS:
        assert environment[key] is None, f"{key} must reach the container unset, got {key!r}"


@needs_compose_cli
def test_a_master_key_left_in_env_still_reaches_the_container(tmp_path: Path) -> None:
    # The overlay deliberately does NOT neutralise a leftover key: an override
    # would silence the mistake. build_guardrail() refuses to boot instead, so
    # the operator gets a named cause rather than a stack that 401s every
    # request. This pins that the hole is real and reaches the guard.
    environment = _render(
        tmp_path, COMPOSE, OVERLAY, env_extra="LITELLM_MASTER_KEY=not-a-real-master-key\n"
    ).litellm["environment"]

    assert environment["LITELLM_MASTER_KEY"] == "not-a-real-master-key"
    assert environment["CORP_LLM_FORWARD_ANTHROPIC_AUTH"] == "1"


@needs_compose_cli
def test_the_base_stack_alone_still_serves_the_virtual_key_config(tmp_path: Path) -> None:
    render = _render(tmp_path, COMPOSE)

    assert render.mounts_at(LITELLM_CONFIG_TARGET) == [VIRTUAL_KEY_CONFIG_SOURCE]
    assert render.litellm["environment"]["CORP_LLM_FORWARD_ANTHROPIC_AUTH"] == "0"

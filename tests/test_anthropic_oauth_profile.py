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

OVERLAY_PATH = ROOT / "docker-compose.anthropic-oauth.yml"
DEMO_COMPOSE_PATH = ROOT / "docker-compose.demo.yml"
ENV_DEMO_EXAMPLE_PATH = ROOT / ".env.demo.example"

# Obvious non-credential; only its presence in the merged render matters.
POISON_MASTER_KEY = "not-a-real-master-key-fixture"


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
    """A `docker compose config` render."""

    def __init__(self, doc: dict[str, Any]) -> None:
        self.doc = doc

    @property
    def litellm(self) -> dict[str, Any]:
        return self.doc["services"]["litellm"]


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
    return _Render(yaml.safe_load(result.stdout))


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

    assert settings.MASTER_KEY_REFUSED_MESSAGE in exc.value.problems
    # A boot diagnostic is a log line; it must not print the key it complains about.
    assert POISON_MASTER_KEY not in str(exc.value)

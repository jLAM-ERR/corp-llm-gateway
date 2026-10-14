"""`compose/docker-compose.build.yml` must build an image this stack can serve with.

`compose/docker-compose.yml` ships `CORP_LLM_REQUIRE_NER=1` (fail-closed F2), so a
gateway image without the spaCy EN model turns every request into a 503
`E_NER_UNAVAILABLE` — the image cannot serve traffic at all. `Dockerfile.gateway`
defaults to the `base` profile, which installs the NER libraries but no model
wheel, so the overlay has to name the profile itself.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.compose.nginx_support import REQUIRED_ENV, bare_compose_env, compose_cli_available

ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "compose" / "docker-compose.build.yml"
COMPOSE = ROOT / "compose" / "docker-compose.yml"
DOCKERFILE = ROOT / "Dockerfile.gateway"


@pytest.mark.skipif(not compose_cli_available(), reason="docker compose CLI not on PATH")
@pytest.mark.parametrize("providers", ["anthropic,codex,corp", "corp"])
def test_the_build_overlay_builds_the_image_once(tmp_path: Path, providers: str) -> None:
    # Read the merged model compose would run, not a build log: `build` output differs
    # between compose versions, and a test must not build the image.
    env_file = tmp_path / ".env"
    env_file.write_text(
        "".join(f"{key}=render-fixture\n" for key in REQUIRED_ENV)
        + f"COMPOSE_PROFILES={providers}\n"
    )
    result = subprocess.run(
        [
            *("docker", "compose", "--env-file", str(env_file), "-p", "corp-build-once"),
            *("-f", COMPOSE.name, "-f", OVERLAY.name, "config", "--format", "json"),
        ],
        cwd=COMPOSE.parent,
        capture_output=True,
        text=True,
        env=bare_compose_env(),
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    services = json.loads(result.stdout)["services"]
    selected = set(providers.split(","))
    active = {
        name: service
        for name, service in services.items()
        if not service.get("profiles") or selected & set(service["profiles"])
    }
    gateways = sorted(name for name in active if name.startswith("gateway-"))
    assert gateways == sorted(f"gateway-{provider}" for provider in selected)

    assert [name for name, service in active.items() if "build" in service] == ["profile-guard"]
    guard = active["profile-guard"]
    assert "profiles" not in guard
    assert guard["pull_policy"] == "build"
    assert guard["build"]["dockerfile"] == DOCKERFILE.name
    assert Path(guard["build"]["context"]).resolve() == ROOT
    assert guard["build"]["args"]["NER_PROFILE"] == "ru-en"
    for name in gateways:
        assert active[name]["image"] == guard["image"], name
        assert active[name]["pull_policy"] == "never", name
        assert "build" not in active[name], name

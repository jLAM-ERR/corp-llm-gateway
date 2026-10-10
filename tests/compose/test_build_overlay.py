"""`compose/docker-compose.build.yml` must build an image this stack can serve with.

`compose/docker-compose.yml` ships `CORP_LLM_REQUIRE_NER=1` (fail-closed F2), so a
gateway image without the spaCy EN model turns every request into a 503
`E_NER_UNAVAILABLE` — the image cannot serve traffic at all. `Dockerfile.gateway`
defaults to the `base` profile, which installs the NER libraries but no model
wheel, so the overlay has to name the profile itself.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.compose.nginx_support import REQUIRED_ENV, bare_compose_env, compose_cli_available

ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "compose" / "docker-compose.build.yml"
COMPOSE = ROOT / "compose" / "docker-compose.yml"
DOCKERFILE = ROOT / "Dockerfile.gateway"
GATEWAYS = ("gateway-anthropic", "gateway-codex", "gateway-corp")


def _build_args() -> dict[str, str]:
    """The one build every gateway-image service runs (it lives on profile-guard)."""
    build = yaml.safe_load(OVERLAY.read_text())["services"]["profile-guard"]["build"]
    args = build.get("args") or {}
    if isinstance(args, list):
        args = dict(item.split("=", 1) for item in args)
    return {key: str(value) for key, value in args.items()}


def _profile_arm(profile: str) -> str:
    """The `case` arm of Dockerfile.gateway that installs one NER profile."""
    match = re.search(rf"^\s*{re.escape(profile)}\)(.*?);;", DOCKERFILE.read_text(), re.S | re.M)
    assert match is not None, f"Dockerfile.gateway has no NER_PROFILE arm for {profile!r}"
    return match.group(1)


def _gateway_environments() -> dict[str, dict[str, str]]:
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    return {name: services[name]["environment"] for name in GATEWAYS}


def test_build_overlay_names_the_ner_profile_instead_of_taking_the_default() -> None:
    assert _build_args().get("NER_PROFILE") == "ru-en"


def test_the_overlay_profile_bakes_the_en_model_wheel() -> None:
    assert "${en_model}" in _profile_arm(_build_args()["NER_PROFILE"])


def test_the_dockerfile_default_would_ship_no_en_model() -> None:
    # The premise: without an explicit arg the overlay inherits `base`, and `base`
    # never installs the model wheel.
    assert re.search(r"^ARG NER_PROFILE=base$", DOCKERFILE.read_text(), re.M)
    assert "${en_model}" not in _profile_arm("base")


def test_the_compose_stack_fails_closed_on_a_missing_ner_engine() -> None:
    # The other half of the coupling: with this at 1 a degraded EN engine is a 503,
    # not a quiet fallback to the regex floor.
    for name, environment in _gateway_environments().items():
        assert environment["CORP_LLM_REQUIRE_NER"] == "${CORP_LLM_REQUIRE_NER:-1}", name


def test_the_build_lives_on_the_one_service_never_profiled_off() -> None:
    overlay = yaml.safe_load(OVERLAY.read_text())["services"]
    base = yaml.safe_load(COMPOSE.read_text())["services"]

    assert [name for name, service in overlay.items() if "build" in service] == ["profile-guard"]
    assert "profiles" not in base["profile-guard"]
    assert overlay["profile-guard"]["pull_policy"] == "build"
    # Same tag: every gateway runs what profile-guard built, and never pulls over it.
    for name in GATEWAYS:
        assert base[name]["image"] == base["profile-guard"]["image"], name
        assert overlay[name] == {"pull_policy": "never"}, name


@pytest.mark.skipif(not compose_cli_available(), reason="docker compose CLI not on PATH")
@pytest.mark.parametrize("providers", ["anthropic,codex,corp", "corp"])
def test_the_build_overlay_builds_the_image_once(tmp_path: Path, providers: str) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "".join(f"{key}=render-fixture\n" for key in REQUIRED_ENV)
        + f"COMPOSE_PROFILES={providers}\n"
    )
    result = subprocess.run(
        [
            *("docker", "compose", "--env-file", str(env_file), "-p", "corp-build-once"),
            *("-f", COMPOSE.name, "-f", OVERLAY.name, "--dry-run", "build"),
        ],
        cwd=COMPOSE.parent,
        capture_output=True,
        text=True,
        env=bare_compose_env(),
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    building = [
        line for line in (result.stdout + result.stderr).splitlines() if " Building" in line
    ]
    assert len(building) == 1, building

"""`compose/docker-compose.build.yml` must build an image this stack can serve with.

`compose/docker-compose.yml` ships `CORP_LLM_REQUIRE_NER=1` (fail-closed F2), so a
gateway image without the spaCy EN model turns every request into a 503
`E_NER_UNAVAILABLE` — the image cannot serve traffic at all. `Dockerfile.gateway`
defaults to the `base` profile, which installs the NER libraries but no model
wheel, so the overlay has to name the profile itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "compose" / "docker-compose.build.yml"
COMPOSE = ROOT / "compose" / "docker-compose.yml"
DOCKERFILE = ROOT / "Dockerfile.gateway"


def _build_args() -> dict[str, str]:
    build = yaml.safe_load(OVERLAY.read_text())["services"]["litellm"]["build"]
    args = build.get("args") or {}
    if isinstance(args, list):
        args = dict(item.split("=", 1) for item in args)
    return {key: str(value) for key, value in args.items()}


def _profile_arm(profile: str) -> str:
    """The `case` arm of Dockerfile.gateway that installs one NER profile."""
    match = re.search(rf"^\s*{re.escape(profile)}\)(.*?);;", DOCKERFILE.read_text(), re.S | re.M)
    assert match is not None, f"Dockerfile.gateway has no NER_PROFILE arm for {profile!r}"
    return match.group(1)


def _litellm_environment() -> list[str]:
    return yaml.safe_load(COMPOSE.read_text())["services"]["litellm"]["environment"]


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
    assert "CORP_LLM_REQUIRE_NER=${CORP_LLM_REQUIRE_NER:-1}" in _litellm_environment()

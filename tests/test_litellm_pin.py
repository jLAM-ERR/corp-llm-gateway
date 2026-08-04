from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

GATEWAY_DOCKERFILE = ROOT / "Dockerfile.gateway"
BUILD_WORKFLOW = ROOT / ".github/workflows/build-image.yml"
RELEASE_GATES = ROOT / "scripts/release/gates.sh"
UPGRADE_DOC = ROOT / "docs/ops/upgrade.md"
PROFILE_DOCKERFILES = (
    ROOT / "docker/demo-litellm/Dockerfile",
    ROOT / "docker/chatgpt-codex/Dockerfile",
    ROOT / "docker/anthropic-oauth/Dockerfile",
)

_FROM_PIN = re.compile(r"^FROM ghcr\.io/berriai/litellm:(\S+)\s*$", re.MULTILINE)
_ARG_PIN = re.compile(r"^ARG LITELLM_VERSION=(\S+)\s*$", re.MULTILINE)
_SHELL_PIN = re.compile(r'^LITELLM_VERSION="([^"]+)"\s*$', re.MULTILINE)
# `LITELLM_VERSION=${{ inputs.litellm_version || 'v1.95.0' }}`
_WORKFLOW_BUILD_ARG_PIN = re.compile(r"LITELLM_VERSION=\$\{\{[^}]*?'([^']+)'\s*\}\}")
# Release images must be reproducible: a moving tag would let two builds of the
# same commit ship different proxies.
_IMMUTABLE_TAG = re.compile(r"^v\d+\.\d+\.\d+$")


def _extract(pattern: re.Pattern[str], text: str, source: str) -> str:
    matches = pattern.findall(text)
    if not matches:
        raise AssertionError(f"no litellm pin found in {source}")
    return matches[0]


def _workflow_input_default() -> str:
    doc = yaml.safe_load(BUILD_WORKFLOW.read_text())
    # PyYAML resolves the bare `on:` key to the boolean True (YAML 1.1).
    triggers = doc.get("on", doc.get(True))
    return triggers["workflow_dispatch"]["inputs"]["litellm_version"]["default"]


def _pins() -> dict[str, str]:
    pins = {
        "Dockerfile.gateway": _extract(
            _ARG_PIN, GATEWAY_DOCKERFILE.read_text(), "Dockerfile.gateway"
        ),
        "build-image.yml:input-default": _workflow_input_default(),
        "build-image.yml:build-arg": _extract(
            _WORKFLOW_BUILD_ARG_PIN, BUILD_WORKFLOW.read_text(), "build-image.yml"
        ),
        "gates.sh": _extract(_SHELL_PIN, RELEASE_GATES.read_text(), "gates.sh"),
    }
    for path in PROFILE_DOCKERFILES:
        pins[str(path.relative_to(ROOT))] = _extract(
            _FROM_PIN, path.read_text(), str(path.relative_to(ROOT))
        )
    return pins


def test_every_litellm_pin_site_names_the_same_tag() -> None:
    # Six sites, one version. A partial bump is the failure mode this catches:
    # the gateway image, the release workflow, the local gate and the three demo
    # profiles each pin independently.
    assert set(_pins().values()) == {"v1.95.0"}


def test_release_workflow_and_local_gate_agree() -> None:
    # gates.sh names the workflow as the source of truth for the pin, so the two
    # must move together or the local gate validates a base the release never uses.
    pins = _pins()

    assert (
        pins["gates.sh"]
        == pins["build-image.yml:input-default"]
        == pins["build-image.yml:build-arg"]
    )


def test_published_image_pin_is_immutable() -> None:
    # `main-stable` and `latest` float; both resolve to v1.95.0 today and to
    # something else after the next release.
    pin = _pins()["Dockerfile.gateway"]

    assert _IMMUTABLE_TAG.match(pin), f"{pin} is a floating tag"


def test_upgrade_doc_records_the_pin_and_the_rollback_versions() -> None:
    text = UPGRADE_DOC.read_text()

    assert _pins()["Dockerfile.gateway"] in text
    for previous in ("main-stable", "v1.85.0", "v1.89.3"):
        assert previous in text


@pytest.mark.parametrize(
    ("pattern", "text"),
    [
        pytest.param(_FROM_PIN, "FROM python:3.12-slim\n", id="from"),
        pytest.param(_ARG_PIN, "ARG NER_PROFILE=base\n", id="arg"),
        pytest.param(_SHELL_PIN, 'IMAGE="gw-test"\n', id="shell"),
        pytest.param(_WORKFLOW_BUILD_ARG_PIN, "NER_PROFILE=ru-en\n", id="workflow"),
    ],
)
def test_pin_extraction_refuses_a_file_without_a_pin(pattern: re.Pattern[str], text: str) -> None:
    # Without this the scrapers above would silently pass on a renamed or gutted
    # file — a green test proving nothing.
    with pytest.raises(AssertionError):
        _extract(pattern, text, "synthetic")

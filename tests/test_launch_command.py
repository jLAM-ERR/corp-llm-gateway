"""Every launch path runs the route gate, not litellm's CLI.

Text-level, so it runs on both venvs and needs neither litellm nor Docker: the
container tests (Task 5) prove the image behaves, this proves nothing still
names the ungated command. A `litellm --config …` anywhere serves three routes
that reach a provider without the guardrail.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

SERVE_MODULE = "corp_llm_gateway.serve"
ASGI_TARGET = "corp_llm_gateway.asgi:app"

DOCKERFILES = (
    ROOT / "Dockerfile.gateway",
    ROOT / "docker/demo-litellm/Dockerfile",
    ROOT / "docker/anthropic-oauth/Dockerfile",
    ROOT / "docker/chatgpt-codex/Dockerfile",
)

COMPOSE_FILES = (
    ROOT / "compose/docker-compose.yml",
    ROOT / "compose/docker-compose.oauth.yml",
    ROOT / "docker-compose.demo.yml",
    ROOT / "docker-compose.anthropic-oauth.yml",
    ROOT / "docker-compose.chatgpt-codex.yml",
)

# `litellm` as a command, not the word in a comment, a service name or a path.
_LITELLM_CLI = re.compile(r"(?:^|[|&;\s\[\"'])litellm\s+(?:--\w|run\b)")

_ENTRYPOINT = re.compile(r"^ENTRYPOINT\s+(.+)$", re.MULTILINE)


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_dockerfile_entrypoint_runs_the_gate(path: Path) -> None:
    entrypoints = _ENTRYPOINT.findall(path.read_text())

    assert entrypoints, f"{path} declares no ENTRYPOINT, so it inherits litellm's ungated one"
    assert SERVE_MODULE in entrypoints[-1], entrypoints[-1]
    assert "litellm" not in entrypoints[-1]


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_compose_file_starts_the_litellm_cli(path: Path) -> None:
    for service, definition in (yaml.safe_load(path.read_text()).get("services") or {}).items():
        for key in ("command", "entrypoint"):
            rendered = _flatten(definition.get(key))
            offending = _LITELLM_CLI.search(rendered)
            assert offending is None, f"{path.name}:{service}:{key} starts the ungated CLI"


@pytest.mark.parametrize(
    "path",
    [ROOT / "compose/docker-compose.yml", ROOT / "docker-compose.demo.yml"],
    ids=lambda p: p.name,
)
def test_the_serving_compose_files_exec_the_gate(path: Path) -> None:
    services = yaml.safe_load(path.read_text())["services"]
    rendered = _flatten(services["litellm"].get("command"))

    assert SERVE_MODULE in rendered


def test_no_tracked_file_targets_litellms_app_directly() -> None:
    # `uvicorn litellm.proxy.proxy_server:app` would bypass the gate exactly as
    # the CLI does.
    hits = subprocess.run(
        ["git", "grep", "-l", "litellm.proxy.proxy_server:app", "--", ".", ":!docs", ":!tests"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()

    assert hits == [], hits


def test_the_asgi_target_is_named_exactly_once_in_serve() -> None:
    assert ASGI_TARGET in (ROOT / "src/corp_llm_gateway/serve.py").read_text()


@pytest.mark.requires_helm
def test_the_helm_gateway_container_inherits_the_image_entrypoint() -> None:
    rendered = subprocess.run(
        ["helm", "template", "gw", str(ROOT / "helm/corp-llm-gateway")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    deployment = next(
        doc
        for doc in yaml.safe_load_all(rendered)
        if doc and doc.get("kind") == "Deployment" and doc["metadata"]["name"].endswith("gateway")
    )
    container = next(
        c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "litellm"
    )

    # No command/args: overriding them would replace the gate with the CLI.
    assert "command" not in container
    assert "args" not in container
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz/live"
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz/ready"


def _flatten(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_flatten(item) for item in value)
    return str(value)


@pytest.mark.parametrize(
    ("text", "caught"),
    [
        ("exec litellm --config /etc/litellm/config.yaml --port 4000", True),
        ('["sh", "-c", "litellm --port 4000"]', True),
        ("exec python -m corp_llm_gateway.serve", False),
        ("./litellm/config.yaml:/etc/litellm/config.yaml:ro", False),
        ("depends_on:\n  litellm:\n    condition: service_healthy", False),
    ],
)
def test_the_cli_pattern_matches_commands_and_not_paths(text: str, caught: bool) -> None:
    # Without this the scraper above could pass by matching nothing at all.
    assert (_LITELLM_CLI.search(text) is not None) is caught

"""The shipped litellm configs turn on no request-content logging (plan 20260926 hazard 17).

litellm hands every success/failure logger the request its logging object holds, and
its DEBUG output prints the request before any pre-call hook runs. Our pre-call keeps
the first sanitised and the arm step refuses the second; these configs are the layer
under both: no extra success/failure logger, no prompts stored in spend logs, no
``set_verbose``, no DEBUG switch in the environment the gateway runs with.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.helm.test_chart_render import _first_of_kind, _litellm_configmap, _render

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose"
OUR_CALLBACK = "corp_llm_gateway.bootstrap.guardrail"

LITELLM_CONFIGS = {
    "compose": COMPOSE / "litellm" / "config.yaml",
    "compose-oauth": COMPOSE / "litellm" / "config.oauth.yaml",
}
COMPOSE_FILES = sorted(COMPOSE.glob("docker-compose*.yml"))

# litellm's own DEBUG switches (proxy_server.initialize, proxy_cli.py) and ours.
DEBUG_ENV_KEYS = ("DETAILED_DEBUG", "DEBUG", "CORP_LLM_ALLOW_LITELLM_DEBUG")

needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm binary not on PATH")


def _helm_docs() -> list[dict[str, Any]]:
    result = _render()
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _helm_litellm_config() -> dict[str, Any]:
    return yaml.safe_load(_litellm_configmap(_helm_docs())["data"]["config.yaml"])


def _assert_logs_no_content(config: dict[str, Any]) -> None:
    litellm_settings = config.get("litellm_settings") or {}
    general = config.get("general_settings") or {}
    assert litellm_settings.get("callbacks") == [OUR_CALLBACK]
    assert "success_callback" not in litellm_settings
    assert litellm_settings.get("failure_callback", []) in ([], [OUR_CALLBACK])
    assert "set_verbose" not in litellm_settings
    for section in (litellm_settings, general):
        assert "store_prompts_in_spend_logs" not in section


def _assert_no_debug_env(env: dict[str, Any]) -> None:
    assert "DEBUG" not in str(env.get("LITELLM_LOG") or "").upper()
    for key in DEBUG_ENV_KEYS:
        assert key not in env, key


@pytest.mark.parametrize("name", sorted(LITELLM_CONFIGS))
def test_compose_litellm_config_logs_no_request_content(name: str) -> None:
    _assert_logs_no_content(yaml.safe_load(LITELLM_CONFIGS[name].read_text()))


@needs_helm
def test_helm_litellm_config_logs_no_request_content() -> None:
    _assert_logs_no_content(_helm_litellm_config())


def _compose_env(entries: Any) -> dict[str, Any]:
    if isinstance(entries, dict):
        return dict(entries)
    env: dict[str, Any] = {}
    for item in entries or ():
        key, sep, value = item.partition("=")
        env[key] = value if sep else None
    return env


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_compose_gateway_env_turns_no_litellm_debug_on(path: Path) -> None:
    services = (yaml.safe_load(path.read_text()) or {}).get("services") or {}
    gateway = services.get("litellm")
    if gateway is None:
        pytest.skip(f"{path.name} does not define the gateway service")
    _assert_no_debug_env(_compose_env(gateway.get("environment")))


@needs_helm
def test_helm_gateway_env_turns_no_litellm_debug_on() -> None:
    pod = _first_of_kind(_helm_docs(), "Deployment")["spec"]["template"]["spec"]
    containers = pod["containers"] + pod.get("initContainers", [])
    for container in containers:
        _assert_no_debug_env({e["name"]: e.get("value") for e in container.get("env", [])})


def test_the_compose_file_set_is_the_one_this_guard_expects() -> None:
    # A new overlay is picked up by the glob; this pins that the glob still finds them.
    assert {p.name for p in COMPOSE_FILES} >= {
        "docker-compose.yml",
        "docker-compose.oauth.yml",
        "docker-compose.issuance.yml",
        "docker-compose.build.yml",
    }


def test_the_guard_catches_what_it_guards_against() -> None:
    with pytest.raises(AssertionError):
        _assert_logs_no_content(
            {"litellm_settings": {"callbacks": [OUR_CALLBACK], "success_callback": ["langfuse"]}}
        )
    with pytest.raises(AssertionError):
        _assert_logs_no_content(
            {"litellm_settings": {"callbacks": [OUR_CALLBACK], "set_verbose": True}}
        )
    with pytest.raises(AssertionError):
        _assert_logs_no_content(
            {
                "litellm_settings": {"callbacks": [OUR_CALLBACK]},
                "general_settings": {"store_prompts_in_spend_logs": True},
            }
        )
    with pytest.raises(AssertionError):
        _assert_no_debug_env(_compose_env(["LITELLM_LOG=${LITELLM_LOG:-DEBUG}"]))
    with pytest.raises(AssertionError):
        _assert_no_debug_env(_compose_env(["DETAILED_DEBUG"]))

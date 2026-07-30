from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_chatgpt_codex_litellm_profile_routes_responses_without_static_secret() -> None:
    profile = yaml.safe_load((ROOT / "docker/chatgpt-codex/litellm-config.yaml").read_text())

    deployment = profile["model_list"][0]
    params = deployment["litellm_params"]
    assert deployment["model_name"] == "*"
    assert params["model"] == "openai/*"
    assert params["api_base"] == "os.environ/CHATGPT_CODEX_API_BASE"
    assert params["api_key"] == "oauth-passthrough-placeholder"
    assert profile["litellm_settings"]["callbacks"] == [
        "corp_llm_gateway._demo_guardrail.guardrail"
    ]


def test_chatgpt_codex_compose_enables_opt_in_auth_bridge() -> None:
    overlay_text = (ROOT / "docker-compose.chatgpt-codex.yml").read_text()
    overlay = yaml.safe_load(overlay_text)
    environment = overlay["services"]["litellm"]["environment"]

    assert environment["CORP_LLM_FORWARD_CHATGPT_AUTH"] == "1"
    assert "chatgpt.com/backend-api/codex" in environment["CHATGPT_CODEX_API_BASE"]
    assert "LITELLM_MASTER_KEY" not in overlay_text


def test_codex_profile_uses_openai_auth_and_responses_wire_api() -> None:
    profile = tomllib.loads((ROOT / "docker/chatgpt-codex/chatgpt-codex.config.toml").read_text())
    provider = profile["model_providers"]["corp-chatgpt-codex"]

    assert profile["model_provider"] == "corp-chatgpt-codex"
    assert provider["base_url"] == "http://127.0.0.1:4000/v1"
    assert provider["wire_api"] == "responses"
    assert provider["requires_openai_auth"] is True
    assert provider["supports_websockets"] is False
    assert provider["http_headers"]["X-Corp-Auth"] == "demo-team-token"

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

PROFILE_PATH = ROOT / "docker/anthropic-oauth/litellm-config.yaml"
OVERLAY_PATH = ROOT / "docker-compose.anthropic-oauth.yml"
CLIENT_ENV_PATH = ROOT / "docker/anthropic-oauth/claude-env.sh"


def _profile() -> dict:
    return yaml.safe_load(PROFILE_PATH.read_text())


def test_anthropic_oauth_profile_routes_native_anthropic_without_static_secret() -> None:
    deployments = _profile()["model_list"]

    assert len(deployments) == 1
    params = deployments[0]["litellm_params"]
    assert deployments[0]["model_name"] == "claude-*"
    assert params["model"] == "anthropic/claude-*"
    assert params["api_key"] == "oauth-passthrough-placeholder"
    # A placeholder shaped like a real OAuth token would select litellm's OAuth
    # branch at boot instead of failing upstream when the bridge is off.
    assert not params["api_key"].startswith("sk-ant-")


def test_anthropic_oauth_profile_has_no_wildcard_and_no_foreign_provider_route() -> None:
    # The binding control behind the hook's alias gate: the gate reads the
    # client-visible model alias, but litellm resolves the deployment after the
    # hook runs. Only an Anthropic-only config keeps the subscription token from
    # reaching another provider.
    for deployment in _profile()["model_list"]:
        assert deployment["model_name"] != "*"
        assert deployment["litellm_params"]["model"].startswith("anthropic/")


def test_anthropic_oauth_profile_uses_the_demo_guardrail_callback() -> None:
    # bootstrap's make_auth_middleware() reads CORP_LLM_PG_DSN, which the demo
    # compose does not set — with it every X-Corp-Auth request 401s before the
    # OAuth bridge runs.
    settings = _profile()["litellm_settings"]

    assert settings["callbacks"] == ["corp_llm_gateway._demo_guardrail.guardrail"]
    assert settings["drop_params"] is True
    assert settings["json_logs"] is True


def test_anthropic_oauth_overlay_enables_the_bridge_and_mounts_its_own_config() -> None:
    overlay = yaml.safe_load(OVERLAY_PATH.read_text())
    litellm = overlay["services"]["litellm"]

    assert litellm["environment"]["CORP_LLM_FORWARD_ANTHROPIC_AUTH"] == "1"
    assert litellm["build"]["dockerfile"] == "docker/anthropic-oauth/Dockerfile"
    assert (
        "./docker/anthropic-oauth/litellm-config.yaml:/etc/litellm/config.yaml:ro"
        in litellm["volumes"]
    )


def test_anthropic_oauth_overlaid_stack_sets_no_master_key() -> None:
    # A master key makes litellm consume the inbound Authorization as a virtual
    # key and reject the request before pre_call ever sees the OAuth bearer. The
    # overlay is applied on top of the demo stack, so check both layers — and the
    # parsed environment, so a comment naming the key does not pass for setting it.
    for path in (ROOT / "docker-compose.demo.yml", OVERLAY_PATH):
        environment = yaml.safe_load(path.read_text())["services"]["litellm"].get("environment", {})
        assert "LITELLM_MASTER_KEY" not in environment

    for line in (ROOT / ".env.demo.example").read_text().splitlines():
        assert not line.strip().startswith("LITELLM_MASTER_KEY=")


def test_anthropic_oauth_overlay_does_not_enable_the_conflicting_bridge() -> None:
    # The two forward-auth bridges are mutually exclusive; both on refuses to boot.
    environment = yaml.safe_load(OVERLAY_PATH.read_text())["services"]["litellm"]["environment"]

    assert "CORP_LLM_FORWARD_CHATGPT_AUTH" not in environment


def test_anthropic_oauth_overlay_ships_no_credential() -> None:
    # The prefix itself is legitimately present (docs + the shape check); a real
    # token is the prefix followed by a long opaque body.
    token_shape = re.compile(r"sk-ant-[a-z]+[A-Za-z0-9_-]{12,}")
    for path in (PROFILE_PATH, OVERLAY_PATH, CLIENT_ENV_PATH):
        text = path.read_text()
        assert token_shape.search(text) is None
        assert "ANTHROPIC_API_KEY=" not in text


def test_anthropic_oauth_client_env_keeps_the_corp_header_in_one_place() -> None:
    text = CLIENT_ENV_PATH.read_text()

    assert text.count("X-Corp-Auth") == 1
    assert 'export ANTHROPIC_CUSTOM_HEADERS="X-Corp-Auth: $CORP_TEAM_TOKEN"' in text
    assert 'export ANTHROPIC_BASE_URL="$CORP_GATEWAY_URL"' in text
    # A stale API key in the shell would shadow the subscription token.
    assert "unset ANTHROPIC_API_KEY" in text


def test_demo_env_example_keeps_client_anthropic_vars_out_of_the_container() -> None:
    # litellm reads ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN from its own process
    # env; .env.demo is env_file for the litellm container, so an uncommented
    # assignment there self-loops the upstream call or installs a shared
    # server-side fallback credential.
    for line in (ROOT / ".env.demo.example").read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not stripped.startswith("ANTHROPIC_BASE_URL=")
        assert not stripped.startswith("ANTHROPIC_AUTH_TOKEN=")
        assert not stripped.startswith("ANTHROPIC_CUSTOM_HEADERS=")

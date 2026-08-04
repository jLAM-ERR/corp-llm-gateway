"""Drift guards for the litellm behavior the two auth bridges depend on.

Every assertion here was verified inside the pinned base image
(`ghcr.io/berriai/litellm:v1.95.0`) as well as against the installed release.
They run wherever litellm is importable — the local 3.14 venv has no litellm and
skips, CI's 3.12 job runs them.
"""

from __future__ import annotations

import pytest

from corp_llm_gateway.litellm_hook import _NON_CHAT_INPUT_CALL_TYPES

_OAUTH_TOKEN = "sk-ant-oat01-abcdef"
_API_KEY = "sk-ant-api03-abcdef"
_IDENTITY_PREAMBLE = "You are Claude Code, Anthropic's official CLI for Claude."

_SKIP_REASON = "litellm absent (3.14 graceful degradation); CI runs this on 3.12"


def _anthropic_chat_headers(api_key: str) -> dict[str, str]:
    transformation = pytest.importorskip(
        "litellm.llms.anthropic.chat.transformation", reason=_SKIP_REASON
    )
    return transformation.AnthropicConfig().validate_environment(
        headers={},
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
        optional_params={},
        litellm_params={},
        api_key=api_key,
    )


def _messages_config():
    transformation = pytest.importorskip(
        "litellm.llms.anthropic.experimental_pass_through.messages.transformation",
        reason=_SKIP_REASON,
    )
    return transformation.AnthropicMessagesConfig()


def test_oauth_api_key_selects_the_bearer_branch_and_suppresses_x_api_key() -> None:
    # The whole bridge: publishing the developer's token as the per-request
    # api_key is what makes litellm emit the subscription's Authorization header.
    headers = {name.lower(): value for name, value in _anthropic_chat_headers(_OAUTH_TOKEN).items()}

    assert headers["authorization"] == f"Bearer {_OAUTH_TOKEN}"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    assert headers["anthropic-dangerous-direct-browser-access"] == "true"
    assert "x-api-key" not in headers


def test_a_plain_api_key_still_takes_the_x_api_key_branch() -> None:
    # Why `_anthropic_upstream_headers` accepts OAuth tokens only: on this branch
    # litellm adds x-api-key while the inbound Authorization is merged in
    # untouched, putting two auth schemes on one upstream request.
    headers = {name.lower(): value for name, value in _anthropic_chat_headers(_API_KEY).items()}

    assert headers["x-api-key"] == _API_KEY
    assert "authorization" not in headers


def test_messages_route_supports_system_and_not_metadata() -> None:
    # `/v1/messages` (the route Claude Code uses) keeps the native request shape.
    supported = _messages_config().get_supported_anthropic_messages_params("claude-sonnet-4-5")

    assert "system" in supported
    assert "metadata" not in supported


def test_messages_route_leaves_the_identity_preamble_byte_identical() -> None:
    # The OAuth path matches the Claude Code identity block by exact equality, so
    # any rewrite upstream of Anthropic breaks it. litellm touches `system` only
    # to filter x-anthropic-billing-header blocks.
    system = [{"type": "text", "text": _IDENTITY_PREAMBLE}]

    transformed = _messages_config().transform_anthropic_messages_request(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
        anthropic_messages_optional_request_params={"system": system, "max_tokens": 16},
        litellm_params={},
        headers={},
    )

    assert transformed["system"] == [{"type": "text", "text": _IDENTITY_PREAMBLE}]


def test_messages_route_does_not_itself_strip_unsupported_metadata() -> None:
    # The supported-param list above is declarative — nothing filters against it
    # on this route. So the bridge's own `data.pop("metadata")` is load-bearing on
    # `/v1/messages` too, not only on the chat-completions adapter. A future
    # litellm that does strip it would be safe, and would land here first.
    transformed = _messages_config().transform_anthropic_messages_request(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
        anthropic_messages_optional_request_params={
            "max_tokens": 16,
            "metadata": {"user_id": "someone@example.com"},
        },
        litellm_params={},
        headers={},
    )

    assert transformed["metadata"] == {"user_id": "someone@example.com"}


def test_chat_adapter_still_maps_a_top_level_user_into_metadata() -> None:
    # The reason the bridge drops `user` as well as `metadata`.
    transformation = pytest.importorskip(
        "litellm.llms.anthropic.chat.transformation", reason=_SKIP_REASON
    )
    optional_params = transformation.AnthropicConfig().map_openai_params(
        non_default_params={"user": "alice"},
        optional_params={},
        model="claude-sonnet-4-5",
        drop_params=False,
    )

    assert optional_params["metadata"] == {"user_id": "alice"}


def test_router_treats_api_key_as_a_clientside_credential() -> None:
    # Both bridges hand the developer's token over as `data["api_key"]`; if the
    # router stopped honouring it, every request would silently fall back to the
    # deployment's configured key.
    handler = pytest.importorskip(
        "litellm.router_utils.clientside_credential_handler", reason=_SKIP_REASON
    )

    assert "api_key" in handler.clientside_credential_keys
    assert handler.is_clientside_credential({"api_key": _OAUTH_TOKEN})


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_master_key_env_var_stays_a_set_master_key(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    # Why `settings.master_key_conflict` refuses a PRESENT master key rather than
    # a truthy one: litellm reads the variable through `get_secret_str`, which
    # preserves a blank value, and skips proxy auth only for `master_key is
    # None`. A blank one therefore still consumes the developer's bearer.
    secret_managers = pytest.importorskip("litellm.secret_managers.main", reason=_SKIP_REASON)
    monkeypatch.setenv("LITELLM_MASTER_KEY", blank)

    resolved = secret_managers.get_secret_str("LITELLM_MASTER_KEY")

    assert resolved == blank
    assert resolved is not None


def test_non_chat_input_call_types_all_exist_in_litellm() -> None:
    # The hook's denylist is written as literal call_type strings; a renamed
    # CallType would silently turn one back into a sanitized-as-chat path.
    utils = pytest.importorskip("litellm.types.utils", reason=_SKIP_REASON)
    values = {call_type.value for call_type in utils.CallTypes}

    assert values >= _NON_CHAT_INPUT_CALL_TYPES

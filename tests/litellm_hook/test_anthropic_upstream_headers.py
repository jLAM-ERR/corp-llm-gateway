from __future__ import annotations

import pytest

from corp_llm_gateway import litellm_hook
from corp_llm_gateway.litellm_hook import (
    _ANTHROPIC_HEADER_ALLOWLIST,
    _ANTHROPIC_OAUTH_TOKEN_PREFIX,
    _ANTHROPIC_OAUTH_TOKEN_PREFIX_FALLBACK,
    _CORP_AUTH_HEADER_LOWER,
    _anthropic_upstream_headers,
    _resolve_anthropic_oauth_prefix,
)

_OAUTH_TOKEN = "sk-ant-oat01-abcdef"


def test_selects_allowlisted_headers() -> None:
    inbound = {
        "Authorization": f"Bearer {_OAUTH_TOKEN}",
        "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": "2023-06-01",
        "User-Agent": "claude-cli/2.1.220",
    }

    assert _anthropic_upstream_headers(inbound) == inbound


def test_accepts_lowercase_bearer_scheme() -> None:
    inbound = {"authorization": f"bearer {_OAUTH_TOKEN}"}

    assert _anthropic_upstream_headers(inbound) == inbound


def test_drops_corp_token_and_unlisted_headers() -> None:
    inbound = {
        "X-Corp-Auth": "tok-1",
        "Authorization": f"Bearer {_OAUTH_TOKEN}",
        "anthropic-version": "2023-06-01",
        "Host": "127.0.0.1:4000",
        "X-Codex-Beta-Features": "feature",
        "X-Unrelated": "do-not-forward",
    }

    selected = {name.lower() for name in _anthropic_upstream_headers(inbound)}

    assert selected == {"authorization", "anthropic-version"}


def test_corp_auth_header_is_not_in_the_allowlist() -> None:
    assert _CORP_AUTH_HEADER_LOWER not in _ANTHROPIC_HEADER_ALLOWLIST


@pytest.mark.parametrize(
    "inbound",
    [
        pytest.param({"anthropic-version": "2023-06-01"}, id="missing"),
        pytest.param({"Authorization": f"Basic {_OAUTH_TOKEN}"}, id="non_bearer"),
        pytest.param({"Authorization": "Bearer "}, id="empty_bearer"),
        pytest.param(
            {"Authorization": f"Bearer {_OAUTH_TOKEN}\r\nX-Injected: 1"},
            id="crlf",
        ),
        pytest.param({"Authorization": "Bearer sk-ant-oa"}, id="truncated_prefix"),
        pytest.param({"Authorization": "Bearer sk-ant-api03-abcdef"}, id="plain_api_key"),
        pytest.param({"Authorization": "Bearer oauth-value"}, id="arbitrary_bearer"),
    ],
)
def test_rejects_bad_authorization(inbound: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        _anthropic_upstream_headers(inbound)


def test_oauth_prefix_matches_litellm() -> None:
    """The gate must use litellm's own selector value, not a copy of it."""
    anthropic_types = pytest.importorskip(
        "litellm.types.llms.anthropic",
        reason="litellm absent (3.14 graceful degradation); CI runs this on 3.12",
    )

    assert anthropic_types.ANTHROPIC_OAUTH_TOKEN_PREFIX == _ANTHROPIC_OAUTH_TOKEN_PREFIX


def test_oauth_prefix_fallback_matches_litellm() -> None:
    """Drift guard: the litellm-less fallback must still equal the real constant."""
    anthropic_types = pytest.importorskip(
        "litellm.types.llms.anthropic",
        reason="litellm absent (3.14 graceful degradation); CI runs this on 3.12",
    )

    assert anthropic_types.ANTHROPIC_OAUTH_TOKEN_PREFIX == _ANTHROPIC_OAUTH_TOKEN_PREFIX_FALLBACK


def test_resolver_prefers_litellm_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(litellm_hook, "_LITELLM_ANTHROPIC_OAUTH_TOKEN_PREFIX", "sk-ant-oat2")

    assert _resolve_anthropic_oauth_prefix() == "sk-ant-oat2"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="litellm_absent"),
        pytest.param("", id="empty"),
        pytest.param(b"sk-ant-oat", id="non_str"),
    ],
)
def test_resolver_falls_back_on_unusable_litellm_value(
    monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    monkeypatch.setattr(litellm_hook, "_LITELLM_ANTHROPIC_OAUTH_TOKEN_PREFIX", value)

    assert _resolve_anthropic_oauth_prefix() == _ANTHROPIC_OAUTH_TOKEN_PREFIX_FALLBACK


def test_resolved_prefix_is_never_empty() -> None:
    """An empty prefix would make every bearer token pass the OAuth-only gate."""
    assert _ANTHROPIC_OAUTH_TOKEN_PREFIX

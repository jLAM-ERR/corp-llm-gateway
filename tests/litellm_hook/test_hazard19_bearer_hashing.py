"""Plan 20260926 hazard 19: litellm hashes the developer's bearer before it logs it.

With no master key litellm builds ``UserAPIKeyAuth`` from whatever bearer the client
sent, and ``UserAPIKeyAuth.check_api_key`` (``proxy/_types.py:3017-3050``) hashes it only
when it starts with ``sk-`` (``hash_token``) or is a JWT (``hashed-jwt-`` + ``hash_token``).
The result is copied to ``user_api_key``, ``user_api_key_hash`` and
``user_api_key_auth.{token,api_key}`` in each metadata bucket and in the body snapshot, and
to ``StandardLoggingPayload.metadata.user_api_key_hash`` — ``KEY_SITES``, 17 log-kwargs
sites on chat (its body snapshot carries both buckets), 13 on messages and responses — and
into the spend-log row. Every
shipped client sends a hashed shape (``sk-ant-oat01-…``, ``sk-ant-api03-…``, ``sk-…``, a
ChatGPT JWT), so the BYOK credential (invariant 3) reaches no logging surface raw. This
pins that precondition. A litellm bump must re-check ``check_api_key``.

Hazard 19b is the one exception, characterised here and not fixed: litellm's log kwargs
carry the provider credential of the call raw (``PROVIDER_KEY_SITES``), and with a
subscription bridge on, that credential is the developer's own bearer.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("litellm.proxy.proxy_server", reason="litellm proxy not installed")

from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import hash_token
from litellm.proxy.spend_tracking.spend_tracking_utils import get_logging_payload

from corp_llm_gateway import litellm_hook
from tests.litellm_hook._dispatch_fixtures import DispatchHarness, StubUpstream, until
from tests.litellm_hook.test_hazard18_corp_token_snapshot import (
    CORP_TOKEN,
    _guardrail,
    token_sites,
)

ROUTES = ["chat", "messages", "responses"]
JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJDQU5BUlktSDE5LUpXVCJ9.c2lnLUNBTkFSWS1IMTk"
SHIPPED_BEARERS = {
    "claude-oauth": "sk-ant-oat01-CANARY-H19-OAT-7c2e",
    "anthropic-api-key": "sk-ant-api03-CANARY-H19-API-7c2e",
    "openai-key": "sk-proj-CANARY-H19-OPENAI-7c2e",
    "chatgpt-jwt": JWT,
}
PLAIN_BEARER = "plain-CANARY-H19-PLAIN-7c2e"


def _bucket_sites(prefix: str) -> list[str]:
    return [
        f"{prefix}.user_api_key",
        f"{prefix}.user_api_key_auth.api_key",
        f"{prefix}.user_api_key_auth.token",
        f"{prefix}.user_api_key_hash",
    ]


# Measured on litellm 1.101.0: every log-kwargs path that holds the key litellm logged.
_COMMON_SITES = [
    *_bucket_sites(".litellm_params.litellm_metadata"),
    *_bucket_sites(".litellm_params.metadata"),
    *_bucket_sites(".litellm_params.proxy_server_request.body.litellm_metadata"),
    ".standard_logging_object.metadata.user_api_key_hash",
]
KEY_SITES: dict[str, list[str]] = {
    "chat": sorted(
        [*_COMMON_SITES, *_bucket_sites(".litellm_params.proxy_server_request.body.metadata")]
    ),
    "messages": sorted(_COMMON_SITES),
    "responses": sorted(_COMMON_SITES),
}
SPEND_KEY_SITES = [".api_key", ".metadata", ".proxy_server_request"]


def _hashed(secret: str) -> str:
    if secret.startswith("sk-"):
        return hash_token(secret)
    assert secret == JWT
    return f"hashed-jwt-{hash_token(token=secret)}"


class KwargsCapture(CustomLogger):
    def __init__(self) -> None:
        super().__init__()
        self.kwargs: list[dict[str, Any]] = []
        self.spend_rows: list[dict[str, Any]] = []

    async def async_log_success_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self.kwargs.append(kwargs)
        timed = {**kwargs, "completion_start_time": kwargs.get("completion_start_time") or end_time}
        self.spend_rows.append(dict(get_logging_payload(timed, response_obj, start_time, end_time)))


def _needles(secret: str) -> list[str]:
    # A JWT's segments too: a surface could carry its payload without the whole token.
    return [secret, *secret.split(".")] if secret.count(".") == 2 else [secret]


async def _send(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    headers: dict[str, str],
    *,
    stream: bool = False,
    ours: Any = None,
) -> KwargsCapture:
    upstream = StubUpstream()
    try:
        capture = KwargsCapture()
        harness = DispatchHarness(
            monkeypatch,
            upstream,
            [ours if ours is not None else _guardrail(), capture],
            general_settings={"store_prompts_in_spend_logs": True},
        )
        exchange = await harness.send(route, stream=stream, token=CORP_TOKEN, headers=headers)
        await until(lambda: len(capture.kwargs) >= 1)
    finally:
        upstream.close()
    assert exchange.status == 200
    return capture


def _logged_key(capture: KwargsCapture) -> str:
    return capture.kwargs[0]["standard_logging_object"]["metadata"]["user_api_key_hash"]


@pytest.mark.parametrize("shape", sorted(SHIPPED_BEARERS))
@pytest.mark.parametrize("route", ROUTES)
async def test_a_shipped_bearer_shape_is_logged_only_hashed(
    monkeypatch: pytest.MonkeyPatch, route: str, shape: str
) -> None:
    secret = SHIPPED_BEARERS[shape]
    capture = await _send(monkeypatch, route, {"Authorization": f"Bearer {secret}"})

    for needle in _needles(secret):
        assert token_sites(capture.kwargs, needle) == []
        assert token_sites(capture.spend_rows, needle) == []
    # Not vacuous: litellm logged the key at every site, in exactly its hashed form.
    logged = _hashed(secret)
    assert _logged_key(capture) == logged
    assert logged.startswith("hashed-jwt-") == (shape == "chatgpt-jwt")
    assert sorted(token_sites(capture.kwargs[0], logged)) == KEY_SITES[route]
    assert token_sites(capture.spend_rows[0], logged) == SPEND_KEY_SITES
    assert capture.spend_rows[0]["api_key"] == logged


@pytest.mark.parametrize("route", ROUTES)
async def test_an_anthropic_x_api_key_is_logged_only_hashed(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    secret = SHIPPED_BEARERS["anthropic-api-key"]
    capture = await _send(monkeypatch, route, {"x-api-key": secret})

    assert token_sites(capture.kwargs, secret) == []
    assert token_sites(capture.spend_rows, secret) == []
    logged = hash_token(secret)
    assert _logged_key(capture) == logged
    assert sorted(token_sites(capture.kwargs[0], logged)) == KEY_SITES[route]
    assert token_sites(capture.spend_rows[0], logged) == SPEND_KEY_SITES


@pytest.mark.parametrize("route", ROUTES)
async def test_a_bearer_of_any_other_shape_is_logged_raw(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """Characterisation, not a breach today: no shipped client sends a bearer that is
    neither ``sk-…`` nor a JWT, and the gateway's own identity is ``X-Corp-Auth``, which
    hazard 18 keeps out of every surface. Such a bearer lands raw at every ``KEY_SITES``
    path (17 on chat, 13 on messages and responses) and in the spend-log
    ``proxy_server_request``; the spend-log ``api_key`` column hashes it on its own. The
    site list is exact, so a partial litellm change fails here: re-read ``check_api_key``
    before changing the expectation."""
    capture = await _send(monkeypatch, route, {"Authorization": f"Bearer {PLAIN_BEARER}"})

    assert sorted(token_sites(capture.kwargs[0], PLAIN_BEARER)) == KEY_SITES[route]
    assert len(KEY_SITES[route]) == (17 if route == "chat" else 13)
    assert _logged_key(capture) == PLAIN_BEARER
    assert token_sites(capture.spend_rows[0], PLAIN_BEARER) == [".proxy_server_request"]
    assert capture.spend_rows[0]["api_key"] == hash_token(PLAIN_BEARER)


# ── hazard 19b: the provider credential in litellm's log kwargs ──────────────

# The deployment key every harness deployment carries (``_dispatch_fixtures._deployment``).
DEPLOYMENT_KEY = "sk-stub-key"


def provider_key_sites(route: str, stream: bool, anthropic_header: str = "x-api-key") -> list[str]:
    """Measured on litellm 1.101.0: where the success log kwargs hold the provider
    credential of the call, raw, in the upstream header litellm sent it in
    (``Authorization`` to OpenAI; ``x-api-key``, or ``authorization`` for an OAuth
    token, to Anthropic). Never the ``StandardLoggingPayload``, never the spend-log row."""
    if route == "chat":
        return [".api_key", ".litellm_params.api_key"]
    header = anthropic_header if route == "messages" else "Authorization"
    sites = [f".httpx_response.request.headers.{header}"] if route == "messages" else []
    if stream:
        sites.insert(0, f".additional_args.headers.{header}")
    return sites


def _assert_only_in_log_kwargs(capture: KwargsCapture, secret: str, expected: list[str]) -> None:
    for needle in _needles(secret):
        assert sorted(token_sites(capture.kwargs[0], needle)) == expected, needle
        assert token_sites(capture.kwargs[0]["standard_logging_object"], needle) == []
        assert token_sites(capture.spend_rows, needle) == []


@pytest.mark.parametrize("stream", [False, True], ids=["unary", "sse"])
@pytest.mark.parametrize("route", ["chat", "responses"])
async def test_the_chatgpt_bridge_bearer_sits_raw_in_litellms_log_kwargs(
    monkeypatch: pytest.MonkeyPatch, route: str, stream: bool
) -> None:
    """Hazard 19b, characterised, not fixed. The ChatGPT bridge hands litellm the
    developer's JWT as the call's ``api_key``; litellm keeps it raw where it keeps any
    provider key. Chat: ``.api_key`` and ``.litellm_params.api_key``, unary and SSE.
    ``/v1/responses``: none unary, the upstream ``Authorization`` header in
    ``additional_args`` on SSE. Only a callback registered in litellm sees log kwargs
    (config-gated, like hazards 17 and 18)."""
    ours = _guardrail()
    ours._forward_chatgpt_auth = True
    capture = await _send(
        monkeypatch, route, {"Authorization": f"Bearer {JWT}"}, stream=stream, ours=ours
    )

    _assert_only_in_log_kwargs(capture, JWT, provider_key_sites(route, stream))


@pytest.mark.parametrize("stream", [False, True], ids=["unary", "sse"])
async def test_the_anthropic_bridge_token_sits_raw_in_litellms_log_kwargs(
    monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    """Hazard 19b on the Anthropic subscription bridge (``/v1/messages``): the
    developer's ``sk-ant-oat01-…`` token is the provider credential, so it sits raw in
    the upstream ``authorization`` header litellm keeps in ``httpx_response`` (and
    ``additional_args`` on SSE)."""
    secret = SHIPPED_BEARERS["claude-oauth"]
    ours = _guardrail()
    ours._forward_anthropic_auth = True
    # The bridge gates on the client's model alias; the harness alias names no provider.
    monkeypatch.setattr(litellm_hook, "_detect_provider", lambda data: "anthropic")
    capture = await _send(
        monkeypatch, "messages", {"Authorization": f"Bearer {secret}"}, stream=stream, ours=ours
    )

    _assert_only_in_log_kwargs(
        capture, secret, provider_key_sites("messages", stream, "authorization")
    )


@pytest.mark.parametrize("stream", [False, True], ids=["unary", "sse"])
@pytest.mark.parametrize("route", ROUTES)
async def test_the_sites_are_where_litellm_keeps_any_provider_key(
    monkeypatch: pytest.MonkeyPatch, route: str, stream: bool
) -> None:
    """Without a bridge the deployment's own key sits at the same sites: litellm's
    behaviour, which the bridges inherit, not a copy the gateway makes."""
    capture = await _send(
        monkeypatch, route, {"Authorization": "Bearer sk-proj-H19B-DEV"}, stream=stream
    )

    _assert_only_in_log_kwargs(capture, DEPLOYMENT_KEY, provider_key_sites(route, stream))

"""Plan 20260926 hazard 19: litellm hashes the developer's bearer before it logs it.

With no master key litellm builds ``UserAPIKeyAuth`` from whatever bearer the client
sent, and ``UserAPIKeyAuth.check_api_key`` (``proxy/_types.py:3017-3050``) hashes it only
when it starts with ``sk-`` or is a JWT. The result is copied to
``metadata.user_api_key``, ``user_api_key_hash``, ``user_api_key_auth.{token,api_key}``
(in each metadata bucket and in the body snapshot), to
``StandardLoggingPayload.metadata.user_api_key_hash`` and into the spend-log row. Every
shipped client sends a hashed shape (``sk-ant-oat01-…``, ``sk-ant-api03-…``, ``sk-…``, a
ChatGPT JWT), so the BYOK credential (invariant 3) reaches no logging surface raw. This
pins that precondition. A litellm bump must re-check ``check_api_key``.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("litellm.proxy.proxy_server", reason="litellm proxy not installed")

from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy.spend_tracking.spend_tracking_utils import get_logging_payload

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
    monkeypatch: pytest.MonkeyPatch, route: str, headers: dict[str, str]
) -> KwargsCapture:
    upstream = StubUpstream()
    try:
        capture = KwargsCapture()
        harness = DispatchHarness(
            monkeypatch,
            upstream,
            [_guardrail(), capture],
            general_settings={"store_prompts_in_spend_logs": True},
        )
        exchange = await harness.send(route, stream=False, token=CORP_TOKEN, headers=headers)
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
    # Not vacuous: litellm did log a key, in its hashed form.
    logged = _logged_key(capture)
    assert logged and logged != secret
    assert capture.spend_rows[0]["api_key"] == logged


@pytest.mark.parametrize("route", ROUTES)
async def test_an_anthropic_x_api_key_is_logged_only_hashed(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    secret = SHIPPED_BEARERS["anthropic-api-key"]
    capture = await _send(monkeypatch, route, {"x-api-key": secret})

    assert token_sites(capture.kwargs, secret) == []
    assert token_sites(capture.spend_rows, secret) == []
    assert _logged_key(capture) not in ("", secret)


@pytest.mark.parametrize("route", ROUTES)
async def test_a_bearer_of_any_other_shape_is_logged_raw(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """Characterisation, not a breach today: no shipped client sends a bearer that is
    neither ``sk-…`` nor a JWT, and the gateway's own identity is ``X-Corp-Auth``, which
    hazard 18 keeps out of every surface. Such a bearer lands raw in the log kwargs
    (``user_api_key*`` in every metadata bucket and the body snapshot), in
    ``StandardLoggingPayload.metadata.user_api_key_hash`` and in the spend-log
    ``proxy_server_request``. The spend-log ``api_key`` column is hashed again on its own.
    If this starts failing, litellm changed ``check_api_key``: re-read it before changing
    the expectation."""
    capture = await _send(monkeypatch, route, {"Authorization": f"Bearer {PLAIN_BEARER}"})

    sites = token_sites(capture.kwargs[0], PLAIN_BEARER)
    assert ".litellm_params.metadata.user_api_key" in sites
    assert ".litellm_params.metadata.user_api_key_auth.api_key" in sites
    assert ".standard_logging_object.metadata.user_api_key_hash" in sites
    assert _logged_key(capture) == PLAIN_BEARER
    assert token_sites(capture.spend_rows[0], PLAIN_BEARER) == [".proxy_server_request"]
    assert capture.spend_rows[0]["api_key"] != PLAIN_BEARER

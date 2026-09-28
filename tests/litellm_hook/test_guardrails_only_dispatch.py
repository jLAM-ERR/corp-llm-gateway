"""Plan 20260926 hazard 3: ``enforces_request_content`` on our plain callback.

litellm's ``guardrails_only`` pre-call walk (``proxy/utils.py`` ``pre_call_hook``) runs
a plain ``CustomLogger``'s pre-call hook only when the leaf class sets
``enforces_request_content``. Its caller is the batch input-file scan
(``batch_guardrails.py``), which ships each record in whatever form the walk hands
back. The batch routes are REFUSE today, so this is latent; the attribute keeps our
pre-call on that path should one ever open.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

pytest.importorskip("litellm.proxy.proxy_server", reason="litellm proxy not installed")

import litellm
from litellm.proxy import proxy_server
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.openai_files_endpoints.batch_guardrails import (
    BatchScanResult,
    rewrite_batch_input_file,
    scan_batch_input_file,
)
from litellm.proxy.utils import ProxyLogging

from corp_llm_gateway.litellm_hook import CorpLlmGuardrail
from tests.litellm_hook._dispatch_fixtures import (
    CHAT_MODEL,
    EMAIL,
    ORIGINAL_MARK,
    PLACEHOLDER,
    TOKEN,
    build_ours,
)


def _batch_file() -> io.BytesIO:
    record = {
        "custom_id": "r1",
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {"model": CHAT_MODEL, "messages": [{"role": "user", "content": f"to {EMAIL}"}]},
    }
    return io.BytesIO((json.dumps(record) + "\n").encode())


async def _scan(monkeypatch: pytest.MonkeyPatch, ours: CorpLlmGuardrail) -> tuple[Any, str]:
    monkeypatch.setattr(litellm, "callbacks", [ours])
    monkeypatch.setattr(ProxyLogging, "_callback_capabilities_cache", {})
    source = _batch_file()
    result = await scan_batch_input_file(
        file_source=source,
        request_metadata={"headers": {"X-Corp-Auth": TOKEN}},
        user_api_key_dict=UserAPIKeyAuth(),
        proxy_logging_obj=proxy_server.proxy_logging_obj,
    )
    assert isinstance(result, BatchScanResult)
    return result, rewrite_batch_input_file(source, result).read().decode()


def test_the_guardrail_declares_it_enforces_request_content() -> None:
    assert vars(CorpLlmGuardrail)["enforces_request_content"] is True


async def test_a_guardrails_only_scan_runs_our_pre_call(monkeypatch: pytest.MonkeyPatch) -> None:
    ours, _ = build_ours()

    result, shipped = await _scan(monkeypatch, ours)

    assert len(result.changes) == 1
    assert PLACEHOLDER in shipped
    assert ORIGINAL_MARK not in shipped


async def test_without_the_attribute_the_scan_skips_our_pre_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the attribute buys: litellm's walk passes the record through untouched."""
    monkeypatch.setattr(CorpLlmGuardrail, "enforces_request_content", False)
    ours, _ = build_ours()

    result, shipped = await _scan(monkeypatch, ours)

    assert result.changes == ()
    assert ORIGINAL_MARK in shipped

"""Exactly one reversal, on the served stack: ``asgi.app`` under uvicorn on a real socket
(route gate, in-flight limiter, the ASGI desanitiser, litellm 1.101.0's app, the armed
lifespan, the guardrail ``bootstrap`` builds) in front of a stub provider.

``tests/desanitize_served_script.py`` runs in a subprocess (importing the entrypoint IS
the boot) with two capture callbacks around ours in ``litellm.callbacks`` and litellm at
DEBUG under the test-only allow key; every assertion here reads its JSON. On all six
flows (chat, ``/v1/messages``, ``/v1/responses`` x unary, SSE): the provider gets
placeholders, no litellm capture (success, header, iterator, per-chunk hooks, the success
log) and no DEBUG chunk log line holds an original, the client gets the originals, and
the request has exactly one audit record — the ticket's terminal record. Chat SSE is
driven through the OpenAI SDK's own stream accumulator too.

Hazard 14c: after the boot the script applies a ``LiteLLM_Config`` row through litellm's
own reconcile functions. Two captures added by name (the unknown-name string in
``litellm.callbacks``, and a ``_known_custom_logger_compatible_callbacks`` name litellm
instantiates into its success/failure lists) see placeholders only, in every hook, log
kwargs and ``StandardLoggingPayload``, success and failure; the spend-log row litellm
builds with ``store_prompts_in_spend_logs`` on holds placeholders only; a pass-through
route the row adds to litellm's app is 404 at the gate.

Hazard 18: no capture, DB-added callback, spend-log row or litellm DEBUG line after our
strip holds the ``X-Corp-Auth`` value.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("uvicorn")
pytest.importorskip("litellm.proxy.proxy_server")
pytest.importorskip("prometheus_client")
pytest.importorskip("openai")

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "desanitize_served_script.py"
SENTINEL = "@@RESULT@@"
_INHERITED = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")

FLOWS = [
    f"{route}-{kind}" for route in ("chat", "messages", "responses") for kind in ("unary", "sse")
]
RESPONSE_SIDE = ("success", "headers", "iterator", "per_chunk", "logged")
# litellm DEBUG lines that print the request before any pre-call hook runs: the reason
# DEBUG is refused at arm in prod. Nothing after the pre-call may hold an original.
REQUEST_LOG_SITES = [["common_request_processing.py", 2209], ["litellm_pre_call_utils.py", 2438]]
# Every field today's record carried, identity and counts (the golden-record comparison
# is tests/route_gate/test_terminal_audit.py).
RECORD_FIELDS = {
    "timestamp",
    "request_id",
    "user_id",
    "team_id",
    "provider",
    "model",
    "latency_ms",
    "prompt_token_count",
    "completion_token_count",
    "redaction_count",
    "finding_label_counts",
    "cache_a_hit",
    "gateway_version",
    "status",
    "placeholder_list",
}


@pytest.fixture(scope="module")
def served() -> dict[str, Any]:
    env = {name: os.environ[name] for name in _INHERITED if name in os.environ}
    env.update(
        {
            "PYTHONPATH": str(ROOT / "src"),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "CORP_LLM_ORACLE_ENABLED": "0",
            "CORP_LLM_LOCAL_FIRST": "1",
            "CORP_AUDIT_SINK": "list",
            "CORP_METRICS_EXPORTER": "prometheus",
            "CORP_LLM_DEV_TEAM_TOKEN": "served-dev-token",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "LITELLM_LOG": "DEBUG",
            "CORP_LLM_ALLOW_LITELLM_DEBUG": "1",
        }
    )
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith(SENTINEL)]
    assert completed.returncode == 0 and lines, (
        f"the served script failed (exit {completed.returncode})\n"
        f"--- stdout ---\n{completed.stdout[-6000:]}\n--- stderr ---\n{completed.stderr[-6000:]}"
    )
    return json.loads(lines[-1][len(SENTINEL) :])


def test_the_stack_armed_with_captures_on_both_sides_of_ours(served: dict[str, Any]) -> None:
    assert served["armed"] is True
    assert served["callback_order"] == ["before", "CorpLlmGuardrail", "after"]
    assert set(served["flows"]) == set(FLOWS)


@pytest.mark.parametrize("flow", FLOWS)
def test_the_provider_gets_placeholders_and_the_client_its_originals(
    served: dict[str, Any], flow: str
) -> None:
    result = served["flows"][flow]

    assert result["status"] == 200
    (body,) = result["provider_bodies"]
    assert "[EM" in body and "alice.secret" not in body
    assert result["client_original"] is True
    assert result["client_placeholder"] is False


@pytest.mark.parametrize("flow", FLOWS)
def test_no_litellm_capture_holds_an_original(served: dict[str, Any], flow: str) -> None:
    result = served["flows"][flow]

    assert result["captures_holding_original"] == {"before": [], "after": []}
    # Not vacuous: each capture saw the response on every hook litellm dispatches.
    for name in ("before", "after"):
        saw = set(result["captures_saw"][name])
        assert {"headers", "logged"} <= saw
        assert ("iterator" if flow.endswith("sse") else "success") in saw
        assert set(result["captures_holding_placeholder"][name]) & set(RESPONSE_SIDE)
    if flow == "chat-sse":
        assert "per_chunk" in result["captures_holding_placeholder"]["before"]


@pytest.mark.parametrize("flow", FLOWS)
def test_litellms_debug_output_after_the_pre_call_holds_no_original(
    served: dict[str, Any], flow: str
) -> None:
    result = served["flows"][flow]

    assert result["debug_original_sites"] == REQUEST_LOG_SITES
    assert result["debug_chunk_sites_with_placeholder"]


@pytest.mark.parametrize("flow", FLOWS)
def test_exactly_one_terminal_record_with_todays_fields(served: dict[str, Any], flow: str) -> None:
    result = served["flows"][flow]

    (record,) = result["records"]
    assert set(record) == RECORD_FIELDS
    assert record["request_id"] == result["call_id"]
    assert (record["user_id"], record["team_id"]) == ("local-dev", "local-dev")
    assert record["status"] == "ok" and record["redaction_count"] == 1
    assert record["finding_label_counts"] == {"EMAIL": 1}
    assert isinstance(record["latency_ms"], int) and record["latency_ms"] >= 0
    assert "alice.secret" not in json.dumps(record)
    assert (result["mappings_left"], result["req_state"]) == (0, 0)


@pytest.mark.parametrize("flow", FLOWS)
def test_token_counts_reach_the_record_from_the_response(served: dict[str, Any], flow: str) -> None:
    """The stub's usage (7 in, 2 out), read off the response by the desanitiser: the JSON
    body's ``usage``, Anthropic's ``message_start`` / ``message_delta``, Responses'
    ``response.completed``, and the chat stream's usage chunk, which the pre-call asks
    the provider for."""
    (record,) = served["flows"][flow]["records"]

    assert (record["prompt_token_count"], record["completion_token_count"]) == (7, 2)


def test_a_chat_stream_client_gets_no_usage_chunk_it_did_not_ask_for(
    served: dict[str, Any],
) -> None:
    result = served["flows"]["chat-sse"]

    (body,) = result["provider_bodies"]
    assert json.loads(body)["stream_options"] == {"include_usage": True}
    assert result["usage_on_wire"] == 0


def test_a_chat_stream_client_that_asked_for_usage_gets_the_chunk(served: dict[str, Any]) -> None:
    result = served["chat_sse_client_usage"]

    assert result["status"] == 200 and result["client_original"] is True
    assert result["usage_on_wire"] == 1
    (record,) = result["records"]
    assert (record["prompt_token_count"], record["completion_token_count"]) == (7, 2)


def test_the_openai_sdk_chat_stream_gets_the_original(served: dict[str, Any]) -> None:
    """The ``release/1.0.x`` defect, closed: ``client.chat.completions.stream`` over the
    wire accumulates the restored text, and ``content.done`` carries it whole."""
    result = served["sdk_chat_stream"]

    assert result["client_original"] is True and result["client_placeholder"] is False
    assert result["content_done"] == [result["content"]]
    assert result["captures_holding_original"] == {"before": [], "after": []}
    (record,) = result["records"]
    assert record["status"] == "ok"


def test_a_body_naming_policies_is_refused_at_the_gate(served: dict[str, Any]) -> None:
    result = served["body_policies"]

    assert (result["status"], result["code"]) == (403, "E_ROUTE_BLOCKED")
    assert result["reason"] == "route_gate_body_policies"
    assert result["provider_bodies"] == []
    (record,) = result["records"]
    assert (record["status"], record["block_reason"]) == ("failed", "route_gate_body_policies")


def test_a_form_body_is_refused_at_the_gate(served: dict[str, Any]) -> None:
    result = served["body_not_json"]

    assert (result["status"], result["code"]) == (415, "E_ROUTE_BLOCKED")
    assert result["reason"] == "route_gate_body_not_json"
    assert result["provider_bodies"] == []
    (record,) = result["records"]
    assert (record["status"], record["block_reason"]) == ("failed", "route_gate_body_not_json")


def test_a_client_that_leaves_mid_stream_gets_one_cancelled_record(
    served: dict[str, Any],
) -> None:
    result = served["cancelled_mid_stream"]

    assert result["read_first"] is True
    (record,) = result["records"]
    assert (record["status"], record["error_code"]) == ("cancelled", "E_CLIENT_DISCONNECTED")
    assert "placeholder_list" not in record
    assert (result["mappings_left"], result["req_state"], result["inflight"]) == (0, 0, 0)


def test_a_restoration_failure_is_one_failed_record_and_a_counted_failure(
    served: dict[str, Any],
) -> None:
    result = served["restoration_failure"]

    assert result["status"] == 200
    assert result["client_original"] is False
    (record,) = result["records"]
    assert (record["status"], record["error_code"]) == ("failed", "E_INTERNAL")
    assert 'gateway_failure{component="desanitize"} 1.0' in served["metrics"]


# ── hazard 14c: what a LiteLLM_Config row adds after the boot ────────────────


def test_the_db_row_adds_both_capture_kinds_prompt_logging_and_a_route(
    served: dict[str, Any],
) -> None:
    assert served["db_overlay"] == {
        "string_in_callbacks": True,
        "known_in_success": True,
        "known_in_failure": True,
        "known_in_callbacks": False,
        "store_prompts": True,
        "pass_through_routed": True,
    }


@pytest.mark.parametrize("flow", FLOWS)
def test_a_db_added_callback_sees_placeholders_only(served: dict[str, Any], flow: str) -> None:
    result = served["flows"][flow]
    db = result["db"]

    assert result["client_original"] is True
    for name in ("db-string", "db-known"):
        seen = db[name]
        assert seen["any_original"] is False, name
        assert seen["holding_original"] == []
        assert seen["kwargs"] == seen["kwargs_with_placeholder"] == 1
        assert "logged" in seen["holding_placeholder"]
    # The string one gets litellm's proxy hooks too; the known one log events only.
    assert ("iterator" if flow.endswith("sse") else "success") in db["db-string"]["saw"]
    assert db["db-known"]["saw"] == ["logged"]


@pytest.mark.parametrize("flow", FLOWS)
def test_a_spend_log_row_with_prompts_on_holds_placeholders_only(
    served: dict[str, Any], flow: str
) -> None:
    for name in ("db-string", "db-known"):
        (row,) = served["flows"][flow]["db"][name]["spend_logs"]
        assert "[EM" in row["proxy_server_request"]
        assert "[EM" in row["response"]
        assert "alice.secret" not in json.dumps(row)


def test_a_db_added_callbacks_failure_log_holds_placeholders_only(
    served: dict[str, Any],
) -> None:
    result = served["provider_error"]

    assert result["status"] == 400
    (body,) = result["provider_bodies"]
    assert "[EM" in body and "alice.secret" not in body
    (record,) = result["records"]
    assert record["status"] == "failed"
    for name in ("db-string", "db-known"):
        seen = result["db"][name]
        assert seen["any_original"] is False, name
        assert seen["failed"] >= 1 and seen["failed_with_placeholder"] >= 1
        assert seen["kwargs"] == seen["kwargs_with_placeholder"] >= 1


# ── hazard 18: the corp token on litellm's logging surfaces ───────────────────


def _corp_token_sites(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "captures": result["corp_token"]["captures"],
        "db": {name: seen["holding_corp_token"] for name, seen in result["db"].items()},
        "debug_after_pre_call": result["corp_token"]["debug_after_pre_call"],
    }


NO_CORP_TOKEN = {
    "captures": {"before": [], "after": []},
    "db": {"db-string": False, "db-known": False},
    "debug_after_pre_call": [],
}


@pytest.mark.parametrize("flow", FLOWS)
def test_no_logging_surface_holds_the_corp_token(served: dict[str, Any], flow: str) -> None:
    """The ``X-Corp-Auth`` value: in no capture hook or ``StandardLoggingPayload``, no
    DB-added callback's log kwargs or spend-log row, no litellm DEBUG line after our strip
    (``[]``, not ``None``: the strip line was seen). Chat carried it in litellm's
    ``requester_metadata`` copy of the request headers."""
    assert _corp_token_sites(served["flows"][flow]) == NO_CORP_TOKEN


def test_a_provider_error_log_holds_no_corp_token(served: dict[str, Any]) -> None:
    assert _corp_token_sites(served["provider_error"]) == NO_CORP_TOKEN


def test_a_pass_through_route_added_at_runtime_is_404_at_the_gate(
    served: dict[str, Any],
) -> None:
    result = served["pass_through"]

    assert (result["status"], result["code"]) == (404, "E_ROUTE_BLOCKED")
    assert result["provider_bodies"] == []

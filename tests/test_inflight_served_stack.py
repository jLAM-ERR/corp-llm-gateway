"""Disconnects on a real socket: the served entrypoint (route gate + in-flight
limiter + litellm 1.101.0 + the guardrail) under uvicorn, in front of a stalled
upstream stub on its own socket.

Each loop flavour runs ``tests/inflight_served_script.py`` once in a subprocess
(importing ``corp_llm_gateway.asgi`` is the boot) and every assertion below reads
its JSON. The six cases, per plan 20260927 Task 3: the client closes (a) before
response headers while litellm awaits the stalled upstream, (b) mid-SSE, (c)
during a non-streaming call, (d) inside our pre-call hook, (e) on a zero-chunk
stream, (f) while an audit emit is in flight. Before them, one client at the cap of 1
sends 200 requests back to back: none may get 429, since the slot frees within two
loop hops of the response's end.

A second run (scenario ``isolation``, the default cap of 64) proves what the
disconnect cases cannot: a token lookup shared by two requests survives the
disconnect of the one that started it; two requests that send one
``x-litellm-call-id`` each get their own id, so one's disconnect never ends the
other; and 64+1 clients that announce a body and never send it hold no slot — a
normal request is served while they idle, and each of them gets 408 once the
body-read deadline passes.

A third run (scenario ``budget``) sets the body byte budget to its floor, 25 MiB:
two admitted 10 MiB bodies hold it, a third that declares 10 MiB gets 429 unread,
and the same request is served once the two have finished.

Skips only where uvicorn or litellm is absent (the graceful-degradation venv).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("uvicorn")
pytest.importorskip("litellm")
pytest.importorskip("prometheus_client")

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "inflight_served_script.py"
SENTINEL = "@@RESULT@@"
_INHERITED = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")

CASES = (
    "a_before_headers",
    "b_mid_sse",
    "c_non_streaming",
    "d_pre_call_hook",
    "e_zero_chunk_stream",
    "f_audit_emit_in_flight",
)
REACHES_UPSTREAM = frozenset(
    {"a_before_headers", "b_mid_sse", "c_non_streaming", "e_zero_chunk_stream"}
)
LOOPS = ["asyncio"] + (["uvloop"] if find_spec("uvloop") is not None else [])


def _served(loop: str, scenario: str = "disconnects", **extra_env: str) -> dict[str, Any]:
    env = {name: os.environ[name] for name in _INHERITED if name in os.environ}
    env.update(
        {
            "PYTHONPATH": str(ROOT / "src"),
            # A developer machine's proxy (env or OS settings) would sit between
            # litellm and the stub and keep the stub's socket open after litellm
            # closed its own.
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "CORP_LLM_ORACLE_ENABLED": "0",
            "CORP_LLM_LOCAL_FIRST": "1",
            "CORP_AUDIT_SINK": "list",
            "CORP_METRICS_EXPORTER": "prometheus",
            "CORP_LLM_DEV_TEAM_TOKEN": "served-dev-token",
            "CORP_LLM_MAX_INFLIGHT": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            **extra_env,
        }
    )
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), loop, scenario],
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
    result: dict[str, Any] = json.loads(lines[-1][len(SENTINEL) :])
    result["stdout"] = completed.stdout
    return result


@pytest.fixture(scope="module", params=LOOPS)
def served(request: pytest.FixtureRequest) -> dict[str, Any]:
    return _served(request.param)


def test_the_stack_served_a_normal_request_first(served: dict[str, Any]) -> None:
    assert served["warmup"] == 200
    assert served["max_inflight"] == 1
    assert set(served["cases"]) == set(CASES)


def test_a_sequential_client_at_the_cap_is_never_refused(served: dict[str, Any]) -> None:
    result = served["sequential"]

    # The client reads EOF inside uvicorn's final send, while the app still unwinds;
    # its next request must not find the slot still held.
    assert result["statuses"] == {"200": 200}


def test_the_slot_frees_within_two_loop_hops_of_the_response_end(served: dict[str, Any]) -> None:
    # asyncio needs 1, uvloop 2; each hop more is a window for a spurious 429.
    assert max(served["sequential"]["release_hops"]) <= 2


@pytest.mark.parametrize("case", CASES)
def test_the_request_held_a_tagged_slot_while_stalled(served: dict[str, Any], case: str) -> None:
    result = served["cases"][case]

    assert result["inflight_before"] == 1
    # The task factory tagged the request's tasks, so "none pending" below is
    # not vacuous.
    assert result["tagged_while_stalled"] >= 1


@pytest.mark.parametrize("case", CASES)
def test_the_downstream_finished_within_the_bound(served: dict[str, Any], case: str) -> None:
    assert served["cases"][case]["released_s"] < served["cancel_grace_s"]


@pytest.mark.parametrize("case", CASES)
def test_the_upstream_socket_is_closed(served: dict[str, Any], case: str) -> None:
    result = served["cases"][case]

    if case in REACHES_UPSTREAM:
        assert result["upstream_contacted"] is True
        assert result["upstream_closed_s"] is not None
        assert result["upstream_closed_s"] < served["cancel_grace_s"]
    else:
        # Cancelled before litellm routed it: nothing ever left for the stub.
        assert result["upstream_contacted"] is False


@pytest.mark.parametrize("case", CASES)
def test_no_request_task_is_left_pending(served: dict[str, Any], case: str) -> None:
    assert served["cases"][case]["pending_request_tasks"] == 0


@pytest.mark.parametrize("case", CASES)
def test_the_guardrail_state_is_back_to_baseline(served: dict[str, Any], case: str) -> None:
    assert served["cases"][case]["req_state_delta"] == 0


@pytest.mark.parametrize("case", CASES)
def test_exactly_one_cancelled_record(served: dict[str, Any], case: str) -> None:
    result = served["cases"][case]

    assert result["statuses"] == ["cancelled"]
    assert result["error_codes"] == ["E_CLIENT_DISCONNECTED"]


@pytest.mark.parametrize("case", CASES)
def test_the_slot_is_released_once_and_the_next_request_is_admitted(
    served: dict[str, Any], case: str
) -> None:
    result = served["cases"][case]

    # 0, not -1: a double release would let the cap admit one request too many.
    assert result["inflight_after"] == 0
    assert result["next_status"] == 200
    assert result["inflight_after_next"] == 0


def test_the_mid_stream_client_saw_the_stream_start(served: dict[str, Any]) -> None:
    status, first_chunk = served["cases"]["b_mid_sse"]["client_saw"]

    assert status == 200
    assert "chat.completion.chunk" in first_chunk


@pytest.mark.parametrize(
    ("case", "watcher"),
    [
        ("a_before_headers", "cancel_on_disconnect_monitor"),
        ("c_non_streaming", "cancel_on_disconnect_monitor"),
        ("e_zero_chunk_stream", "first_chunk_disconnect_task"),
    ],
)
def test_litellms_own_watchers_get_the_disconnect_through_the_replay(
    served: dict[str, Any], case: str, watcher: str
) -> None:
    # They read the limiter's replay receive, never the socket; without the
    # forwarded disconnect they would wait on it forever.
    assert watcher in served["cases"][case]["litellm_watchers"]


def test_the_metrics_count_every_cancellation_and_no_gate_failure(served: dict[str, Any]) -> None:
    assert served["metrics"]["status"] == 200
    text = served["metrics"]["text"]

    assert f"gateway_cancelled_requests_total {float(len(CASES))}" in text
    assert "gateway_inflight_requests 0.0" in text
    assert 'gateway_failure{component="route_gate"}' not in text


# ── isolation: shared tasks and idle bodies ──────────────────────────────────

BODY_READ_S = 2.0
# The server's deadline runs on the loop clock; the client measures with time.monotonic().
# uvloop's loop clock is libuv's whole-millisecond time, so its timer can fire up to ~1 ms
# (a Linux coarse clock: ~2 ms) before time.monotonic() has moved by the full deadline.
CLOCK_GRANULARITY_S = 0.01


@pytest.fixture(scope="module", params=LOOPS)
def isolated(request: pytest.FixtureRequest) -> dict[str, Any]:
    return _served(
        request.param,
        "isolation",
        CORP_LLM_MAX_INFLIGHT="64",
        CORP_LLM_BODY_READ_SECONDS=str(BODY_READ_S),
    )


def test_the_isolation_stack_runs_the_default_limits(isolated: dict[str, Any]) -> None:
    assert isolated["warmup"] == 200
    assert (isolated["max_inflight"], isolated["max_draining"]) == (64, 256)
    assert isolated["body_read_s"] == BODY_READ_S


def test_a_shared_lookup_survives_the_disconnect_of_the_request_that_started_it(
    isolated: dict[str, Any],
) -> None:
    result = isolated["shared_lookup"]

    # Both requests were parked on one lookup, started by A.
    assert (result["lookups_in_flight"], result["inflight_both"]) == (1, 2)
    assert result["a_released_s"] < isolated["cancel_grace_s"]
    assert result["shared_cancelled"] is False
    assert result["b_answered_early"] is False
    assert result["b_status"] == 200
    assert result["b_completion"] is True
    # A was cancelled inside its auth lookup, before it had an identity.
    assert result["records"] == [["cancelled", "unknown"], ["ok", "local-dev"]]
    assert result["pending_request_tasks"] == 0
    assert result["req_state_delta"] == 0
    assert result["inflight_after"] == 0


def test_a_client_call_id_never_lets_one_disconnect_end_another_request(
    isolated: dict[str, Any],
) -> None:
    result = isolated["shared_call_id"]

    # litellm minted its own id for each request: the header never reached it.
    assert result["client_call_id_recorded"] is False
    assert result["b_echoed_client_call_id"] is False
    assert (result["b_status"], result["b_completion"]) == (200, True)
    assert result["statuses"] == ["cancelled", "ok"]
    assert result["distinct_request_ids"] == 2
    assert result["req_state_delta"] == 0
    assert result["inflight_after"] == 0


def test_idle_bodies_hold_no_slot_and_a_normal_request_is_served(
    isolated: dict[str, Any],
) -> None:
    result = isolated["idle_bodies"]

    assert result["inflight_samples"] > 0
    assert result["inflight_samples_max"] == 0
    assert result["gauge_while_idle"] == ["gateway_inflight_requests 0.0"]
    assert result["normal_status"] == 200
    # The request's own round trip, not the idle sockets' setup before it.
    assert result["normal_latency_s"] < 1.5
    # The stall was still on when the normal request finished.
    assert result["draining_after_normal"] == 65


def test_idle_bodies_get_408_once_the_deadline_passes(isolated: dict[str, Any]) -> None:
    result = isolated["idle_bodies"]

    assert result["count"] == 65
    assert result["statuses"] == ["408"]
    assert result["body_timeout_codes"] == 65
    # No refusal before the first deadline; all of them within 3 s of the last one.
    assert result["first_refused_s"] >= BODY_READ_S - CLOCK_GRANULARITY_S
    assert result["refused_after_draining_s"] <= BODY_READ_S + 3
    assert result["draining_after"] == 0
    text = isolated["metrics"]["text"]
    assert 'corp_llm_gateway_blocked_requests_total{block_reason="body_timeout"} 65.0' in text
    assert 'gateway_failure{component="route_gate"}' not in text


# ── the body byte budget ─────────────────────────────────────────────────────

BUDGET_BYTES = 25 * 1024 * 1024
BUDGET_BODY_BYTES = 10 * 1024 * 1024


@pytest.fixture(scope="module", params=LOOPS)
def budgeted(request: pytest.FixtureRequest) -> dict[str, Any]:
    return _served(
        request.param,
        "budget",
        CORP_LLM_MAX_INFLIGHT="64",
        CORP_LLM_MAX_DRAINING_BYTES=str(BUDGET_BYTES),
    )


def test_the_budget_stack_reads_the_byte_budget_from_the_environment(
    budgeted: dict[str, Any],
) -> None:
    assert budgeted["warmup"] == 200
    assert budgeted["max_draining_bytes"] == BUDGET_BYTES
    assert budgeted["byte_budget"]["body_bytes"] == BUDGET_BODY_BYTES


def test_admitted_bodies_hold_the_budget_until_they_finish(budgeted: dict[str, Any]) -> None:
    result = budgeted["byte_budget"]

    assert result["inflight_while_held"] == 2
    assert result["buffered_while_held"] == 2 * BUDGET_BODY_BYTES
    assert result["gauge_while_held"] == 2 * BUDGET_BODY_BYTES
    assert result["held_statuses"] == [200, 200]
    assert result["held_completions"] == [True, True]


def test_a_body_that_would_pass_the_budget_gets_429_then_200_once_it_is_free(
    budgeted: dict[str, Any],
) -> None:
    result = budgeted["byte_budget"]

    assert result["third_status"] == 429
    assert result["third_capacity"] is True
    assert result["third_retry_after"] is True
    assert result["buffered_after_refusal"] == 2 * BUDGET_BODY_BYTES
    assert result["retry_status"] == 200
    assert result["retry_completion"] is True


def test_the_budget_is_all_given_back(budgeted: dict[str, Any]) -> None:
    result = budgeted["byte_budget"]

    assert (result["buffered_after"], result["inflight_after"]) == (0, 0)
    assert budgeted["buffered_bytes"] == 0
    text = budgeted["metrics"]["text"]
    assert "gateway_draining_bytes 0.0" in text
    assert 'corp_llm_gateway_blocked_requests_total{block_reason="capacity"} 1.0' in text
    assert 'gateway_failure{component="route_gate"}' not in text

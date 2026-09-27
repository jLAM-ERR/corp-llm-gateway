"""Disconnects on a real socket: the served entrypoint (route gate + in-flight
limiter + litellm 1.101.0 + the guardrail) under uvicorn, in front of a stalled
upstream stub on its own socket.

Each loop flavour runs ``tests/inflight_served_script.py`` once in a subprocess
(importing ``corp_llm_gateway.asgi`` is the boot) and every assertion below reads
its JSON. The six cases, per plan 20260927 Task 3: the client closes (a) before
response headers while litellm awaits the stalled upstream, (b) mid-SSE, (c)
during a non-streaming call, (d) inside our pre-call hook, (e) on a zero-chunk
stream, (f) while an audit emit is in flight.

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


def _served(loop: str) -> dict[str, Any]:
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
        }
    )
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), loop],
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

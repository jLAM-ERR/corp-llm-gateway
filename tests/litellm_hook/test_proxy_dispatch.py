"""Plan 20260926 Task 0: litellm 1.101.0's real dispatch, driven end to end.

Each request goes through our gate, litellm's own proxy app and a stub provider on a
socket (``_dispatch_fixtures``). "Today" is ``CorpLlmGuardrail(CustomLogger)`` as
registered in production; "migrated" is the throwaway ``UnsafeMigratedGuardrail``, what a
base-class switch without the sentinel would build. Characterisation tests assert what
litellm does now and name the hazard each one documents.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("litellm.proxy.proxy_server", reason="litellm proxy not installed")

import litellm
from litellm.proxy.policy_engine.policy_registry import get_policy_registry
from litellm.proxy.utils import ProxyLogging

from corp_llm_gateway.route_gate.desanitize_middleware import (
    DesanitizeMiddleware,
    ResponseMappings,
)
from tests.litellm_hook._dispatch_fixtures import (
    _CALLBACK_LISTS,
    E_SANITIZER_SKIPPED,
    GUARDRAIL_NAME,
    ORIGINAL_MARK,
    PLACEHOLDER,
    PLACEHOLDER_MARK,
    POLICY_NAME,
    ApplyGuardrailMigrated,
    Capture,
    DispatchHarness,
    MetadataSpy,
    NativeApplyGuardrailMigrated,
    OptionAPreCall,
    SentinelCallback,
    StandInGuardrail,
    StubUpstream,
    UnsafeMigratedGuardrail,
    arm_problems,
    body_names_policies,
    build_ours,
    load_policies,
    pipeline_names_us,
    policy_config,
    request_body,
    until,
)

FLOWS = [(route, stream) for route in ("chat", "messages", "responses") for stream in (False, True)]
FLOW_IDS = [f"{route}-{'sse' if stream else 'unary'}" for route, stream in FLOWS]


@pytest.fixture
def upstream() -> Iterator[StubUpstream]:
    stub = StubUpstream()
    yield stub
    stub.close()


def _egressed_placeholders(bodies: list[str]) -> bool:
    return len(bodies) == 1 and PLACEHOLDER in bodies[0] and ORIGINAL_MARK not in bodies[0]


def _egressed_original(bodies: list[str]) -> bool:
    return len(bodies) == 1 and ORIGINAL_MARK in bodies[0]


# ── the harness itself ───────────────────────────────────────────────────────


@pytest.mark.parametrize(("route", "stream"), FLOWS, ids=FLOW_IDS)
async def test_harness_drives_every_hook_litellm_dispatches(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream, route: str, stream: bool
) -> None:
    ours, sink = build_ours()
    before, after = Capture("before"), Capture("after")
    harness = DispatchHarness(monkeypatch, upstream, [before, ours, after])

    exchange = await harness.send(route, stream=stream)
    await until(lambda: bool(before.seen.logged and after.seen.logged and sink.records))

    assert exchange.status == 200
    assert _egressed_placeholders(exchange.provider_bodies)
    for capture in (before, after):
        assert len(capture.seen.headers) == 1
        if stream:
            assert capture.seen.iterator and not capture.seen.success
        else:
            assert len(capture.seen.success) == 1 and not capture.seen.iterator
        # Per-chunk hooks get content only for ModelResponseStream chunks (chat SSE):
        # proxy/utils.py:3091-3098 skips bytes (/v1/messages) and Responses events.
        assert bool(capture.seen.per_chunk) is (route == "chat" and stream)
        assert capture.seen.logged
    assert [(r["status"], r["redaction_count"]) for r in sink.records] == [("ok", 1)]


def _litellm_globals() -> dict[str, Any]:
    clients = litellm.in_memory_llm_clients_cache
    return {
        **{name: getattr(litellm, name) for name in _CALLBACK_LISTS},
        "clients.cache_dict": clients.cache_dict,
        "clients.ttl_dict": clients.ttl_dict,
        "clients.expiration_heap": clients.expiration_heap,
        "callback_capabilities": ProxyLogging._callback_capabilities_cache,
    }


async def test_the_harness_leaves_litellms_globals_as_it_found_them(
    upstream: StubUpstream,
) -> None:
    """``Router.__init__`` appends its deployment callbacks to litellm's process-wide lists
    and a request caches a provider client: undoing the harness puts back the very objects
    it found, at their old lengths, so nothing accumulates across the suite."""
    before = _litellm_globals()
    lengths = {name: len(value) for name, value in before.items()}
    patch = pytest.MonkeyPatch()
    try:
        ours, _ = build_ours()
        harness = DispatchHarness(patch, upstream, [Capture("c"), ours])
        exchange = await harness.send("chat", stream=True)
        assert exchange.status == 200
        assert len(litellm.success_callback) > lengths["success_callback"]
    finally:
        patch.undo()

    after = _litellm_globals()
    assert [name for name in before if after[name] is not before[name]] == []
    assert {name: len(value) for name, value in after.items()} == lengths


@pytest.mark.parametrize(("route", "stream"), FLOWS, ids=FLOW_IDS)
async def test_client_gets_originals_back_except_chat_sse(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream, route: str, stream: bool
) -> None:
    """Today's callback reversal, per flow.

    Chat-completions SSE is NOT restored: litellm feeds ``ModelResponseStream`` objects to
    the iterator hook and ``_post_call_stream_impl`` only rewrites bytes/str, dicts and
    Responses events (litellm_hook.py:1478-1497), so the client keeps the placeholders. A
    usability defect, not a leak; the ASGI prototype restores it (Option A).
    """
    ours, _ = build_ours()
    harness = DispatchHarness(monkeypatch, upstream, [ours])

    exchange = await harness.send(route, stream=stream)

    if route == "chat" and stream:
        assert PLACEHOLDER_MARK in exchange.text and ORIGINAL_MARK not in exchange.text
    else:
        assert ORIGINAL_MARK in exchange.text and PLACEHOLDER_MARK not in exchange.text


# ── Option 0: the plain callback is never skipped ────────────────────────────


@pytest.mark.parametrize("selector", ["attachment", "tag_header", "body_name"])
async def test_today_plain_callback_runs_regardless_of_policies(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream, selector: str
) -> None:
    """Option 0 proof (hazards 14, 15b do not apply to a plain ``CustomLogger``).

    A policy whose ``post_call`` pipeline names ``corp-llm-sanitizer`` is selected by a
    config attachment, an ``x-litellm-tags`` attachment, or a body ``policies`` list. litellm
    marks our name pipeline-managed (the spy sees it), yet the plain-callback branch
    (proxy/utils.py:1848-1865) has no pipeline check, so our pre-call still rewrites.
    Would fail if litellm ever applied the skip to plain callbacks.
    """
    ours, _ = build_ours()
    spy = MetadataSpy()
    harness = DispatchHarness(monkeypatch, upstream, [spy, ours])
    attachment, extra, headers = _selector(selector)
    await load_policies(policy_config(attachment=attachment))

    exchange = await harness.send("chat", stream=False, extra=extra, headers=headers)

    assert spy.managed == [frozenset({GUARDRAIL_NAME})]
    assert exchange.status == 200
    assert _egressed_placeholders(exchange.provider_bodies)


def _selector(selector: str) -> tuple[dict | None, dict | None, dict | None]:
    if selector == "attachment":
        return {"scope": "*"}, None, None
    if selector == "tag_header":
        return {"tags": ["corp-tag"]}, None, {"x-litellm-tags": "corp-tag"}
    return None, {"policies": [POLICY_NAME]}, None


# ── hazard 7 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("route", "stream"), FLOWS, ids=FLOW_IDS)
async def test_apply_guardrail_flips_dispatch(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream, route: str, stream: bool
) -> None:
    """Hazard 7: an ``apply_guardrail`` on the migrated class moves every hook to
    ``unified_guardrail`` (proxy/utils.py:1221-1239, 2155, 2821). Our pre-call never runs
    and the provider gets the original; ``use_native_lifecycle_hooks=True`` restores native
    dispatch and each of our hooks runs exactly once. Unsafe fixture fails because the
    unified path hands texts to an ``apply_guardrail`` that does not rewrite them.
    """
    ours, _ = build_ours()
    flipped = ApplyGuardrailMigrated(ours)
    harness = DispatchHarness(monkeypatch, upstream, [flipped])
    applied = ApplyGuardrailMigrated.apply_calls

    exchange = await harness.send(route, stream=stream)

    assert "pre_call" not in flipped.calls
    assert ApplyGuardrailMigrated.apply_calls > applied
    assert _egressed_original(exchange.provider_bodies)
    assert "apply_guardrail" in arm_problems([flipped])

    native_ours, _ = build_ours()
    native = NativeApplyGuardrailMigrated(native_ours)
    harness = DispatchHarness(monkeypatch, upstream, [native])

    exchange = await harness.send(route, stream=stream)

    reversal = "streaming_iterator" if stream else "post_call_success"
    assert native.calls == ["pre_call", reversal]
    assert _egressed_placeholders(exchange.provider_bodies)
    # Still refused: the plan forbids apply_guardrail outright, escape hatch or not.
    assert "apply_guardrail" in arm_problems([native])


# ── hazard 14 on the migrated fixture ────────────────────────────────────────


@pytest.mark.parametrize("selector", ["attachment", "tag_header", "body_name"])
async def test_migrated_pipeline_skip_egresses_originals(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream, selector: str
) -> None:
    """Hazard 14 (a), (c) by name, (d): a ``post_call`` pipeline naming our guardrail makes
    litellm skip the migrated fixture in the pre-call loop (proxy/utils.py:1830-1833) while
    the pipeline itself never runs pre-call (:1600-1602): the provider gets the original.

    The sentinel (a plain callback after ours) refuses before egress: the ticket never got
    our call id. The body-name selector is also refused by the gate-level check prototype
    before litellm parses the body. (b) and the ``policy_<uuid>`` form of (c) need a DB
    row: ``test_fail_open_probes.py``.
    """
    ours, _ = build_ours()
    migrated = UnsafeMigratedGuardrail(ours)
    harness = DispatchHarness(monkeypatch, upstream, [migrated])
    attachment, extra, headers = _selector(selector)
    await load_policies(policy_config(attachment=attachment))

    exchange = await harness.send("chat", stream=False, extra=extra, headers=headers)

    assert "pre_call" not in migrated.calls
    assert _egressed_original(exchange.provider_bodies)
    assert pipeline_names_us(get_policy_registry())
    assert "pipeline_manages_us" in arm_problems([migrated], registry=get_policy_registry())

    guarded_ours, _ = build_ours()
    guarded = DispatchHarness(
        monkeypatch, upstream, [UnsafeMigratedGuardrail(guarded_ours), SentinelCallback()]
    )
    await load_policies(policy_config(attachment=attachment))

    refused = await guarded.send("chat", stream=False, extra=extra, headers=headers)

    assert refused.status == 503
    assert E_SANITIZER_SKIPPED in refused.text
    assert refused.provider_bodies == []

    body = request_body("chat", stream=False) | (extra or {})
    assert body_names_policies(json.dumps(body).encode()) is (selector == "body_name")


async def test_sentinel_passes_a_request_our_pre_call_ran_on(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream
) -> None:
    """The sentinel's marker is the call id our pre-call binds: present on a normal request."""
    ours, _ = build_ours()
    harness = DispatchHarness(
        monkeypatch, upstream, [UnsafeMigratedGuardrail(ours), SentinelCallback()]
    )

    exchange = await harness.send("chat", stream=False)

    assert exchange.status == 200
    assert _egressed_placeholders(exchange.provider_bodies)
    assert exchange.ticket is not None and exchange.ticket.call_ids


# ── hazard 15b ───────────────────────────────────────────────────────────────


async def test_duplicate_guardrail_name_substitutes_callback(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream
) -> None:
    """Hazard 15b: with two router ``guardrail_list`` entries named ``corp-llm-sanitizer``,
    litellm runs the router-selected callback instead of the registered one
    (proxy/utils.py:1181-1196, 1339-1357). The stand-in passes content through, so the
    provider gets the original; the migrated fixture is present and correctly configured
    yet never runs its pre-call. The sentinel refuses; the arm check refuses the name.
    A plain callback is not load-balanced and still rewrites (Option 0).
    """
    stand_in = StandInGuardrail()
    ours, _ = build_ours()
    migrated = UnsafeMigratedGuardrail(ours)
    # Weights make the router's pick deterministic: always the stand-in.
    duplicates = [
        {"guardrail_name": GUARDRAIL_NAME, "callback": migrated, "litellm_params": {"weight": 0}},
        {"guardrail_name": GUARDRAIL_NAME, "callback": stand_in, "litellm_params": {"weight": 1}},
    ]
    harness = DispatchHarness(monkeypatch, upstream, [migrated], guardrail_list=duplicates)

    exchange = await harness.send("chat", stream=False)

    assert stand_in.pre_calls == 1
    assert "pre_call" not in migrated.calls
    assert _egressed_original(exchange.provider_bodies)
    assert "guardrail_name_not_unique" in arm_problems([migrated], router=harness.router)

    guarded_ours, _ = build_ours()
    guarded_migrated = UnsafeMigratedGuardrail(guarded_ours)
    guarded = DispatchHarness(
        monkeypatch,
        upstream,
        [guarded_migrated, SentinelCallback()],
        guardrail_list=[{**duplicates[0], "callback": guarded_migrated}, duplicates[1]],
    )
    refused = await guarded.send("chat", stream=False)
    assert refused.status == 503 and refused.provider_bodies == []

    plain, _ = build_ours()
    today = DispatchHarness(monkeypatch, upstream, [plain], guardrail_list=duplicates)
    served = await today.send("chat", stream=False)
    assert _egressed_placeholders(served.provider_bodies)


def test_a_stand_in_answering_to_our_name_is_not_our_guardrail() -> None:
    """The arm check identifies our guardrail by type, never by ``guardrail_name``: a
    stand-in that answers to ``corp-llm-sanitizer`` leaves the guardrail absent."""
    ours, _ = build_ours()

    assert arm_problems([StandInGuardrail()]) == ["guardrail_absent"]
    assert arm_problems([StandInGuardrail(), ours]) == []


# ── hazards 10 / 13 / 15 ─────────────────────────────────────────────────────

# Which response-side hooks of each capture hold an original today, with
# [capture_before, ours, capture_after]. Unary: plain success hooks run in list order
# (proxy/utils.py:2855-2863), so the one after ours sees the restored response (10);
# the header hook runs after every success hook and gets the restored response for
# both captures (15; common_request_processing.py:2715). Streaming: iterator wrappers
# chain in list order (proxy/utils.py:3189-3226), so the one after ours consumes
# restored chunks (13); the header hook gets the stream object, before any chunk.
# Chat SSE is never restored by the callback (see the test above), so nothing holds one.
TODAY: dict[tuple[str, bool], tuple[set[str], set[str]]] = {
    ("chat", False): ({"headers"}, {"success", "headers"}),
    ("chat", True): (set(), set()),
    ("messages", False): ({"headers"}, {"success", "headers"}),
    ("messages", True): (set(), {"iterator"}),
    ("responses", False): ({"headers"}, {"success", "headers"}),
    ("responses", True): (set(), {"iterator"}),
}
RESPONSE_SIDE = {"success", "headers", "iterator", "per_chunk"}


@pytest.mark.parametrize(("route", "stream"), FLOWS, ids=FLOW_IDS)
async def test_capture_positions_unary_and_streaming(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream, route: str, stream: bool
) -> None:
    """Hazards 10, 13, 15: where restored originals are visible inside litellm today, and
    that the ASGI prototype (Option A) leaves placeholders in every capture while the
    client still gets the originals. On today's callback reversal the assertion that no
    capture holds an original fails at the positions in ``TODAY``.
    """
    ours, _ = build_ours()
    before, after = Capture("before"), Capture("after")
    harness = DispatchHarness(monkeypatch, upstream, [before, ours, after])

    await harness.send(route, stream=stream)

    expected_before, expected_after = TODAY[(route, stream)]
    assert before.seen.holding(ORIGINAL_MARK) & RESPONSE_SIDE == expected_before
    assert after.seen.holding(ORIGINAL_MARK) & RESPONSE_SIDE == expected_after

    mappings = ResponseMappings()
    engine, _ = build_ours()
    before, after = Capture("before"), Capture("after")
    option_a = DispatchHarness(
        monkeypatch,
        upstream,
        [before, OptionAPreCall(engine, mappings), after],
        wrap=lambda app: DesanitizeMiddleware(app, mappings, enabled=True),
    )

    exchange = await option_a.send(route, stream=stream)

    assert _egressed_placeholders(exchange.provider_bodies)
    for capture in (before, after):
        assert capture.seen.holding(ORIGINAL_MARK) & RESPONSE_SIDE == set()
    assert ORIGINAL_MARK in exchange.text and PLACEHOLDER_MARK not in exchange.text
    assert len(mappings) == 0

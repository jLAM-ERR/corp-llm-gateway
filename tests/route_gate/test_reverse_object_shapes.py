"""⚠️ `_apply_reverse_to_response` on model objects (`model_dump` / `model_validate` /
`model_copy`). The middleware calls it only with decoded wire JSON
(`desanitize_middleware.py:150`), so these shapes are not reached in production. They wait
on the src follow-up that removes those branches from `litellm_hook.py`; prune them only
with that change and a per-node-id proof of unreachability (plan 20260926, Task 2)."""

import logging

import pytest

from tests.hook_fixtures import (
    _build_guardrail,
    _data_with_token,
    _FakeChatModelResponse,
    _FakeChatModelResponseHiddenParamsRestoreFails,
    _FakeChatModelResponseValidateFails,
    _FakeChatModelResponseValidateFailsWithPayloadInMessage,
    _restore_object,
    _RestoredWithReadOnlyHiddenParams,
)


async def test_post_call_unary_reverses_chat_completions_model_response_object() -> None:
    """Decision (Task 12): keep the broadening. litellm can hand
    ``post_call_unary`` a real Pydantic ``ModelResponse`` object instead of a
    dict — release's str/dict-only branch left that case un-desanitized
    (safe, but broken). ``desanitize_responses_payload`` is field-name-keyed
    (``content``/``arguments`` are shared with Chat Completions), so reusing it
    here is not Responses-specific and correctly restores this shape too."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    response = _FakeChatModelResponse(
        [{"message": {"role": "assistant", "content": "hello [N1]!"}}]
    )
    out = _restore_object(g, data, response)

    assert isinstance(out, _FakeChatModelResponse)
    assert out.choices[0]["message"]["content"] == "hello alice!"


async def test_post_call_unary_real_litellm_model_response_preserves_hidden_params() -> None:
    """MAJOR 8: the pydantic branch was only ever exercised by the duck-typed
    fake above (pydantic/litellm are absent from the local .venv) — a REAL
    litellm.ModelResponse round-tripped through model_dump -> model_validate
    loses `_hidden_params` (a pydantic PRIVATE attribute, never in the dumped
    dict), which the proxy reads for cost tracking / x-litellm-* headers.
    This is on the DEFAULT (flag-off) path, so CI (which has litellm) must
    exercise the real object, not just the fake."""
    litellm = pytest.importorskip("litellm")

    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    response = litellm.ModelResponse(
        id="chatcmpl-x",
        model="gpt-4",
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello [N1]!"},
                "finish_reason": "stop",
            }
        ],
    )
    response._hidden_params = {"x-litellm-key": "team-a-key-hash"}

    out = _restore_object(g, data, response)

    assert isinstance(out, litellm.ModelResponse)
    assert out.choices[0].message.content == "hello alice!"
    assert out._hidden_params == {"x-litellm-key": "team-a-key-hash"}


async def test_post_call_unary_hidden_params_restore_failure_keeps_desanitized_response() -> None:
    """A failure restoring litellm's private response attrs onto the newly
    validated object must not discard the already-validated,
    already-desanitized `restored` object and fall back to the
    still-placeholdered original — that is the exact silent degradation the
    reconstruction-failure path above deliberately refuses."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    response = _FakeChatModelResponseHiddenParamsRestoreFails(
        [{"message": {"role": "assistant", "content": "hello [N1]!"}}]
    )
    response._hidden_params = {"x-litellm-key": "abc"}

    out = _restore_object(g, data, response)

    assert isinstance(out, _RestoredWithReadOnlyHiddenParams)
    assert out.choices[0]["message"]["content"] == "hello alice!"


async def test_post_call_unary_hidden_params_restore_failure_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Round-4 IMPORTANT 9: the private-attr restore's `contextlib.suppress
    (Exception)` swallowed a restore failure with no log or metric, three
    lines below a comment saying "surface the failure instead of degrading
    quietly" — inconsistent with the sibling reconstruct-failure path below
    it, which does log. A failure here must still keep the desanitized
    response (not discard it), but it must also be observable."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    response = _FakeChatModelResponseHiddenParamsRestoreFails(
        [{"message": {"role": "assistant", "content": "hello [N1]!"}}]
    )
    response._hidden_params = {"x-litellm-key": "abc"}

    with caplog.at_level(logging.WARNING):
        out = _restore_object(g, data, response)

    assert isinstance(out, _RestoredWithReadOnlyHiddenParams)
    assert out.choices[0]["message"]["content"] == "hello alice!"
    assert "litellm_post_call_hidden_params_restore_failed" in caplog.text
    assert "attr=_hidden_params" in caplog.text


async def test_post_call_unary_response_reconstruct_failure_does_not_bypass_validation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``model_validate`` failure must not silently fall through to an
    unvalidated ``model_copy`` — it must surface (logged) and fall back to the
    untouched, still-valid original response rather than an unchecked object."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    response = _FakeChatModelResponseValidateFails(
        [{"message": {"role": "assistant", "content": "hello [N1]!"}}]
    )

    with caplog.at_level(logging.WARNING):
        out = _restore_object(g, data, response)

    assert out is response
    assert "litellm_post_call_response_reconstruct_failed" in caplog.text
    assert "E_RESPONSE_RECONSTRUCT_FAILED" in caplog.text


async def test_post_call_unary_reconstruct_failure_log_has_no_original(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M1-14: the reconstruct-failure log must never carry the original — the
    payload at that point is already desanitized, so a real pydantic
    ValidationError (which embeds the offending `input_value` in its own
    message) must not be logged with its exception details."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)
    caplog.clear()  # only the post_call_unary reconstruct-failure log matters here

    response = _FakeChatModelResponseValidateFailsWithPayloadInMessage(
        [{"message": {"role": "assistant", "content": "hello [N1]!"}}]
    )

    with caplog.at_level(logging.WARNING):
        out = _restore_object(g, data, response)

    assert out is response
    assert "alice" not in caplog.text

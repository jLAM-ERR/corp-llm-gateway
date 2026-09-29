"""The allow-list for our ``guardrail_information`` entry in litellm's logging payload.

litellm's ``StandardLoggingGuardrailInformation`` carries free-form fields
(``guardrail_response`` can hold an exception's text, hazard 8). Ours may carry only
identity, outcome, timing and counts: a NEVER key or any free text is refused.
"""

from __future__ import annotations

from typing import Any

import pytest

from corp_llm_gateway.audit import (
    GUARDRAIL_INFORMATION_KEYS,
    GUARDRAIL_RESPONSE_KEYS,
    NEVER_FIELDS,
    GuardrailInformationRejectedError,
    NeverFieldPresentError,
    assert_guardrail_information_allowed,
    assert_no_never_fields,
)

ORIGINAL = "alice.secret@corp.example"


def _entry(**changes: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "guardrail_name": "corp-llm-sanitizer",
        "guardrail_provider": None,
        "guardrail_mode": "pre_call",
        "guardrail_response": {"redaction_count": 1, "finding_label_counts": {"EMAIL": 1}},
        "guardrail_status": "success",
        "start_time": 1758500000.25,
        "end_time": 1758500000.5,
        "duration": 0.25,
        "masked_entity_count": None,
    }
    entry.update(changes)
    return entry


def _response(**changes: Any) -> dict[str, Any]:
    response = dict(_entry()["guardrail_response"])
    response.update(changes)
    return response


def test_the_allow_list_is_litellms_entry_plus_our_three_facts() -> None:
    assert {
        "guardrail_name",
        "guardrail_provider",
        "guardrail_mode",
        "guardrail_response",
        "guardrail_status",
        "start_time",
        "end_time",
        "duration",
        "masked_entity_count",
    } == GUARDRAIL_INFORMATION_KEYS
    assert {"block_reason", "redaction_count", "finding_label_counts"} == GUARDRAIL_RESPONSE_KEYS


@pytest.mark.parametrize(
    "entry",
    [
        _entry(),
        _entry(
            guardrail_status="guardrail_intervened",
            guardrail_response=_response(
                block_reason="dlp:secret_leak",
                redaction_count=2,
                finding_label_counts={"EMAIL": 1, "API_KEY": 1},
            ),
        ),
        _entry(guardrail_response=_response(block_reason="config:env", redaction_count=0)),
        _entry(guardrail_response=_response(block_reason="request:ambiguous_shape")),
        _entry(guardrail_response=_response(redaction_count=0, finding_label_counts={})),
        _entry(start_time=None, end_time=None, duration=None),
        _entry(duration=0),
    ],
    ids=["pass", "stage5", "stage0", "policy", "no-findings", "no-timing", "int-duration"],
)
def test_an_allow_listed_entry_passes(entry: dict[str, Any]) -> None:
    assert_guardrail_information_allowed(entry)
    assert_guardrail_information_allowed([entry])
    assert_no_never_fields({"request_id": "r1", "guardrail_information": [entry]})
    assert_no_never_fields({"metadata": {"standard_logging_guardrail_information": [entry]}})


def test_an_enum_mode_passes_by_its_value() -> None:
    from enum import StrEnum

    class Hook(StrEnum):
        pre_call = "pre_call"

    assert_guardrail_information_allowed(_entry(guardrail_mode=Hook.pre_call))


@pytest.mark.parametrize("key", sorted(NEVER_FIELDS))
def test_a_never_key_in_the_entry_is_refused(key: str) -> None:
    # The NEVER walk runs first, so the refusal names the NEVER key.
    with pytest.raises(NeverFieldPresentError, match=key):
        assert_guardrail_information_allowed(_entry(**{key: "x"}))
    with pytest.raises(NeverFieldPresentError, match=key):
        assert_guardrail_information_allowed(_entry(guardrail_response={**_response(), key: "x"}))
    with pytest.raises(NeverFieldPresentError, match=key):
        assert_no_never_fields({"guardrail_information": [_entry(**{key: "x"})]})


FREE_TEXT: dict[str, Any] = {
    "extra-entry-key": _entry(guardrail_request=f"write to {ORIGINAL}"),
    "extra-response-key": _entry(
        guardrail_response={**_response(), "error": f"ValueError: cannot scan {ORIGINAL}"}
    ),
    "response-is-exception-text": _entry(guardrail_response=f"cannot scan {ORIGINAL}"),
    "response-is-a-list": _entry(guardrail_response=[_response()]),
    "response-missing-a-fact": _entry(guardrail_response={"finding_label_counts": {}}),
    "block-reason-none": _entry(guardrail_response=_response(block_reason=None)),
    "missing-entry-key": {k: v for k, v in _entry().items() if k != "duration"},
    "block-reason-sentence": _entry(
        guardrail_response=_response(block_reason=f"blocked because {ORIGINAL}")
    ),
    "block-reason-original": _entry(guardrail_response=_response(block_reason=ORIGINAL)),
    "block-reason-upper": _entry(guardrail_response=_response(block_reason="DLP:SECRET")),
    "block-reason-long": _entry(guardrail_response=_response(block_reason="a" * 65)),
    "block-reason-not-str": _entry(guardrail_response=_response(block_reason=["dlp:canary"])),
    "name-sentence": _entry(guardrail_name=f"sanitizer for {ORIGINAL}"),
    "name-none": _entry(guardrail_name=None),
    "mode-free-text": _entry(guardrail_mode="pre_call because of alice"),
    "mode-list": _entry(guardrail_mode=["pre_call"]),
    "status-unknown": _entry(guardrail_status=f"blocked: {ORIGINAL}"),
    "count-str": _entry(guardrail_response=_response(redaction_count="1")),
    "count-bool": _entry(guardrail_response=_response(redaction_count=True)),
    "count-negative": _entry(guardrail_response=_response(redaction_count=-1)),
    "label-original": _entry(guardrail_response=_response(finding_label_counts={ORIGINAL: 1})),
    "label-lower": _entry(guardrail_response=_response(finding_label_counts={"email": 1})),
    "label-count-str": _entry(
        guardrail_response=_response(finding_label_counts={"EMAIL": ORIGINAL})
    ),
    "labels-not-a-map": _entry(guardrail_response=_response(finding_label_counts=["EMAIL"])),
    "provider-set": _entry(guardrail_provider=ORIGINAL),
    "masked-count-set": _entry(masked_entity_count={"EMAIL": 1}),
    "start-str": _entry(start_time=ORIGINAL),
    "duration-bool": _entry(duration=True),
}


@pytest.mark.parametrize("entry", list(FREE_TEXT.values()), ids=list(FREE_TEXT))
def test_free_text_or_an_unlisted_shape_is_refused(entry: dict[str, Any]) -> None:
    with pytest.raises(GuardrailInformationRejectedError) as raised:
        assert_guardrail_information_allowed(entry)

    # The refusal names no value: a value can be the content it refuses.
    assert ORIGINAL not in str(raised.value)
    assert "alice" not in str(raised.value)
    with pytest.raises(NeverFieldPresentError):
        assert_no_never_fields({"request_id": "r1", "guardrail_information": [entry]})


@pytest.mark.parametrize(
    "value",
    ["exception text", [f"{ORIGINAL}"], [None], {"guardrail_name": "x"}, 3],
    ids=["str", "list-of-str", "list-of-none", "bare-partial-map", "int"],
)
def test_a_value_that_is_no_entry_list_is_refused(value: Any) -> None:
    with pytest.raises(GuardrailInformationRejectedError):
        assert_guardrail_information_allowed(value)


def test_an_empty_list_passes() -> None:
    assert_guardrail_information_allowed([])


def test_none_passes_as_no_guardrail_ran() -> None:
    # litellm's own value in a StandardLoggingPayload when no guardrail wrote an entry.
    assert_guardrail_information_allowed(None)
    assert_no_never_fields({"request_id": "r1", "guardrail_information": None})
    assert_no_never_fields({"metadata": {"standard_logging_guardrail_information": None}})


def test_an_unknown_key_name_is_not_quoted_either() -> None:
    with pytest.raises(GuardrailInformationRejectedError) as raised:
        assert_guardrail_information_allowed(_entry(**{ORIGINAL: 1}))

    assert ORIGINAL not in str(raised.value)

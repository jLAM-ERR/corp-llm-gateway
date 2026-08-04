import json
import logging

import httpx
import pytest

from corp_llm_gateway.corp_ner import (
    KNOWN_LABELS,
    MAX_BODY_BYTES,
    MAX_INPUT_CHARS,
    MAX_TEXTS,
    AnalyzeResult,
    CorpNerClient,
    CorpNerUnavailableError,
    Span,
    ner_error_code,
)
from corp_llm_gateway.detectors import NerUnavailableError

BASE_URL = "http://corp-ner.corp.lan:8004"


def _mock_transport(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _spans_response(texts: list[str]) -> httpx.Response:
    return httpx.Response(200, json={"results": [{"spans": [], "truncated": False} for _ in texts]})


def _echo_handler(captured: list[dict]) -> object:  # type: ignore[type-arg]
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(
            {
                "url": str(request.url),
                "method": request.method,
                "texts": body["texts"],
                "request_id": request.headers.get("X-Request-Id"),
                "content_length": len(request.content),
            }
        )
        return _spans_response(body["texts"])

    return handler


async def test_analyze_posts_to_v1_analyze() -> None:
    captured: list[dict] = []
    client = CorpNerClient(BASE_URL, http=_mock_transport(_echo_handler(captured)))

    await client.analyze(["hello"], request_id="req-1")

    assert captured[0]["method"] == "POST"
    assert captured[0]["url"] == f"{BASE_URL}/v1/analyze"
    assert captured[0]["texts"] == ["hello"]
    assert captured[0]["request_id"] == "req-1"


async def test_strips_trailing_slash_in_base_url() -> None:
    captured: list[dict] = []
    client = CorpNerClient(BASE_URL + "/", http=_mock_transport(_echo_handler(captured)))

    await client.analyze(["x"])

    assert captured[0]["url"] == f"{BASE_URL}/v1/analyze"


async def test_generates_a_request_id_when_none_given() -> None:
    captured: list[dict] = []
    client = CorpNerClient(BASE_URL, http=_mock_transport(_echo_handler(captured)))

    await client.analyze(["x"])

    assert captured[0]["request_id"]


async def test_same_request_id_across_chunks() -> None:
    captured: list[dict] = []
    client = CorpNerClient(BASE_URL, http=_mock_transport(_echo_handler(captured)), max_texts=1)

    await client.analyze(["a", "b", "c"], request_id="req-9")

    assert [c["request_id"] for c in captured] == ["req-9", "req-9", "req-9"]


async def test_parses_spans_source_and_null_score() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "spans": [
                            {
                                "start": 0,
                                "end": 4,
                                "label": "PERSON",
                                "score": 0.98,
                                "source": "ner",
                            },
                            {
                                "start": 5,
                                "end": 9,
                                "label": "SECRET",
                                "score": None,
                                "source": "regex",
                            },
                        ],
                        "truncated": False,
                    }
                ]
            },
        )

    client = CorpNerClient(BASE_URL, http=_mock_transport(handler))
    results = await client.analyze(["Иван abcd"])

    assert len(results) == 1
    assert isinstance(results[0], AnalyzeResult)
    assert results[0].truncated is False
    assert results[0].spans == (
        Span(start=0, end=4, label="PERSON", score=0.98, source="ner"),
        Span(start=5, end=9, label="SECRET", score=None, source="regex"),
    )


async def test_parses_truncated_flag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"spans": [], "truncated": True}]})

    client = CorpNerClient(BASE_URL, http=_mock_transport(handler))
    results = await client.analyze(["x"])

    assert results[0].truncated is True


def _static_handler(payload: object) -> object:  # type: ignore[type-arg]
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


async def test_bare_list_response_fails_closed() -> None:
    """The contract is an object with `results`. A bare list is a different
    service (or a hostile one) — it must never read as a clean scan."""
    client = CorpNerClient(
        BASE_URL, http=_mock_transport(_static_handler([{"spans": [], "truncated": False}]))
    )

    with pytest.raises(CorpNerUnavailableError, match="malformed"):
        await client.analyze(["x"])


async def test_missing_results_key_fails_closed() -> None:
    client = CorpNerClient(BASE_URL, http=_mock_transport(_static_handler({"spans": []})))

    with pytest.raises(CorpNerUnavailableError, match="malformed"):
        await client.analyze(["x"])


async def test_results_not_a_list_fails_closed() -> None:
    client = CorpNerClient(BASE_URL, http=_mock_transport(_static_handler({"results": {}})))

    with pytest.raises(CorpNerUnavailableError, match="malformed"):
        await client.analyze(["x"])


async def test_empty_result_object_fails_closed() -> None:
    """`{"results":[{}]}` is the hostile shape: nothing was scanned, yet the
    old parser reported zero findings."""
    client = CorpNerClient(BASE_URL, http=_mock_transport(_static_handler({"results": [{}]})))

    with pytest.raises(CorpNerUnavailableError, match="malformed"):
        await client.analyze(["x"])


async def test_missing_spans_key_fails_closed() -> None:
    client = CorpNerClient(
        BASE_URL, http=_mock_transport(_static_handler({"results": [{"truncated": False}]}))
    )

    with pytest.raises(CorpNerUnavailableError, match="malformed"):
        await client.analyze(["x"])


async def test_null_spans_fails_closed() -> None:
    client = CorpNerClient(
        BASE_URL,
        http=_mock_transport(_static_handler({"results": [{"spans": None, "truncated": False}]})),
    )

    with pytest.raises(CorpNerUnavailableError, match="malformed"):
        await client.analyze(["x"])


async def test_missing_truncated_key_fails_closed() -> None:
    """A missing flag must not default to "the whole text was scanned"."""
    client = CorpNerClient(
        BASE_URL, http=_mock_transport(_static_handler({"results": [{"spans": []}]}))
    )

    with pytest.raises(CorpNerUnavailableError, match="malformed"):
        await client.analyze(["x"])


async def test_non_bool_truncated_fails_closed() -> None:
    client = CorpNerClient(
        BASE_URL,
        http=_mock_transport(_static_handler({"results": [{"spans": [], "truncated": 0}]})),
    )

    with pytest.raises(CorpNerUnavailableError, match="malformed"):
        await client.analyze(["x"])


def _one_span(**overrides: object) -> dict:  # type: ignore[type-arg]
    span = {"start": 0, "end": 1, "label": "PERSON", "score": 0.9, "source": "ner"}
    span.update(overrides)
    return {"results": [{"spans": [span], "truncated": False}]}


async def test_unknown_label_fails_closed() -> None:
    client = CorpNerClient(
        BASE_URL, http=_mock_transport(_static_handler(_one_span(label="MYSTERY")))
    )

    with pytest.raises(CorpNerUnavailableError, match="malformed"):
        await client.analyze(["x"])


async def test_unknown_source_fails_closed() -> None:
    client = CorpNerClient(
        BASE_URL, http=_mock_transport(_static_handler(_one_span(source="oracle")))
    )

    with pytest.raises(CorpNerUnavailableError, match="malformed"):
        await client.analyze(["x"])


async def test_missing_source_fails_closed() -> None:
    """Defaulting to "ner" would fabricate the provenance B2 derives scores from."""
    payload = _one_span()
    del payload["results"][0]["spans"][0]["source"]  # type: ignore[index]
    client = CorpNerClient(BASE_URL, http=_mock_transport(_static_handler(payload)))

    with pytest.raises(CorpNerUnavailableError, match="malformed"):
        await client.analyze(["x"])


async def test_unknown_label_value_never_reaches_the_error_message() -> None:
    """M1-14: an unknown label is attacker-controlled text, not a constant."""
    canary = "RAW-SECRET-as-a-label-7b1e"
    client = CorpNerClient(BASE_URL, http=_mock_transport(_static_handler(_one_span(label=canary))))

    with pytest.raises(CorpNerUnavailableError) as exc_info:
        await client.analyze(["x"])

    assert canary not in str(exc_info.value)


async def test_bool_score_fails_closed() -> None:
    client = CorpNerClient(BASE_URL, http=_mock_transport(_static_handler(_one_span(score=True))))

    with pytest.raises(CorpNerUnavailableError, match="malformed"):
        await client.analyze(["x"])


async def test_all_nine_contract_labels_are_accepted() -> None:
    labels = sorted(KNOWN_LABELS)
    assert len(labels) == 9
    payload = {
        "results": [
            {
                "spans": [
                    {"start": i, "end": i + 1, "label": label, "score": None, "source": "regex"}
                    for i, label in enumerate(labels)
                ],
                "truncated": False,
            }
        ]
    }
    client = CorpNerClient(BASE_URL, http=_mock_transport(_static_handler(payload)))

    results = await client.analyze(["x"])

    assert [s.label for s in results[0].spans] == labels


async def test_all_three_sources_are_accepted() -> None:
    payload = {
        "results": [
            {
                "spans": [
                    {"start": i, "end": i + 1, "label": "PERSON", "score": None, "source": source}
                    for i, source in enumerate(("ner", "regex", "both"))
                ],
                "truncated": False,
            }
        ]
    }
    client = CorpNerClient(BASE_URL, http=_mock_transport(_static_handler(payload)))

    results = await client.analyze(["x"])

    assert [s.source for s in results[0].spans] == ["ner", "regex", "both"]


async def test_absent_score_is_none() -> None:
    """The one deliberate laxity: a missing score cannot hide a finding, so it
    reads as None (same as the contract's null) instead of failing the batch."""
    payload = _one_span()
    del payload["results"][0]["spans"][0]["score"]  # type: ignore[index]
    client = CorpNerClient(BASE_URL, http=_mock_transport(_static_handler(payload)))

    results = await client.analyze(["x"])

    assert results[0].spans[0].score is None


async def test_empty_texts_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call must be made for an empty batch")

    client = CorpNerClient(BASE_URL, http=_mock_transport(handler))

    assert await client.analyze([]) == []


async def test_chunks_at_max_texts_and_preserves_order() -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append({"texts": body["texts"]})
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "spans": [
                            {
                                "start": ord(t) - ord("a"),
                                "end": ord(t) - ord("a") + 1,
                                "label": "PERSON",
                                "score": None,
                                "source": "regex",
                            }
                        ],
                        "truncated": False,
                    }
                    for t in body["texts"]
                ]
            },
        )

    client = CorpNerClient(BASE_URL, http=_mock_transport(handler), max_texts=2)
    results = await client.analyze(["a", "b", "c", "d", "e"])

    assert [c["texts"] for c in captured] == [["a", "b"], ["c", "d"], ["e"]]
    assert [r.spans[0].start for r in results] == [0, 1, 2, 3, 4]


async def test_exactly_max_texts_is_one_request() -> None:
    captured: list[dict] = []
    client = CorpNerClient(BASE_URL, http=_mock_transport(_echo_handler(captured)), max_texts=3)

    await client.analyze(["a", "b", "c"])

    assert len(captured) == 1


async def test_chunks_at_max_input_chars() -> None:
    captured: list[dict] = []
    client = CorpNerClient(
        BASE_URL, http=_mock_transport(_echo_handler(captured)), max_input_chars=10
    )

    results = await client.analyze(["a" * 6, "b" * 6, "c" * 4])

    assert [len(c["texts"]) for c in captured] == [1, 2]
    assert len(results) == 3


async def test_chunks_at_max_body_bytes() -> None:
    captured: list[dict] = []
    client = CorpNerClient(
        BASE_URL, http=_mock_transport(_echo_handler(captured)), max_body_bytes=32
    )

    await client.analyze(["a" * 8, "b" * 8, "c" * 8])

    assert len(captured) > 1
    assert all(c["content_length"] <= 32 for c in captured)


async def test_single_text_over_max_input_chars_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an unscannable text must not be sent")

    client = CorpNerClient(BASE_URL, http=_mock_transport(handler), max_input_chars=10)

    with pytest.raises(CorpNerUnavailableError, match="max_input_chars"):
        await client.analyze(["a" * 11])


async def test_single_text_over_max_body_bytes_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an unscannable text must not be sent")

    client = CorpNerClient(BASE_URL, http=_mock_transport(handler), max_body_bytes=16)

    with pytest.raises(CorpNerUnavailableError, match="max_body_bytes"):
        await client.analyze(["a" * 64])


async def test_default_limits_match_the_service_contract() -> None:
    assert (MAX_TEXTS, MAX_INPUT_CHARS, MAX_BODY_BYTES) == (256, 200_000, 64 * 1024 * 1024)
    client = CorpNerClient(BASE_URL, http=_mock_transport(_echo_handler([])))
    assert client._max_texts == MAX_TEXTS
    assert client._max_input_chars == MAX_INPUT_CHARS
    assert client._max_body_bytes == MAX_BODY_BYTES


async def test_non_2xx_fails_closed_without_the_response_body() -> None:
    canary = "RAW-SECRET-echoed-in-body-9f3c"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text=f"upstream said: {canary}")

    client = CorpNerClient(BASE_URL, http=_mock_transport(handler))

    with pytest.raises(CorpNerUnavailableError) as exc_info:
        await client.analyze(["x"])

    exc = exc_info.value
    assert "503" in str(exc)
    assert canary not in str(exc)
    assert exc.__cause__ is None
    assert canary not in repr(exc.__context__)


async def test_transport_error_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = CorpNerClient(BASE_URL, http=_mock_transport(handler))

    with pytest.raises(CorpNerUnavailableError, match="ConnectError"):
        await client.analyze(["x"])


async def test_timeout_names_the_exception_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    client = CorpNerClient(BASE_URL, http=_mock_transport(handler))

    with pytest.raises(CorpNerUnavailableError, match="ReadTimeout"):
        await client.analyze(["x"])


async def test_malformed_json_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    client = CorpNerClient(BASE_URL, http=_mock_transport(handler))

    with pytest.raises(CorpNerUnavailableError, match="malformed"):
        await client.analyze(["x"])


async def test_result_count_mismatch_fails_closed() -> None:
    """Misaligned results would attach one text's offsets to another text."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"spans": [], "truncated": False}]})

    client = CorpNerClient(BASE_URL, http=_mock_transport(handler))

    with pytest.raises(CorpNerUnavailableError, match="result count"):
        await client.analyze(["a", "b"])


async def test_malformed_span_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "spans": [{"start": "nope", "end": 3, "label": "PERSON"}],
                        "truncated": False,
                    }
                ]
            },
        )

    client = CorpNerClient(BASE_URL, http=_mock_transport(handler))

    with pytest.raises(CorpNerUnavailableError, match="malformed span"):
        await client.analyze(["x"])


async def test_error_subclasses_ner_unavailable_error() -> None:
    """Load-bearing: the existing fail-closed `except NerUnavailableError`
    handlers in litellm_hook.py must cover corp-NER failures too."""
    assert issubclass(CorpNerUnavailableError, NerUnavailableError)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = CorpNerClient(BASE_URL, http=_mock_transport(handler))
    with pytest.raises(NerUnavailableError):
        await client.analyze(["x"])


def test_ner_error_code_distinguishes_corp_ner_from_local_ner() -> None:
    assert ner_error_code(CorpNerUnavailableError("down")) == "E_CORP_NER_UNAVAILABLE"
    assert ner_error_code(NerUnavailableError("model absent")) == "E_NER_UNAVAILABLE"


async def test_never_logs_request_or_response_text(caplog: pytest.LogCaptureFixture) -> None:
    """M1-14: counts, labels and exception type only — never content."""
    request_canary = "Иван Петрович secret-token-abc123"
    response_canary = "ECHOED-ORIGINAL-4f2a"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=f"bad input: {response_canary}")

    caplog.set_level(logging.DEBUG)
    client = CorpNerClient(BASE_URL, http=_mock_transport(handler))

    with pytest.raises(CorpNerUnavailableError) as exc_info:
        await client.analyze([request_canary])

    emitted = caplog.text
    for canary in (request_canary, response_canary):
        assert canary not in emitted
        assert canary not in str(exc_info.value)


async def test_owned_client_is_closed_by_aclose(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    client = CorpNerClient(BASE_URL)
    assert client._http.is_closed is False
    await client.aclose()
    assert client._http.is_closed is True


async def test_injected_client_is_not_closed_by_aclose() -> None:
    http = _mock_transport(_echo_handler([]))
    client = CorpNerClient(BASE_URL, http=http)

    await client.aclose()

    assert http.is_closed is False

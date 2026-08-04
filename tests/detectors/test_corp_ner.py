"""CorpNerDetector: NFC offset mapping, fail-closed paths, label map, batching."""

from __future__ import annotations

import json
import logging
import random
import unicodedata
from typing import Any

import httpx
import pytest

from corp_llm_gateway.corp_ner import (
    MAX_INPUT_CHARS,
    MAX_TEXTS,
    AnalyzeResult,
    CorpNerClient,
    CorpNerUnavailableError,
    Span,
)
from corp_llm_gateway.detectors.base import BatchPIIDetector, Finding
from corp_llm_gateway.detectors.corp_ner import _MIN_COMPOSABLE_SECOND, CorpNerDetector
from corp_llm_gateway.metrics.base import MetricsExporter

BASE_URL = "http://corp-ner.corp.lan:8004"

# U+0438 + combining breve U+0306 -> U+0439 ; U+0435 + diaeresis U+0308 -> U+0451.
# Two code points collapse to one, so NFC is SHORTER than the input and naive
# offset passthrough slices the wrong substring.
_I_BREVE = "\u0438\u0306"
_E_DIAERESIS = "\u0435\u0308"
_DECOMPOSED = f"Андре{_I_BREVE} Корол{_E_DIAERESIS}в"
_NFC = unicodedata.normalize("NFC", _DECOMPOSED)


class _RecordingMetrics(MetricsExporter):
    def __init__(self) -> None:
        self.blocks: list[str] = []
        self.failures: list[str] = []

    def record_block(self, block_reason: str) -> None:
        self.blocks.append(block_reason)

    def record_failure(self, component: str) -> None:
        self.failures.append(component)

    def observe_request_latency(self, seconds: float, *, status: str) -> None:
        return None


class _StubClient:
    """Bypasses the wire so off-contract payloads (which the real client rejects)
    can still reach the detector's defence-in-depth branches."""

    def __init__(self, results: list[AnalyzeResult]) -> None:
        self._results = results
        self.calls: list[list[str]] = []

    async def analyze(
        self, texts: list[str], *, request_id: str | None = None
    ) -> list[AnalyzeResult]:
        self.calls.append(list(texts))
        return self._results


def _client(handler: Any) -> CorpNerClient:
    return CorpNerClient(BASE_URL, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _handler(spans_for: Any, *, truncated: bool = False) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["texts"]
        return httpx.Response(
            200,
            json={
                "results": [{"spans": spans_for(t), "truncated": truncated} for t in texts],
            },
        )

    return handler


def _span(start: int, end: int, label: str, score: float | None = 0.9, source: str = "ner") -> dict:
    return {"start": start, "end": end, "label": label, "score": score, "source": source}


def _fixed(*spans: dict, truncated: bool = False) -> Any:
    return _handler(lambda _t: list(spans), truncated=truncated)


# ---------------------------------------------------------------------------
# NFC offset mapping
# ---------------------------------------------------------------------------


def test_fixture_actually_diverges_in_length_under_nfc() -> None:
    assert len(_NFC) != len(_DECOMPOSED)
    assert (len(_DECOMPOSED), len(_NFC)) == (16, 14)
    assert _NFC == "Андрей Королёв"


async def test_nfc_offsets_map_back_to_the_original_string() -> None:
    # Offsets are indices into _NFC: "Андрей"=[0,6), "Королёв"=[7,14).
    detector = CorpNerDetector(_client(_fixed(_span(0, 6, "PERSON"), _span(7, 14, "ORGANIZATION"))))

    findings = await detector.detect(_DECOMPOSED)

    assert [(f.start, f.end, f.label) for f in findings] == [(0, 7, "PERSON"), (8, 16, "ORG")]
    for f in findings:
        assert f.text == _DECOMPOSED[f.start : f.end]
    assert unicodedata.normalize("NFC", findings[0].text) == "Андрей"
    assert unicodedata.normalize("NFC", findings[1].text) == "Королёв"


async def test_span_over_a_single_composed_char_covers_both_code_points() -> None:
    # "й" alone: NFC [5,6) -> original [5,7) (base + combining mark).
    detector = CorpNerDetector(_client(_fixed(_span(5, 6, "PERSON"))))

    findings = await detector.detect(_DECOMPOSED)

    assert (findings[0].start, findings[0].end) == (5, 7)
    assert findings[0].text == _I_BREVE


async def test_fast_path_when_text_is_already_nfc() -> None:
    text = "Contact Анна Кузнецова today"
    start, end = text.index("Анна"), text.index("Анна") + len("Анна Кузнецова")
    detector = CorpNerDetector(_client(_fixed(_span(start, end, "PERSON"))))

    findings = await detector.detect(text)

    assert findings[0].text == text[start:end] == "Анна Кузнецова"


async def test_composing_starters_do_not_break_the_map() -> None:
    # Hangul jamo compose across a starter boundary (all three have ccc == 0),
    # so a naive starter-chunked map desynchronises here.
    decomposed = "\u1100\u1161\u11a8 ok"  # jamo L+V+T -> \uac01, all three ccc == 0
    nfc = unicodedata.normalize("NFC", decomposed)
    assert (len(decomposed), len(nfc)) == (6, 4)
    detector = CorpNerDetector(_client(_fixed(_span(2, 4, "PERSON"))))

    findings = await detector.detect(decomposed)

    assert findings[0].text == decomposed[findings[0].start : findings[0].end] == "ok"


async def test_offsets_beyond_the_nfc_text_fail_closed() -> None:
    detector = CorpNerDetector(_client(_fixed(_span(0, 999, "PERSON"))))

    with pytest.raises(CorpNerUnavailableError):
        await detector.detect("short")


async def test_zero_length_spans_are_dropped() -> None:
    detector = CorpNerDetector(_client(_fixed(_span(3, 3, "PERSON"))))

    assert await detector.detect("hello world") == []


# ---------------------------------------------------------------------------
# Fail-closed paths
# ---------------------------------------------------------------------------


async def test_truncated_result_fails_closed() -> None:
    metrics = _RecordingMetrics()
    detector = CorpNerDetector(_client(_fixed(truncated=True)), metrics=metrics)

    with pytest.raises(CorpNerUnavailableError):
        await detector.detect("some prose that was only partly scanned")
    assert metrics.failures == ["corp_ner"]


async def test_timeout_fails_closed_and_records_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("", request=request)

    metrics = _RecordingMetrics()
    detector = CorpNerDetector(_client(handler), metrics=metrics)

    with pytest.raises(CorpNerUnavailableError):
        await detector.detect("hello")
    assert metrics.failures == ["corp_ner"]


async def test_connect_error_fails_closed_and_records_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("", request=request)

    metrics = _RecordingMetrics()
    detector = CorpNerDetector(_client(handler), metrics=metrics)

    with pytest.raises(CorpNerUnavailableError):
        await detector.detect_batch(["hello", "world"])
    assert metrics.failures == ["corp_ner"]


async def test_non_2xx_fails_closed_and_records_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "model loading"})

    metrics = _RecordingMetrics()
    detector = CorpNerDetector(_client(handler), metrics=metrics)

    with pytest.raises(CorpNerUnavailableError):
        await detector.detect("hello")
    assert metrics.failures == ["corp_ner"]


async def test_result_count_mismatch_fails_closed() -> None:
    metrics = _RecordingMetrics()
    detector = CorpNerDetector(_StubClient([]), metrics=metrics)

    with pytest.raises(CorpNerUnavailableError):
        await detector.detect_batch(["a", "b"])
    assert metrics.failures == ["corp_ner"]


async def test_default_metrics_exporter_is_a_noop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(CorpNerUnavailableError):
        await CorpNerDetector(_client(handler)).detect("hello")


async def test_raw_httpx_error_from_the_client_is_wrapped() -> None:
    # Defence in depth: CorpNerClient wraps transport errors itself, so a bare
    # httpx error can only come from a client that forgot to.
    class _LeakyClient:
        async def analyze(self, texts: list[str], *, request_id: str | None = None) -> list:
            raise httpx.ReadTimeout("")

    metrics = _RecordingMetrics()

    with pytest.raises(CorpNerUnavailableError) as excinfo:
        await CorpNerDetector(_LeakyClient(), metrics=metrics).detect("hello")

    assert "ReadTimeout" in str(excinfo.value)
    assert metrics.failures == ["corp_ner"]


# ---------------------------------------------------------------------------
# Score + label map
# ---------------------------------------------------------------------------


async def test_null_score_becomes_one() -> None:
    detector = CorpNerDetector(_client(_fixed(_span(0, 5, "SECRET", score=None, source="regex"))))

    findings = await detector.detect("abcde fghij")

    assert findings[0].score == 1.0


async def test_score_is_preserved_when_present() -> None:
    detector = CorpNerDetector(_client(_fixed(_span(0, 5, "PERSON", score=0.42))))

    findings = await detector.detect("abcde fghij")

    assert findings[0].score == pytest.approx(0.42)


@pytest.mark.parametrize(
    ("service_label", "expected"),
    [
        ("PERSON", "PERSON"),
        ("LOCATION", "LOCATION"),
        ("ORGANIZATION", "ORG"),
        ("LOGIN", "LOGIN"),
        ("PASSWORD", "PASSWORD"),
        ("AUTH_TOKEN", "AUTH_TOKEN"),
        ("SECRET_KEY", "SECRET_KEY"),
        ("CONTRACT_NUMBER", "CONTRACT_NUMBER"),
        ("SECRET", "SECRET"),
    ],
)
async def test_label_map(service_label: str, expected: str) -> None:
    detector = CorpNerDetector(_client(_fixed(_span(0, 5, service_label))))

    findings = await detector.detect("abcde fghij")

    assert findings[0].label == expected


async def test_unknown_label_is_dropped() -> None:
    # Defence in depth: the client hard-fails off-contract labels, so this can
    # only be reached through a stub. The branch must stay.
    stub = _StubClient([AnalyzeResult(spans=(Span(0, 5, "NRP", 0.9, "ner"),), truncated=False)])
    detector = CorpNerDetector(stub)

    assert await detector.detect("abcde fghij") == []


async def test_overlapping_spans_are_deduplicated() -> None:
    detector = CorpNerDetector(
        _client(
            _fixed(
                _span(0, 11, "SECRET", score=None, source="regex"),
                _span(0, 5, "PERSON", score=0.9),
            )
        )
    )

    findings = await detector.detect("abcde fghij")

    assert [(f.start, f.end, f.label) for f in findings] == [(0, 11, "SECRET")]


# ---------------------------------------------------------------------------
# Batch behaviour
# ---------------------------------------------------------------------------


def test_detector_is_a_batch_detector() -> None:
    assert isinstance(CorpNerDetector(_StubClient([])), BatchPIIDetector)


async def test_detect_batch_returns_one_list_per_text_in_order() -> None:
    def spans_for(text: str) -> list[dict]:
        return [_span(0, len(text), "PERSON")] if text.startswith("hit") else []

    detector = CorpNerDetector(_client(_handler(spans_for)))

    out = await detector.detect_batch(["hit one", "miss", "hit two"])

    assert [len(x) for x in out] == [1, 0, 1]
    assert out[0][0].text == "hit one"
    assert out[2][0].text == "hit two"


async def test_batch_chunks_at_max_texts() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["texts"]
        seen.append(len(texts))
        return httpx.Response(
            200, json={"results": [{"spans": [], "truncated": False} for _ in texts]}
        )

    texts = [f"prose number {i}" for i in range(MAX_TEXTS + 3)]
    out = await CorpNerDetector(_client(handler)).detect_batch(texts)

    assert seen == [MAX_TEXTS, 3]
    assert len(out) == len(texts)


async def test_batch_chunks_at_max_input_chars() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["texts"]
        seen.append(sum(len(t) for t in texts))
        return httpx.Response(
            200, json={"results": [{"spans": [], "truncated": False} for _ in texts]}
        )

    texts = ["x" * (MAX_INPUT_CHARS // 2)] * 3
    out = await CorpNerDetector(_client(handler)).detect_batch(texts)

    assert len(seen) == 2
    assert all(total <= MAX_INPUT_CHARS for total in seen)
    assert len(out) == 3


async def test_text_over_max_input_chars_fails_closed() -> None:
    metrics = _RecordingMetrics()
    detector = CorpNerDetector(_client(_fixed()), metrics=metrics)

    with pytest.raises(CorpNerUnavailableError):
        await detector.detect("y" * (MAX_INPUT_CHARS + 1))
    assert metrics.failures == ["corp_ner"]


async def test_empty_and_blank_texts_never_reach_the_service() -> None:
    stub = _StubClient([AnalyzeResult(spans=(Span(0, 5, "PERSON", 0.9, "ner"),), truncated=False)])
    detector = CorpNerDetector(stub)

    out = await detector.detect_batch(["", "   \n", "abcde fghij"])

    assert stub.calls == [["abcde fghij"]]
    assert [len(x) for x in out] == [0, 0, 1]


async def test_empty_input_returns_empty() -> None:
    stub = _StubClient([])

    assert await CorpNerDetector(stub).detect("") == []
    assert await CorpNerDetector(stub).detect_batch([]) == []
    assert stub.calls == []


# ---------------------------------------------------------------------------
# M1-14
# ---------------------------------------------------------------------------


async def test_never_logs_request_or_response_text(caplog: pytest.LogCaptureFixture) -> None:
    secret = "\u041a\u043e\u0440\u043e\u043b\u0451\u0432 \u0410\u043d\u0434\u0440\u0435\u0439"
    detector = CorpNerDetector(_client(_fixed(_span(0, len(secret), "PERSON"))))

    with caplog.at_level(logging.DEBUG):
        await detector.detect(secret)

    for record in caplog.records:
        assert secret not in record.getMessage()
        assert "\u041a\u043e\u0440\u043e\u043b" not in record.getMessage()


async def test_failure_message_carries_no_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="failed on '\u041a\u043e\u0440\u043e\u043b\u0451\u0432'")

    with pytest.raises(CorpNerUnavailableError) as excinfo:
        await CorpNerDetector(_client(handler)).detect("\u041a\u043e\u0440\u043e\u043b\u0451\u0432")

    assert "\u041a\u043e\u0440\u043e\u043b" not in str(excinfo.value)


def test_finding_is_the_shared_dataclass() -> None:
    assert Finding(text="a", label="PERSON", start=0, end=1, score=1.0).label == "PERSON"


# ---------------------------------------------------------------------------
# Unicode facts the mapper relies on
# ---------------------------------------------------------------------------


def test_no_composition_second_below_the_gate() -> None:
    """_MIN_COMPOSABLE_SECOND is a valid skip gate for the whole Unicode table."""
    seconds = set()
    for cp in range(0x110000):
        decomp = unicodedata.decomposition(chr(cp))
        parts = decomp.split()
        if decomp and not decomp.startswith("<") and len(parts) == 2:
            seconds.add(int(parts[1], 16))
    assert min(seconds) >= _MIN_COMPOSABLE_SECOND
    # Hangul composition is algorithmic, so it is absent from the table above.
    assert min(range(0x1161, 0x1176)) >= _MIN_COMPOSABLE_SECOND
    assert min(range(0x11A8, 0x11C3)) >= _MIN_COMPOSABLE_SECOND


_FUZZ_ALPHABET = [
    "a",
    " ",
    "\n",
    "\u0430",  # CYRILLIC A
    "\u0438",  # CYRILLIC I
    "\u0435",  # CYRILLIC IE
    "\u0439",  # CYRILLIC SHORT I, precomposed
    "\u0451",  # CYRILLIC IO, precomposed
    "\u0306",  # combining breve
    "\u0308",  # combining diaeresis
    "\u0301",  # combining acute
    "\u1100",  # Hangul L
    "\u1161",  # Hangul V
    "\u11a8",  # Hangul T
    "\uac01",  # precomposed Hangul syllable
    "e",
    "\u00e9",  # e-acute precomposed
    "\u304b",  # KA
    "\u3099",  # combining voiced sound mark (ccc 8)
]


@pytest.mark.parametrize("seed", range(60))
async def test_mapped_spans_always_cover_the_reported_nfc_span(seed: int) -> None:
    rng = random.Random(seed)
    text = "".join(rng.choice(_FUZZ_ALPHABET) for _ in range(rng.randint(1, 40)))
    nfc = unicodedata.normalize("NFC", text)
    if not nfc or not text.strip():
        pytest.skip("degenerate sample")
    start = rng.randrange(len(nfc))
    end = rng.randrange(start + 1, len(nfc) + 1)

    findings = await CorpNerDetector(_client(_fixed(_span(start, end, "PERSON")))).detect(text)

    assert len(findings) == 1
    f = findings[0]
    assert f.text == text[f.start : f.end]
    # The redaction must cover at least what the service reported.
    assert nfc[start:end] in unicodedata.normalize("NFC", f.text)

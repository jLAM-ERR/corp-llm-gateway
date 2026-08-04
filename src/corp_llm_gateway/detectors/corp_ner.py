"""Detector backed by the corp NER service (`POST /v1/analyze`).

Distinct from `presidio.py`, which stays a stub: this is a real service, not a
library. Batch-capable (``BatchPIIDetector``) because one round-trip per segment
would serialise against the service timeout.

Fail-closed everywhere: any failure raises ``CorpNerUnavailableError``, which the
existing ``except NerUnavailableError`` handlers on the egress path already turn
into a refusal. Never logs request or response text (M1-14).

Failures are NOT counted here. ``litellm_hook._record_failure`` is the one
request-level choke point for ``gateway_failure{component}``; counting in both
places doubled every failed request in that series.
"""

from __future__ import annotations

import logging
import unicodedata
from bisect import bisect_left, bisect_right
from collections.abc import Iterator

import httpx

from corp_llm_gateway.corp_ner import AnalyzeResult, CorpNerClient, CorpNerUnavailableError
from corp_llm_gateway.detectors.base import Finding, PIIDetector
from corp_llm_gateway.detectors.regex_checksum import _deduplicate

logger = logging.getLogger(__name__)

# The gateway_failure{component} label the hook emits for E_CORP_NER_UNAVAILABLE.
FAILURE_COMPONENT = "corp_ner"

# The nine contract labels. ORGANIZATION -> ORG matches ner_ru/ner_en; the rest
# pass through. Anything absent is dropped.
_LABEL_MAP: dict[str, str] = {
    "PERSON": "PERSON",
    "LOCATION": "LOCATION",
    "ORGANIZATION": "ORG",
    "LOGIN": "LOGIN",
    "PASSWORD": "PASSWORD",
    "AUTH_TOKEN": "AUTH_TOKEN",
    "SECRET_KEY": "SECRET_KEY",
    "CONTRACT_NUMBER": "CONTRACT_NUMBER",
    "SECRET": "SECRET",
}

# Regex spans arrive with score null; they are deterministic rules, so they
# outrank probabilistic NER spans in _deduplicate.
_REGEX_SCORE = 1.0

# No canonical composition has a second element below U+0300, so a character
# under it can never merge into the one before it — the cheap gate that keeps
# ASCII-heavy text off the expensive boundary check. Pinned by
# tests/detectors/test_corp_ner.py::test_no_composition_second_below_the_gate.
_MIN_COMPOSABLE_SECOND = 0x300


# ---------------------------------------------------------------------------
# NFC offset mapping
#
# The service reports offsets into unicodedata.normalize("NFC", text), not into
# the text we were handed. For decomposed Cyrillic (U+0438 + breve, U+0435 +
# diaeresis) the two differ in length, so passing offsets through slices the
# wrong substring and corrupts the M1-9 placeholder bijection.
# ---------------------------------------------------------------------------


def _cluster_end(text: str, start: int) -> int:
    """End of the starter-plus-combining-marks cluster beginning at ``start``."""
    j = start + 1
    n = len(text)
    while j < n and unicodedata.combining(text[j]):
        j += 1
    return j


def _chunks(text: str) -> Iterator[tuple[int, int]]:
    """Yield ``(start, end)`` slices that normalize independently.

    A starter and its marks are one chunk. Some starters still compose with the
    starter before them (Hangul jamo L+V+T, all ccc == 0), so a chunk keeps
    absorbing the next one until the split is provably lossless.
    """
    n = len(text)
    i = 0
    while i < n:
        j = _cluster_end(text, i)
        if j < n and ord(text[j]) >= _MIN_COMPOSABLE_SECOND:
            while j < n:
                k = _cluster_end(text, j)
                head, tail = text[i:j], text[j:k]
                if unicodedata.normalize("NFC", head + tail) == unicodedata.normalize(
                    "NFC", head
                ) + unicodedata.normalize("NFC", tail):
                    break
                j = k
        yield i, j
        i = j


class _OffsetMap:
    """Maps NFC code-point offsets back onto the original string.

    Offsets that land inside a chunk whose NFC form differs snap outward to the
    chunk edges: over-covering a composed character is safe, splitting one is not.
    """

    __slots__ = ("_n_ends", "_n_starts", "_nfc_len", "_orig_len", "_runs")

    def __init__(self, runs: list[tuple[int, int, int, int, bool]], nfc_len: int, orig_len: int):
        self._runs = runs
        self._n_starts = [r[2] for r in runs]
        self._n_ends = [r[3] for r in runs]
        self._nfc_len = nfc_len
        self._orig_len = orig_len

    def start(self, pos: int) -> int:
        if pos >= self._nfc_len:
            return self._orig_len
        o_start, _, n_start, _, identity = self._runs[bisect_right(self._n_starts, pos) - 1]
        return o_start + (pos - n_start) if identity else o_start

    def end(self, pos: int) -> int:
        if pos <= 0:
            return 0
        if pos >= self._nfc_len:
            return self._orig_len
        o_start, o_end, n_start, _, identity = self._runs[bisect_left(self._n_ends, pos)]
        return o_start + (pos - n_start) if identity else o_end


def _build_map(text: str, nfc: str) -> _OffsetMap:
    runs: list[tuple[int, int, int, int, bool]] = []
    pieces: list[str] = []
    n_pos = 0
    for o_start, o_end in _chunks(text):
        chunk = text[o_start:o_end]
        piece = unicodedata.normalize("NFC", chunk)
        pieces.append(piece)
        n_end = n_pos + len(piece)
        identity = piece == chunk
        if identity and runs and runs[-1][4]:
            prev = runs[-1]
            runs[-1] = (prev[0], o_end, prev[2], n_end, True)
        else:
            runs.append((o_start, o_end, n_pos, n_end, identity))
        n_pos = n_end
    if "".join(pieces) != nfc:
        # Backstop: if chunking ever lost a composition the offsets are unusable,
        # and an unusable offset must not become a wrong redaction.
        raise CorpNerUnavailableError("corp-ner offsets are not mappable onto the source text")
    return _OffsetMap(runs, len(nfc), len(text))


class CorpNerDetector(PIIDetector):
    def __init__(self, client: CorpNerClient) -> None:
        self._client = client

    async def detect(self, text: str) -> list[Finding]:
        return (await self.detect_batch([text]))[0]

    async def detect_batch(self, texts: list[str]) -> list[list[Finding]]:
        out: list[list[Finding]] = [[] for _ in texts]
        indices = [i for i, t in enumerate(texts) if t.strip()]
        if not indices:
            return out
        try:
            results = await self._analyze([texts[i] for i in indices])
            for i, result in zip(indices, results, strict=True):
                out[i] = _findings(texts[i], result)
        except httpx.HTTPError as exc:
            raise CorpNerUnavailableError(
                f"corp-ner transport error: {type(exc).__name__}"
            ) from exc
        logger.debug("corp_ner_detect texts=%d findings=%d", len(texts), sum(len(f) for f in out))
        return out

    async def _analyze(self, texts: list[str]) -> list[AnalyzeResult]:
        results = await self._client.analyze(texts)
        if len(results) != len(texts):
            raise CorpNerUnavailableError(
                f"corp-ner returned {len(results)} results for {len(texts)} texts"
            )
        return results


def _findings(text: str, result: AnalyzeResult) -> list[Finding]:
    if result.truncated:
        # Part of the text went unscanned. "Not scanned" must never read as "clean".
        raise CorpNerUnavailableError(f"corp-ner truncated a text of {len(text)} chars")
    if not result.spans:
        return []

    nfc = unicodedata.normalize("NFC", text)
    offsets = None if nfc == text else _build_map(text, nfc)

    raw: list[Finding] = []
    for span in result.spans:
        label = _LABEL_MAP.get(span.label)
        if label is None:
            continue
        if span.end > len(nfc):
            # The service scanned something other than what we sent; mapping the
            # offset would redact the wrong bytes.
            raise CorpNerUnavailableError(
                f"corp-ner returned a span past the end of the text: "
                f"end={span.end} nfc_len={len(nfc)}"
            )
        if offsets is None:
            start, end = span.start, span.end
        else:
            start, end = offsets.start(span.start), offsets.end(span.end)
        if start >= end:
            continue
        raw.append(
            Finding(
                text=text[start:end],
                label=label,
                start=start,
                end=end,
                score=_REGEX_SCORE if span.score is None else span.score,
            )
        )
    return _deduplicate(raw)

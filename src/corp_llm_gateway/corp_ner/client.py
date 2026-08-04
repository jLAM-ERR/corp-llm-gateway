"""Corp-NER HTTP client.

Speaks ``POST /v1/analyze`` (spans) only. ``/v1/mask`` is deliberately unused:
it returns ``[REDACTED:PERSON]``, so two different people collapse onto the same
token — not bijective, which would break ``post_call`` desanitization (M1-9).

Thin like ``corp_llm/client.py``: no retries, no fallbacks. The guardrail layer
owns the failure policy; every failure here is fail-closed by type
(``CorpNerUnavailableError``).

Never logs or embeds request/response text — counts, labels, status codes and
exception types only (M1-14).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from corp_llm_gateway.corp_ner.errors import CorpNerUnavailableError

logger = logging.getLogger(__name__)

ANALYZE_PATH = "/v1/analyze"

# Service-side limits (see ADR-004; connect-corp-ner.pdf stays untracked).
MAX_TEXTS = 256
MAX_INPUT_CHARS = 200_000
MAX_BODY_BYTES = 64 * 1024 * 1024
# The service declares CLIENT_TIMEOUT_S=60; we sit deliberately under it.
DEFAULT_TIMEOUT_S = 30.0

# Eight model classes plus SECRET, which only ever arrives from the regex half.
KNOWN_LABELS = frozenset(
    {
        "PERSON",
        "LOCATION",
        "ORGANIZATION",
        "LOGIN",
        "PASSWORD",
        "AUTH_TOKEN",
        "SECRET_KEY",
        "CONTRACT_NUMBER",
        "SECRET",
    }
)

_ENVELOPE_BYTES = len(b'{"texts":[]}')
_SEPARATORS = (",", ":")
_UNPARSEABLE = object()


@dataclass(frozen=True)
class Span:
    """One detected span. Offsets are code-point indices into the NFC form of
    the submitted text — the caller maps them back (B2)."""

    start: int
    end: int
    label: str
    score: float | None = None
    source: str = "ner"


@dataclass(frozen=True)
class AnalyzeResult:
    spans: tuple[Span, ...]
    truncated: bool


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=_SEPARATORS).encode("utf-8"))


class CorpNerClient:
    def __init__(
        self,
        base_url: str,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        verify: bool | str = True,
        max_texts: int = MAX_TEXTS,
        max_input_chars: int = MAX_INPUT_CHARS,
        max_body_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_texts = max_texts
        self._max_input_chars = max_input_chars
        self._max_body_bytes = max_body_bytes
        # Same ownership rule as CorpLlmClient: an injected client's lifecycle
        # (and TLS config) belongs to the caller.
        self._http = http or httpx.AsyncClient(timeout=timeout, verify=verify)
        self._owned_http = http is None

    async def analyze(
        self, texts: list[str], *, request_id: str | None = None
    ) -> list[AnalyzeResult]:
        """Analyze ``texts``, returning one result per input text, in order."""
        if not texts:
            return []
        rid = request_id or uuid.uuid4().hex
        results: list[AnalyzeResult] = []
        for batch in self._batches(texts):
            results.extend(await self._analyze_batch(batch, rid))
        return results

    def _batches(self, texts: list[str]) -> Iterator[list[str]]:
        batch: list[str] = []
        chars = 0
        body = _ENVELOPE_BYTES
        for text in texts:
            size = _json_bytes(text)
            # A text that cannot fit alone would be silently truncated by the
            # service — that is content going unscanned, so fail closed.
            if len(text) > self._max_input_chars:
                raise CorpNerUnavailableError(
                    f"text of {len(text)} chars exceeds max_input_chars={self._max_input_chars}"
                )
            if _ENVELOPE_BYTES + size > self._max_body_bytes:
                raise CorpNerUnavailableError(
                    f"text of {size} encoded bytes exceeds max_body_bytes={self._max_body_bytes}"
                )
            if batch and (
                len(batch) + 1 > self._max_texts
                or chars + len(text) > self._max_input_chars
                or body + 1 + size > self._max_body_bytes
            ):
                yield batch
                batch, chars, body = [], 0, _ENVELOPE_BYTES
            body += size + (1 if batch else 0)
            chars += len(text)
            batch.append(text)
        if batch:
            yield batch

    async def _analyze_batch(self, batch: list[str], request_id: str) -> list[AnalyzeResult]:
        # Serialized here (not via ``json=``) so the byte budget in _batches
        # matches what actually goes on the wire.
        content = json.dumps({"texts": batch}, ensure_ascii=False, separators=_SEPARATORS).encode(
            "utf-8"
        )
        logger.debug(
            "corp_ner_analyze request_id=%s texts=%d body_bytes=%d",
            request_id,
            len(batch),
            len(content),
        )
        try:
            resp = await self._http.post(
                f"{self._base_url}{ANALYZE_PATH}",
                content=content,
                headers={"Content-Type": "application/json", "X-Request-Id": request_id},
            )
        except httpx.HTTPError as exc:
            # httpx timeouts stringify to '' — name the type or the log line
            # is undiagnosable.
            raise CorpNerUnavailableError(
                f"corp-ner transport error: {type(exc).__name__}: {exc}"
            ) from exc

        if resp.status_code >= 400:
            # Never embed resp.text: an error body may echo the RAW text back
            # and would then ride the exception chain into logs/audit.
            raise CorpNerUnavailableError(f"corp-ner returned {resp.status_code}")

        try:
            raw: Any = resp.json()
        except ValueError:
            raw = _UNPARSEABLE
        # Raised outside the except block so no decoder message (which quotes
        # the body) is attached as __context__.
        if raw is _UNPARSEABLE:
            raise CorpNerUnavailableError("corp-ner returned a malformed analyze response")
        return _parse_results(raw, len(batch))

    async def aclose(self) -> None:
        if self._owned_http:
            await self._http.aclose()


def _parse_results(raw: Any, expected: int) -> list[AnalyzeResult]:
    items = raw.get("results") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise CorpNerUnavailableError("corp-ner returned a malformed analyze response")
    if len(items) != expected:
        raise CorpNerUnavailableError(
            f"corp-ner returned a mismatched result count: expected {expected}, got {len(items)}"
        )
    return [_parse_result(item) for item in items]


def _parse_result(item: Any) -> AnalyzeResult:
    if not isinstance(item, dict):
        raise CorpNerUnavailableError("corp-ner returned a malformed analyze result")
    raw_spans = item.get("spans") or []
    if not isinstance(raw_spans, list):
        raise CorpNerUnavailableError("corp-ner returned a malformed analyze result")
    return AnalyzeResult(
        spans=tuple(_parse_span(s) for s in raw_spans),
        truncated=bool(item.get("truncated", False)),
    )


def _parse_span(raw: Any) -> Span:
    if not isinstance(raw, dict):
        raise CorpNerUnavailableError("corp-ner returned a malformed span")
    start, end, label = raw.get("start"), raw.get("end"), raw.get("label")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not isinstance(label, str)
        or start < 0
        or end < start
    ):
        raise CorpNerUnavailableError("corp-ner returned a malformed span")
    score = raw.get("score")
    if score is not None and not isinstance(score, int | float):
        raise CorpNerUnavailableError("corp-ner returned a malformed span")
    source = raw.get("source")
    return Span(
        start=start,
        end=end,
        label=label,
        score=None if score is None else float(score),
        source=source if isinstance(source, str) else "ner",
    )

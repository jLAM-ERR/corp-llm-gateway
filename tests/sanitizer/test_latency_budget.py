"""Tests for DP-9: thread-offloaded local NER + latency budget.

NER concurrency tests skip automatically when natasha/spaCy are absent (Python 3.14).
The regex floor test runs everywhere.

Oracle off-hot-path coverage:
    tests/sanitizer/test_oracle_trigger.py::test_gazetteer_nohit_oracle_not_called
    already proves that a no-gazetteer-hit request does not call corp_llm.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from corp_llm_gateway.detectors.regex_checksum import RegexChecksumDetector
from corp_llm_gateway.sanitizer.local_pass import LocalDetectionPass

# ---------------------------------------------------------------------------
# Floor sanity — runs on 3.12 and 3.14 (no NER deps)
# ---------------------------------------------------------------------------

_REGEX_TEXT = "Send invoice to bob@example.com, IP 192.168.1.1, phone +79161234567"


async def test_regex_only_pass_is_fast() -> None:
    """LocalDetectionPass with only regex completes well under 50 ms."""
    lp = LocalDetectionPass([RegexChecksumDetector()])
    # warm up (pattern compile is cached but first call may allocate)
    await lp.findings(_REGEX_TEXT)
    t0 = time.monotonic()
    findings = await lp.findings(_REGEX_TEXT)
    elapsed_ms = (time.monotonic() - t0) * 1000
    assert elapsed_ms < 50, f"regex-only pass took {elapsed_ms:.1f}ms, expected <50ms"
    assert any(f.label == "EMAIL" for f in findings)
    assert any(f.label == "IP_ADDRESS" for f in findings)


# ---------------------------------------------------------------------------
# NER concurrency tests — skipped when natasha / spaCy not installed
# ---------------------------------------------------------------------------

# Mixed RU/EN input; exercises both engines simultaneously.
_MIXED_TEXT = (
    "Директор Сбербанка Герман Греф met John Smith at Acme Corp in Moscow."
    " contact: john@example.com, 10.0.0.5"
)

_K = 8  # concurrent callers


async def test_concurrent_ner_correct() -> None:
    """K concurrent DualNerDetector.detect() calls return identical results."""
    pytest.importorskip("natasha")
    pytest.importorskip("spacy")
    from corp_llm_gateway.detectors.dual_ner import DualNerDetector

    det = DualNerDetector()
    # warm up so model load doesn't skew the measurement
    serial = await det.detect(_MIXED_TEXT)
    results = await asyncio.gather(*[det.detect(_MIXED_TEXT) for _ in range(_K)])
    serial_texts = sorted(f.text for f in serial)
    for i, r in enumerate(results):
        concurrent_texts = sorted(f.text for f in r)
        assert concurrent_texts == serial_texts, (
            f"concurrent call {i} differs from serial:\n"
            f"  serial:     {serial_texts}\n"
            f"  concurrent: {concurrent_texts}"
        )


async def test_concurrent_ner_within_budget() -> None:
    """K=8 concurrent NER calls on short mixed input complete within 2 s wall time."""
    pytest.importorskip("natasha")
    pytest.importorskip("spacy")
    from corp_llm_gateway.detectors.dual_ner import DualNerDetector

    det = DualNerDetector()
    # warm up — model load must not count against the budget
    await det.detect(_MIXED_TEXT)
    t0 = time.monotonic()
    await asyncio.gather(*[det.detect(_MIXED_TEXT) for _ in range(_K)])
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"K={_K} concurrent NER calls took {elapsed:.2f}s, expected <2s"

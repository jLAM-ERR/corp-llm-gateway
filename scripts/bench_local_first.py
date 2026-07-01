"""Local-first detection benchmark: regex + dual-NER + gazetteer.

Builds the local detector stack, runs each sample N times, and prints p50/p95
per-call latency. Degrades gracefully on interpreters without NER deps (3.14):
falls back to regex-only and notes which engines are active.

Run with:
    PYTHONPATH=src .venv-bench/bin/python scripts/bench_local_first.py
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time

SAMPLES: dict[str, str] = {
    "ru_pii": (
        "Директор Сбербанка Герман Греф подписал документ."
        " ИНН 7707083893. Адрес: Москва, ул. Вавилова, д.19."
    ),
    "en_pii": (
        "John Smith at Acme Corp sent invoice to john@example.com"
        " from 192.168.1.1. Account: GB29NWBK60161331926819."
    ),
    "mixed": (
        "// owner: Анна Кузнецова — see AcmeService for John Smith\n"
        "контакт: anna@corp.lan, тел. +79161234567. Project Polaris."
    ),
    "code": (
        "const API_KEY = 'sk-abc123def456ghi789';\n"
        "const DB_HOST = 'db.prod.corp.lan';\n"
        "# password=s3cr3tP@ss\n"
    ),
}

N_WARMUP = 3
N_BENCH = 20


async def bench_one(lp: object, text: str) -> list[float]:
    for _ in range(N_WARMUP):
        await lp.findings(text)  # type: ignore[attr-defined]
    times: list[float] = []
    for _ in range(N_BENCH):
        t0 = time.monotonic()
        await lp.findings(text)  # type: ignore[attr-defined]
        times.append((time.monotonic() - t0) * 1000)
    return times


def p50(times: list[float]) -> float:
    return statistics.median(times)


def p95(times: list[float]) -> float:
    s = sorted(times)
    idx = max(0, int(len(s) * 0.95) - 1)
    return s[idx]


async def main() -> None:
    from corp_llm_gateway.detectors.regex_checksum import RegexChecksumDetector
    from corp_llm_gateway.sanitizer.local_pass import LocalDetectionPass

    detectors = [RegexChecksumDetector()]
    active_engines = ["regex"]

    try:
        from corp_llm_gateway.detectors.dual_ner import DualNerDetector

        ner = DualNerDetector()
        # probe to check models actually load
        await ner.detect("test")
        detectors.append(ner)
        active_engines.append("dual-NER(RU+EN)")
    except Exception as exc:
        print(f"[warn] dual-NER unavailable ({exc}); regex-only mode", file=sys.stderr)

    lp = LocalDetectionPass(detectors)

    print(f"Python  : {sys.version.split()[0]}")
    print(f"Engines : {' + '.join(active_engines)}")
    print(f"Warmup  : {N_WARMUP}  Bench: {N_BENCH} per sample")
    print()
    print(f"{'Sample':<20} {'p50 ms':>8} {'p95 ms':>8}  findings")
    print("-" * 50)

    for name, text in SAMPLES.items():
        times = await bench_one(lp, text)
        findings = await lp.findings(text)
        print(f"{name:<20} {p50(times):>8.1f} {p95(times):>8.1f}  {len(findings)} findings")


if __name__ == "__main__":
    asyncio.run(main())

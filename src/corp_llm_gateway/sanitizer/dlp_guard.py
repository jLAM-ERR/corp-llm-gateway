"""DLP egress guard (Stage 5) — defence-in-depth re-scan of the sanitized request.

Catches canary tokens and high-confidence raw secrets that survived the
primary sanitizer (a sanitizer miss). Scans the OUTBOUND sanitized request,
never the post_call desanitized response (which legitimately holds originals).

Block reason codes:
  "dlp:canary"       — an ops-seeded sentinel survived sanitization.
  "dlp:secret_leak"  — a high-confidence raw secret survived sanitization.
"""

from __future__ import annotations

import re

# High-confidence secret patterns that MUST NOT appear in a correctly
# sanitized outbound payload.  Score ≥ 0.95 in RegexChecksumDetector:
# PEM private keys, JWT, OpenAI / AWS / GitHub API keys.
# A correctly sanitized payload replaces these with [LABEL_NNN] placeholders,
# which will NOT match these patterns.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"-----BEGIN (?:[\w]+ )?PRIVATE KEY-----[\s\S]{1,8192}?-----END (?:[\w]+ )?PRIVATE KEY-----"
    ),
    re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
)


class DlpEgressGuard:
    """Re-scan the sanitized outbound request for canary hits and raw secrets."""

    def __init__(
        self,
        canary_patterns: list[str] | None = None,
        *,
        secret_rescan: bool = True,
    ) -> None:
        self._secret_rescan = secret_rescan
        self._canary_pats: tuple[re.Pattern[str], ...] = (
            tuple(re.compile(p) for p in canary_patterns) if canary_patterns else ()
        )

    def scan(self, text: str) -> str | None:
        """Return a block_reason if *text* holds a canary or raw secret; else None.

        Raw content is never embedded in the returned reason string.
        """
        for pat in self._canary_pats:
            if pat.search(text):
                return "dlp:canary"
        if self._secret_rescan:
            for pat in _SECRET_PATTERNS:
                if pat.search(text):
                    return "dlp:secret_leak"
        return None

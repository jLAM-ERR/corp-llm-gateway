"""Test-data allowlist: known synthetic ПДн passes through un-redacted.

A pair is dropped iff its original is allowlisted AND the placeholder label
is NOT in SECRET_LABELS — so an allowlisted value can never suppress a secret.
Empty / unset config → no-op allowlist.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from corp_llm_gateway import config

_LABEL_RE = re.compile(r"^\[([A-Z_]+)_\d+\]$")

SECRET_LABELS: frozenset[str] = frozenset(
    {
        "API_KEY",
        "TOKEN",
        "PASSWORD",
        "JWT",
        "PEM_PRIVATE_KEY",
        "SECRET",
    }
)


class Allowlist:
    def __init__(self, originals: Iterable[str]) -> None:
        self._allowed: frozenset[str] = frozenset(originals)

    def filter_pairs(
        self,
        pairs: tuple[tuple[str, str], ...],
    ) -> tuple[tuple[str, str], ...]:
        """Return pairs minus any that are allowlisted non-secrets."""
        if not self._allowed:
            return pairs
        kept: list[tuple[str, str]] = []
        for original, placeholder in pairs:
            if original in self._allowed:
                m = _LABEL_RE.match(placeholder)
                # Unparseable placeholder → treat label as non-secret → drop pair.
                label = m.group(1) if m else None
                if label not in SECRET_LABELS:
                    continue  # allowlisted + non-secret: pass through un-redacted
            kept.append((original, placeholder))
        return tuple(kept)

    @classmethod
    def from_config(cls) -> Allowlist:
        """Build from CORP_LLM_TESTDATA_ALLOWLIST and CORP_LLM_TESTDATA_ALLOWLIST_FILE."""
        originals: list[str] = []
        inline = config.get("CORP_LLM_TESTDATA_ALLOWLIST", "")
        if inline:
            originals.extend(v.strip() for v in inline.split(",") if v.strip())
        file_path = config.get("CORP_LLM_TESTDATA_ALLOWLIST_FILE", "")
        if file_path:
            path = Path(file_path).expanduser()
            if path.is_file():
                for line in path.read_text(encoding="utf-8").splitlines():
                    stripped = line.strip()
                    if stripped:
                        originals.append(stripped)
        return cls(originals)

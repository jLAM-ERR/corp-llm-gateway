"""Deterministic regex + checksum PII/secret detector.

Pure stdlib (re, ipaddress). No third-party deps. All patterns compiled once
at class initialisation. Checksum validators drive near-zero false-positive
rates for structured Russian requisites (ИНН, ОГРН, СНИЛС) and infra/secrets.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from typing import NamedTuple

from corp_llm_gateway.detectors.base import Finding, PIIDetector

# ---------------------------------------------------------------------------
# Checksum / structural validators  (True = accept the candidate)
# ---------------------------------------------------------------------------


def _inn10_ok(s: str) -> bool:
    """ИНН 10-digit (legal entity) control-digit check."""
    d = [int(c) for c in s]
    w = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    return sum(w[i] * d[i] for i in range(9)) % 11 % 10 == d[9]


def _inn12_ok(s: str) -> bool:
    """ИНН 12-digit (individual) two-digit control check."""
    d = [int(c) for c in s]
    w1 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
    w2 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
    n1 = sum(w1[i] * d[i] for i in range(10)) % 11 % 10
    n2 = sum(w2[i] * d[i] for i in range(11)) % 11 % 10
    return d[10] == n1 and d[11] == n2


def _inn_ok(s: str) -> bool:
    if not s.isdigit():
        return False
    if len(s) == 10:
        return _inn10_ok(s)
    if len(s) == 12:
        return _inn12_ok(s)
    return False


def _ogrn13_ok(s: str) -> bool:
    return s.isdigit() and int(s[:12]) % 11 % 10 == int(s[12])


def _ogrn15_ok(s: str) -> bool:
    return s.isdigit() and int(s[:14]) % 13 % 10 == int(s[14])


def _ogrn_ok(s: str) -> bool:
    if len(s) == 13:
        return _ogrn13_ok(s)
    if len(s) == 15:
        return _ogrn15_ok(s)
    return False


def _snils_ok(s: str) -> bool:
    """СНИЛС 11-digit control-sum check."""
    if not s.isdigit() or len(s) != 11:
        return False
    if int(s[:9]) <= 1_001_998:
        # Legacy/test serial range: control digits must be "00"
        return int(s[9:11]) == 0
    d = [int(c) for c in s]
    total = sum(d[i] * (9 - i) for i in range(9))
    if total < 100:
        check = total
    elif total in (100, 101):
        check = 0
    else:
        check = total % 101
        if check in (100, 101):
            check = 0
    return int(s[9:11]) == check


def _bik_ok(s: str) -> bool:
    """БИК structural: 9 digits, starts with 04, bank-code part ≠ 000."""
    return s.isdigit() and s.startswith("04") and s[-3:] != "000"


def _kpp_ok(s: str) -> bool:
    """КПП structural: 4 digits + 2 alphanumeric + 3 digits, serial ≠ 000."""
    return (
        len(s) == 9 and s[:4].isdigit() and s[4:6].isalnum() and s[6:].isdigit() and s[6:] != "000"
    )


def _ipv4_ok(s: str) -> bool:
    try:
        ipaddress.IPv4Address(s)
        return True
    except ValueError:
        return False


def _ipv6_ok(s: str) -> bool:
    try:
        # Strip optional zone ID (e.g. fe80::1%eth0)
        ipaddress.IPv6Address(s.split("%")[0])
        return True
    except ValueError:
        return False


def _cidr_ok(s: str) -> bool:
    try:
        ipaddress.ip_network(s, strict=False)
        return True
    except ValueError:
        return False


def _true(_s: str) -> bool:
    return True


# ---------------------------------------------------------------------------
# Bank account control-key check (requires last 3 digits of the BIK)
# ---------------------------------------------------------------------------

_ACCT_WEIGHTS = (7, 1, 3, 7, 1, 3, 7, 1, 3, 7, 1, 3, 7, 1, 3, 7, 1, 3, 7, 1, 3, 7, 1)


def _bank_acct_key_ok(account20: str, bik9: str) -> bool:
    """Russian bank account control-key check (requires BIK)."""
    combined = bik9[-3:] + account20
    if len(combined) != 23 or not combined.isdigit():
        return False
    return sum(_ACCT_WEIGHTS[i] * int(combined[i]) for i in range(23)) % 10 == 0


# ---------------------------------------------------------------------------
# Compiled regex patterns (module-level; safe to share across instances)
# ---------------------------------------------------------------------------

# ИНН: 10 or 12 decimal digits, word-bounded
_INN_PAT = re.compile(r"\b(\d{10}|\d{12})\b")

# КПП: 4 digits + 2 alphanumeric (uppercase) + 3 digits
_KPP_PAT = re.compile(r"\b(\d{4}[0-9A-Z]{2}\d{3})\b")

# ОГРН (13) / ОГРНИП (15) — first digit 1-9
_OGRN_PAT = re.compile(r"\b([1-9]\d{12}|[1-9]\d{14})\b")

# СНИЛС: keyword-preceded 11 digits or formatted NNN-NNN-NNN NN
_SNILS_KW_PAT = re.compile(r"СНИЛС\s+(\d{11})")
_SNILS_FMT_PAT = re.compile(r"(\d{3})-(\d{3})-(\d{3})\s+(\d{2})")

# БИК: 9 digits starting with 04
_BIK_PAT = re.compile(r"\b(04\d{7})\b")

# Bank account — keyword context: abbreviated bank-account forms in Russian
_ACCT_KW_PAT = re.compile(
    r"(?:р/?с(?:чёт|чет|ч)?[.\s]*№?\s*|"  # noqa: RUF001
    r"(?:расчётный|расчетный)\s+счёт?\s*)(\d{20})",
    re.IGNORECASE,
)
# Bank account — bare 20-digit with common Russian account-prefix digits
_ACCT_BARE_PAT = re.compile(r"\b((?:30|40|42|43|45)\d{18})\b")

# IPv4 — strict-octet validated below
_IPV4_PAT = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")

# CIDR — validated by ipaddress
_CIDR_PAT = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2})\b")

# IPv6: full 8-group form and common compressed forms; validated by ipaddress
_IPV6_PAT = re.compile(
    r"(?<![0-9A-Fa-f:])"
    r"("
    r"[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){7}"  # full 8-group
    r"|(?:[0-9A-Fa-f]{1,4}:){1,7}:"  # trailing ::
    r"|::(?:[0-9A-Fa-f]{1,4}:){0,5}[0-9A-Fa-f]{1,4}"  # leading ::x
    r"|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}"  # middle ::
    r"|::"  # loopback / all-zeros
    r")"
    r"(?![0-9A-Fa-f:])"
)

# Internal hostnames
_HOSTNAME_PAT = re.compile(
    r"\b([\w][\w.\-]*\.(?:corp\.internal|corp\.lan|local|lan))\b",
    re.IGNORECASE,
)

# DB URLs with optional embedded credentials
_DB_URL_PAT = re.compile(
    r"((?:postgres(?:ql)?|mysql|mongodb|redis)://[^\s\"'<>]{3,})",
    re.IGNORECASE,
)

# JWT: base64url header always decodes to '{' → starts with eyJ
_JWT_PAT = re.compile(r"\b(eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*)\b")

# Bearer token value in body text (NOT the BYOK Authorization header —
# the dev's header is forwarded untouched; here we detect the value if it
# appears embedded in body content, e.g. copied into a config or log snippet)
_BEARER_PAT = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9\-._~+/]{20,}={0,2})")

# PEM private key blocks
_PEM_PAT = re.compile(
    r"(-----BEGIN (?:[\w]+ )?PRIVATE KEY-----[\s\S]{1,8192}?-----END (?:[\w]+ )?PRIVATE KEY-----)"
)

# Named API key patterns (distinctive prefixes → score 1.0)
_OPENAI_KEY_PAT = re.compile(r"\b(sk-[A-Za-z0-9]{32,})\b")
_AWS_KEY_PAT = re.compile(r"\b(AKIA[A-Z0-9]{16})\b")
_GH_TOKEN_PAT = re.compile(r"\b(ghp_[A-Za-z0-9]{36})\b")

# Generic secret: key/token keyword followed by base64-ish value
_GENERIC_SECRET_PAT = re.compile(
    r"(?i)(?:token|secret|api[_\-]?key|access[_\-]?key|private[_\-]?key)\s*[=:]\s*[\"']?"
    r"([A-Za-z0-9+/\-_]{20,}={0,2})"
)

# Password literals
_PASSWORD_PAT = re.compile(r"(?i)(?:password|passwd|pwd|pass)\s*[=:]\s*[\"']?([^\s\"',;]{4,})")

# Email
_EMAIL_PAT = re.compile(r"\b([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})\b")

# Phone — Russian (+7 / 8) and international E.164
# [\s\-]?\(?\d{3}\)? handles both "+7(900)" and "+7 (900)" and "+79001234567"
_PHONE_RU_PAT = re.compile(
    r"(?<!\d)((?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2})(?!\d)"
)
_PHONE_INTL_PAT = re.compile(r"(?<!\d)(\+[2-9]\d{6,14})(?!\d)")

# ---------------------------------------------------------------------------
# Rule table
# ---------------------------------------------------------------------------


class _Rule(NamedTuple):
    label: str
    pattern: re.Pattern[str]
    validator: Callable[[str], bool]
    score: float
    group: int = 1  # capture-group index holding the sensitive value


# Order: specific / high-confidence patterns first to win deduplication ties.
_RULES: tuple[_Rule, ...] = (
    _Rule("PEM_PRIVATE_KEY", _PEM_PAT, _true, 1.0),
    _Rule("DB_URL", _DB_URL_PAT, _true, 1.0, 0),
    _Rule("JWT", _JWT_PAT, _true, 0.95),
    _Rule("API_KEY", _OPENAI_KEY_PAT, _true, 1.0),
    _Rule("API_KEY", _AWS_KEY_PAT, _true, 1.0),
    _Rule("API_KEY", _GH_TOKEN_PAT, _true, 1.0),
    _Rule("API_KEY", _GENERIC_SECRET_PAT, _true, 0.85),
    _Rule("TOKEN", _BEARER_PAT, _true, 0.9),
    _Rule("PASSWORD", _PASSWORD_PAT, _true, 0.9),
    _Rule("CIDR", _CIDR_PAT, _cidr_ok, 1.0),
    _Rule("IP_ADDRESS", _IPV4_PAT, _ipv4_ok, 1.0),
    _Rule("IP_ADDRESS", _IPV6_PAT, _ipv6_ok, 0.9),
    _Rule("HOSTNAME", _HOSTNAME_PAT, _true, 0.7),
    _Rule("OGRN", _OGRN_PAT, _ogrn_ok, 1.0),
    _Rule("RU_INN", _INN_PAT, _inn_ok, 1.0),
    _Rule("SNILS", _SNILS_KW_PAT, _snils_ok, 1.0),
    # BIK (starts with 04) is more structurally specific than KPP → higher score wins dedup
    _Rule("BIK", _BIK_PAT, _bik_ok, 0.95),
    _Rule("KPP", _KPP_PAT, _kpp_ok, 0.9),
    _Rule("EMAIL", _EMAIL_PAT, _true, 0.95),
    _Rule("PHONE_NUMBER", _PHONE_RU_PAT, _true, 0.9),
    _Rule("PHONE_NUMBER", _PHONE_INTL_PAT, _true, 0.85),
)

# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def _overlaps(a: Finding, b: Finding) -> bool:
    return a.start < b.end and b.start < a.end


def _deduplicate(findings: list[Finding]) -> list[Finding]:
    """Remove overlapping findings; keep highest-score then longest span."""
    by_prio = sorted(findings, key=lambda f: (-f.score, -(f.end - f.start), f.start))
    kept: list[Finding] = []
    for f in by_prio:
        if not any(_overlaps(f, k) for k in kept):
            kept.append(f)
    return sorted(kept, key=lambda f: f.start)


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


class RegexChecksumDetector(PIIDetector):
    """Floor-layer detector: regex + checksum, deterministic, stdlib-only."""

    async def detect(self, text: str) -> list[Finding]:
        raw: list[Finding] = []

        # Standard rule table
        for rule in _RULES:
            for m in rule.pattern.finditer(text):
                val = m.group(rule.group)
                if val is not None and rule.validator(val):
                    start = m.start(rule.group)
                    end = m.end(rule.group)
                    raw.append(
                        Finding(text=val, label=rule.label, start=start, end=end, score=rule.score)
                    )

        # СНИЛС formatted pattern: NNN-NNN-NNN NN (4 capture groups)
        for m in _SNILS_FMT_PAT.finditer(text):
            s = m.group(1) + m.group(2) + m.group(3) + m.group(4)
            if _snils_ok(s):
                raw.append(
                    Finding(text=m.group(0), label="SNILS", start=m.start(), end=m.end(), score=1.0)
                )

        # Bank accounts: collect BIK positions first, then score accounts
        biks = [(m.group(1), m.start(1), m.end(1)) for m in _BIK_PAT.finditer(text)]
        for pat in (_ACCT_KW_PAT, _ACCT_BARE_PAT):
            for m in pat.finditer(text):
                acct = m.group(1)
                a_start, a_end = m.start(1), m.end(1)
                score = 0.7
                for bv, bs, be in biks:
                    if min(abs(a_start - be), abs(bs - a_end)) < 300 and _bank_acct_key_ok(
                        acct, bv
                    ):
                        score = 1.0
                        break
                raw.append(
                    Finding(text=acct, label="BANK_ACCOUNT", start=a_start, end=a_end, score=score)
                )

        return _deduplicate(raw)

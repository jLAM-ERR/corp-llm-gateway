"""Tests for RegexChecksumDetector.

Valid test values are checksum-verified in comments so the math is auditable.
Each category has: one valid example (detected) + one invalid (not detected).
"""

from __future__ import annotations

import pytest

from corp_llm_gateway.detectors import Finding, RegexChecksumDetector

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def labels(findings: list[Finding]) -> list[str]:
    return [f.label for f in findings]


def texts(findings: list[Finding]) -> list[str]:
    return [f.text for f in findings]


def has_label(findings: list[Finding], label: str) -> bool:
    return any(f.label == label for f in findings)


def get_label(findings: list[Finding], label: str) -> Finding | None:
    return next((f for f in findings if f.label == label), None)


@pytest.fixture
def det() -> RegexChecksumDetector:
    return RegexChecksumDetector()


# ---------------------------------------------------------------------------
# ИНН (RU_INN)
# ---------------------------------------------------------------------------
# ИНН 7707083893 (Сбербанк, 10-digit):
#   weights=[2,4,10,3,5,9,4,6,8], d=[7,7,0,7,0,8,3,8,9,3]
#   sum=14+28+0+21+0+72+12+48+72=267, 267%11=3, 3%10=3 == d[9]=3 ✓
# ИНН 500100732259 (12-digit individual):
#   w1=[7,2,4,10,3,5,9,4,6,8]: 35+0+0+10+0+0+63+12+12+16=148, 148%11=5, d[10]=5 ✓
#   w2=[3,7,2,4,10,3,5,9,4,6,8]: 15+0+0+4+0+0+35+27+8+12+40=141, 141%11=9, d[11]=9 ✓


async def test_inn10_valid_detected(det: RegexChecksumDetector) -> None:
    text = "ИНН 7707083893"
    findings = await det.detect(text)
    f = get_label(findings, "RU_INN")
    assert f is not None
    assert f.text == "7707083893"
    assert f.score == 1.0


async def test_inn10_wrong_checksum_not_detected(det: RegexChecksumDetector) -> None:
    # Last digit changed 3→1 → checksum fails
    findings = await det.detect("ИНН 7707083891")
    assert not has_label(findings, "RU_INN")


async def test_inn12_valid_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("ИНН 500100732259")
    f = get_label(findings, "RU_INN")
    assert f is not None
    assert f.text == "500100732259"
    assert f.score == 1.0


async def test_inn12_wrong_checksum_not_detected(det: RegexChecksumDetector) -> None:
    # 500100732250 — last digit 9→0, second check digit fails
    findings = await det.detect("ИНН 500100732250")
    assert not has_label(findings, "RU_INN")


async def test_inn_span_matches_number(det: RegexChecksumDetector) -> None:
    text = "prefix 7707083893 suffix"
    findings = await det.detect(text)
    f = get_label(findings, "RU_INN")
    assert f is not None
    assert text[f.start : f.end] == "7707083893"


# ---------------------------------------------------------------------------
# ОГРН / ОГРНИП (OGRN)
# ---------------------------------------------------------------------------
# ОГРН 1027700132195 (Сбербанк, 13-digit):
#   int("102770013219") % 11 = 5, 5%10=5 == d[12]=5 ✓
# ОГРНИП 304010000000017 (15-digit):
#   int("30401000000001") % 13 = 7, 7%10=7 == d[14]=7 ✓


async def test_ogrn13_valid_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("ОГРН 1027700132195")
    f = get_label(findings, "OGRN")
    assert f is not None
    assert f.text == "1027700132195"
    assert f.score == 1.0


async def test_ogrn13_wrong_checksum_not_detected(det: RegexChecksumDetector) -> None:
    # Last digit changed 5→0
    findings = await det.detect("ОГРН 1027700132190")
    assert not has_label(findings, "OGRN")


async def test_ogrn15_valid_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("ОГРНИП 304010000000017")
    f = get_label(findings, "OGRN")
    assert f is not None
    assert f.text == "304010000000017"
    assert f.score == 1.0


async def test_ogrn15_wrong_checksum_not_detected(det: RegexChecksumDetector) -> None:
    # Last digit changed 7→0
    findings = await det.detect("ОГРНИП 304010000000010")
    assert not has_label(findings, "OGRN")


# ---------------------------------------------------------------------------
# СНИЛС (SNILS)
# ---------------------------------------------------------------------------
# СНИЛС 11223344595:
#   d=[1,1,2,2,3,3,4,4,5], sum=9+8+14+12+15+12+12+8+5=95 < 100 → check=95 ✓
# Formatted 112-233-445 95: same digits concatenated → same check ✓


async def test_snils_keyword_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("СНИЛС 11223344595")
    f = get_label(findings, "SNILS")
    assert f is not None
    assert f.text == "11223344595"
    assert f.score == 1.0


async def test_snils_formatted_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("страховой номер: 112-233-445 95")
    f = get_label(findings, "SNILS")
    assert f is not None
    assert "11223344595" in f.text.replace("-", "").replace(" ", "")


async def test_snils_wrong_checksum_not_detected(det: RegexChecksumDetector) -> None:
    # Last 2 digits 95→01
    findings = await det.detect("СНИЛС 11223344501")
    assert not has_label(findings, "SNILS")


async def test_snils_formatted_wrong_checksum_not_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("112-233-445 01")
    assert not has_label(findings, "SNILS")


# ---------------------------------------------------------------------------
# КПП (KPP)
# ---------------------------------------------------------------------------


async def test_kpp_valid_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("КПП 773601001")
    f = get_label(findings, "KPP")
    assert f is not None
    assert f.text == "773601001"


async def test_kpp_serial_000_not_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("773601000")
    assert not has_label(findings, "KPP")


# ---------------------------------------------------------------------------
# БИК (BIK)
# ---------------------------------------------------------------------------
# БИК 044525225 (Сбербанк): starts with "04", bank-code "225" ≠ "000" ✓


async def test_bik_valid_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("БИК 044525225")
    f = get_label(findings, "BIK")
    assert f is not None
    assert f.text == "044525225"
    assert f.score == 0.95


async def test_bik_wrong_prefix_not_detected(det: RegexChecksumDetector) -> None:
    # Does not start with "04"
    findings = await det.detect("123456789")
    assert not has_label(findings, "BIK")


async def test_bik_zero_bank_code_not_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("044000000")
    assert not has_label(findings, "BIK")


# ---------------------------------------------------------------------------
# Bank account (BANK_ACCOUNT)
# ---------------------------------------------------------------------------
# Bank account 40702810000000000007 + BIK 044525225:
#   combined = "225"+"40702810000000000007" = "22540702810000000000007" (23 chars)
#   weights=[7,1,3,7,1,3,7,1,3,7,1,3,7,1,3,7,1,3,7,1,3,7,1]
#   sum=14+2+15+28+0+21+0+2+24+7+0+0+0+0+0+0+0+0+0+0+0+0+7=120, 120%10=0 ✓

_ACCT_KEYWORD_TEXT = "р/счёт 40702810000000000007"  # noqa: RUF001
_ACCT_WITH_BIK = "р/счёт 40702810000000000007 в банке с БИК 044525225"  # noqa: RUF001
_ACCT_WRONG = "р/счёт 40702810000000000001 БИК 044525225"  # noqa: RUF001


async def test_bank_account_keyword_structural(det: RegexChecksumDetector) -> None:
    findings = await det.detect(_ACCT_KEYWORD_TEXT)
    f = get_label(findings, "BANK_ACCOUNT")
    assert f is not None
    assert f.text == "40702810000000000007"
    assert f.score == 0.7  # no adjacent BIK → structural only


async def test_bank_account_with_bik_key_check(det: RegexChecksumDetector) -> None:
    findings = await det.detect(_ACCT_WITH_BIK)
    f = get_label(findings, "BANK_ACCOUNT")
    assert f is not None
    assert f.score == 1.0  # BIK present + control key passes


async def test_bank_account_bare_prefix_detected(det: RegexChecksumDetector) -> None:
    # Starts with "40" — bare detection
    findings = await det.detect("40702810000000000007")
    assert has_label(findings, "BANK_ACCOUNT")


async def test_bank_account_invalid_bik_key_falls_back(det: RegexChecksumDetector) -> None:
    # BIK present but wrong account → key check fails → stays at 0.7
    findings = await det.detect(_ACCT_WRONG)
    f = get_label(findings, "BANK_ACCOUNT")
    assert f is not None
    assert f.score == 0.7


# ---------------------------------------------------------------------------
# IPv4 / IPv6 / CIDR
# ---------------------------------------------------------------------------


async def test_ipv4_private_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("host at 192.168.1.1 port 8080")
    f = get_label(findings, "IP_ADDRESS")
    assert f is not None
    assert f.text == "192.168.1.1"
    assert f.score == 1.0


async def test_ipv4_invalid_octet_not_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("address 999.1.1.1")
    assert not has_label(findings, "IP_ADDRESS")


async def test_cidr_detected_and_preferred_over_bare_ip(det: RegexChecksumDetector) -> None:
    findings = await det.detect("subnet 10.0.0.0/8")
    cidr = get_label(findings, "CIDR")
    assert cidr is not None
    assert cidr.text == "10.0.0.0/8"
    # Bare IP_ADDRESS should be deduplicated away
    assert not any(f.label == "IP_ADDRESS" and f.text == "10.0.0.0" for f in findings)


async def test_ipv6_full_detected(det: RegexChecksumDetector) -> None:
    text = "server 2001:0db8:85a3:0000:0000:8a2e:0370:7334"
    findings = await det.detect(text)
    f = get_label(findings, "IP_ADDRESS")
    assert f is not None
    assert "2001" in f.text


async def test_ipv6_compressed_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("loopback ::1 configured")
    f = get_label(findings, "IP_ADDRESS")
    assert f is not None
    assert f.text == "::1"


# ---------------------------------------------------------------------------
# Internal hostnames (HOSTNAME)
# ---------------------------------------------------------------------------


async def test_internal_hostname_corp_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("api.corp.internal")
    f = get_label(findings, "HOSTNAME")
    assert f is not None
    assert f.text == "api.corp.internal"


async def test_internal_hostname_local_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("myservice.local")
    assert has_label(findings, "HOSTNAME")


async def test_external_hostname_not_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("api.example.com")
    assert not has_label(findings, "HOSTNAME")


# ---------------------------------------------------------------------------
# DB URLs (DB_URL)
# ---------------------------------------------------------------------------


async def test_db_url_postgres_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("DATABASE_URL=postgres://user:pass@db.corp.internal:5432/mydb")
    f = get_label(findings, "DB_URL")
    assert f is not None
    assert f.text.startswith("postgres://")


async def test_db_url_redis_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("redis://localhost:6379/0")
    assert has_label(findings, "DB_URL")


async def test_db_url_mysql_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("mysql://root:secret@prod.db.lan/orders")
    assert has_label(findings, "DB_URL")


# ---------------------------------------------------------------------------
# JWT (JWT)
# ---------------------------------------------------------------------------

_SAMPLE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)


async def test_jwt_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect(f"Authorization header value: {_SAMPLE_JWT}")
    f = get_label(findings, "JWT")
    assert f is not None
    assert f.text == _SAMPLE_JWT
    assert f.score == 0.95


async def test_non_jwt_base64_not_detected(det: RegexChecksumDetector) -> None:
    # Does not start with eyJ → not a JWT
    findings = await det.detect("dGhpcyBpcyBub3QgYSBqd3Q=")
    assert not has_label(findings, "JWT")


# ---------------------------------------------------------------------------
# Bearer token (TOKEN)
# ---------------------------------------------------------------------------


async def test_bearer_value_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("curl -H 'Authorization: Bearer my-very-long-secret-token-abc123'")
    f = get_label(findings, "TOKEN")
    assert f is not None
    assert "Bearer" not in f.text  # value only, not the keyword
    assert len(f.text) >= 20


async def test_bearer_short_value_not_detected(det: RegexChecksumDetector) -> None:
    # Value shorter than 20 chars → not flagged
    findings = await det.detect("Bearer short")
    assert not has_label(findings, "TOKEN")


# ---------------------------------------------------------------------------
# PEM private key (PEM_PRIVATE_KEY)
# ---------------------------------------------------------------------------

_PEM_BLOCK = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA0Z3VS5JJcds3xHn/ygWep4PAtEs11Gy\n"
    "-----END RSA PRIVATE KEY-----"
)


async def test_pem_private_key_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect(_PEM_BLOCK)
    f = get_label(findings, "PEM_PRIVATE_KEY")
    assert f is not None
    assert "BEGIN RSA PRIVATE KEY" in f.text


async def test_pem_public_key_not_detected(det: RegexChecksumDetector) -> None:
    # Public key → not a secret
    findings = await det.detect("-----BEGIN PUBLIC KEY-----\nABCDEF==\n-----END PUBLIC KEY-----")
    assert not has_label(findings, "PEM_PRIVATE_KEY")


# ---------------------------------------------------------------------------
# API keys (API_KEY)
# ---------------------------------------------------------------------------


async def test_openai_key_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123456789abcd")
    f = get_label(findings, "API_KEY")
    assert f is not None
    assert f.text.startswith("sk-")
    assert f.score == 1.0


async def test_aws_key_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE")
    assert has_label(findings, "API_KEY")


async def test_github_token_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("token: ghp_" + "A" * 36)
    assert has_label(findings, "API_KEY")


async def test_generic_secret_keyword_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("api_key=abcdefghijklmnopqrstuvwxyz1234567")
    assert has_label(findings, "API_KEY")


# ---------------------------------------------------------------------------
# Password literals (PASSWORD)
# ---------------------------------------------------------------------------


async def test_password_eq_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("password=SuperSecret123!")
    f = get_label(findings, "PASSWORD")
    assert f is not None
    assert f.text == "SuperSecret123!"


async def test_pwd_colon_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("pwd: mysecretpassword")
    assert has_label(findings, "PASSWORD")


async def test_password_too_short_not_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("pass=abc")
    assert not has_label(findings, "PASSWORD")


# ---------------------------------------------------------------------------
# Email (EMAIL)
# ---------------------------------------------------------------------------


async def test_email_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("Contact john.doe@corp.internal for access")
    f = get_label(findings, "EMAIL")
    assert f is not None
    assert f.text == "john.doe@corp.internal"
    assert f.score == 0.95


async def test_no_email_no_finding(det: RegexChecksumDetector) -> None:
    findings = await det.detect("No email here at all.")
    assert not has_label(findings, "EMAIL")


# ---------------------------------------------------------------------------
# Phone (PHONE_NUMBER)
# ---------------------------------------------------------------------------


async def test_phone_ru_formatted_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("Тел.: +7 (900) 123-45-67")
    f = get_label(findings, "PHONE_NUMBER")
    assert f is not None
    assert "+7" in f.text or "7" in f.text


async def test_phone_ru_8_format_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("звоните 8(900)1234567")
    assert has_label(findings, "PHONE_NUMBER")


async def test_phone_international_detected(det: RegexChecksumDetector) -> None:
    findings = await det.detect("UK office: +442079460123")
    assert has_label(findings, "PHONE_NUMBER")


# ---------------------------------------------------------------------------
# Negative: clean prose → no findings
# ---------------------------------------------------------------------------


async def test_clean_prose_no_findings(det: RegexChecksumDetector) -> None:
    text = (
        "The quarterly report shows strong performance across all business units. "
        "Management is pleased with the results and looks forward to continued growth."
    )
    findings = await det.detect(text)
    assert findings == []


# ---------------------------------------------------------------------------
# Negative: number that looks like ИНН but fails checksum
# ---------------------------------------------------------------------------


async def test_inn_lookalike_wrong_checksum(det: RegexChecksumDetector) -> None:
    # 1234567890 — random 10-digit number
    # d=[1,2,3,4,5,6,7,8,9,0], w=[2,4,10,3,5,9,4,6,8]
    # sum=2+8+30+12+25+54+28+48+72=279, 279%11=3, 3%10=3 ≠ d[9]=0 → invalid
    findings = await det.detect("number 1234567890 in text")
    assert not has_label(findings, "RU_INN")


# ---------------------------------------------------------------------------
# Counterparty full-profile fixture
# ---------------------------------------------------------------------------


async def test_counterparty_full_profile(det: RegexChecksumDetector) -> None:
    """Regression: all standard реквизиты detected in one Russian string."""
    text = (
        "Контрагент Ромашка, ИНН 7707083893, КПП 773601001, "
        "ОГРН 1027700132195, "
        + _ACCT_WITH_BIK
        + ", тел.: +7 (900) 123-45-67, email: john.doe@corp.internal"
    )
    findings = await det.detect(text)
    found = {f.label for f in findings}
    assert "RU_INN" in found, f"RU_INN missing from {found}"
    assert "KPP" in found, f"KPP missing from {found}"
    assert "OGRN" in found, f"OGRN missing from {found}"
    assert "BANK_ACCOUNT" in found, f"BANK_ACCOUNT missing from {found}"
    assert "BIK" in found, f"BIK missing from {found}"
    assert "PHONE_NUMBER" in found, f"PHONE_NUMBER missing from {found}"
    assert "EMAIL" in found, f"EMAIL missing from {found}"

    bank = get_label(findings, "BANK_ACCOUNT")
    assert bank is not None and bank.score == 1.0, "BANK_ACCOUNT should have score 1.0 with BIK"


# ---------------------------------------------------------------------------
# Span integrity — start/end must match .text in the original input
# ---------------------------------------------------------------------------


async def test_finding_spans_correct(det: RegexChecksumDetector) -> None:
    text = "host 192.168.100.1 serves postgres://admin:pw@db.lan/prod here"
    findings = await det.detect(text)
    for f in findings:
        assert text[f.start : f.end] == f.text, (
            f"Span mismatch for {f.label}: text[{f.start}:{f.end}]="
            f"{text[f.start : f.end]!r} != {f.text!r}"
        )


# ---------------------------------------------------------------------------
# СНИЛС + ИНН + ОГРНИП combined
# ---------------------------------------------------------------------------


async def test_snils_inn_ogrn_in_one_text(det: RegexChecksumDetector) -> None:
    text = "СНИЛС 11223344595, ИНН 7707083893, ОГРНИП 304010000000017"
    findings = await det.detect(text)
    found = {f.label for f in findings}
    assert "SNILS" in found
    assert "RU_INN" in found
    assert "OGRN" in found

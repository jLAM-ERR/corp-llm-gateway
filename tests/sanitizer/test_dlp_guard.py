"""Unit tests for DlpEgressGuard (Stage 5)."""

from __future__ import annotations

from corp_llm_gateway.sanitizer.dlp_guard import DlpEgressGuard

# ---------------------------------------------------------------------------
# Canary detection
# ---------------------------------------------------------------------------


def test_literal_canary_in_text_returns_dlp_canary() -> None:
    guard = DlpEgressGuard(canary_patterns=["DLP-CANARY-SENTINEL"])
    assert guard.scan("hello DLP-CANARY-SENTINEL world") == "dlp:canary"


def test_regex_canary_matches_returns_dlp_canary() -> None:
    guard = DlpEgressGuard(canary_patterns=[r"CANARY-\d+"])
    assert guard.scan("contains CANARY-9999 here") == "dlp:canary"


def test_multiple_canary_patterns_first_match_wins() -> None:
    guard = DlpEgressGuard(canary_patterns=["SENTINEL-A", "SENTINEL-B"])
    assert guard.scan("text with SENTINEL-B embedded") == "dlp:canary"


def test_canary_no_match_returns_none() -> None:
    guard = DlpEgressGuard(canary_patterns=["DLP-CANARY-SENTINEL"])
    assert guard.scan("normal text with no canary") is None


# ---------------------------------------------------------------------------
# Secret rescan — PEM private key
# ---------------------------------------------------------------------------


def test_pem_private_key_returns_dlp_secret_leak() -> None:
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQC7\n"
        "-----END PRIVATE KEY-----"
    )
    guard = DlpEgressGuard()
    assert guard.scan(pem) == "dlp:secret_leak"


def test_pem_rsa_private_key_returns_dlp_secret_leak() -> None:
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA2a2rwplBQLF29amygykEMmYz0+Kcj3bKBp29\n"
        "-----END RSA PRIVATE KEY-----"
    )
    guard = DlpEgressGuard()
    assert guard.scan(f"key: {pem}") == "dlp:secret_leak"


# ---------------------------------------------------------------------------
# Secret rescan — JWT
# ---------------------------------------------------------------------------


def test_jwt_in_text_returns_dlp_secret_leak() -> None:
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    guard = DlpEgressGuard()
    assert guard.scan(f"Bearer {jwt}") == "dlp:secret_leak"


# ---------------------------------------------------------------------------
# Secret rescan — OpenAI / AWS / GitHub keys
# ---------------------------------------------------------------------------


def test_openai_key_returns_dlp_secret_leak() -> None:
    guard = DlpEgressGuard()
    assert guard.scan("sk-" + "a" * 48) == "dlp:secret_leak"


def test_aws_access_key_returns_dlp_secret_leak() -> None:
    guard = DlpEgressGuard()
    assert guard.scan("key=AKIAIOSFODNN7EXAMPLE") == "dlp:secret_leak"


def test_github_token_returns_dlp_secret_leak() -> None:
    guard = DlpEgressGuard()
    assert guard.scan("token: ghp_" + "a" * 36) == "dlp:secret_leak"


# ---------------------------------------------------------------------------
# Clean / placeholder text → None
# ---------------------------------------------------------------------------


def test_placeholder_only_returns_none() -> None:
    guard = DlpEgressGuard()
    text = "hello [NAME_001] and [EMAIL_002] and [API_KEY_003] and [PEM_PRIVATE_KEY_004]"
    assert guard.scan(text) is None


def test_empty_text_returns_none() -> None:
    guard = DlpEgressGuard()
    assert guard.scan("") is None


def test_clean_prose_returns_none() -> None:
    guard = DlpEgressGuard()
    assert guard.scan("The weather is nice today, no secrets here.") is None


def test_no_canaries_configured_clean_text_returns_none() -> None:
    guard = DlpEgressGuard(canary_patterns=None, secret_rescan=True)
    assert guard.scan("normal sanitized text with [PLACEHOLDER_001]") is None


# ---------------------------------------------------------------------------
# secret_rescan=False ignores secrets
# ---------------------------------------------------------------------------


def test_secret_rescan_false_ignores_pem_key() -> None:
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQC7\n"
        "-----END PRIVATE KEY-----"
    )
    guard = DlpEgressGuard(secret_rescan=False)
    assert guard.scan(pem) is None


def test_secret_rescan_false_ignores_jwt() -> None:
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    guard = DlpEgressGuard(secret_rescan=False)
    assert guard.scan(jwt) is None


def test_secret_rescan_false_canary_still_works() -> None:
    guard = DlpEgressGuard(canary_patterns=["MY-CANARY"], secret_rescan=False)
    assert guard.scan("text with MY-CANARY here") == "dlp:canary"


# ---------------------------------------------------------------------------
# No canary patterns at all
# ---------------------------------------------------------------------------


def test_no_canary_patterns_no_secrets_returns_none() -> None:
    guard = DlpEgressGuard(canary_patterns=[], secret_rescan=True)
    assert guard.scan("placeholder [EMAIL_001] only") is None

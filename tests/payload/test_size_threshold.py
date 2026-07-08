import pytest

from corp_llm_gateway.payload import (
    DEFAULT_THRESHOLD_BYTES,
    OVERSIZE_CHUNK,
    OVERSIZE_DELIVER_FLAG,
    OVERSIZE_FAIL_CLOSED,
    OversizeContentError,
    normalize_oversize_policy,
    should_skip_sanitization,
)


def test_default_threshold_is_100kb() -> None:
    assert DEFAULT_THRESHOLD_BYTES == 100 * 1024


def test_below_threshold_does_not_skip() -> None:
    assert should_skip_sanitization(0) is False
    assert should_skip_sanitization(1024) is False
    assert should_skip_sanitization(DEFAULT_THRESHOLD_BYTES - 1) is False


def test_at_threshold_does_not_skip() -> None:
    assert should_skip_sanitization(DEFAULT_THRESHOLD_BYTES) is False


def test_above_threshold_skips() -> None:
    assert should_skip_sanitization(DEFAULT_THRESHOLD_BYTES + 1) is True


def test_custom_threshold() -> None:
    assert should_skip_sanitization(50, threshold_bytes=100) is False
    assert should_skip_sanitization(101, threshold_bytes=100) is True


def test_negative_content_bytes_rejected() -> None:
    with pytest.raises(ValueError):
        should_skip_sanitization(-1)


def test_negative_threshold_rejected() -> None:
    with pytest.raises(ValueError):
        should_skip_sanitization(0, threshold_bytes=-1)


def test_normalize_oversize_policy_defaults_fail_closed() -> None:
    assert normalize_oversize_policy(None) == OVERSIZE_FAIL_CLOSED
    assert normalize_oversize_policy("") == OVERSIZE_FAIL_CLOSED


def test_normalize_oversize_policy_accepts_known_values() -> None:
    assert normalize_oversize_policy("chunk") == OVERSIZE_CHUNK
    assert normalize_oversize_policy(" Deliver-Flag ") == OVERSIZE_DELIVER_FLAG


def test_normalize_oversize_policy_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        normalize_oversize_policy("deliver")


def test_oversize_content_error_carries_sizes_not_content() -> None:
    exc = OversizeContentError(content_bytes=200_000, threshold_bytes=102_400)
    assert exc.content_bytes == 200_000
    assert exc.threshold_bytes == 102_400
    # Message is sizes only — no raw payload can hide in it.
    assert "200000" in str(exc)
    assert "102400" in str(exc)

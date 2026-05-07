import pytest

from corp_llm_gateway.payload import (
    DEFAULT_THRESHOLD_BYTES,
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

from __future__ import annotations

DEFAULT_THRESHOLD_BYTES = 100 * 1024

# CORP_LLM_OVERSIZE_POLICY values. Governs what happens when a single text leaf
# exceeds DEFAULT_THRESHOLD_BYTES. The default fails closed — an oversize leaf
# must never be forwarded UNSANITIZED (F1).
OVERSIZE_FAIL_CLOSED = "fail-closed"
OVERSIZE_CHUNK = "chunk"
OVERSIZE_DELIVER_FLAG = "deliver-flag"
OVERSIZE_POLICIES = frozenset({OVERSIZE_FAIL_CLOSED, OVERSIZE_CHUNK, OVERSIZE_DELIVER_FLAG})


def should_skip_sanitization(
    content_bytes: int,
    threshold_bytes: int = DEFAULT_THRESHOLD_BYTES,
) -> bool:
    if content_bytes < 0:
        raise ValueError(f"content_bytes must be >= 0, got {content_bytes}")
    if threshold_bytes < 0:
        raise ValueError(f"threshold_bytes must be >= 0, got {threshold_bytes}")
    return content_bytes > threshold_bytes


def normalize_oversize_policy(value: str | None) -> str:
    """Validate CORP_LLM_OVERSIZE_POLICY; unset/empty → fail-closed. Unknown → ValueError."""
    policy = (value or OVERSIZE_FAIL_CLOSED).strip().lower()
    if policy not in OVERSIZE_POLICIES:
        raise ValueError(
            f"invalid oversize policy {value!r}; expected one of {sorted(OVERSIZE_POLICIES)}"
        )
    return policy


class OversizeContentError(Exception):
    """A text leaf exceeds the threshold and the resolved policy blocks egress.

    Carries byte sizes only — never raw content — so callers can map it to a
    stable block without leaking the payload (M1-14).
    """

    def __init__(self, *, content_bytes: int, threshold_bytes: int) -> None:
        super().__init__(f"content {content_bytes} bytes exceeds threshold {threshold_bytes} bytes")
        self.content_bytes = content_bytes
        self.threshold_bytes = threshold_bytes

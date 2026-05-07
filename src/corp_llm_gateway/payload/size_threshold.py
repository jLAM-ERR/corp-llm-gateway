DEFAULT_THRESHOLD_BYTES = 100 * 1024


def should_skip_sanitization(
    content_bytes: int,
    threshold_bytes: int = DEFAULT_THRESHOLD_BYTES,
) -> bool:
    if content_bytes < 0:
        raise ValueError(f"content_bytes must be >= 0, got {content_bytes}")
    if threshold_bytes < 0:
        raise ValueError(f"threshold_bytes must be >= 0, got {threshold_bytes}")
    return content_bytes > threshold_bytes

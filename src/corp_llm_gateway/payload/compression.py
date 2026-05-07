import gzip

DEFAULT_GZIP_MIN_BYTES = 1024


def maybe_gzip(
    value: bytes | str,
    min_bytes: int = DEFAULT_GZIP_MIN_BYTES,
) -> tuple[bytes, bool]:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    if len(raw) < min_bytes:
        return raw, False
    return gzip.compress(raw), True


def maybe_gunzip(data: bytes, is_gzipped: bool) -> bytes:
    if not is_gzipped:
        return data
    return gzip.decompress(data)

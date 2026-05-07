from corp_llm_gateway.payload import maybe_gunzip, maybe_gzip


def test_small_value_not_gzipped() -> None:
    data, gz = maybe_gzip("hello", min_bytes=1024)
    assert gz is False
    assert data == b"hello"


def test_large_value_gzipped() -> None:
    payload = "abc" * 1000
    data, gz = maybe_gzip(payload, min_bytes=100)
    assert gz is True
    assert data != payload.encode("utf-8")
    assert len(data) < len(payload)


def test_round_trip_string_small() -> None:
    payload = "hi"
    data, gz = maybe_gzip(payload, min_bytes=1024)
    assert maybe_gunzip(data, gz).decode() == payload


def test_round_trip_string_large() -> None:
    payload = "abcdef" * 500
    data, gz = maybe_gzip(payload, min_bytes=100)
    assert gz is True
    assert maybe_gunzip(data, gz).decode() == payload


def test_round_trip_bytes() -> None:
    payload = b"\x00\x01\x02" * 1000
    data, gz = maybe_gzip(payload, min_bytes=100)
    assert gz is True
    assert maybe_gunzip(data, gz) == payload


def test_at_min_bytes_boundary() -> None:
    payload = "x" * 1024
    _, gz = maybe_gzip(payload, min_bytes=1024)
    assert gz is True


def test_just_under_min_bytes_boundary() -> None:
    payload = "x" * 1023
    _, gz = maybe_gzip(payload, min_bytes=1024)
    assert gz is False

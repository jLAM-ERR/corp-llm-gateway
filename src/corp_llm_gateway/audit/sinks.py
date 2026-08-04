import json
import sys
from abc import ABC, abstractmethod
from typing import Any, TextIO


class AuditWriteAmbiguousError(Exception):
    """A sink MAY raise this instead of a plain `Exception` when `write()`
    failed but the record may already have been persisted downstream (e.g.
    an HTTP response was accepted but reading the acknowledgement timed
    out) — as opposed to a confirmed non-delivery (connection refused, DNS
    failure). Callers must not blindly retry-emit the same event on this
    error, to avoid writing a duplicate record for one logical write; any
    OTHER exception is treated as confirmed non-delivery and safe to retry.
    """


class Sink(ABC):
    @abstractmethod
    async def write(self, record: dict[str, Any]) -> None: ...


class StdoutSink(Sink):
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    async def write(self, record: dict[str, Any]) -> None:
        self._stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._stream.flush()


class ListSink(Sink):
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)

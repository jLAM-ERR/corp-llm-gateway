import json
import sys
from abc import ABC, abstractmethod
from typing import Any, TextIO


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

from collections.abc import AsyncIterable, AsyncIterator

from corp_llm_gateway.sanitizer.placeholder import sort_placeholders_by_descending_length
from corp_llm_gateway.sanitizer.strategies import StrategyResult


class StreamingDesanitizer:
    """Stateful de-sanitizer for SSE streaming chunks.

    Plan ref: M1-8. The corp-LLM proxy streams response chunks; placeholders
    may span chunk boundaries (e.g. `[NAME` in one chunk, `_001]` in the
    next). We buffer the last `max_placeholder_length - 1` characters so any
    in-flight placeholder of up to `max_placeholder_length` is fully visible
    when its closing bytes arrive.

    Replacement order is length-descending (M1-9) so a longer placeholder
    can't be shadowed by a shorter prefix-match one.
    """

    def __init__(self, mapping: StrategyResult) -> None:
        self._by_placeholder: dict[str, str] = {
            placeholder: original for original, placeholder in mapping.pairs
        }
        self._sorted_placeholders: tuple[str, ...] = tuple(
            sort_placeholders_by_descending_length(self._by_placeholder)
        )
        self._max_len = max((len(p) for p in self._by_placeholder), default=0)
        self._buffer = ""
        self._flushed = False

    def feed(self, chunk: str) -> str:
        if self._flushed:
            raise RuntimeError("StreamingDesanitizer.feed called after flush")
        self._buffer += chunk
        self._buffer = self._replace_all(self._buffer)

        if self._max_len <= 1:
            safe = self._buffer
            self._buffer = ""
            return safe

        hold = self._max_len - 1
        if len(self._buffer) <= hold:
            return ""
        safe = self._buffer[:-hold]
        self._buffer = self._buffer[-hold:]
        return safe

    def flush(self) -> str:
        if self._flushed:
            return ""
        self._flushed = True
        remaining = self._replace_all(self._buffer)
        self._buffer = ""
        return remaining

    async def stream(self, chunks: AsyncIterable[str]) -> AsyncIterator[str]:
        async for chunk in chunks:
            out = self.feed(chunk)
            if out:
                yield out
        tail = self.flush()
        if tail:
            yield tail

    def _replace_all(self, text: str) -> str:
        for placeholder in self._sorted_placeholders:
            text = text.replace(placeholder, self._by_placeholder[placeholder])
        return text

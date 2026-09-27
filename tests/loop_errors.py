"""Record what reaches the running loop's exception handler.

Python 3.13+ hands a shielded future's failure, and an unretrieved one, to this
handler with the exception's full text — a driver message can quote a token.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
from collections.abc import AsyncIterator
from typing import Any


@contextlib.asynccontextmanager
async def loop_errors(*, settle_s: float = 0.0) -> AsyncIterator[list[dict[str, Any]]]:
    """Every handler call in the block, plus ``settle_s`` and a GC pass after it."""
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    seen: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: seen.append(context))
    try:
        yield seen
        await asyncio.sleep(settle_s)
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)


def describe(seen: list[dict[str, Any]]) -> list[str]:
    return [f"{c.get('message')}: {c.get('exception')!r}" for c in seen]

"""A TCP relay in front of the test Postgres that can black-hole on demand.

While stalled, no byte moves either way and a new connection is accepted but
never answered — what a client sees from a paused or partitioned server, including
asyncpg's out-of-band cancel request. ``resume()`` lets everything flow again.
While dropping, a new connection is closed unread: a cancel request that is lost.
"""

from __future__ import annotations

import asyncio
import contextlib
from urllib.parse import urlsplit, urlunsplit


class StallingProxy:
    def __init__(self, upstream_dsn: str) -> None:
        parts = urlsplit(upstream_dsn)
        self._dsn = parts
        self._upstream = (parts.hostname or "127.0.0.1", parts.port or 5432)
        self._flowing = asyncio.Event()
        self._flowing.set()
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self.accepted = 0
        self.dropping = False

    async def start(self) -> str:
        """Listen on a loopback port; the DSN that goes through the relay."""
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        auth = self._dsn.netloc.rpartition("@")[0]
        netloc = f"{auth}@127.0.0.1:{port}" if auth else f"127.0.0.1:{port}"
        return urlunsplit(self._dsn._replace(netloc=netloc))

    def stall(self) -> None:
        self._flowing.clear()

    def resume(self) -> None:
        self._flowing.set()

    def drop_new(self, dropping: bool = True) -> None:
        self.dropping = dropping

    async def close(self) -> None:
        self.resume()
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers):
            writer.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        # The relay tasks' own cancellations are results; a cancelled close propagates.
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.accepted += 1
        if self.dropping:
            writer.close()
            return
        self._writers.add(writer)
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)  # type: ignore[arg-type]
        try:
            await self._flowing.wait()
            up_reader, up_writer = await asyncio.open_connection(*self._upstream)
            self._writers.add(up_writer)
            await asyncio.gather(self._pump(reader, up_writer), self._pump(up_reader, writer))
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            self._tasks.discard(task)  # type: ignore[arg-type]
            writer.close()

    async def _pump(self, src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await src.read(65536)
                await self._flowing.wait()
                if not chunk:
                    break
                dst.write(chunk)
                await dst.drain()
        except OSError:
            pass
        finally:
            dst.close()

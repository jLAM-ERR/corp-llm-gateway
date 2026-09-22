"""TCP relay onto the egress-blocked network, for the route-gate container tests.

Docker refuses to publish a port from a container whose only network is
``--internal`` (the host side has no route back), so the gateway cannot both sit
on the egress-blocked network and answer on 127.0.0.1. This relay does: it is the
one container attached to BOTH networks, publishes 4000, and forwards raw TCP to
the gateway. Raw TCP, so HTTP, SSE and the WebSocket upgrade all pass through
unchanged; the gateway itself still has no route off the internal network.

Not a pytest module: it is mounted into a container and run as a script.
"""

from __future__ import annotations

import asyncio
import os

TARGET_HOST = os.environ["RELAY_TARGET_HOST"]
TARGET_PORT = int(os.environ["RELAY_TARGET_PORT"])
LISTEN_PORT = int(os.environ.get("RELAY_LISTEN_PORT", "4000"))


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(TARGET_HOST, TARGET_PORT)
    except OSError:
        writer.close()
        return
    await asyncio.gather(
        _pump(reader, upstream_writer),
        _pump(upstream_reader, writer),
    )


async def main() -> None:
    server = await asyncio.start_server(_handle, "0.0.0.0", LISTEN_PORT)
    print(f"relay listening on {LISTEN_PORT} -> {TARGET_HOST}:{TARGET_PORT}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())

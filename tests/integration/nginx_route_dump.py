"""The route table of litellm's proxy app as the pinned image serves it.

Runs INSIDE the gateway image (``python -`` on stdin), never under pytest. A bare
import of the app is not the full table: ``attach_lazy_features`` defers litellm's
optional routers — ``POST /v1/messages`` among them — until a request matches
their prefix, so every lazy feature is force-loaded first. ``_force_load`` is a
private name: if a release renames it, this import fails and so does the test.

Prints one ``@@ROUTES@@<json>`` line: ``pairs`` (method, path) per route by its
kind, ``unknown`` for any route class this does not know, ``failed`` for any lazy
feature that did not load.
"""

import asyncio
import json

from fastapi.routing import APIWebSocketRoute
from litellm.proxy._lazy_features import LAZY_FEATURES, _force_load
from litellm.proxy.proxy_server import app
from starlette.routing import Mount, Route, WebSocketRoute

MARKER = "@@ROUTES@@"


async def _warm() -> list[str]:
    return [feature.name for feature in LAZY_FEATURES if not await _force_load(app, feature)]


def main() -> None:
    failed = asyncio.run(_warm())
    pairs: list[list[str]] = []
    unknown: list[str] = []
    for route in app.routes:
        # fastapi.routing.APIRoute is a starlette Route: one pair per method.
        if isinstance(route, Route):
            pairs += [[method, route.path] for method in sorted(route.methods or ())]
        elif isinstance(route, (APIWebSocketRoute, WebSocketRoute)):
            pairs.append(["WEBSOCKET", route.path])
        elif isinstance(route, Mount):
            pairs.append(["MOUNT", route.path])
        else:
            unknown.append(f"{type(route).__module__}.{type(route).__qualname__}")
    print(MARKER + json.dumps({"pairs": pairs, "unknown": unknown, "failed": failed}), flush=True)


if __name__ == "__main__":
    main()

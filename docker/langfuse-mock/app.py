"""Mock Langfuse for e2e testing.

Captures POSTs to /api/public/ingestion and exposes a /__captures
endpoint so tests can introspect what landed.

Auth: accepts any Basic auth (the real Langfuse validates public/secret
keys; the mock just records what was sent).
"""
from __future__ import annotations

import base64
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="langfuse-mock")
_captures: list[dict[str, Any]] = []


@app.get("/api/public/health")
async def health() -> dict[str, str]:
    return {"status": "OK"}


@app.post("/api/public/ingestion")
async def ingestion(
    request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    body = await request.json()
    parsed_auth = _parse_basic(authorization)
    _captures.append({"auth": parsed_auth, "body": body, "headers": dict(request.headers)})
    batch = body.get("batch") or []
    return JSONResponse(
        status_code=207,
        content={"successes": [{"id": e.get("id")} for e in batch], "errors": []},
    )


@app.get("/__captures")
async def list_captures() -> dict[str, Any]:
    return {"count": len(_captures), "captures": _captures}


@app.delete("/__captures")
async def clear_captures() -> dict[str, str]:
    _captures.clear()
    return {"status": "cleared"}


def _parse_basic(value: str | None) -> dict[str, str | None]:
    if not value or not value.lower().startswith("basic "):
        return {"public_key": None, "secret_key": None}
    try:
        decoded = base64.b64decode(value.split(" ", 1)[1]).decode()
        public, _, secret = decoded.partition(":")
        return {"public_key": public, "secret_key": secret}
    except Exception:
        return {"public_key": None, "secret_key": None}

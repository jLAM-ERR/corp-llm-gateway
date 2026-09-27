"""The issuance bounds on a real socket: uvicorn in process, serving the same
chain the entrypoint serves (route gate -> HealthRouter -> downstream).

Skips where uvicorn is absent (the graceful-degradation venv); the JWKS case
also needs `cryptography`.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

uvicorn = pytest.importorskip("uvicorn")

from corp_llm_gateway.audit import AuditLogger, ListSink  # noqa: E402
from corp_llm_gateway.healthz import build_health_router  # noqa: E402
from corp_llm_gateway.healthz.checks import (  # noqa: E402
    ExtensionsCheck,
    HealthStatus,
    LiveCheck,
    ReadyCheck,
    SanitizationCheck,
)
from corp_llm_gateway.metrics import NoopExporter  # noqa: E402
from corp_llm_gateway.route_gate import RouteGateMiddleware  # noqa: E402
from corp_llm_gateway.tokens import (  # noqa: E402
    InMemoryTokenStore,
    OidcClaims,
    TokenIssuer,
)

PATH = "/internal/issue-token"


async def _ok() -> bool:
    return True


async def _ext() -> dict[str, HealthStatus]:
    return {}


def _stack(issuer: TokenIssuer | None, fallthrough: Any = None) -> RouteGateMiddleware:
    router = build_health_router(
        live_check=LiveCheck(),
        ready_check=ReadyCheck(check_redis=_ok, check_postgres=_ok),
        sanitization_check=SanitizationCheck(run_round_trip=_ok),
        extensions_check=ExtensionsCheck(health_all=_ext),
        token_issuer=issuer,
        fallthrough=fallthrough,
    )
    return RouteGateMiddleware(
        router,
        metrics=NoopExporter(),
        audit_logger=AuditLogger(ListSink(), gateway_version="0.0.0"),
    )


@asynccontextmanager
async def _served(app: Any) -> AsyncIterator[int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            lifespan="off",
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=5,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        yield port
    finally:
        server.should_exit = True
        await task
        sock.close()


async def _raw_request(
    port: int, head: str, body: bytes = b"", *, timeout: float = 10.0
) -> tuple[int, dict[str, Any], float]:
    """Send ``head`` (+ ``body``) and never finish the body: returns the status,
    the JSON body and the seconds until the response started."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        start = time.monotonic()
        writer.write(head.encode("latin-1") + body)
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), timeout)
        elapsed = time.monotonic() - start
        headers: dict[str, str] = {}
        while (line := await asyncio.wait_for(reader.readline(), timeout)) not in (b"\r\n", b""):
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        payload = await asyncio.wait_for(
            reader.readexactly(int(headers["content-length"])), timeout
        )
        return int(status_line.split()[1]), json.loads(payload), elapsed
    finally:
        writer.close()


def _head(extra: str) -> str:
    return (
        f"POST {PATH} HTTP/1.1\r\nHost: gateway\r\nAuthorization: Bearer served-bearer\r\n"
        f"{extra}\r\n"
    )


def _gated_issuer(
    entered: asyncio.Semaphore, release: asyncio.Event
) -> Callable[[str], Awaitable[OidcClaims]]:
    async def verify(token: str) -> OidcClaims:
        entered.release()
        await release.wait()
        return OidcClaims(user_id="alice", team_id="t1")

    return verify


async def test_one_request_over_the_default_inflight_bound_gets_429() -> None:
    entered = asyncio.Semaphore(0)
    release = asyncio.Event()
    issuer = TokenIssuer(InMemoryTokenStore(), _gated_issuer(entered, release))
    inflight = 4  # CORP_GATEWAY_ISSUE_MAX_INFLIGHT's default

    async with (
        _served(_stack(issuer)) as port,
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as client,
    ):
        held = [
            asyncio.create_task(client.post(PATH, headers={"Authorization": f"Bearer b{i}"}))
            for i in range(inflight)
        ]
        for _ in range(inflight):
            await asyncio.wait_for(entered.acquire(), timeout=5)
        over = await asyncio.wait_for(
            client.post(PATH, headers={"Authorization": "Bearer over"}), timeout=2
        )
        release.set()
        statuses = sorted([over.status_code] + [r.status_code for r in await asyncio.gather(*held)])

    assert over.status_code == 429
    assert over.json() == {"error": "E_ISSUE_INFLIGHT"}
    assert statuses == [200, 200, 200, 200, 429]


async def test_an_oversized_body_is_refused_400_at_the_cap_not_the_deadline() -> None:
    issuer = TokenIssuer(InMemoryTokenStore(), _gated_issuer(asyncio.Semaphore(0), asyncio.Event()))

    async with _served(_stack(issuer)) as port:
        # Declares 10 MiB, sends 4 KiB, then stalls: the answer must come from the
        # 1 KiB cap, well before the 2 s deadline would fire.
        status, body, elapsed = await _raw_request(
            port, _head("Content-Length: 10485760\r\n"), b"a" * 4096
        )

    assert status == 400
    assert body == {"error": "E_ISSUE_BODY"}
    assert elapsed < 1.0


@pytest.mark.parametrize(
    "framing",
    ["Transfer-Encoding: chunked\r\n", "Content-Length: 16\r\n"],
    ids=["chunked", "content-length"],
)
async def test_a_slow_body_is_refused_408_at_the_two_second_deadline(framing: str) -> None:
    issuer = TokenIssuer(InMemoryTokenStore(), _gated_issuer(asyncio.Semaphore(0), asyncio.Event()))

    async with _served(_stack(issuer)) as port:
        status, body, elapsed = await _raw_request(port, _head(framing))

    assert status == 408
    assert body == {"error": "E_ISSUE_BODY_TIMEOUT"}
    assert 1.8 <= elapsed < 4.0


async def test_disabled_issuance_is_a_local_404_with_a_fallthrough_configured() -> None:
    reached: list[str] = []

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        reached.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"downstream"})

    async with (
        _served(_stack(None, fallthrough=downstream)) as port,
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client,
    ):
        post = await client.post(PATH, headers={"Authorization": "Bearer x"})
        get = await client.get(PATH)

    assert post.status_code == 404
    assert post.json() == {"error": "E_ISSUE_DISABLED"}
    # GET has no table row, so the gate refuses it before the router.
    assert get.status_code == 404
    assert reached == []


async def test_liveness_answers_within_100ms_while_the_jwks_endpoint_hangs() -> None:
    pytest.importorskip("cryptography")
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    from corp_llm_gateway.tokens import JwksClient, KeycloakOidcVerifier

    hung: list[asyncio.StreamWriter] = []

    async def hang(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        hung.append(writer)
        await reader.read(65536)
        await asyncio.sleep(3600)

    jwks_server = await asyncio.start_server(hang, "127.0.0.1", 0)
    jwks_port = jwks_server.sockets[0].getsockname()[1]
    issuer_url = f"http://127.0.0.1:{jwks_port}/realms/dev"
    verifier = KeycloakOidcVerifier(
        issuer=issuer_url,
        audience="corp-gateway-issuance",
        client_id="corp-gateway-cli",
        team_map=(("devs", "t1"),),
        jwks=JwksClient(f"{issuer_url}/certs", allow_insecure_http=True),
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    token = jwt.encode(
        {"iss": issuer_url, "sub": "s", "iat": now, "exp": now + 300},
        key,
        "RS256",
        headers={"kid": "kid-unknown", "typ": "JWT"},
    )
    issuer = TokenIssuer(InMemoryTokenStore(), verifier)

    try:
        async with (
            _served(_stack(issuer)) as port,
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as issuing,
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as probe,
        ):
            stuck = [
                asyncio.create_task(
                    issuing.post(PATH, headers={"Authorization": f"Bearer {token}"})
                )
                for _ in range(4)
            ]
            while not hung:
                await asyncio.sleep(0.01)
            latencies = []
            for _ in range(10):
                start = time.monotonic()
                live = await probe.get("/healthz/live")
                latencies.append(time.monotonic() - start)
                assert live.status_code == 200
            outcomes = await asyncio.gather(*stuck)
    finally:
        for writer in hung:
            writer.close()
        jwks_server.close()
        await verifier.aclose()

    assert max(latencies) < 0.1, latencies
    # The JWKS fetch is bounded too: the stuck requests end, as 503s.
    assert [r.status_code for r in outcomes] == [503] * 4
    assert {r.json()["error"] for r in outcomes} == {"E_JWKS_UNAVAILABLE"}

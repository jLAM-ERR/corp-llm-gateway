"""The issuance surface speaks in error codes: `E_` + upper-case words, one
exception family per code, passed through the route verbatim. Its log lines are
code-shaped too, and nothing on the path prints to stdout.

The code inventory is collected from the source (as the route-table collector
does), so a code added later is checked without editing this file.
"""

from __future__ import annotations

import ast
import functools
import logging
import re
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from corp_llm_gateway.healthz import build_health_router
from corp_llm_gateway.healthz.checks import (
    ExtensionsCheck,
    HealthStatus,
    LiveCheck,
    ReadyCheck,
    SanitizationCheck,
)
from corp_llm_gateway.tokens import (
    InMemoryTokenStore,
    IssuancePolicy,
    OidcClaims,
    OidcVerificationError,
    TokenIssuer,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "corp_llm_gateway"
ISSUANCE_SOURCES = (
    "tokens/oidc_verifier.py",
    "tokens/issuance.py",
    "tokens/issuance_policy.py",
    "tokens/errors.py",
    "tokens/in_memory.py",
    "tokens/postgres_store.py",
    "healthz/server.py",
    "bootstrap.py",
)
CODE_SHAPE = re.compile(r"E_[A-Z]+(?:_[A-Z]+)*")
FAMILIES = (
    "OidcVerificationError",
    "OidcTeamMappingError",
    "JwksUnavailableError",
    "IssuancePolicyError",
)
PATH = "/internal/issue-token"
_ISSUER = "https://kc.corp.lan/realms/dev"


def _trees() -> Iterator[tuple[str, ast.Module]]:
    for rel in ISSUANCE_SOURCES:
        yield rel, ast.parse((SRC / rel).read_text())


_IDENTIFIER_LIKE = re.compile(r"E_\w+")


def _codes_in_source() -> set[str]:
    return {
        node.value
        for _, tree in _trees()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _IDENTIFIER_LIKE.fullmatch(node.value)
    }


def _policy_constants() -> dict[str, str]:
    tree = ast.parse((SRC / "tokens/errors.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "IssuancePolicyError":
            return {
                stmt.targets[0].id: stmt.value.value
                for stmt in node.body
                if isinstance(stmt, ast.Assign)
                and isinstance(stmt.targets[0], ast.Name)
                and isinstance(stmt.value, ast.Constant)
            }
    raise AssertionError("IssuancePolicyError not found")


def _raised_codes_by_family() -> dict[str, set[str]]:
    """Codes each exception family is raised with: a literal first argument, an
    ``IssuancePolicyError.X`` constant, or a local ``code`` variable's literals."""
    policy = _policy_constants()
    found: dict[str, set[str]] = {family: set() for family in FAMILIES}
    for _, tree in _trees():
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            local_codes: dict[str, set[str]] = {}
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                    and node.value.value.startswith("E_")
                ):
                    local_codes.setdefault(node.targets[0].id, set()).add(node.value.value)
            for node in ast.walk(func):
                call = node.exc if isinstance(node, ast.Raise) else node
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id in FAMILIES
                    and call.args
                ):
                    continue
                arg = call.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value.startswith("E_"):
                        found[call.func.id].add(arg.value)
                elif isinstance(arg, ast.Attribute) and arg.attr in policy:
                    found[call.func.id].add(policy[arg.attr])
                elif isinstance(arg, ast.Name):
                    found[call.func.id] |= local_codes.get(arg.id, set())
    return found


def test_the_inventory_is_not_empty() -> None:
    found = _raised_codes_by_family()
    assert "E_OIDC_EXPIRED" in found["OidcVerificationError"]
    assert "E_ISSUE_UNKNOWN_TEAM" in found["OidcTeamMappingError"]
    assert "E_JWKS_UNAVAILABLE" in found["JwksUnavailableError"]
    assert {"E_ISSUE_RATE", "E_ISSUE_REPLAY", "E_ISSUE_BUSY"} <= found["IssuancePolicyError"]


def test_every_code_is_upper_case_words_after_e() -> None:
    codes = _codes_in_source()
    assert len(codes) >= 20
    assert sorted(c for c in codes if not CODE_SHAPE.fullmatch(c)) == []


def test_no_code_is_raised_by_two_exception_families() -> None:
    found = _raised_codes_by_family()
    for i, first in enumerate(FAMILIES):
        for second in FAMILIES[i + 1 :]:
            assert found[first].isdisjoint(found[second]), (first, second)


async def _ok() -> bool:
    return True


async def _ext() -> dict[str, HealthStatus]:
    return {}


def _router(verifier: object, **bounds: object) -> object:
    return build_health_router(
        live_check=LiveCheck(),
        ready_check=ReadyCheck(check_redis=_ok, check_postgres=_ok),
        sanitization_check=SanitizationCheck(run_round_trip=_ok),
        extensions_check=ExtensionsCheck(health_all=_ext),
        token_issuer=TokenIssuer(InMemoryTokenStore(), verifier),  # type: ignore[arg-type]
        **bounds,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("code", sorted(_codes_in_source()))
async def test_every_code_survives_the_route_verbatim(code: str) -> None:
    async def verify(token: str) -> OidcClaims:
        raise OidcVerificationError(code)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_router(verify)), base_url="http://gateway"
    ) as client:
        resp = await client.post(PATH, headers={"Authorization": "Bearer t"})

    assert resp.json() == {"error": code}


_LOG_SHAPES = (
    re.compile(r"issue_token status=\d{3} code=(ok|E_[A-Z_]+)"),
    re.compile(r"issuance token rejected: E_[A-Z_]+"),
    re.compile(r"issuance JWKS fetch failed: [A-Za-z0-9 ]+"),
)


@functools.cache
def _key() -> object:
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _real_router(*, jwks_up: bool, rate_per_minute: int = 30) -> object:
    import jwt

    from corp_llm_gateway.settings import IssuanceSettings
    from corp_llm_gateway.tokens import KeycloakOidcVerifier

    jwk = {
        **jwt.algorithms.RSAAlgorithm.to_jwk(_key().public_key(), as_dict=True),
        "kid": "kid-codes",
        "use": "sig",
        "alg": "RS256",
    }

    def _jwks(request: httpx.Request) -> httpx.Response:
        if not jwks_up:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"keys": [jwk]})

    configured = IssuanceSettings(
        issuer=_ISSUER,
        audience="corp-gateway-issuance",
        client_id="corp-gateway-cli",
        jwks_url=f"{_ISSUER}/certs",
        team_claim="groups",
        team_map=(("/devs", "t1"),),
        user_claim="preferred_username",
        operator_audience="",
        ca_bundle=None,
        token_ttl_days=30,
        max_active=2,
        min_interval_seconds=600,
        max_inflight=4,
        rate_per_minute=rate_per_minute,
    )
    verifier = KeycloakOidcVerifier.from_settings(
        configured, http=httpx.AsyncClient(transport=httpx.MockTransport(_jwks))
    )
    store = InMemoryTokenStore()
    return build_health_router(
        live_check=LiveCheck(),
        ready_check=ReadyCheck(check_redis=_ok, check_postgres=_ok),
        sanitization_check=SanitizationCheck(run_round_trip=_ok),
        extensions_check=ExtensionsCheck(health_all=_ext),
        token_issuer=TokenIssuer(store, verifier, policy=IssuancePolicy(store, configured)),
        issue_rate_per_minute=rate_per_minute,
    )


def _bearer(jti: str, **claims: object) -> str:
    import jwt

    now = int(time.time())
    body = {
        "iss": _ISSUER,
        "aud": "corp-gateway-issuance",
        "azp": "corp-gateway-cli",
        "sub": "sub-codes",
        "jti": jti,
        "iat": now,
        "exp": now + 300,
        "groups": ["/devs"],
        **claims,
    }
    return jwt.encode(body, _key(), "RS256", headers={"kid": "kid-codes", "typ": "JWT"})


async def _post(router: object, token: str | None, **kwargs: object) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=router), base_url="http://gateway"
    ) as client:
        resp = await client.post(PATH, headers=headers, **kwargs)  # type: ignore[arg-type]
    return resp.status_code


async def test_log_lines_are_code_shaped_and_stdout_stays_empty_on_every_path(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("cryptography")
    up = _real_router(jwks_up=True, rate_per_minute=9)
    down = _real_router(jwks_up=False)

    with caplog.at_level(logging.DEBUG, logger="corp_llm_gateway"):
        statuses = [
            await _post(up, _bearer("j1")),
            await _post(up, _bearer("j1")),
            await _post(up, _bearer("j2")),
            await _post(up, _bearer("j3", groups=["/other"])),
            await _post(up, _bearer("j4", aud="someone-else")),
            await _post(up, "not-a-jwt"),
            await _post(up, None),
            await _post(up, _bearer("j5"), content=b"x"),
            await _post(down, _bearer("j6")),
            await _post(up, _bearer("j7")),
            await _post(up, _bearer("j8")),
        ]

    assert statuses == [200, 403, 403, 403, 401, 401, 401, 400, 503, 403, 429]
    lines = [r.getMessage() for r in caplog.records if r.name.startswith("corp_llm_gateway")]
    assert len(lines) >= len(statuses)
    for line in lines:
        assert any(shape.fullmatch(line) for shape in _LOG_SHAPES), line
    assert capsys.readouterr().out == ""

"""Issuance through the real composition root: a token minted by
`bootstrap.build_health_router(issuance_schema_verified=True)` is accepted by the real guardrail's
`AuthMiddleware`, because both hold the same lazily built store. The router is
built as the entrypoint builds it after a boot whose schema check passed.

Signs real RS256 tokens and serves the JWKS from a local HTTP server, so it needs
`cryptography` (the 'oidc' extra) and skips on the venv that lacks it.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("cryptography")

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from corp_llm_gateway import bootstrap, config
from corp_llm_gateway.settings import ConfigError
from corp_llm_gateway.team_config import InMemoryTeamConfigStore, TeamConfig
from corp_llm_gateway.tokens import InMemoryTokenStore

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KID = "kid-composition"
_AUDIENCE = "corp-gateway-issuance"
_CLIENT_ID = "corp-gateway-cli"
_PATH = "/internal/issue-token"
_TEAM_MAP_TOML = '[CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP]\n"/devs/payments" = "payments"\n'
_ISSUE_KEYS = (
    "CORP_GATEWAY_ISSUE_OIDC_ISSUER",
    "CORP_GATEWAY_ISSUE_OIDC_AUDIENCE",
    "CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID",
    "CORP_GATEWAY_ISSUE_OIDC_JWKS_URL",
    "CORP_GATEWAY_ISSUE_MAX_INFLIGHT",
    "CORP_GATEWAY_OIDC_AUDIENCE",
)


@pytest.fixture(scope="module")
def jwks_url() -> Iterator[str]:
    document = json.dumps(
        {
            "keys": [
                {
                    **jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key(), as_dict=True),
                    "kid": _KID,
                    "use": "sig",
                    "alg": "RS256",
                }
            ]
        }
    ).encode()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(document)))
            self.end_headers()
            self.wfile.write(document)

        def log_message(self, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/realms/dev"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def shared(
    hermetic_gateway_config: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Fresh process singletons for every test; never leak one into the next."""
    for name in _ISSUE_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(bootstrap, "_guardrail", None)
    monkeypatch.setattr(bootstrap, "_token_store", None)
    monkeypatch.setattr(bootstrap, "_team_config_store", None)
    yield


def _enable_issuance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, issuer: str) -> None:
    cfg = tmp_path / "issuance.toml"
    cfg.write_text(_TEAM_MAP_TOML)
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_ISSUER", issuer)
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID", _CLIENT_ID)
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_JWKS_URL", f"{issuer}/certs")
    # Issuance refuses to run without Postgres; the stores are seeded in memory
    # below, so this DSN is never dialled.
    monkeypatch.setenv("CORP_LLM_PG_DSN", "postgresql://unused:unused@127.0.0.1:1/unused")
    config.reset_cache()


async def _seed_stores(*teams: str) -> tuple[InMemoryTokenStore, InMemoryTeamConfigStore]:
    tokens = InMemoryTokenStore()
    team_store = InMemoryTeamConfigStore()
    for team in teams:
        await team_store.upsert(TeamConfig(team_id=team, name=team))
    bootstrap._token_store = tokens
    bootstrap._team_config_store = team_store
    return tokens, team_store


def _token(issuer: str, *, groups: list[str], jti: str = "jti-composition-1") -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": issuer,
            "aud": _AUDIENCE,
            "azp": _CLIENT_ID,
            "sub": "sub-composition",
            "jti": jti,
            "iat": now,
            "exp": now + 300,
            "preferred_username": "alice.composition",
            "groups": groups,
        },
        _KEY,
        "RS256",
        headers={"kid": _KID, "typ": "JWT"},
    )


async def _issue(router: Any, bearer: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=router), base_url="http://gateway"
    ) as client:
        return await client.post(_PATH, headers={"Authorization": f"Bearer {bearer}"})


async def test_a_token_issued_by_the_router_authenticates_through_the_guardrail(
    shared: None, jwks_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_issuance(monkeypatch, tmp_path, jwks_url)
    tokens, _ = await _seed_stores("payments")

    router = bootstrap.build_health_router(issuance_schema_verified=True)
    # Building the routes must not build the guardrail (the entrypoint contract).
    assert bootstrap._guardrail is None

    resp = await _issue(router, _token(jwks_url, groups=["/devs/payments"]))
    assert resp.status_code == 200, resp.text
    corp_token = resp.json()["corp_token"]

    guardrail = bootstrap.guardrail
    assert guardrail._auth._store is tokens
    assert bootstrap.get_token_store() is tokens
    ctx = await guardrail._auth.authenticate(corp_token)
    assert (ctx.user_id, ctx.team_id) == ("alice.composition", "payments")


async def test_the_guardrail_and_the_router_share_the_team_config_store(
    shared: None, jwks_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_issuance(monkeypatch, tmp_path, jwks_url)
    _, teams = await _seed_stores("payments")

    bootstrap.build_health_router(issuance_schema_verified=True)

    assert bootstrap.get_team_config_store() is teams
    assert bootstrap.guardrail.orchestrator._team_store is teams  # type: ignore[attr-defined]


async def test_a_group_mapped_to_an_unknown_team_is_refused_403(
    shared: None, jwks_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The map names "payments", but no such team exists: the gateway never
    # creates teams from claims.
    _enable_issuance(monkeypatch, tmp_path, jwks_url)
    tokens, _ = await _seed_stores()

    resp = await _issue(
        bootstrap.build_health_router(issuance_schema_verified=True),
        _token(jwks_url, groups=["/devs/payments"]),
    )

    assert resp.status_code == 403
    assert resp.json() == {"error": "E_ISSUE_UNKNOWN_TEAM"}
    assert await tokens.list_tokens() == ()


async def test_an_unmapped_group_is_refused_403(
    shared: None, jwks_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_issuance(monkeypatch, tmp_path, jwks_url)
    await _seed_stores("payments")

    resp = await _issue(
        bootstrap.build_health_router(issuance_schema_verified=True),
        _token(jwks_url, groups=["/other"]),
    )

    assert resp.status_code == 403
    assert resp.json() == {"error": "E_ISSUE_NO_TEAM"}


async def test_the_router_applies_the_per_subject_policy(
    shared: None, jwks_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_issuance(monkeypatch, tmp_path, jwks_url)
    await _seed_stores("payments")
    router = bootstrap.build_health_router(issuance_schema_verified=True)
    bearer = _token(jwks_url, groups=["/devs/payments"])

    first = await _issue(router, bearer)
    replay = await _issue(router, bearer)
    too_soon = await _issue(router, _token(jwks_url, groups=["/devs/payments"], jti="jti-2"))

    assert first.status_code == 200
    assert (replay.status_code, replay.json()) == (403, {"error": "E_ISSUE_REPLAY"})
    assert (too_soon.status_code, too_soon.json()) == (403, {"error": "E_ISSUE_RATE"})


async def test_with_issuance_off_the_router_has_no_issuer_and_builds_no_store(
    shared: None,
) -> None:
    router = bootstrap.build_health_router(issuance_schema_verified=True)

    resp = await _issue(router, "anything")

    assert resp.status_code == 404
    assert resp.json() == {"error": "E_ISSUE_DISABLED"}
    assert bootstrap._token_store is None
    assert bootstrap._team_config_store is None
    assert bootstrap._guardrail is None


def test_issuance_without_postgres_is_refused_by_the_router_build(
    shared: None, jwks_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_issuance(monkeypatch, tmp_path, jwks_url)
    monkeypatch.delenv("CORP_LLM_PG_DSN")
    config.reset_cache()

    with pytest.raises(ConfigError, match="CORP_LLM_PG_DSN"):
        bootstrap.build_health_router(issuance_schema_verified=True)


def test_the_token_store_pool_is_sized_for_issuance_plus_auth(
    shared: None, jwks_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("asyncpg", reason="PostgresTokenStore requires the 'postgres' extra")
    from corp_llm_gateway.tokens import PostgresTokenStore

    _enable_issuance(monkeypatch, tmp_path, jwks_url)
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_MAX_INFLIGHT", "8")
    config.reset_cache()

    store = bootstrap.get_token_store()

    assert isinstance(store, PostgresTokenStore)
    assert store._pool_max_size == 8 + bootstrap.TOKEN_POOL_BASE_SIZE
    assert bootstrap.get_token_store() is store


def test_the_shared_stores_are_built_once_and_lazily(shared: None) -> None:
    assert bootstrap._token_store is None
    assert bootstrap._team_config_store is None

    tokens = bootstrap.get_token_store()
    teams = bootstrap.get_team_config_store()

    assert bootstrap.get_token_store() is tokens
    assert bootstrap.get_team_config_store() is teams
    assert isinstance(tokens, InMemoryTokenStore)
    assert isinstance(teams, InMemoryTeamConfigStore)


async def test_an_unknown_team_refusal_does_not_burn_the_jti(
    shared: None, jwks_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_issuance(monkeypatch, tmp_path, jwks_url)
    tokens, teams = await _seed_stores()
    router = bootstrap.build_health_router(issuance_schema_verified=True)
    bearer = _token(jwks_url, groups=["/devs/payments"])

    refused = await _issue(router, bearer)
    await teams.upsert(TeamConfig(team_id="payments", name="payments"))
    accepted = await _issue(router, bearer)

    assert refused.status_code == 403
    assert accepted.status_code == 200, accepted.text
    assert len(await tokens.list_tokens()) == 1


class _FlakyTeams(InMemoryTeamConfigStore):
    def __init__(self) -> None:
        super().__init__()
        self.down = True

    async def get(self, team_id: str) -> TeamConfig:
        if self.down:
            raise ConnectionError(f"team store unreachable while looking up {team_id}")
        return await super().get(team_id)


async def test_a_team_store_outage_is_a_503_that_stores_nothing_and_burns_nothing(
    shared: None,
    jwks_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _enable_issuance(monkeypatch, tmp_path, jwks_url)
    tokens = InMemoryTokenStore()
    teams = _FlakyTeams()
    await teams.upsert(TeamConfig(team_id="payments", name="payments"))
    bootstrap._token_store = tokens
    bootstrap._team_config_store = teams
    router = bootstrap.build_health_router(issuance_schema_verified=True)
    bearer = _token(jwks_url, groups=["/devs/payments"])

    with caplog.at_level("DEBUG"):
        outage = await _issue(router, bearer)
    teams.down = False
    recovered = await _issue(router, bearer)

    assert (outage.status_code, outage.json()) == (503, {"error": "E_ISSUE_STORE_UNAVAILABLE"})
    assert "payments" not in outage.text
    assert "team store unreachable" not in caplog.text
    assert bearer not in caplog.text
    assert recovered.status_code == 200, recovered.text
    assert len(await tokens.list_tokens()) == 1


async def test_the_config_file_map_order_decides_between_overlapping_groups(
    shared: None, jwks_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_issuance(monkeypatch, tmp_path, jwks_url)
    cfg = tmp_path / "issuance.toml"
    cfg.write_text(
        '[CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP]\n"/devs/zeta" = "zeta"\n"/devs/alpha" = "alpha"\n'
    )
    config.reset_cache()
    tokens, _ = await _seed_stores("zeta", "alpha")

    resp = await _issue(
        bootstrap.build_health_router(issuance_schema_verified=True),
        _token(jwks_url, groups=["/devs/alpha", "/devs/zeta"]),
    )

    assert resp.status_code == 200, resp.text
    (info,) = await tokens.list_tokens()
    assert info.team_id == "zeta"


async def test_the_router_takes_its_store_bound_from_settings(
    shared: None, jwks_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_issuance(monkeypatch, tmp_path, jwks_url)
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_STORE_TIMEOUT_SECONDS", "7")
    config.reset_cache()
    await _seed_stores("payments")

    router = bootstrap.build_health_router(issuance_schema_verified=True)
    try:
        assert router._issue_timeout_s == 7.0
    finally:
        await router.aclose()


async def test_the_default_store_bound_is_ten_seconds(
    shared: None, jwks_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_issuance(monkeypatch, tmp_path, jwks_url)
    await _seed_stores("payments")

    router = bootstrap.build_health_router(issuance_schema_verified=True)
    try:
        assert router._issue_timeout_s == 10.0
    finally:
        await router.aclose()


async def test_closing_the_router_closes_the_verifiers_jwks_client(
    shared: None, jwks_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_issuance(monkeypatch, tmp_path, jwks_url)
    await _seed_stores("payments")
    router = bootstrap.build_health_router(issuance_schema_verified=True)
    verifier = router._on_close.__self__  # type: ignore[union-attr]
    http = verifier._jwks._http

    ok = await _issue(router, _token(jwks_url, groups=["/devs/payments"]))
    assert not http.is_closed
    await router.aclose()

    assert ok.status_code == 200
    assert http.is_closed

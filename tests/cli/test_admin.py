import asyncio
import json
import logging
import re
import secrets
import socket
import sys
import types
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest

from corp_llm_gateway import config, settings
from corp_llm_gateway.cli.admin import build_parser, main
from corp_llm_gateway.extensions import Extension, ExtensionRegistry, ExtensionSpec
from corp_llm_gateway.healthz import HealthStatus
from corp_llm_gateway.team_config import InMemoryTeamConfigStore, TeamConfig
from corp_llm_gateway.tokens import AuthMiddleware, InMemoryTokenStore, TokenInfo
from tests.postgres_support import pg_dsn, require_asyncpg, skip_or_fail


@pytest.fixture(autouse=True)
def _bypass_rbac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "0")


@pytest.fixture
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> ExtensionRegistry:
    """Swap the shared extension REGISTRY for an empty one so `extensions`
    verbs (which populate it on demand) run isolated from other tests."""
    reg = ExtensionRegistry()
    monkeypatch.setattr("corp_llm_gateway.extensions.REGISTRY", reg)
    return reg


class _FakeExt(Extension):
    def __init__(self, spec: ExtensionSpec, *, healthy: bool) -> None:
        self.spec = spec
        self._healthy = healthy

    async def health(self) -> HealthStatus:
        return HealthStatus(self._healthy, "ok" if self._healthy else "boom")


def _register_fake(registry: ExtensionRegistry, *, healthy: bool, fail_policy: str) -> None:
    spec = ExtensionSpec(
        name="fake",
        kind="detector",
        version="1",
        api_version="1",
        fail_policy=fail_policy,  # type: ignore[arg-type]
    )
    registry.register(spec, lambda: _FakeExt(spec, healthy=healthy))


# ---------------------------------------------------------------------------
# team / token — store-backed verbs (in-memory store injected)
# ---------------------------------------------------------------------------


@pytest.fixture
def team_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryTeamConfigStore:
    store = InMemoryTeamConfigStore()
    monkeypatch.setattr("corp_llm_gateway.cli.admin._team_store", lambda: store)
    return store


@pytest.fixture
def token_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryTokenStore:
    store = InMemoryTokenStore()
    monkeypatch.setattr("corp_llm_gateway.cli.admin._token_store", lambda: store)
    return store


def _token_info(corp_token: str, user_id: str = "alice") -> TokenInfo:
    now = datetime.now(UTC)
    return TokenInfo(
        corp_token=corp_token,
        user_id=user_id,
        team_id="t1",
        scopes=("read",),
        issued_at=now,
        expires_at=now + timedelta(days=30),
    )


def test_team_create_persists(
    team_store: InMemoryTeamConfigStore, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["team", "create", "--team-id", "t1", "--name", "Team One"])
    assert rc == 0
    assert "team created: t1" in capsys.readouterr().out
    assert asyncio.run(team_store.get("t1")).name == "Team One"


def test_team_create_duplicate_errors(
    team_store: InMemoryTeamConfigStore, capsys: pytest.CaptureFixture[str]
) -> None:
    asyncio.run(team_store.upsert(TeamConfig(team_id="t1", name="Existing")))
    rc = main(["team", "create", "--team-id", "t1", "--name", "Dup"])
    assert rc == 2
    assert "already exists" in capsys.readouterr().err


def test_team_create_if_absent_leaves_an_existing_team_unchanged(
    team_store: InMemoryTeamConfigStore, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = TeamConfig(team_id="t1", name="Existing", replace_md_path="rules.md")
    asyncio.run(team_store.upsert(existing))

    rc = main(["team", "create", "--team-id", "t1", "--name", "Other", "--if-absent"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "team exists: t1 (unchanged)" in captured.out
    assert captured.err == ""
    assert asyncio.run(team_store.get("t1")) == existing

    assert main(["team", "create", "--team-id", "t1", "--name", "Other"]) == 2
    assert "already exists" in capsys.readouterr().err
    assert asyncio.run(team_store.get("t1")) == existing


def test_team_create_if_absent_creates_a_missing_team(
    team_store: InMemoryTeamConfigStore, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["team", "create", "--team-id", "t1", "--name", "Team One", "--if-absent"])
    assert rc == 0
    assert "team created: t1" in capsys.readouterr().out
    assert asyncio.run(team_store.get("t1")).name == "Team One"


def test_team_create_if_absent_is_rbac_gated(
    team_store: InMemoryTeamConfigStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "1")
    monkeypatch.delenv("CORP_GATEWAY_ADMIN_TOKEN", raising=False)
    rc = main(["team", "create", "--team-id", "t1", "--name", "X", "--if-absent"])
    assert rc == 2
    assert "gateway:operator" in capsys.readouterr().err
    assert not asyncio.run(team_store.list_all())


# db init — apply both store schemas -----------------------------------------


def test_db_init_without_a_dsn_names_the_key(
    hermetic_gateway_config: None, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["db", "init"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "CORP_LLM_PG_DSN" in captured.err
    assert captured.out == ""


def test_db_init_rbac_enforced(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "1")
    monkeypatch.delenv("CORP_GATEWAY_ADMIN_TOKEN", raising=False)
    rc = main(["db", "init"])
    assert rc == 2
    assert "gateway:operator" in capsys.readouterr().err


_DSN_PASSWORD = "dsn-secret-pw-8Kq3"


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize("shape", ["refused", "malformed-port", "unknown-host"])
def test_db_init_never_prints_the_dsn_on_a_connection_error(
    shape: str,
    hermetic_gateway_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    host = {
        "refused": f"127.0.0.1:{_closed_port()}",
        "malformed-port": "127.0.0.1:notaport",
        "unknown-host": "db-init-no-such-host.invalid:5432",
    }[shape]
    dsn = f"postgresql://gateway:{_DSN_PASSWORD}@{host}/gateway"
    monkeypatch.setenv("CORP_LLM_PG_DSN", dsn)
    rc = main(["db", "init"])
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    for secret in (dsn, _DSN_PASSWORD):
        assert secret not in captured.out
        assert secret not in captured.err
        assert secret not in caplog.text


def test_team_set_rules_updates_path(team_store: InMemoryTeamConfigStore) -> None:
    asyncio.run(team_store.upsert(TeamConfig(team_id="t1", name="One")))
    rc = main(["team", "set-rules", "--team-id", "t1", "--from-file", "/etc/rules/t1.md"])
    assert rc == 0
    assert asyncio.run(team_store.get("t1")).replace_md_path == "/etc/rules/t1.md"


def test_team_set_rules_unknown_team(
    team_store: InMemoryTeamConfigStore, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["team", "set-rules", "--team-id", "ghost", "--from-file", "r.md"])
    assert rc == 2
    assert "unknown team" in capsys.readouterr().err


def test_team_set_retention_persists(team_store: InMemoryTeamConfigStore) -> None:
    asyncio.run(team_store.upsert(TeamConfig(team_id="t1", name="One")))
    rc = main(["team", "set-retention", "--team-id", "t1", "--hot-days", "30", "--cold-years", "1"])
    assert rc == 0
    cfg = asyncio.run(team_store.get("t1"))
    assert cfg.retention_hot_days == 30
    assert cfg.retention_cold_years == 1


def test_team_list_renders(
    team_store: InMemoryTeamConfigStore, capsys: pytest.CaptureFixture[str]
) -> None:
    asyncio.run(team_store.upsert(TeamConfig(team_id="t1", name="One")))
    asyncio.run(team_store.upsert(TeamConfig(team_id="t2", name="Two")))
    rc = main(["team", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "TEAM_ID" in out
    assert "t1" in out and "t2" in out


def test_team_list_json(
    team_store: InMemoryTeamConfigStore, capsys: pytest.CaptureFixture[str]
) -> None:
    asyncio.run(team_store.upsert(TeamConfig(team_id="t1", name="One")))
    rc = main(["team", "list", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["team_id"] == "t1"
    assert data[0]["fail_policy"]["audit_buffer_full"] == "fail-closed"


def test_team_list_empty(
    team_store: InMemoryTeamConfigStore, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["team", "list"])
    assert rc == 0
    assert "no teams configured" in capsys.readouterr().out


def test_team_show(team_store: InMemoryTeamConfigStore, capsys: pytest.CaptureFixture[str]) -> None:
    asyncio.run(team_store.upsert(TeamConfig(team_id="t1", name="One", replace_md_path="/r.md")))
    rc = main(["team", "show", "--team-id", "t1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "team_id: t1" in out
    assert "/r.md" in out


def test_team_show_unknown(
    team_store: InMemoryTeamConfigStore, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["team", "show", "--team-id", "ghost"])
    assert rc == 2
    assert "unknown team" in capsys.readouterr().err


def test_token_issue_persists(
    token_store: InMemoryTokenStore, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["token", "issue", "--user", "alice", "--team", "t1", "--scopes", "read,write"])
    assert rc == 0
    assert "token: ct_" in capsys.readouterr().out
    tokens = asyncio.run(token_store.list_tokens("alice"))
    assert len(tokens) == 1
    assert tokens[0].team_id == "t1"
    assert tokens[0].scopes == ("read", "write")


def test_token_issue_json(
    token_store: InMemoryTokenStore, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["token", "issue", "--user", "alice", "--team", "t1", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["user_id"] == "alice"
    assert data["corp_token"].startswith("ct_")


def test_token_revoke_actually_revokes(
    token_store: InMemoryTokenStore, capsys: pytest.CaptureFixture[str]
) -> None:
    token_store.upsert(_token_info("ct-1", user_id="alice"))
    rc = main(["token", "revoke", "--user", "alice"])
    assert rc == 0
    assert "revoked 1 token(s) for user=alice" in capsys.readouterr().out
    got = asyncio.run(token_store.lookup("ct-1"))
    assert got is not None and got.revoked_at is not None


def test_token_list_masks_secret(
    token_store: InMemoryTokenStore, capsys: pytest.CaptureFixture[str]
) -> None:
    token_store.upsert(_token_info("ct_supersecretvalue", user_id="alice"))
    rc = main(["token", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "alice" in out
    assert "ct_supersecretvalue" not in out  # full secret never printed
    assert "secretvalue" not in out


@pytest.mark.parametrize("as_json", [False, True], ids=["table", "json"])
def test_token_list_masks_a_chosen_value_fully(
    token_store: InMemoryTokenStore, capsys: pytest.CaptureFixture[str], as_json: bool
) -> None:
    token_store.upsert(_token_info("local-team-token-7Qx2", user_id="alice"))
    token_store.upsert(_token_info("ct_supersecretvalue", user_id="bob"))
    assert main(["token", "list", *(["--json"] if as_json else [])]) == 0
    out = capsys.readouterr().out
    assert "local-te" not in out
    if as_json:
        masked = {row["user_id"]: row["token"] for row in json.loads(out)}
        assert masked == {"alice": "***", "bob": "ct_super…"}
    else:
        assert "***" in out
        assert "ct_super…" in out


def test_token_list_filters_by_user(
    token_store: InMemoryTokenStore, capsys: pytest.CaptureFixture[str]
) -> None:
    token_store.upsert(_token_info("ct-a", user_id="alice"))
    token_store.upsert(_token_info("ct-b", user_id="bob"))
    rc = main(["token", "list", "--user", "alice"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "alice" in out
    assert "bob" not in out


def test_token_list_empty(
    token_store: InMemoryTokenStore, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["token", "list"])
    assert rc == 0
    assert "no tokens issued" in capsys.readouterr().out


# token issue --value — a caller-chosen token value ----------------------------

_VALUE = "local-team-token-7Qx2"


def _issue_value(value: str = _VALUE, *extra: str) -> int:
    argv = ["token", "issue", "--user", "local", "--team", "local", "--value", value, *extra]
    return main(argv)


def _assert_value_absent(
    value: str, captured: pytest.CaptureResult[str], caplog: pytest.LogCaptureFixture
) -> None:
    assert value not in captured.out
    assert value not in captured.err
    assert value not in caplog.text


def test_token_issue_value_parses_apart_from_the_operator_jwt() -> None:
    args = build_parser().parse_args(
        ["--token", "op-jwt", "token", "issue", "--user", "u", "--team", "t", "--value", _VALUE]
    )
    assert args.token == "op-jwt"
    assert args.corp_token_value == _VALUE


def test_token_issue_without_value_leaves_it_unset() -> None:
    args = build_parser().parse_args(["token", "issue", "--user", "u", "--team", "t"])
    assert args.corp_token_value is None


def test_token_issue_value_stores_it_and_it_authenticates(
    token_store: InMemoryTokenStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _issue_value() == 0
    ctx = asyncio.run(AuthMiddleware(token_store).authenticate(_VALUE))
    assert (ctx.user_id, ctx.team_id) == ("local", "local")
    out = capsys.readouterr().out
    assert "issued corp token for user=local team=local" in out
    assert "expires: " in out
    assert "token:" not in out


def test_token_issue_value_of_exactly_16_chars_is_accepted(
    token_store: InMemoryTokenStore,
) -> None:
    value = "a" * 16
    assert _issue_value(value) == 0
    assert asyncio.run(token_store.lookup(value)) is not None


def test_token_issue_value_of_256_printable_ascii_chars_is_accepted(
    token_store: InMemoryTokenStore,
) -> None:
    printable = "".join(chr(c) for c in range(0x21, 0x7F))
    value = (printable * 3)[:256]
    assert _issue_value(value) == 0
    assert asyncio.run(token_store.lookup(value)) is not None


def test_token_issue_value_json_omits_the_value(
    token_store: InMemoryTokenStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _issue_value(_VALUE, "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"user_id", "team_id", "scopes", "expires_at"}
    assert (data["user_id"], data["team_id"]) == ("local", "local")
    stored = asyncio.run(token_store.lookup(_VALUE))
    assert stored is not None
    assert data["expires_at"] == stored.expires_at.isoformat()


def test_token_issue_value_twice_keeps_one_row_with_a_new_expiry(
    token_store: InMemoryTokenStore,
) -> None:
    assert _issue_value(_VALUE, "--ttl-days", "1") == 0
    first = asyncio.run(token_store.lookup(_VALUE))
    assert _issue_value(_VALUE, "--ttl-days", "36500") == 0
    rows = asyncio.run(token_store.list_tokens())
    assert [r.corp_token for r in rows] == [_VALUE]
    assert first is not None
    assert rows[0].expires_at > first.expires_at
    assert rows[0].revoked_at is None


def test_token_issue_value_refuses_a_revoked_value(
    token_store: InMemoryTokenStore,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    assert _issue_value() == 0
    asyncio.run(token_store.revoke_user("local"))
    capsys.readouterr()
    assert _issue_value() == 2
    captured = capsys.readouterr()
    assert "revoked" in captured.err
    _assert_value_absent(_VALUE, captured, caplog)
    stored = asyncio.run(token_store.lookup(_VALUE))
    assert stored is not None
    assert stored.revoked_at is not None


@pytest.mark.parametrize(
    ("user", "team"),
    [
        pytest.param("other", "local", id="other-user"),
        pytest.param("local", "other", id="other-team"),
    ],
)
def test_token_issue_value_refuses_a_value_another_owner_holds(
    token_store: InMemoryTokenStore,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    user: str,
    team: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    assert _issue_value() == 0
    before = asyncio.run(token_store.lookup(_VALUE))
    capsys.readouterr()
    argv = ["token", "issue", "--user", user, "--team", team, "--value", _VALUE]
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert "another user or team" in captured.err
    _assert_value_absent(_VALUE, captured, caplog)
    assert asyncio.run(token_store.lookup(_VALUE)) == before


_ISSUE_PARSER_ERROR = "gateway-admin token issue: error: "
_CHARSET = "argument --value: must be printable ASCII (0x21-0x7E)"


@pytest.mark.parametrize(
    ("value", "extra", "reason"),
    [
        pytest.param("tooShort-7Qx2ab", (), "--value: must be at least 16", id="15-chars"),
        pytest.param("", (), "--value: must be at least 16", id="empty"),
        pytest.param("b" * 257, (), "--value: must be at most 256", id="257-chars"),
        pytest.param("local team token 7Qx2", (), _CHARSET, id="space"),
        pytest.param("local-team\ttoken-7Qx2", (), _CHARSET, id="tab"),
        pytest.param("local-team-token\n7Qx2", (), _CHARSET, id="newline"),
        pytest.param("local-team-token\x077Qx2", (), _CHARSET, id="control-char"),
        pytest.param("local-team-token\u200b7Qx2", (), _CHARSET, id="zero-width-format-char"),
        pytest.param("локальный-токен-команды", (), _CHARSET, id="cyrillic"),
        pytest.param(
            "ct_local-team-token-7Qx2",
            (),
            "argument --value: must not start with 'ct_'",
            id="generated-token-prefix",
        ),
        pytest.param(_VALUE, ("--ttl-days", "0"), "--ttl-days: must be at least 1", id="ttl-zero"),
        pytest.param(
            _VALUE, ("--ttl-days", "-1"), "--ttl-days: must be at least 1", id="ttl-negative"
        ),
        pytest.param(
            _VALUE, ("--ttl-days", "3000000"), "--ttl-days: is too large", id="ttl-past-year-9999"
        ),
        pytest.param(
            _VALUE,
            ("--ttl-days", "99999999999"),
            "--ttl-days: is too large",
            id="ttl-past-timedelta-max",
        ),
    ],
)
def test_token_issue_value_usage_errors_never_echo_the_value(
    token_store: InMemoryTokenStore,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    value: str,
    extra: tuple[str, ...],
    reason: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(SystemExit) as excinfo:
        _issue_value(value, *extra)
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert _ISSUE_PARSER_ERROR in captured.err
    assert "usage: gateway-admin token issue" in captured.err
    assert reason in captured.err
    if value:
        _assert_value_absent(value, captured, caplog)
    assert asyncio.run(token_store.list_tokens()) == ()


@pytest.mark.parametrize("with_value", [True, False], ids=["with-value", "random"])
@pytest.mark.parametrize(
    ("ttl", "reason"),
    [
        pytest.param("0", "must be at least 1", id="zero"),
        pytest.param("-1", "must be at least 1", id="negative"),
        pytest.param("3000000", "is too large", id="past-year-9999"),
        pytest.param("99999999999", "is too large", id="past-timedelta-max"),
    ],
)
def test_token_issue_ttl_usage_errors_on_both_paths(
    token_store: InMemoryTokenStore,
    capsys: pytest.CaptureFixture[str],
    with_value: bool,
    ttl: str,
    reason: str,
) -> None:
    argv = ["token", "issue", "--user", "local", "--team", "local", "--ttl-days", ttl]
    if with_value:
        argv += ["--value", _VALUE]
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert f"{_ISSUE_PARSER_ERROR}argument --ttl-days: {reason}" in err
    assert "Traceback" not in err
    assert asyncio.run(token_store.list_tokens()) == ()


@pytest.mark.parametrize("path", ["plain", "json", "revoked", "no-store"])
def test_token_issue_value_never_reaches_stdout_stderr_or_logs(
    path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    store = InMemoryTokenStore()
    if path == "no-store":
        empty = tmp_path / "config.toml"
        empty.write_text("")
        monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(empty))
        monkeypatch.delenv("CORP_LLM_PG_DSN", raising=False)
    else:
        monkeypatch.setattr("corp_llm_gateway.cli.admin._token_store", lambda: store)
    config.reset_cache()
    try:
        if path == "revoked":
            assert _issue_value() == 0
            asyncio.run(store.revoke_user("local"))
        rc = _issue_value(_VALUE, "--json") if path == "json" else _issue_value()
    finally:
        config.reset_cache()
    assert rc == (0 if path in ("plain", "json") else 2)
    captured = capsys.readouterr()
    if path == "no-store":
        assert "CORP_LLM_PG_DSN" in captured.err
    _assert_value_absent(_VALUE, captured, caplog)


@pytest.fixture
def pg_cli(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str, str]]:
    """(dsn, user, value) for a CLI run against the test Postgres; rows removed after."""
    require_asyncpg()
    dsn = pg_dsn()
    user = f"pg-test-cli-{secrets.token_hex(4)}"
    value = f"pg-test-cli-value-{secrets.token_hex(8)}"

    async def _cleanup() -> None:
        from corp_llm_gateway.tokens import PostgresTokenStore

        store = PostgresTokenStore(dsn)
        try:
            await store.init_schema()
            pool = await store._get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM corp_tokens WHERE user_id = $1 OR corp_token = $2", user, value
                )
        finally:
            await store.close()

    try:
        asyncio.run(_cleanup())
    except Exception as exc:
        skip_or_fail(f"Postgres unreachable: {exc}")
    monkeypatch.setenv("CORP_LLM_PG_DSN", dsn)
    try:
        yield dsn, user, value
    finally:
        asyncio.run(_cleanup())


async def _pg_lookup(dsn: str, value: str, *, owner: tuple[str, str] | None) -> TokenInfo | None:
    from corp_llm_gateway.tokens import PostgresTokenStore

    store = PostgresTokenStore(dsn)
    try:
        if owner is not None:
            ctx = await AuthMiddleware(store).authenticate(value)
            assert (ctx.user_id, ctx.team_id) == owner
        return await store.lookup(value)
    finally:
        await store.close()


async def _pg_revoke(dsn: str, user: str) -> None:
    from corp_llm_gateway.tokens import PostgresTokenStore

    store = PostgresTokenStore(dsn)
    try:
        await store.revoke_user(user)
    finally:
        await store.close()


def test_token_issue_value_postgres_stores_authenticates_and_refuses_revoked(
    pg_cli: tuple[str, str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    dsn, user, value = pg_cli
    argv = ["token", "issue", "--user", user, "--team", "local", "--value", value]

    assert main([*argv, "--ttl-days", "1"]) == 0
    captured = capsys.readouterr()
    assert value not in captured.out + captured.err
    first = asyncio.run(_pg_lookup(dsn, value, owner=(user, "local")))
    assert first is not None
    assert first.user_id == user

    assert main([*argv, "--ttl-days", "36500", "--json"]) == 0
    captured = capsys.readouterr()
    assert value not in captured.out + captured.err
    second = asyncio.run(_pg_lookup(dsn, value, owner=(user, "local")))
    assert second is not None
    assert second.expires_at > first.expires_at

    asyncio.run(_pg_revoke(dsn, user))
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert "revoked" in captured.err
    assert value not in captured.out + captured.err
    stored = asyncio.run(_pg_lookup(dsn, value, owner=None))
    assert stored is not None
    assert stored.revoked_at is not None


def test_token_issue_value_postgres_refuses_a_value_another_owner_holds(
    pg_cli: tuple[str, str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    dsn, user, value = pg_cli
    assert main(["token", "issue", "--user", user, "--team", "local", "--value", value]) == 0
    before = asyncio.run(_pg_lookup(dsn, value, owner=(user, "local")))
    capsys.readouterr()
    for other_user, other_team in ((f"{user}-other", "local"), (user, "other")):
        argv = ["token", "issue", "--user", other_user, "--team", other_team, "--value", value]
        assert main(argv) == 2
        captured = capsys.readouterr()
        assert "another user or team" in captured.err
        assert value not in captured.out + captured.err
    assert asyncio.run(_pg_lookup(dsn, value, owner=(user, "local"))) == before


@pytest.fixture
def pg_empty_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """DSN of a freshly created, empty database on the test Postgres; dropped after."""
    require_asyncpg()
    import asyncpg

    admin_dsn = pg_dsn()
    name = f"cli_db_init_{secrets.token_hex(6)}"

    async def _admin(sql: str) -> None:
        conn = await asyncpg.connect(admin_dsn, timeout=5.0)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    try:
        asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    except Exception as exc:
        skip_or_fail(f"Postgres unreachable or CREATE DATABASE refused: {exc}")
    dsn = urlunsplit(urlsplit(admin_dsn)._replace(path=f"/{name}"))
    monkeypatch.setenv("CORP_LLM_PG_DSN", dsn)
    try:
        yield dsn
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


async def _pg_schema_snapshot(dsn: str) -> dict[str, list[tuple[Any, ...]]]:
    import asyncpg

    conn = await asyncpg.connect(dsn, timeout=5.0)
    try:
        columns = await conn.fetch(
            "SELECT table_name, column_name, data_type, column_default, is_nullable "
            "FROM information_schema.columns WHERE table_schema = 'public' "
            "ORDER BY table_name, column_name"
        )
        indexes = await conn.fetch(
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' ORDER BY indexname"
        )
        teams = await conn.fetch("SELECT team_id, name FROM team_config ORDER BY team_id")
        tokens = await conn.fetch("SELECT corp_token, user_id FROM corp_tokens ORDER BY 1")
    finally:
        await conn.close()
    return {
        "columns": [tuple(r) for r in columns],
        "indexes": [tuple(r) for r in indexes],
        "teams": [tuple(r) for r in teams],
        "tokens": [tuple(r) for r in tokens],
    }


async def _pg_table_names(dsn: str) -> set[str]:
    import asyncpg

    conn = await asyncpg.connect(dsn, timeout=5.0)
    try:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
    finally:
        await conn.close()
    return {r["table_name"] for r in rows}


def test_db_init_postgres_creates_both_schemas_and_reruns_without_change(
    pg_empty_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    dsn = pg_empty_db
    assert asyncio.run(_pg_table_names(dsn)) == set()

    assert main(["db", "init"]) == 0
    captured = capsys.readouterr()
    assert "db init: schema applied (corp_tokens, team_config)" in captured.out
    assert asyncio.run(_pg_table_names(dsn)) >= {"corp_tokens", "team_config"}

    assert main(["team", "create", "--team-id", "local", "--name", "local"]) == 0
    assert main(["token", "issue", "--user", "local", "--team", "local", "--value", _VALUE]) == 0
    before = asyncio.run(_pg_schema_snapshot(dsn))
    assert before["teams"] == [("local", "local")]
    assert len(before["tokens"]) == 1
    capsys.readouterr()

    assert main(["db", "init"]) == 0
    captured = capsys.readouterr()
    assert asyncio.run(_pg_schema_snapshot(dsn)) == before
    assert dsn not in captured.out + captured.err


def test_team_create_if_absent_postgres_is_idempotent(
    pg_empty_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    dsn = pg_empty_db
    assert main(["db", "init"]) == 0
    argv = ["team", "create", "--team-id", "local", "--name", "local", "--if-absent"]
    assert main(argv) == 0
    assert "team created: local" in capsys.readouterr().out
    assert main(["team", "set-rules", "--team-id", "local", "--from-file", "local.md"]) == 0
    retention = ["--hot-days", "30", "--cold-years", "2"]
    assert main(["team", "set-retention", "--team-id", "local", *retention]) == 0
    asyncio.run(
        _pg_execute(
            dsn,
            "UPDATE team_config SET fail_policy = $1::jsonb, profile_ids = $2::text[] "
            "WHERE team_id = 'local'",
            json.dumps({"pre_pass_down": "fail-closed", "audit_sink_down": "fail-closed"}),
            ["core"],
        )
    )
    before = asyncio.run(_pg_team_row(dsn, "local"))
    assert before["replace_md_path"] == "local.md"
    assert before["retention_hot_days"] == 30
    capsys.readouterr()

    other_name = ["team", "create", "--team-id", "local", "--name", "Other", "--if-absent"]
    assert main(other_name) == 0
    assert "team exists: local (unchanged)" in capsys.readouterr().out
    assert asyncio.run(_pg_team_row(dsn, "local")) == before
    assert main(other_name[:-1]) == 2
    assert "already exists" in capsys.readouterr().err
    assert asyncio.run(_pg_team_row(dsn, "local")) == before


async def _pg_execute(dsn: str, sql: str, *params: Any) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn, timeout=5.0)
    try:
        await conn.execute(sql, *params)
    finally:
        await conn.close()


async def _pg_team_row(dsn: str, team_id: str) -> dict[str, Any]:
    import asyncpg

    conn = await asyncpg.connect(dsn, timeout=5.0)
    try:
        row = await conn.fetchrow("SELECT * FROM team_config WHERE team_id = $1", team_id)
    finally:
        await conn.close()
    assert row is not None
    return dict(row)


def _run_concurrently(argv: list[str], n: int) -> list[int]:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(lambda _: main(argv), range(n)))


@pytest.mark.parametrize("state", ["fresh", "existing"])
def test_db_init_postgres_concurrent_runs_all_succeed(
    state: str, pg_empty_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    if state == "existing":
        assert main(["db", "init"]) == 0
    for _ in range(3):
        assert _run_concurrently(["db", "init"], 6) == [0] * 6
    assert "db init failed" not in capsys.readouterr().err
    assert asyncio.run(_pg_table_names(pg_empty_db)) >= {"corp_tokens", "team_config"}


def test_db_init_postgres_gives_up_on_a_held_table_lock(
    pg_empty_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import threading
    import time

    import asyncpg

    from corp_llm_gateway.cli import admin

    assert main(["db", "init"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(admin, "_DB_INIT_DDL_LOCK_TIMEOUT_MS", 1000)
    held = threading.Event()
    release = threading.Event()

    def _hold_lock() -> None:
        async def _hold() -> None:
            conn = await asyncpg.connect(pg_empty_db, timeout=5.0)
            try:
                async with conn.transaction():
                    await conn.execute("LOCK TABLE corp_tokens IN ACCESS SHARE MODE")
                    held.set()
                    while not release.is_set():
                        await asyncio.sleep(0.05)
            finally:
                await conn.close()

        asyncio.run(_hold())

    holder = threading.Thread(target=_hold_lock)
    holder.start()
    try:
        assert held.wait(10)
        result: list[int] = []
        runner = threading.Thread(target=lambda: result.append(main(["db", "init"])))
        started = time.monotonic()
        runner.start()
        runner.join(30)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(10)
    assert not runner.is_alive(), "db init kept waiting on the table lock"
    assert result == [2]
    assert elapsed < 10
    captured = capsys.readouterr()
    assert "error: db init failed: LockNotAvailableError" in captured.err
    assert pg_empty_db not in captured.out + captured.err


def test_db_init_postgres_failure_after_connect_closes_and_names_the_type_only(
    pg_empty_db: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncpg

    from corp_llm_gateway.cli import admin
    from corp_llm_gateway.team_config import PostgresTeamConfigStore

    caplog.set_level(logging.DEBUG)
    dsn = pg_empty_db
    opened: list[Any] = []
    real_connect = asyncpg.connect

    async def _spy_connect(*args: Any, **kwargs: Any) -> Any:
        conn = await real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    async def _no_pool(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("db init must not open a pool")

    async def _broken_init_schema(self: Any, conn: Any = None) -> None:
        await conn.execute("SELECT 1")
        raise asyncpg.exceptions.UndefinedTableError(f"boom near {dsn}")

    monkeypatch.setattr(asyncpg, "connect", _spy_connect)
    monkeypatch.setattr(asyncpg, "create_pool", _no_pool)
    monkeypatch.setattr(PostgresTeamConfigStore, "init_schema", _broken_init_schema)

    assert main(["db", "init"]) == 2
    captured = capsys.readouterr()
    assert captured.err == "error: db init failed: UndefinedTableError\n"
    assert dsn not in captured.out + captured.err
    assert dsn not in caplog.text
    assert opened
    assert all(conn.is_closed() for conn in opened)
    monkeypatch.setattr(asyncpg, "connect", real_connect)

    async def _lock_is_free() -> bool:
        conn = await real_connect(dsn, timeout=5.0)
        try:
            return bool(
                await conn.fetchval("SELECT pg_try_advisory_lock($1)", admin._DB_INIT_LOCK_KEY)
            )
        finally:
            await conn.close()

    assert asyncio.run(_lock_is_free())


def test_db_init_postgres_leaves_no_session_state_behind(pg_empty_db: str) -> None:
    import asyncpg

    from corp_llm_gateway.cli import admin

    async def _run() -> None:
        conn = await asyncpg.connect(pg_empty_db, timeout=5.0)
        try:
            default = await conn.fetchval("SHOW lock_timeout")
            await admin._apply_schemas(conn, pg_empty_db)
            assert await conn.fetchval("SHOW lock_timeout") == default
            assert not conn.is_in_transaction()
            held = await conn.fetchval(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
            )
            assert held == 0
            tables = await conn.fetch(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
            assert {"corp_tokens", "team_config"} <= {r["table_name"] for r in tables}
        finally:
            await conn.close()

    asyncio.run(_run())


def test_db_init_postgres_gives_up_waiting_for_another_db_init(
    pg_empty_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import threading
    import time

    import asyncpg

    from corp_llm_gateway.cli import admin

    monkeypatch.setattr(admin, "_DB_INIT_LOCK_WAIT_MS", 500)
    held = threading.Event()
    release = threading.Event()

    def _hold_key() -> None:
        async def _hold() -> None:
            conn = await asyncpg.connect(pg_empty_db, timeout=5.0)
            try:
                await conn.execute("SELECT pg_advisory_lock($1)", admin._DB_INIT_LOCK_KEY)
                held.set()
                while not release.is_set():
                    await asyncio.sleep(0.05)
            finally:
                await conn.close()

        asyncio.run(_hold())

    holder = threading.Thread(target=_hold_key)
    holder.start()
    try:
        assert held.wait(10)
        started = time.monotonic()
        rc = main(["db", "init"])
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(10)
    assert rc == 2
    assert elapsed < 10
    captured = capsys.readouterr()
    assert captured.err == (
        "error: db init failed: another db init holds the lock (LockNotAvailableError)\n"
    )
    assert asyncio.run(_pg_table_names(pg_empty_db)) == set()


def test_db_init_names_a_pooler_rejecting_startup_parameters(
    hermetic_gateway_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    require_asyncpg()
    from tests.postgres_support import RejectingPgBouncer

    bouncer = RejectingPgBouncer()
    try:
        dsn = f"postgresql://gateway:{_DSN_PASSWORD}@127.0.0.1:{bouncer.port}/gateway"
        monkeypatch.setenv("CORP_LLM_PG_DSN", dsn)
        rc = main(["db", "init"])
    finally:
        bouncer.close()
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.err == "error: db init failed: StartupParameterRejectedError\n"
    assert _DSN_PASSWORD not in captured.out + captured.err


_SCHEMA_KNOWN_TABLES = frozenset({"corp_tokens", "team_config"})
_SQL_TABLE = r'(?:ONLY\s+)?(?:"?\w+"?\.)?"?(?P<table>\w+)"?'
# Every statement a schema file may hold, with the table it locks (None: no table).
_SCHEMA_STATEMENT_SHAPES = (
    rf"CREATE TABLE IF NOT EXISTS {_SQL_TABLE} \(.*\)",
    rf"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS \w+ ON {_SQL_TABLE} \(.*\)(?: WHERE .*)?",
    rf"ALTER TABLE {_SQL_TABLE} ADD COLUMN IF NOT EXISTS [^,]+",
    r"CREATE OR REPLACE FUNCTION \w+\(\) RETURNS TRIGGER AS \$\$.*\$\$ LANGUAGE plpgsql",
    rf"DROP TRIGGER IF EXISTS \w+ ON {_SQL_TABLE}",
    rf"CREATE TRIGGER \w+ (?:BEFORE|AFTER) (?:INSERT|UPDATE|DELETE) ON {_SQL_TABLE}"
    r" FOR EACH ROW EXECUTE FUNCTION \w+\(\)",
)


def _sql_statements(sql: str) -> list[str]:
    """Split on `;` outside quotes and $$ bodies, dropping `--` comments."""
    statements: list[str] = []
    current: list[str] = []
    i, quote, dollar = 0, False, False
    while i < len(sql):
        ch = sql[i]
        if not quote and not dollar and sql.startswith("--", i):
            i = sql.find("\n", i) if "\n" in sql[i:] else len(sql)
            continue
        if not quote and sql.startswith("$$", i):
            dollar = not dollar
            current.append("$$")
            i += 2
            continue
        if not dollar and ch == "'":
            quote = not quote
        if ch == ";" and not quote and not dollar:
            statements.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    statements.append("".join(current))
    return [" ".join(stmt.split()) for stmt in statements if stmt.strip()]


def _schema_tables(sql: str) -> set[str]:
    tables: set[str] = set()
    for statement in _sql_statements(sql):
        for shape in _SCHEMA_STATEMENT_SHAPES:
            match = re.fullmatch(shape, statement, flags=re.IGNORECASE | re.DOTALL)
            if match is None:
                continue
            table = match.groupdict().get("table")
            if table is None or table.lower() in _SCHEMA_KNOWN_TABLES:
                if table is not None:
                    tables.add(table.lower())
                break
        else:
            raise ValueError(
                f"unrecognised schema statement {statement[:80]!r}: re-check the db init "
                "lock bound (admin._DB_INIT_MAX_TABLES_PER_TXN) and extend this allow-list"
            )
    return tables


def test_db_init_ddl_lock_bound_stays_under_the_token_lookup_timeout() -> None:
    import corp_llm_gateway.team_config as team_config_pkg
    import corp_llm_gateway.tokens as tokens_pkg
    from corp_llm_gateway.cli import admin
    from corp_llm_gateway.tokens.postgres_store import LOOKUP_TIMEOUT_S

    per_file = {
        pkg.__name__: _schema_tables((Path(pkg.__file__).parent / "schema.sql").read_text())
        for pkg in (tokens_pkg, team_config_pkg)
    }
    assert per_file["corp_llm_gateway.tokens"] == {"corp_tokens", "team_config"}
    assert per_file["corp_llm_gateway.team_config"] == {"team_config"}
    assert max(len(t) for t in per_file.values()) <= admin._DB_INIT_MAX_TABLES_PER_TXN
    # A lookup can queue behind each table lock the transaction waits for in turn.
    worst_stall_s = admin._DB_INIT_MAX_TABLES_PER_TXN * admin._DB_INIT_DDL_LOCK_TIMEOUT_MS / 1000
    assert worst_stall_s <= 0.75 * LOOKUP_TIMEOUT_S


@pytest.mark.parametrize(
    ("sql", "tables"),
    [
        pytest.param(
            'ALTER TABLE ONLY public."corp_tokens" ADD COLUMN IF NOT EXISTS x TEXT;',
            {"corp_tokens"},
            id="quoted-qualified-only",
        ),
        pytest.param(
            "-- a; comment\nCREATE INDEX IF NOT EXISTS i ON team_config (name) WHERE x = ';';",
            {"team_config"},
            id="comment-and-quoted-semicolon",
        ),
    ],
)
def test_schema_statement_allow_list_extracts_the_table(sql: str, tables: set[str]) -> None:
    assert _schema_tables(sql) == tables


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param('ALTER TABLE "audit" ADD COLUMN IF NOT EXISTS x TEXT;', id="unknown-table"),
        pytest.param(
            "ALTER TABLE corp_tokens ADD COLUMN IF NOT EXISTS a TEXT, DROP COLUMN b;",
            id="extra-alter-clause",
        ),
        pytest.param("DROP INDEX corp_tokens_user_id_idx;", id="drop-index"),
        pytest.param("TRUNCATE corp_tokens;", id="truncate"),
        pytest.param("LOCK TABLE team_config IN ACCESS EXCLUSIVE MODE;", id="lock"),
        pytest.param("UPDATE corp_tokens SET revoked_at = now();", id="update"),
        pytest.param("INSERT INTO team_config (team_id, name) VALUES ('a', 'b');", id="insert"),
    ],
)
def test_schema_statement_allow_list_refuses_an_unknown_shape(sql: str) -> None:
    with pytest.raises(ValueError, match="re-check the db init lock bound"):
        _schema_tables(sql)


async def _poll(conn: Any, sql: str, *, within_s: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + within_s
    while not await conn.fetchval(sql):
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for: {sql}")
        await asyncio.sleep(0.01)


def _lock_sql(table: str, mode: str, *, granted: bool, pid: str) -> str:
    return (
        f"SELECT EXISTS (SELECT 1 FROM pg_locks WHERE relation = '{table}'::regclass "
        f"AND mode = '{mode}' AND granted = {str(granted).lower()} AND pid {pid})"
    )


def test_db_init_postgres_never_stalls_a_token_lookup_past_its_timeout(pg_empty_db: str) -> None:
    import time

    import asyncpg

    from corp_llm_gateway.cli import admin
    from corp_llm_gateway.tokens import PostgresTokenStore
    from corp_llm_gateway.tokens.postgres_store import LOOKUP_TIMEOUT_S

    dsn = pg_empty_db
    assert main(["db", "init"]) == 0

    async def _run() -> float:
        holders = []
        for table in ("corp_tokens", "team_config"):
            conn = await asyncpg.connect(dsn, timeout=5.0)
            tx = conn.transaction()
            await tx.start()
            await conn.fetch(f"SELECT 1 FROM {table} LIMIT 1")
            holders.append((conn, tx))
        monitor = await asyncpg.connect(dsn, timeout=5.0)
        store = PostgresTokenStore(dsn)
        await store._get_pool()
        init = await asyncpg.connect(dsn, timeout=5.0)
        pid = f"= {init.get_server_pid()}"
        try:
            task = asyncio.create_task(admin._apply_schemas(init, dsn))
            # 1. db init queues for ACCESS EXCLUSIVE on corp_tokens.
            await _poll(
                monitor, _lock_sql("corp_tokens", "AccessExclusiveLock", granted=False, pid=pid)
            )
            started = time.monotonic()
            lookup = asyncio.create_task(store.lookup("no-such-token"))
            # 2. The lookup queues behind it.
            await _poll(
                monitor,
                _lock_sql("corp_tokens", "AccessShareLock", granted=False, pid=f"<> {pid[2:]}"),
            )
            await holders[0][1].rollback()
            # 3. db init holds corp_tokens and queues for team_config, the lookup still behind.
            await _poll(
                monitor, _lock_sql("corp_tokens", "AccessExclusiveLock", granted=True, pid=pid)
            )
            await _poll(
                monitor, _lock_sql("team_config", "AccessExclusiveLock", granted=False, pid=pid)
            )
            assert not lookup.done()
            assert await lookup is None
            elapsed = time.monotonic() - started
            with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
                await task
        finally:
            await holders[1][1].rollback()
            for conn, _ in holders:
                await conn.close()
            for conn in (init, monitor):
                await conn.close()
            await store.close()
        return elapsed

    assert asyncio.run(_run()) < LOOKUP_TIMEOUT_S


def _close_fails_on(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every connection db init opens fails its close(); returns those connections."""
    import asyncpg

    opened: list[Any] = []
    real_connect = asyncpg.connect
    real_close = asyncpg.connection.Connection.close

    async def _spy_connect(*args: Any, **kwargs: Any) -> Any:
        conn = await real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    async def _failing_close(self: Any, *, timeout: float | None = None) -> None:
        if any(self is conn for conn in opened):
            raise OSError("close failed")
        await real_close(self, timeout=timeout)

    monkeypatch.setattr(asyncpg, "connect", _spy_connect)
    monkeypatch.setattr(asyncpg.connection.Connection, "close", _failing_close)
    return opened


def test_db_init_postgres_close_failure_after_success_still_succeeds(
    pg_empty_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    try:
        opened = _close_fails_on(monkeypatch)
        assert main(["db", "init"]) == 0
        captured = capsys.readouterr()
        assert "db init: schema applied (corp_tokens, team_config)" in captured.out
        assert captured.err == ""
        assert opened
        assert all(conn.is_closed() for conn in opened)
    finally:
        monkeypatch.undo()  # the fixture's teardown connections close normally


def test_db_init_postgres_close_failure_after_failure_reports_the_first_error(
    pg_empty_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncpg

    from corp_llm_gateway.team_config import PostgresTeamConfigStore

    async def _broken_init_schema(self: Any, conn: Any = None) -> None:
        raise asyncpg.exceptions.UndefinedTableError("boom")

    try:
        monkeypatch.setattr(PostgresTeamConfigStore, "init_schema", _broken_init_schema)
        opened = _close_fails_on(monkeypatch)
        assert main(["db", "init"]) == 2
        assert capsys.readouterr().err == "error: db init failed: UndefinedTableError\n"
        assert opened
        assert all(conn.is_closed() for conn in opened)
    finally:
        monkeypatch.undo()  # the fixture's teardown connections close normally


def test_db_init_postgres_busy_on_the_second_transaction(
    pg_empty_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncpg

    from corp_llm_gateway.cli import admin
    from corp_llm_gateway.team_config import PostgresTeamConfigStore

    dsn = pg_empty_db
    monkeypatch.setattr(admin, "_DB_INIT_LOCK_WAIT_MS", 500)
    real_apply = admin._apply_schema
    applied: list[str] = []

    async def _apply_with_the_key_taken(conn: Any, store: Any) -> None:
        if not isinstance(store, PostgresTeamConfigStore):
            await real_apply(conn, store)
            applied.append(type(store).__name__)
            return
        holder = await asyncpg.connect(dsn, timeout=5.0)
        try:
            await holder.execute(f"SELECT pg_advisory_lock({admin._DB_INIT_LOCK_KEY})")
            await real_apply(conn, store)
        finally:
            await holder.close()

    monkeypatch.setattr(admin, "_apply_schema", _apply_with_the_key_taken)
    assert main(["db", "init"]) == 2
    assert capsys.readouterr().err == (
        "error: db init failed: another db init holds the lock (LockNotAvailableError)\n"
    )
    assert applied == ["PostgresTokenStore"]
    assert "corp_tokens" in asyncio.run(_pg_table_names(dsn))


def test_db_init_with_a_broken_asyncpg_names_the_extra(
    hermetic_gateway_config: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    broken = tmp_path / "broken-site" / "asyncpg"
    broken.mkdir(parents=True)
    (broken / "__init__.py").write_text("raise ImportError('broken asyncpg build')\n")
    monkeypatch.syspath_prepend(str(broken.parent))
    monkeypatch.delitem(sys.modules, "asyncpg", raising=False)
    dsn = f"postgresql://gateway:{_DSN_PASSWORD}@127.0.0.1:{_closed_port()}/gateway"
    monkeypatch.setenv("CORP_LLM_PG_DSN", dsn)
    assert main(["db", "init"]) == 2
    captured = capsys.readouterr()
    assert "install the 'postgres' extra" in captured.err
    assert _DSN_PASSWORD not in captured.out + captured.err


# team / token — RBAC (mutations gated, reads ungated) ----------------------


def test_team_create_rbac_enforced(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "1")  # override autouse bypass
    monkeypatch.delenv("CORP_GATEWAY_ADMIN_TOKEN", raising=False)
    rc = main(["team", "create", "--team-id", "t1", "--name", "X"])
    assert rc == 2
    assert "gateway:operator" in capsys.readouterr().err


def test_token_revoke_rbac_enforced(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "1")
    monkeypatch.delenv("CORP_GATEWAY_ADMIN_TOKEN", raising=False)
    rc = main(["token", "revoke", "--user", "alice"])
    assert rc == 2
    assert "gateway:operator" in capsys.readouterr().err


def test_team_list_read_verb_skips_rbac(
    team_store: InMemoryTeamConfigStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "1")  # enforce; read verb must still run
    assert main(["team", "list"]) == 0


def test_token_list_read_verb_skips_rbac(
    token_store: InMemoryTokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "1")
    assert main(["token", "list"]) == 0


def test_team_backend_not_configured_errors(
    hermetic_gateway_config: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # No CORP_LLM_PG_DSN and no injected store: fail clearly, never fake success.
    rc = main(["team", "create", "--team-id", "t1", "--name", "X"])
    assert rc == 2
    assert "CORP_LLM_PG_DSN" in capsys.readouterr().err


def test_token_backend_not_configured_errors(
    hermetic_gateway_config: None, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["token", "list"])
    assert rc == 2
    assert "CORP_LLM_PG_DSN" in capsys.readouterr().err


def test_missing_required_arg_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["team", "create", "--team-id", "t1"])
    assert excinfo.value.code != 0


def test_no_command_errors() -> None:
    with pytest.raises(SystemExit):
        main([])


# ---------------------------------------------------------------------------
# extensions — read verbs (no RBAC)
# ---------------------------------------------------------------------------


def test_extensions_list_renders(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "FAIL-POLICY" in out
    for token in ("provider", "anthropic", "openai", "corp-vllm", "audit_sink", "stdout"):
        assert token in out


def test_extensions_list_json(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "list", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    names = {entry["name"] for entry in data}
    assert {"anthropic", "openai", "corp-vllm", "stdout"} <= names
    assert set(data[0]) == {"kind", "name", "version", "api_version", "fail_policy"}


def test_extensions_list_kind_filter(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "list", "--kind", "provider"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "anthropic" in out
    assert "stdout" not in out  # audit_sink filtered out


def test_extensions_list_unknown_kind_rejected() -> None:
    with pytest.raises(SystemExit):
        main(["extensions", "list", "--kind", "bogus"])


def test_extensions_inspect_provider(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "inspect", "provider:anthropic"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "anthropic" in out
    assert "upstream" in out
    assert "wire_format" in out


def test_extensions_inspect_json(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "inspect", "provider:anthropic", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["kind"] == "provider"
    assert data["role"] == "upstream"
    assert data["wire_format"] == "anthropic"
    assert "capabilities" in data


def test_extensions_inspect_audit_sink(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "inspect", "audit_sink:stdout"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stdout" in out
    assert "continue" in out  # audit sink fail_policy


def test_extensions_inspect_bad_ref(
    fresh_registry: ExtensionRegistry,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "inspect", "bogus"])
    assert rc == 2
    assert "KIND:NAME" in capsys.readouterr().err


def test_extensions_inspect_unknown(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "inspect", "provider:nope"])
    assert rc == 2
    assert "Unknown extension" in capsys.readouterr().err


def test_extensions_health_ok(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "health"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "anthropic" in out
    assert "OK" in out


def test_extensions_health_json(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "health", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["healthy"] is True
    assert data["extensions"]


def test_extensions_health_unhealthy_fail_closed_exits_nonzero(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _register_fake(fresh_registry, healthy=False, fail_policy="fail-closed")
    rc = main(["extensions", "health"])
    assert rc != 0
    assert "UNHEALTHY" in capsys.readouterr().out


def test_extensions_health_unhealthy_continue_stays_zero(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
) -> None:
    _register_fake(fresh_registry, healthy=False, fail_policy="continue")
    rc = main(["extensions", "health"])
    assert rc == 0  # a `continue`-policy ext being down never fails the probe


def test_extensions_read_verb_skips_rbac(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "1")  # enforce; read verb must still run
    assert main(["extensions", "list"]) == 0


# ---------------------------------------------------------------------------
# extensions — mutating verbs (RBAC-gated + persistence stub)
# ---------------------------------------------------------------------------


def test_extensions_enable_rbac_enforced(
    fresh_registry: ExtensionRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "1")  # override the autouse bypass
    monkeypatch.delenv("CORP_GATEWAY_ADMIN_TOKEN", raising=False)
    rc = main(["extensions", "enable", "audit_sink:stdout"])
    assert rc == 2
    assert "gateway:operator" in capsys.readouterr().err


def test_extensions_enable_stub_raises(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
) -> None:
    with pytest.raises(NotImplementedError, match="extension-state store"):
        main(["extensions", "enable", "audit_sink:stdout"])


def test_extensions_disable_stub_raises(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
) -> None:
    with pytest.raises(NotImplementedError, match="extension-state store"):
        main(["extensions", "disable", "provider:anthropic"])


def test_extensions_enable_unknown_target(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "enable", "provider:nope"])
    assert rc == 2
    assert "Unknown extension" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# config check — validate settings + probe dependencies
# ---------------------------------------------------------------------------


async def _probe_ok(_arg: str) -> tuple[bool, str]:
    return True, "reachable"


async def _probe_down(_arg: str) -> tuple[bool, str]:
    return False, "unreachable: ConnectError"


def test_config_check_ok(
    hermetic_gateway_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.corp.lan/v1")
    rc = main(["config", "check", "--no-probe"])
    assert rc == 0
    assert "config: OK" in capsys.readouterr().out


def test_config_check_missing_endpoint_exits_nonzero(
    hermetic_gateway_config: None, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["config", "check", "--no-probe"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "config: INVALID" in err
    assert "CORP_LLM_ENDPOINT" in err


def test_config_check_malformed_value_exits_nonzero(
    hermetic_gateway_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_OVERSIZE_POLICY", "banana")
    rc = main(["config", "check", "--no-probe"])
    assert rc == 1
    assert "CORP_LLM_OVERSIZE_POLICY" in capsys.readouterr().err


def test_config_check_json_ok(
    hermetic_gateway_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    rc = main(["config", "check", "--no-probe", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["config_valid"] is True
    assert data["healthy"] is True
    assert data["probes"] == []


def test_config_check_json_invalid(
    hermetic_gateway_config: None, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["config", "check", "--no-probe", "--json"])
    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["config_valid"] is False
    assert data["healthy"] is False
    assert any("CORP_LLM_ENDPOINT" in p for p in data["problems"])


def test_config_check_probe_unreachable_exits_nonzero(
    hermetic_gateway_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setattr("corp_llm_gateway.cli.admin._probe_corp_llm", _probe_down)
    rc = main(["config", "check"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "UNREACHABLE" in out
    assert "corp-llm" in out


def test_config_check_probe_ok_only_configured_deps(
    hermetic_gateway_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # only CORP_LLM_ENDPOINT is configured (no PG/Redis), so only corp-llm is probed.
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setattr("corp_llm_gateway.cli.admin._probe_corp_llm", _probe_ok)
    rc = main(["config", "check", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert [p["dependency"] for p in data["probes"]] == ["corp-llm"]
    assert data["healthy"] is True


def test_config_check_routes_prints_the_effective_table(
    hermetic_gateway_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH", "GET /internal/ops-status")

    rc = main(["config", "check", "--no-probe", "--routes"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "route gate:" in out
    # The eight generation spellings are the operator-visible claim: everything
    # else either carries no user text or is refused.
    assert "POST /v1/messages" in out
    assert "GET /internal/ops-status  PASSTHROUGH" in out


def test_config_check_routes_without_extras_says_none(
    hermetic_gateway_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")

    rc = main(["config", "check", "--no-probe", "--routes"])

    assert rc == 0
    assert "(none)" in capsys.readouterr().out


def test_config_check_routes_json_carries_the_counts(
    hermetic_gateway_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from corp_llm_gateway.route_gate import LITELLM_ROUTE_TABLE, Verdict

    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")

    rc = main(["config", "check", "--no-probe", "--routes", "--json"])

    assert rc == 0
    gate = json.loads(capsys.readouterr().out)["route_gate"]
    assert gate["exact_rows"] == len(LITELLM_ROUTE_TABLE)
    assert set(gate["counts"]) == {v.name for v in Verdict}
    # the eight generation spellings, the only routes whose body is rewritten
    assert gate["counts"]["REWRITTEN"] == 8
    assert len(gate["rewritten"]) == 8
    assert gate["extras"] == []
    assert gate["extras_problem"] is None


def test_config_check_routes_reports_a_malformed_extra_rather_than_raising(
    hermetic_gateway_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `validate()` already fails the check; --routes must still print the table
    # instead of dying on the same value, or the operator cannot see the typo.
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH", "GET without-a-slash")

    rc = main(["config", "check", "--no-probe", "--routes"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "route gate:" in captured.out
    # both streams carry it: a stdout-only reader must not see an empty section
    assert "INVALID" in captured.out
    assert "INVALID" in captured.err


def test_config_check_routes_json_reports_a_malformed_extra(
    hermetic_gateway_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH", "GET without-a-slash")

    rc = main(["config", "check", "--no-probe", "--routes", "--json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    gate = payload["route_gate"]
    assert gate["extras_problem"] is not None
    assert gate["extras"] == []
    assert gate["counts"]["REWRITTEN"] == 8


def test_config_check_without_routes_prints_no_table(
    hermetic_gateway_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")

    rc = main(["config", "check", "--no-probe", "--json"])

    assert rc == 0
    assert "route_gate" not in json.loads(capsys.readouterr().out)


def _issuance_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "issuance.toml"
    cfg.write_text('[CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP]\n"/devs" = "t1"\n')
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_PG_DSN", "postgresql://gw:gw@pg:5432/gw")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_ISSUER", "https://kc.corp.lan/realms/dev")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_AUDIENCE", "corp-gateway-issuance")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID", "corp-gateway-cli")
    for name in ("cryptography", "jwt", "asyncpg"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    config.reset_cache()


def test_config_check_reports_what_the_boot_refuses_at_runtime(
    hermetic_gateway_config: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _issuance_config(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "asyncpg", None)
    monkeypatch.setenv("CORP_LLM_CA_BUNDLE", str(tmp_path / "missing-bundle-6d0e.pem"))

    rc = main(["config", "check", "--no-probe", "--json"])

    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["config_valid"] is False
    assert data["problems"] == [
        settings.ISSUANCE_NEEDS_POSTGRES_EXTRA,
        settings.ISSUANCE_CA_BUNDLE_UNREADABLE,
    ]
    assert "missing-bundle-6d0e" not in json.dumps(data)


def test_config_check_passes_a_servable_issuance_config(
    hermetic_gateway_config: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _issuance_config(tmp_path, monkeypatch)

    rc = main(["config", "check", "--no-probe"])

    assert rc == 0
    assert "config: OK" in capsys.readouterr().out


def test_config_check_refuses_an_issuance_bound_past_its_ceiling(
    hermetic_gateway_config: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _issuance_config(tmp_path, monkeypatch)
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_MIN_INTERVAL_SECONDS", str(10**14))

    rc = main(["config", "check", "--no-probe"])

    assert rc == 1
    assert "CORP_GATEWAY_ISSUE_MIN_INTERVAL_SECONDS" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("env", "cap"),
    [("production", "0"), ("prod", "0"), ("", "-5"), ("production", "10001")],
    ids=["zero-in-production", "zero-in-prod", "negative", "past-the-ceiling"],
)
def test_config_check_refuses_the_capacity_the_boot_refuses(
    hermetic_gateway_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    env: str,
    cap: str,
) -> None:
    # The same resolver as the entrypoint's boot step: settings.capacity().
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_ENV", env)
    monkeypatch.setenv("CORP_LLM_MAX_INFLIGHT", cap)
    config.reset_cache()

    rc = main(["config", "check", "--no-probe", "--json"])

    assert rc == 1
    problems = json.loads(capsys.readouterr().out)["problems"]
    assert any(problem.startswith("CORP_LLM_MAX_INFLIGHT") for problem in problems)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CORP_LLM_BODY_READ_SECONDS", "0"),
        ("CORP_LLM_BODY_READ_SECONDS", "301"),
        ("CORP_LLM_MAX_DRAINING", "3"),
        ("CORP_LLM_MAX_DRAINING_BYTES", "1048576"),
        ("CORP_LLM_MAX_DRAINING_BYTES", "17179869185"),
    ],
    ids=[
        "body-deadline-zero",
        "body-deadline-past-the-ceiling",
        "draining-below-the-cap",
        "byte-budget-below-the-body-cap",
        "byte-budget-past-the-ceiling",
    ],
)
def test_config_check_refuses_the_body_limits_the_boot_refuses(
    hermetic_gateway_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv(name, value)
    config.reset_cache()

    rc = main(["config", "check", "--no-probe", "--json"])

    assert rc == 1
    problems = json.loads(capsys.readouterr().out)["problems"]
    assert any(problem.startswith(name) for problem in problems)


def test_config_check_accepts_a_zero_cap_outside_prod(
    hermetic_gateway_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_MAX_INFLIGHT", "0")
    config.reset_cache()

    assert main(["config", "check", "--no-probe"]) == 0

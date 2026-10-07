"""Ways litellm could let an original reach the provider, driven through its real proxy app.

A client's guardrail opt-out, in `metadata`, `litellm_metadata` or at the root, is stripped or
overwritten; our refusal keeps its status and error code through litellm; litellm's
`scan_raw_request` and `run_in_parallel` flags are refused when the gateway arms. A policy row
written into litellm's database, or a request body naming one, makes a guardrail registered the
litellm way (under `guardrails:`, not as a plain callback, as ours is) skip the request, so the
original would leave. The same tests pin the defences: our plain callback still rewrites, a check
that runs after our pre-call refuses the request with 503, pinning `supported_db_objects` keeps
the row out, and the gate refuses a body with a `policies` key with 403. That after-pre-call
check cannot see a `scan_raw_request` run; the arm check refuses the flag instead.

Plan 20260926 Task 0, hazards 2, 4, 11, 14a, 14b.

14a/14b write rows straight into litellm's policy tables in Postgres, created from
litellm's own migration SQL; ``prisma`` (litellm's client) is not installed, so a thin
asyncpg stand-in answers ``find_many`` for exactly the two tables the sync reads. From
``ProxyConfig._init_non_llm_objects_in_db`` down, every line that runs is litellm's.
Postgres skips locally without a server and fails on CI (``tests/postgres_support.py``).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("litellm.proxy.proxy_server", reason="litellm proxy not installed")

from litellm.proxy import proxy_server
from litellm.proxy.guardrails.guardrail_registry import (
    _apply_configured_bool_overrides,
)
from litellm.proxy.guardrails.init_guardrails import init_guardrails_v2
from litellm.proxy.policy_engine.policy_registry import get_policy_registry
from litellm.types.guardrails import LitellmParams

from corp_llm_gateway.tokens import AuthMiddleware
from tests.hook_fixtures import (
    _build_guardrail,
    _corp_llm_unreachable,
    _RaisingTokenStore,
)
from tests.litellm_hook._dispatch_fixtures import (
    E_SANITIZER_SKIPPED,
    EMAIL,
    GUARDRAIL_NAME,
    ORIGINAL_MARK,
    PLACEHOLDER,
    DispatchHarness,
    SentinelCallback,
    StubUpstream,
    UnsafeMigratedGuardrail,
    arm_problems,
    body_names_policies,
    build_ours,
    pipeline_names_us,
    request_body,
    use_engine,
)
from tests.postgres_support import pg_dsn, require_asyncpg, skip_or_fail


@pytest.fixture
def upstream() -> Iterator[StubUpstream]:
    stub = StubUpstream()
    yield stub
    stub.close()


def _egressed_placeholders(bodies: list[str]) -> bool:
    return len(bodies) == 1 and PLACEHOLDER in bodies[0] and ORIGINAL_MARK not in bodies[0]


def _egressed_original(bodies: list[str]) -> bool:
    return len(bodies) == 1 and ORIGINAL_MARK in bodies[0]


# ── hazard 2 ─────────────────────────────────────────────────────────────────

# Every REWRITTEN row of the route table.
REWRITTEN = [
    ("chat", "/v1/chat/completions"),
    ("chat", "/chat/completions"),
    ("chat", "/engines/corp-chat/chat/completions"),
    ("messages", "/v1/messages"),
    ("responses", "/v1/responses"),
    ("responses", "/responses"),
    ("responses", "/v1/responses/compact"),
    ("responses", "/responses/compact"),
]
_OPT_OUT = {"disable_global_guardrails": True, "opted_out_global_guardrails": [GUARDRAIL_NAME]}
SPOOFS = {
    "metadata": {
        "metadata": {"user_api_key_metadata": _OPT_OUT, "user_api_key_team_metadata": _OPT_OUT}
    },
    "litellm_metadata": {
        "litellm_metadata": {
            "user_api_key_metadata": _OPT_OUT,
            "user_api_key_team_metadata": _OPT_OUT,
        }
    },
    "root": dict(_OPT_OUT),
}


@pytest.mark.parametrize("spoof", sorted(SPOOFS))
@pytest.mark.parametrize(("route", "path"), REWRITTEN, ids=[path for _, path in REWRITTEN])
async def test_opt_out_metadata_overwritten(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream, route: str, path: str, spoof: str
) -> None:
    """Hazard 2: a client-supplied global-guardrail opt-out, in ``metadata``,
    ``litellm_metadata`` or at the root, never reaches ``should_run_guardrail`` — litellm
    strips or overwrites it (litellm_pre_call_utils.py:1935-1953, 2300-2301). The
    default-on migrated fixture still runs on every REWRITTEN route; were the opt-out
    honoured it would be skipped and the original would egress. (Today's plain callback
    never consults it at all.)
    """
    ours, _ = build_ours()
    migrated = UnsafeMigratedGuardrail(ours)
    harness = DispatchHarness(monkeypatch, upstream, [migrated])

    exchange = await harness.send(route, stream=False, path=path, extra=SPOOFS[spoof])

    assert exchange.status == 200
    assert migrated.calls[0] == "pre_call"
    assert _egressed_placeholders(exchange.provider_bodies)


# ── hazard 4 ─────────────────────────────────────────────────────────────────


def _internal_error_guardrail() -> Any:
    guardrail, _ = build_ours()
    guardrail._auth = AuthMiddleware(_RaisingTokenStore())
    return guardrail


def _corp_llm_down_guardrail() -> Any:
    guardrail, _ = _build_guardrail([(EMAIL, PLACEHOLDER)], corp_llm=_corp_llm_unreachable())
    guardrail._strip_inbound_headers_to_upstream = True
    return guardrail


M4 = {
    "missing_token": (lambda: build_ours()[0], {"token": None}, 401, "E_MISSING_TOKEN"),
    "invalid_token": (lambda: build_ours()[0], {"token": "not-a-token"}, 401, "E_TOKEN_INVALID"),
    "dlp_blocked": (
        lambda: build_ours()[0],
        {"content": "my key is sk-" + "a" * 48},
        422,
        "E_DLP_BLOCKED",
    ),
    "corp_llm_down": (_corp_llm_down_guardrail, {"content": "hello there"}, 503, "E_CORP_LLM_DOWN"),
    "internal": (_internal_error_guardrail, {}, 500, "E_INTERNAL"),
}


@pytest.mark.parametrize("migrated", [False, True], ids=["today", "migrated"])
@pytest.mark.parametrize("route", ["chat", "messages", "responses"])
@pytest.mark.parametrize("case", sorted(M4))
async def test_m4_status_codes_through_proxy(
    monkeypatch: pytest.MonkeyPatch,
    upstream: StubUpstream,
    case: str,
    route: str,
    migrated: bool,
) -> None:
    """Hazard 4: litellm answers our ``GuardrailHttpException`` with its own status code
    and the stable error code, on every route family, today and on the migrated fixture
    (``_enrich_http_exception_with_guardrail_context`` only touches ``HTTPException``).
    Nothing reaches the provider, and no secret or original is echoed. Were litellm to
    flatten to 500 or fail open, the status or the empty provider list would fail.
    """
    factory, options, status, code = M4[case]
    ours = factory()
    callbacks = [UnsafeMigratedGuardrail(ours)] if migrated else [ours]
    harness = DispatchHarness(monkeypatch, upstream, callbacks)

    exchange = await harness.send(route, stream=False, **options)

    assert exchange.status == status
    assert code in exchange.text
    assert exchange.provider_bodies == []
    assert "a" * 48 not in exchange.text and ORIGINAL_MARK not in exchange.text


# ── hazard 11 ────────────────────────────────────────────────────────────────


def _guardrail_config(cls: str, flag: str) -> list[dict[str, Any]]:
    return [
        {
            "guardrail_name": GUARDRAIL_NAME,
            "litellm_params": {
                "guardrail": f"tests.litellm_hook._dispatch_fixtures.{cls}",
                "mode": ["pre_call", "post_call"],
                "default_on": True,
                flag: True,
            },
        }
    ]


@pytest.mark.parametrize("flag", ["scan_raw_request", "run_in_parallel"])
async def test_scan_raw_request_and_run_in_parallel_refused(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream, tmp_path: Path, flag: str
) -> None:
    """Hazard 11: presence is not enforcement.

    Migrated, registered through ``guardrails:`` with the flag set. ``scan_raw_request``:
    litellm scans a snapshot and discards the rewrite (proxy/utils.py:1394-1466), so the
    original egresses. ``run_in_parallel``: the return value is discarded too, but the
    group is handed the live dict (:1942-1945) and our engine rewrites it in place, so the
    rewrite survives by accident; the group also runs after every sequential callback, the
    sentinel included. Both are refused.

    A constructor that refuses the flag is not enough on its own: ``init_guardrails_v2``
    logs and starts WITHOUT the guardrail (init_guardrails.py:36-42), which only the arm
    check turns into a refusal (``guardrail_absent``). The registry can also set the flag
    after construction (guardrail_registry.py:418-426); the arm check sees that too.
    Today's plain callback cannot be given either flag through ``callbacks:`` and ignores
    the attribute if set.
    """
    ours, _ = build_ours()
    use_engine(ours)
    harness = DispatchHarness(monkeypatch, upstream, [])
    config_path = str(tmp_path / "config.yaml")
    init_guardrails_v2(
        _guardrail_config("UnsafeMigratedGuardrail", flag),
        config_file_path=config_path,
        llm_router=harness.router,
    )
    (registered,) = proxy_server.litellm.callbacks
    assert getattr(registered, flag) is True

    exchange = await harness.send("chat", stream=False)

    if flag == "scan_raw_request":
        assert _egressed_original(exchange.provider_bodies)
    else:
        assert _egressed_placeholders(exchange.provider_bodies)
        parallel_ours, _ = build_ours()
        ordered = DispatchHarness(
            monkeypatch,
            upstream,
            [UnsafeMigratedGuardrail(parallel_ours, run_in_parallel=True), SentinelCallback()],
        )
        assert (await ordered.send("chat", stream=False)).status == 503
    assert flag in arm_problems(proxy_server.litellm.callbacks)

    refusing = DispatchHarness(monkeypatch, upstream, [])
    init_guardrails_v2(
        _guardrail_config("RefusingMigratedGuardrail", flag),
        config_file_path=config_path,
        llm_router=refusing.router,
    )
    assert proxy_server.litellm.callbacks == []
    assert arm_problems(proxy_server.litellm.callbacks) == ["guardrail_absent"]

    late = UnsafeMigratedGuardrail(ours)
    _apply_configured_bool_overrides(
        late, LitellmParams(guardrail="custom", mode="pre_call", **{flag: True})
    )
    assert flag in arm_problems([late])

    plain, _ = build_ours()
    setattr(plain, flag, True)
    today = DispatchHarness(monkeypatch, upstream, [plain])
    served = await today.send("chat", stream=False)
    assert _egressed_placeholders(served.provider_bodies)
    assert flag in arm_problems([plain])


async def test_sentinel_does_not_catch_scan_raw_request(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream
) -> None:
    """A design limit of the sentinel. With ``scan_raw_request`` litellm runs our pre-call on
    a snapshot carrying the same ``litellm_call_id`` (proxy/utils.py:1421-1466): the call id
    is bound on the ticket, then litellm discards the rewrite. The marker proves "our
    pre-call ran", not "the live dict was rewritten", so the sentinel passes and the
    original egresses. Only the arm check (``scan_raw_request``) closes hazard 11.
    """
    ours, _ = build_ours()
    migrated = UnsafeMigratedGuardrail(ours, scan_raw_request=True)
    harness = DispatchHarness(monkeypatch, upstream, [migrated, SentinelCallback()])

    exchange = await harness.send("chat", stream=False)

    assert exchange.status == 200
    assert "pre_call" in migrated.calls
    assert exchange.ticket is not None and exchange.ticket.call_ids
    assert _egressed_original(exchange.provider_bodies)
    assert "scan_raw_request" in arm_problems([migrated])


# ── hazards 14a / 14b: rows straight into litellm's Postgres ─────────────────

_MIGRATIONS = Path(__import__("litellm_proxy_extras").__file__).parent / "migrations"


def _policy_ddl() -> list[str]:
    statements: list[str] = []
    for migration in sorted(_MIGRATIONS.glob("*/migration.sql")):
        for statement in migration.read_text().split(";"):
            body = "\n".join(
                line for line in statement.splitlines() if not line.strip().startswith("--")
            ).strip()
            if body and ("LiteLLM_PolicyTable" in body or "LiteLLM_PolicyAttachmentTable" in body):
                statements.append(body)
    return statements


class _Table:
    """``find_many`` over one table, the only call litellm's policy sync makes."""

    def __init__(self, conn: Any, schema: str, table: str) -> None:
        self._conn = conn
        self._source = f'"{schema}"."{table}"'

    async def find_many(
        self, where: dict[str, Any] | None = None, order: dict[str, str] | None = None, **_: Any
    ) -> list[SimpleNamespace]:
        clauses: list[str] = []
        args: list[Any] = []
        for column, condition in (where or {}).items():
            if isinstance(condition, dict) and "in" in condition:
                args.append(list(condition["in"]))
                clauses.append(f'"{column}" = ANY(${len(args)})')
            else:
                args.append(condition)
                clauses.append(f'"{column}" = ${len(args)}')
        sql = f"SELECT * FROM {self._source}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        for column, direction in (order or {}).items():
            sql += f' ORDER BY "{column}" {"DESC" if direction == "desc" else "ASC"}'
        rows = await self._conn.fetch(sql, *args)
        return [SimpleNamespace(**_decoded(dict(row))) for row in rows]


def _decoded(row: dict[str, Any]) -> dict[str, Any]:
    for column in ("condition", "pipeline"):
        if isinstance(row.get(column), str):
            row[column] = json.loads(row[column])
    return row


class _Nothing:
    async def find_many(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def find_first(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def find_unique(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def count(self, *args: Any, **kwargs: Any) -> int:
        return 0


class _Db:
    def __init__(self, tables: dict[str, _Table]) -> None:
        self._tables = tables

    def __getattr__(self, name: str) -> Any:
        return self._tables.get(name) or _Nothing()


class LitellmPolicyDb:
    """litellm's two policy tables in a throwaway schema, and a client litellm can sync from."""

    def __init__(self, conn: Any, schema: str) -> None:
        self.conn = conn
        self.schema = schema
        self.client = SimpleNamespace(
            db=_Db(
                {
                    "litellm_policytable": _Table(conn, schema, "LiteLLM_PolicyTable"),
                    "litellm_policyattachmenttable": _Table(
                        conn, schema, "LiteLLM_PolicyAttachmentTable"
                    ),
                }
            ),
            # Other loaders on the same path read LiteLLM_Config rows: there are none.
            get_generic_data=_Nothing().find_first,
        )

    async def insert_policy(self, name: str, *, status: str) -> str:
        policy_id = str(uuid.uuid4())
        pipeline = {"mode": "post_call", "steps": [{"guardrail": GUARDRAIL_NAME}]}
        await self.conn.execute(
            f'INSERT INTO "{self.schema}"."LiteLLM_PolicyTable" '
            "(policy_id, policy_name, guardrails_add, pipeline, version_status) "
            "VALUES ($1, $2, $3, $4::jsonb, $5)",
            policy_id,
            name,
            [GUARDRAIL_NAME],
            json.dumps(pipeline),
            status,
        )
        return policy_id

    async def attach_everywhere(self, name: str) -> None:
        await self.conn.execute(
            f'INSERT INTO "{self.schema}"."LiteLLM_PolicyAttachmentTable" '
            "(attachment_id, policy_name, scope) VALUES ($1, $2, '*')",
            str(uuid.uuid4()),
            name,
        )


@pytest.fixture
async def policy_db() -> AsyncIterator[LitellmPolicyDb]:
    require_asyncpg()
    import asyncpg

    try:
        conn = await asyncpg.connect(pg_dsn(), timeout=5)
    except (OSError, asyncpg.PostgresError) as exc:
        skip_or_fail(f"Postgres unreachable at the test DSN: {type(exc).__name__}")
    schema = f"litellm_probe_{uuid.uuid4().hex[:12]}"
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        for statement in _policy_ddl():
            await conn.execute(statement)
        yield LitellmPolicyDb(conn, schema)
    finally:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()


async def _sync(db: LitellmPolicyDb) -> None:
    """The boot / reconcile path: ``_init_non_llm_objects_in_db`` (proxy_server.py:7291)."""
    await proxy_server.ProxyConfig()._init_non_llm_objects_in_db(prisma_client=db.client)


async def test_migrated_pipeline_skip_egresses_originals_via_db_row(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream, policy_db: LitellmPolicyDb
) -> None:
    """Hazard 14a: a production row + attachment written straight into litellm's DB (no
    HTTP request through our gate) reaches the registry at the next sync, because
    ``supported_db_objects`` is unset (should_load_db_object, proxy_server.py:4538-4551);
    the migrated fixture is then skipped and the original egresses. The sentinel refuses;
    the arm check sees our name in a pipeline. Pinning ``supported_db_objects`` without
    ``policies`` keeps the row out (defence in depth: config ``policies:`` bypass it,
    proxy_server.py:5990). Today's plain callback runs either way.
    """
    await policy_db.insert_policy("db-pipeline", status="production")
    await policy_db.attach_everywhere("db-pipeline")
    ours, _ = build_ours()
    migrated = UnsafeMigratedGuardrail(ours)
    harness = DispatchHarness(monkeypatch, upstream, [migrated])
    assert "supported_db_objects" not in proxy_server.general_settings
    await _sync(policy_db)

    exchange = await harness.send("chat", stream=False)

    assert "pre_call" not in migrated.calls
    assert _egressed_original(exchange.provider_bodies)
    assert "pipeline_manages_us" in arm_problems([migrated], registry=get_policy_registry())

    sentinel_ours, _ = build_ours()
    guarded = DispatchHarness(
        monkeypatch, upstream, [UnsafeMigratedGuardrail(sentinel_ours), SentinelCallback()]
    )
    await _sync(policy_db)
    refused = await guarded.send("chat", stream=False)
    assert refused.status == 503 and E_SANITIZER_SKIPPED in refused.text
    assert refused.provider_bodies == []

    plain, _ = build_ours()
    today = DispatchHarness(monkeypatch, upstream, [plain])
    await _sync(policy_db)
    assert _egressed_placeholders((await today.send("chat", stream=False)).provider_bodies)

    pinned_ours, _ = build_ours()
    pinned = DispatchHarness(
        monkeypatch,
        upstream,
        [UnsafeMigratedGuardrail(pinned_ours)],
        general_settings={"supported_db_objects": ["models", "mcp"]},
    )
    await _sync(policy_db)
    assert not pipeline_names_us(get_policy_registry())
    assert _egressed_placeholders((await pinned.send("chat", stream=False)).provider_bodies)


async def test_migrated_pipeline_skip_egresses_originals_via_body_version_id(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream, policy_db: LitellmPolicyDb
) -> None:
    """Hazard 14b: a DRAFT row, never attached, is synced into ``_policies_by_id``
    (policy_registry.py:676-695); any client then selects it per request with a body
    ``policies: ["policy_<uuid>"]`` (litellm_pre_call_utils.py:3246-3340) and the migrated
    fixture is skipped. The sentinel refuses; the gate-level refusal of a top-level
    ``policies`` key stops the body before litellm parses it (lifted below, to show
    what litellm does with such a body, then live).
    """
    policy_id = await policy_db.insert_policy("db-draft", status="draft")
    extra = {"policies": [f"policy_{policy_id}"]}
    ours, _ = build_ours()
    migrated = UnsafeMigratedGuardrail(ours)
    harness = DispatchHarness(monkeypatch, upstream, [migrated], body_gate=False)
    await _sync(policy_db)

    exchange = await harness.send("chat", stream=False, extra=extra)

    assert "pre_call" not in migrated.calls
    assert _egressed_original(exchange.provider_bodies)

    sentinel_ours, _ = build_ours()
    guarded = DispatchHarness(
        monkeypatch,
        upstream,
        [UnsafeMigratedGuardrail(sentinel_ours), SentinelCallback()],
        body_gate=False,
    )
    await _sync(policy_db)
    refused = await guarded.send("chat", stream=False, extra=extra)
    assert refused.status == 503 and refused.provider_bodies == []

    body = json.dumps(request_body("chat", stream=False) | extra).encode()
    assert body_names_policies(body)
    assert not body_names_policies(json.dumps(request_body("chat", stream=False)).encode())

    gated_ours, _ = build_ours()
    gated = DispatchHarness(monkeypatch, upstream, [UnsafeMigratedGuardrail(gated_ours)])
    blocked = await gated.send("chat", stream=False, extra=extra)
    assert blocked.status == 403 and "route_gate_body_policies" in blocked.text
    assert blocked.provider_bodies == []

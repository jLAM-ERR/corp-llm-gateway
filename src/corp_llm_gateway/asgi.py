"""The gateway's ASGI entrypoint: litellm's app behind the route gate.

**litellm's ``proxy_server`` app and the ``litellm`` CLI must never be the served
target again.** Either one serves litellm's routers with nothing in front
of them, and three of those routers reach a provider without ever calling
``pre_call_hook`` — raw user text out of the corp boundary, unsanitized and
unaudited (see ``docs/plans/20260922-litellm-route-gate.md``). This module is the
only supported target; ``Dockerfile.gateway``, compose and Helm all run
``python -m corp_llm_gateway.serve``, which imports ``corp_llm_gateway.asgi:app``.

Importing this module has side effects, deliberately: it IS the boot sequence.
In order —

1. resolve and check litellm's config (``CORP_LLM_LITELLM_CONFIG``); exit 78
   (``EX_CONFIG``) if it is missing, unreadable, not YAML or configures
   ``pass_through_endpoints`` or ``general_settings.database_url``. litellm's own
   lifespan skips a missing config in silence (``proxy_server.py:1088-1095``) and
   would serve with NO guardrail. Then the gateway's own issuance config, through
   ``settings.serving_issuance()`` (the resolver ``config check`` uses): partial,
   set without ``CORP_LLM_PG_DSN``, missing the ``oidc``/``postgres`` extras or
   naming an unreadable ``CORP_LLM_CA_BUNDLE``, exits 78; so does a Postgres that
   refuses the DSN (credentials, database name, DSN syntax) or whose
   ``corp_tokens`` lacks the issuance columns or the unique ``oidc_jti`` index. A
   Postgres the network cannot reach does not refuse the boot: readiness stays 503
   and the issuance route answers 503 ``E_ISSUE_SCHEMA`` until a re-check (readiness
   or an issuance attempt, at most one per 15 s) sees the schema current.
   Then the in-flight cap through ``settings.capacity()`` (also ``config check``'s):
   a non-integer, negative or oversized ``CORP_LLM_MAX_INFLIGHT``, ``0`` under
   ``CORP_ENV=prod|production``, or a bad ``CORP_LLM_CANCEL_GRACE_SECONDS``,
   ``CORP_LLM_BODY_READ_SECONDS``, ``CORP_LLM_MAX_DRAINING`` or
   ``CORP_LLM_MAX_DRAINING_BYTES`` exits 78.
   Last, ``CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH``: a malformed item, or one naming
   a route the table refused, exits 78;
2. run litellm's Prisma schema sequence when ``DATABASE_URL`` is set, with the
   same four guards and the same exit codes as ``proxy_cli.py:1326-1375``;
3. ``save_worker_config(...)`` so litellm's lifespan takes the
   ``initialize(**worker_config)`` branch — the one that applies
   ``drop_params``, the request timeout, telemetry and the log level.
   ``CONFIG_FILE_PATH`` is deliberately NOT set: that branch skips
   ``initialize()`` entirely;
4. import litellm's app, put the gateway-owned ``/healthz/*`` and ``/metrics``
   routes in front of it, and wrap the app's **lifespan** (not ``on_startup``,
   which Starlette never runs for an app built with an explicit ``lifespan=``) so
   that after litellm's startup has run, a ``CorpLlmGuardrail`` must be in
   ``litellm.callbacks``. If it is not, the process exits 70 (``EX_SOFTWARE``)
   rather than serve unsanitized;
5. wrap the whole chain in ``RouteGateMiddleware`` — by wrapping, not
   ``add_middleware``, so the gate is outermost. Every middleware litellm adds
   sits inside it and none can answer ahead of the gate. The gate carries the
   in-flight limiter (``route_gate/inflight.py``); once armed, the lifespan
   hands it the guardrail's ``on_request_cancelled`` and installs the task
   factory that tags each request's tasks on uvicorn's own loop.

What moved here from the ``litellm`` CLI: the config-load check, the Prisma
sequence, ``WORKER_CONFIG``, and (in ``serve.py``) uvicorn's arguments.
Deliberately NOT carried over:

* the two extra DSN sources the CLI reads before the Prisma sequence
  (``proxy_cli.py:1183-1190``) — ``general_settings.database_url`` from the YAML,
  and the ``DATABASE_HOST``/``DATABASE_USERNAME``/``DATABASE_PASSWORD``/
  ``DATABASE_NAME``/``DATABASE_SCHEMA`` composition
  (``proxy/utils.py:7276``). This entrypoint's Prisma step reads ``DATABASE_URL``
  and ``DIRECT_URL`` only, so either of the other two would let litellm connect to
  a database whose schema was never set up. The YAML one is refused at step 1
  (exit 78); the env composition is documented here — set ``DATABASE_URL``
  instead;
* the DB connection-URL rewriting (``proxy_cli.py:1241-1325`` — pool/timeout
  query params this deployment sets on its own DSN);
* the ``--num_workers``/gunicorn/granian/hypercorn runners: each worker would run
  this module, hence the Prisma schema sequence, concurrently against one
  database, and each would build its own Prometheus registry so ``/metrics``
  would report whichever worker answered the scrape. Scale with replicas;
* the random-port fallback when 4000 is busy (a port collision must fail, not
  move), the prometheus-multiproc directory (this process exposes one registry at
  ``/metrics``), and ``--skip_server_startup``/``--test``/``--health`` (developer
  affordances).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, NoReturn

from corp_llm_gateway import config, litellm_cli, litellm_config, pg_session, settings
from corp_llm_gateway.audit import AuditLogger, get_sink
from corp_llm_gateway.metrics import get_exporter
from corp_llm_gateway.route_gate import RouteGateMiddleware
from corp_llm_gateway.route_gate.inflight import InflightLimiter, install_task_factory

# `corp_llm_gateway.bootstrap` is NOT imported here: it reaches
# `litellm_hook`, which imports litellm itself (the guardrail has to subclass
# `CustomLogger`), and importing litellm fetches the model cost map. litellm is
# imported at step 3, after the config has been accepted; `bootstrap` at step 4.

log = logging.getLogger(__name__)

# sysexits.h. 78 = the config is wrong (defined in litellm_config, which serve.py
# also exits with); 70 = the software is wrong — the guardrail did not register.
# The container tests and the runbook key off these.
EXIT_CONFIG = litellm_config.EXIT_CONFIG
EXIT_NO_CALLBACK = 70

_MOUNT_HEALTHZ = "/healthz"
_MOUNT_METRICS = "/metrics"
_ISSUE_TOKEN = "/internal/issue-token"


class _BootJsonFormatter(logging.Formatter):
    """litellm's JSON record shape, written without importing litellm.

    Vector parses this container's stdout and the boot lines go to that same
    stream, so a plain line here would be an unparseable record inside JSON
    ones. It cannot be ``litellm._logging.JsonFormatter``: this runs before step
    1, and importing litellm there would break the step order this module's
    docstring defines — the import at step 3 is what fetches the cost map, after
    the config has been accepted. The keys are the ones litellm's formatter
    writes first (``litellm/_logging.py:459-502``).

    It has **no redaction filter** — litellm's own handler carries
    ``_secret_filter``, this one cannot (same import bar). So a boot line must
    never carry a DSN, a token or any other credential: log the setting's name
    and the decision, never its value.
    """

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "message": record.getMessage(),
                "level": record.levelname,
                "timestamp": datetime.fromtimestamp(record.created).isoformat(),
                "component": record.name,
                "logger": f"{record.filename}:{record.lineno}",
            }
        )


def _boot_formatter(json_logs: bool) -> logging.Formatter:
    if not json_logs:
        return logging.Formatter("%(levelname)s %(name)s %(message)s")
    return _BootJsonFormatter()


def _open_the_boot_log(*, json_logs: bool) -> logging.Handler:
    """Make the step lines below visible, at INFO, before litellm configures logging.

    uvicorn's log config names only its own loggers, and litellm installs the
    root JSON handler at step 3 — so without this the boot would be silent up to
    that point and filtered by the root WARNING level after it.
    """
    level = (config.get("CORP_LLM_LOG_LEVEL", "INFO") or "INFO").upper()
    package = logging.getLogger("corp_llm_gateway")
    package.setLevel(getattr(logging, level, logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_boot_formatter(json_logs))
    package.addHandler(handler)
    return handler


def _hand_over_the_boot_log(handler: logging.Handler, *, json_logs: bool) -> None:
    """End the boot window: give the package logger to litellm, or keep it.

    JSON mode (``litellm_settings.json_logs`` in the YAML, or litellm's own
    ``JSON_LOGS=true``) — ``_turn_on_json`` has put a JSON handler on the ROOT
    logger and this package still propagates to it, so the boot handler has to
    go or every line is written twice, once plain and once JSON.

    Plain mode — litellm installs no root handler at all, so the package keeps
    this handler deliberately and stops propagating: without it every line from
    step 3 on (including "route gate armed") would be dropped, and with it plus
    propagation a root handler installed later by anything else would double them.
    """
    package = logging.getLogger("corp_llm_gateway")
    if json_logs:
        package.removeHandler(handler)
        return
    package.propagate = False


def _fail_config(problems: list[str]) -> None:
    for problem in problems:
        log.error("litellm config refused: %s", problem)
    raise SystemExit(EXIT_CONFIG)


def _fail_gateway_config(problems: list[str]) -> NoReturn:
    for problem in problems:
        log.error("gateway config refused: %s", problem)
    raise SystemExit(EXIT_CONFIG)


_TOKEN_SCHEMA_REFUSED = {
    pg_session.BOOT_REFUSE: (
        "issuance schema check: Postgres refused CORP_LLM_PG_DSN ({}) — check the DSN "
        "syntax, the credentials and the database name"
    ),
    pg_session.BOOT_REFUSE_TLS: (
        "issuance schema check: TLS to CORP_LLM_PG_DSN failed ({}) — check sslmode, the "
        "server certificate and the CA the driver trusts"
    ),
    pg_session.BOOT_REFUSE_PRIVILEGE: (
        "issuance schema check: the CORP_LLM_PG_DSN role lacks SELECT on corp_tokens ({}) "
        "— grant the gateway role SELECT, INSERT and UPDATE on corp_tokens"
    ),
    pg_session.BOOT_REFUSE_STARTUP_PARAMETER: (
        "issuance schema check: " + pg_session.STARTUP_PARAMETER_REJECTED + " ({})"
    ),
}


async def _token_schema_state(dsn: str) -> str | None:
    """The schema problem to refuse on; ``None`` when the table is current."""
    import asyncpg

    # The pools' own startup parameters: a PgBouncer that rejects them refuses here.
    conn = await pg_session.connect_with_keepalives(
        asyncpg.connect, dsn, timeout=pg_session.TOKEN_SCHEMA_TIMEOUT_S
    )
    try:
        return await pg_session.token_schema_problem(conn)
    finally:
        await conn.close()


def _token_schema_problem(dsn: str) -> tuple[str | None, bool]:
    """The issuance schema check: the problem to refuse on, and whether the schema
    was seen current. ``(None, False)`` when Postgres cannot be reached."""
    # uvicorn imports this module inside its running loop, so the probe gets a
    # loop of its own on a worker thread.
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            problem = pool.submit(lambda: asyncio.run(_token_schema_state(dsn))).result()
    except Exception as exc:
        # The type only: a driver message can carry the DSN.
        outcome = pg_session.boot_probe_outcome(exc, dsn)
        if outcome != pg_session.BOOT_WARN:
            return _TOKEN_SCHEMA_REFUSED[outcome].format(type(exc).__name__), False
        log.warning(
            "issuance schema check skipped, Postgres not reachable at boot (%s); readiness "
            "stays 503 and %s answers 503 E_ISSUE_SCHEMA until the schema is seen current",
            type(exc).__name__,
            _ISSUE_TOKEN,
        )
        return None, False
    return problem, problem is None


def _check_issuance() -> tuple[bool, bool]:
    """Refuse (exit 78) a gateway that cannot serve its issuance config.

    Returns (issuance on, token schema seen current at boot).
    """
    try:
        issuance = settings.serving_issuance()
    except settings.ConfigError as exc:
        _fail_gateway_config(exc.problems)
    if issuance is None:
        log.info("developer token issuance disabled (CORP_GATEWAY_ISSUE_OIDC_ISSUER unset)")
        return False, False
    runtime = settings.issuance_runtime_problems()
    if runtime:
        _fail_gateway_config(runtime)
    problem, verified = _token_schema_problem(config.get("CORP_LLM_PG_DSN") or "")
    if problem is not None:
        _fail_gateway_config([problem])
    log.info("developer token issuance enabled: POST /internal/issue-token")
    return True, verified


def _check_capacity() -> settings.CapacitySettings:
    """Refuse (exit 78) an in-flight cap the gateway must not serve with."""
    try:
        capacity = settings.capacity()
    except settings.ConfigError as exc:
        _fail_gateway_config(exc.problems)
    if capacity.max_inflight:
        log.info(
            "in-flight cap: %d rewritten requests per pod, %d reading a body (%gs deadline), "
            "%d body bytes buffered at most",
            capacity.max_inflight,
            capacity.max_draining,
            capacity.body_read_seconds,
            capacity.max_draining_bytes,
        )
    else:
        log.warning("in-flight cap off (CORP_LLM_MAX_INFLIGHT=0); not allowed in prod")
    return capacity


def _route_gate_extras() -> dict[Any, Any]:
    """Refuse (exit 78) a malformed extra, or one naming a route the table refused."""
    try:
        return config.route_gate_extras()
    except ValueError as exc:
        _fail_gateway_config([f"CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH: {exc}"])
        return {}


def _setup_prisma(config_path: Any) -> None:
    """litellm's boot-time Prisma schema setup — ``proxy_cli.py:1326-1375``.

    The app's own lifespan only CONNECTS Prisma (``_setup_prisma_client``); the
    schema setup lived in the CLI, and compose relies on it happening at boot.
    Same four guards, same exit codes.
    """
    # litellm's own env var, read exactly where its CLI reads it
    # (proxy_cli.py:1241). Not a gateway setting, so not a settings.KEYS entry.
    if os.getenv("DATABASE_URL") is None and os.getenv("DIRECT_URL") is None:
        return
    try:
        subprocess.run(["prisma"], capture_output=True)  # litellm's own runnability probe
    except FileNotFoundError:
        log.error("DATABASE_URL is set but the prisma package is not installed; skipping setup")
        return

    from litellm.proxy.db.check_migration import check_prisma_schema_diff
    from litellm.proxy.db.prisma_client import PrismaManager, should_update_prisma_schema

    general = litellm_config.general_settings(config_path)
    if should_update_prisma_schema(general.get("disable_prisma_schema_update")) is False:
        check_prisma_schema_diff(db_url=None)
        log.info("prisma schema update disabled by config; diff checked only")
        return

    options = litellm_cli.option_values(litellm_cli.PRISMA_KEYS)
    try:
        setup_ok = PrismaManager.setup_database(
            use_migrate=not options["use_prisma_db_push"],
            use_v2_resolver=options["use_v2_migration_resolver"],
        )
    except RuntimeError as exc:
        log.error("database migration cannot proceed: %s", exc)
        raise SystemExit(2) from exc
    if setup_ok:
        log.info("prisma schema setup complete")
        return
    if options["enforce_prisma_migration_check"]:
        log.error("database setup failed after retries; refusing to start")
        raise SystemExit(1)
    log.warning(
        "database migration failed; continuing (set ENFORCE_PRISMA_MIGRATION_CHECK=true to exit)"
    )


def _registered_guardrail() -> Any:
    import litellm

    from corp_llm_gateway.litellm_hook import CorpLlmGuardrail

    return next((cb for cb in (litellm.callbacks or ()) if isinstance(cb, CorpLlmGuardrail)), None)


def _guardrail_registered() -> bool:
    return _registered_guardrail() is not None


def _nothing() -> None:
    return None


def _start_litellm_logging_worker() -> None:
    """Start litellm's logging worker outside any request.

    It starts lazily on the first callback, inside that request's task tree; a
    worker tagged to that request would be cancelled with it, taking every later
    success/failure callback (and our audit events) along. The task factory also
    refuses to tag the worker's tasks, so a restart from inside a request is safe.
    """
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

    GLOBAL_LOGGING_WORKER.start()


# ── 1. litellm's config ──────────────────────────────────────────────────────

CONFIG_PATH = litellm_config.config_path()
# Read before the boot log opens: the formatter depends on it, and a file this
# step is about to refuse still reads as "no JSON logs" rather than raising.
JSON_LOGS = litellm_config.json_logs(CONFIG_PATH)
_boot_log = _open_the_boot_log(json_logs=JSON_LOGS)

_problems = litellm_config.problems(CONFIG_PATH, require_file=True)
if _problems:
    _fail_config(_problems)
log.info("litellm config accepted: %s", CONFIG_PATH)
ISSUANCE_ENABLED, ISSUANCE_SCHEMA_VERIFIED = _check_issuance()
CAPACITY = _check_capacity()
ROUTE_GATE_EXTRAS = _route_gate_extras()

# ── 2. Prisma schema setup ───────────────────────────────────────────────────

_setup_prisma(CONFIG_PATH)

# ── 3. WORKER_CONFIG, so the lifespan runs initialize() ──────────────────────

_worker_config = litellm_cli.option_values(litellm_cli.WORKER_CONFIG_KEYS) | {
    "config": str(CONFIG_PATH)
}

import litellm  # noqa: E402 - step order is the contract of this module

if JSON_LOGS:
    # Before proxy_server is imported, as the CLI does it (proxy_cli.py:1158-1165
    # runs well ahead of the app import): the handlers that module installs at
    # import time otherwise keep the plain formatter. The lifespan's initialize()
    # sets the log LEVEL, never the JSON formatter.
    litellm.json_logs = True
    litellm._turn_on_json()
_hand_over_the_boot_log(_boot_log, json_logs=JSON_LOGS)

from litellm.proxy.proxy_server import app as _app  # noqa: E402
from litellm.proxy.proxy_server import proxy_startup_event, save_worker_config  # noqa: E402

save_worker_config(**_worker_config)
log.info("litellm WORKER_CONFIG set from %s", CONFIG_PATH)

# ── 4. gateway-owned routes + the lifespan wrapper ───────────────────────────

from corp_llm_gateway.bootstrap import build_health_router, gateway_version  # noqa: E402

_exporter = get_exporter()


class _MetricsRoute:
    """Serve ``GET|HEAD /metrics`` from the shared exporter; forward the rest.

    A raw ASGI hop rather than a Starlette route on litellm's app, for the same
    reason ``_GATEWAY_ROUTES`` is one at all (see below).
    """

    def __init__(self, app: Any, fallthrough: Any) -> None:
        self._app = app
        self._fallthrough = fallthrough

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            scope["type"] == "http"
            and scope.get("path") == _MOUNT_METRICS
            and str(scope.get("method") or "").upper() in ("GET", "HEAD")
        ):
            await self._app(scope, receive, send)
            return
        await self._fallthrough(scope, receive, send)


# The gateway's own routes sit BETWEEN the gate and litellm's app, not on
# litellm's router. Every middleware litellm adds with `add_middleware` wraps the
# whole router, and one of them — `PrometheusAuthMiddleware`
# (`proxy/middleware/prometheus_auth_middleware.py:37-44`) — answers 401 on any
# path containing `/metrics` before routing, whenever a master key is set. Helm's
# ServiceMonitor and the kubelet probes carry no litellm credential, so a
# gateway-owned route mounted inside litellm's router would be unscrapable in
# Mode A. `HealthRouter` already takes a `fallthrough`, so the chain is:
# /healthz/* -> /metrics -> litellm.
_GATEWAY_ROUTES = build_health_router(
    fallthrough=_MetricsRoute(_exporter.asgi_app(), _app),
    issuance_schema_verified=ISSUANCE_SCHEMA_VERIFIED,
)
log.info(
    "gateway routes served ahead of litellm: %s/*, %s, %s",
    _MOUNT_HEALTHZ,
    _MOUNT_METRICS,
    f"POST {_ISSUE_TOKEN}" if ISSUANCE_ENABLED else f"{_ISSUE_TOKEN} (issuance disabled, 404)",
)

limiter = InflightLimiter(
    CAPACITY.max_inflight,
    metrics=_exporter,
    cancel_grace_s=CAPACITY.cancel_grace_seconds,
    body_read_s=CAPACITY.body_read_seconds,
    max_draining=CAPACITY.max_draining,
    max_draining_bytes=CAPACITY.max_draining_bytes,
)
gate = RouteGateMiddleware(
    _GATEWAY_ROUTES,
    metrics=_exporter,
    audit_logger=AuditLogger(get_sink(), gateway_version=gateway_version()),
    extras=ROUTE_GATE_EXTRAS,
    limiter=limiter,
)

_litellm_lifespan = _app.router.lifespan_context


@asynccontextmanager
async def _armed_lifespan(scoped_app: Any) -> AsyncIterator[None]:
    """litellm's lifespan, then the arming check.

    Wrapping `lifespan_context` is the only hook that runs: Starlette ignores
    `on_startup` / `add_event_handler("startup")` on an app built with an
    explicit `lifespan=`, and litellm's is. `litellm.callbacks` is the
    authoritative list — `initialize_callbacks_on_proxy` extends it and
    `ProxyLogging._callback_capabilities` walks it; `litellm.success_callback`
    is a different list and would pass while the hook was absent.
    """
    async with _litellm_lifespan(scoped_app):
        guardrail = _registered_guardrail()
        restore_task_factory: Callable[[], None] = _nothing
        if guardrail is not None:
            _start_litellm_logging_worker()
            restore_task_factory = install_task_factory(asyncio.get_running_loop())
            limiter.bind_cancel_hook(guardrail.on_request_cancelled)
            gate.arm()
            log.info("CorpLlmGuardrail found in litellm.callbacks; route gate armed")
        else:
            log.error(
                "no CorpLlmGuardrail in litellm.callbacks after startup — the config at %s "
                "did not register corp_llm_gateway.bootstrap.guardrail. Refusing to serve.",
                CONFIG_PATH,
            )
            # _exit, not sys.exit: this runs inside uvicorn's lifespan task,
            # where an exception is caught and logged and the server keeps going.
            os._exit(EXIT_NO_CALLBACK)
        try:
            yield
        finally:
            restore_task_factory()


_app.router.lifespan_context = _armed_lifespan
# Both names: `_litellm_lifespan` is whatever the app was built with, and
# `proxy_startup_event` is what litellm builds it with today. If a future release
# wraps its own lifespan, the first check still holds.
if _app.router.lifespan_context is _litellm_lifespan:
    raise RuntimeError("the arming lifespan did not install; the gate would never arm")
if _app.router.lifespan_context is proxy_startup_event:
    raise RuntimeError("litellm's own lifespan is still installed; the gate would never arm")

# ── 5. the gate, outermost ───────────────────────────────────────────────────

app = gate

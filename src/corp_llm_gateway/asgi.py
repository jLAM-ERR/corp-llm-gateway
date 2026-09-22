"""The gateway's ASGI entrypoint: litellm's app behind the route gate.

**``litellm.proxy.proxy_server:app`` and the ``litellm`` CLI must never be the
served target again.** Either one serves litellm's routers with nothing in front
of them, and three of those routers reach a provider without ever calling
``pre_call_hook`` — raw user text out of the corp boundary, unsanitized and
unaudited (see ``docs/plans/20260922-litellm-route-gate.md``). This module is the
only supported target; ``Dockerfile.gateway``, compose and Helm all run
``python -m corp_llm_gateway.serve``, which imports ``corp_llm_gateway.asgi:app``.

Importing this module has side effects, deliberately: it IS the boot sequence.
In order —

1. resolve and check litellm's config (``CORP_LLM_LITELLM_CONFIG``); exit 78
   (``EX_CONFIG``) if it is missing, unreadable, not YAML or configures
   ``pass_through_endpoints``. litellm's own lifespan skips a missing config in
   silence (``proxy_server.py:1088-1095``) and would serve with NO guardrail;
2. run litellm's Prisma schema sequence when ``DATABASE_URL`` is set, with the
   same four guards and the same exit codes as ``proxy_cli.py:1326-1375``;
3. ``save_worker_config(...)`` so litellm's lifespan takes the
   ``initialize(**worker_config)`` branch — the one that applies
   ``drop_params``, the request timeout, telemetry and the log level.
   ``CONFIG_FILE_PATH`` is deliberately NOT set: that branch skips
   ``initialize()`` entirely;
4. import litellm's app, mount the gateway-owned ``/healthz/*`` and ``/metrics``
   routes, and wrap the app's **lifespan** (not ``on_startup``, which Starlette
   never runs for an app built with an explicit ``lifespan=``) so that after
   litellm's startup has run, a ``CorpLlmGuardrail`` must be in
   ``litellm.callbacks``. If it is not, the process exits 70 (``EX_SOFTWARE``)
   rather than serve unsanitized;
5. wrap the whole app in ``RouteGateMiddleware`` — by wrapping, not
   ``add_middleware``, so the gate is outermost. Every middleware litellm adds
   sits inside it and none can answer ahead of the gate.

What moved here from the ``litellm`` CLI: the config-load check, the Prisma
sequence, ``WORKER_CONFIG``, and (in ``serve.py``) uvicorn's arguments.
Deliberately NOT carried over: the DB connection-URL rewriting
(``proxy_cli.py:1241-1325`` — pool/timeout query params this deployment sets on
its own DSN), the ``--num_workers``/gunicorn/granian/hypercorn runners (the gate
arms in the lifespan, so the gateway runs one uvicorn worker), the random-port
fallback when 4000 is busy (a port collision must fail, not move), the
prometheus-multiproc directory (this process exposes one registry at
``/metrics``), and ``--skip_server_startup``/``--test``/``--health`` (developer
affordances).
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from corp_llm_gateway import config, litellm_cli, litellm_config
from corp_llm_gateway.audit import AuditLogger, get_sink
from corp_llm_gateway.bootstrap import build_health_router, gateway_version
from corp_llm_gateway.metrics import get_exporter
from corp_llm_gateway.route_gate import RouteGateMiddleware

log = logging.getLogger(__name__)

# sysexits.h. 78 = the config is wrong; 70 = the software is wrong (the
# guardrail did not register). The container tests and the runbook key off these.
EXIT_CONFIG = 78
EXIT_NO_CALLBACK = 70

_MOUNT_HEALTHZ = "/healthz"
_MOUNT_METRICS = "/metrics"


def _fail_config(problems: list[str]) -> None:
    for problem in problems:
        log.error("litellm config refused: %s", problem)
    raise SystemExit(EXIT_CONFIG)


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
        return
    if options["enforce_prisma_migration_check"]:
        log.error("database setup failed after retries; refusing to start")
        raise SystemExit(1)
    log.warning(
        "database migration failed; continuing (set ENFORCE_PRISMA_MIGRATION_CHECK=true to exit)"
    )


def _guardrail_registered() -> bool:
    import litellm

    from corp_llm_gateway.litellm_hook import CorpLlmGuardrail

    return any(isinstance(cb, CorpLlmGuardrail) for cb in (litellm.callbacks or ()))


# ── 1. litellm's config ──────────────────────────────────────────────────────

CONFIG_PATH = litellm_config.config_path()
_problems = litellm_config.problems(CONFIG_PATH, require_file=True)
if _problems:
    _fail_config(_problems)

# ── 2. Prisma schema setup ───────────────────────────────────────────────────

_setup_prisma(CONFIG_PATH)

# ── 3. WORKER_CONFIG, so the lifespan runs initialize() ──────────────────────

_worker_config = litellm_cli.option_values(litellm_cli.WORKER_CONFIG_KEYS) | {
    "config": str(CONFIG_PATH)
}

import litellm  # noqa: E402 - step order is the contract of this module
from litellm.proxy.proxy_server import app as _app  # noqa: E402
from litellm.proxy.proxy_server import save_worker_config  # noqa: E402

save_worker_config(**_worker_config)
if litellm_config.json_logs(CONFIG_PATH):
    # The CLI does this before uvicorn starts (proxy_cli.py:1158-1165); the
    # lifespan's initialize() sets the log LEVEL but not the JSON formatter.
    litellm.json_logs = True
    litellm._turn_on_json()

# ── 4. gateway-owned routes + the lifespan wrapper ───────────────────────────

_exporter = get_exporter()


class _AsgiSubApp:
    """A raw ASGI app Starlette will route to as an app, not as an endpoint.

    Two reasons this is a class and not the callable itself: Starlette wraps a
    plain function endpoint in request/response handling, and a ``Mount`` may
    strip its own prefix from ``scope["path"]``. ``HealthRouter`` matches
    absolute paths (`/healthz/live`), so the prefix is put back when it is gone.
    """

    def __init__(self, app: Any, prefix: str = "") -> None:
        self._app = app
        self._prefix = prefix

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        # Starlette changed this: older releases hand the sub-app the remainder
        # (`/live`), 1.x keeps the full path and moves the prefix to root_path.
        # Restore only when it was actually stripped.
        if (
            self._prefix
            and scope["type"] in ("http", "websocket")
            and not path.startswith(self._prefix)
        ):
            scope = dict(scope)
            scope["path"] = self._prefix + path
        await self._app(scope, receive, send)


def _mount_gateway_routes() -> None:
    from starlette.routing import Mount, Route

    # Index 0, not append: litellm registers first-segment catch-alls
    # (`/{mcp_server_name}/mcp`, `/{provider}/v1/files`) and Starlette matches in
    # registration order, so an appended mount could be shadowed.
    #
    # /metrics is a Route, not a Mount: a Mount's pattern is `<prefix>/...`, so
    # the bare `/metrics` a Prometheus scrape asks for would fall through to the
    # router's trailing-slash redirect and answer 307.
    _app.router.routes.insert(
        0,
        Route(
            _MOUNT_METRICS,
            endpoint=_AsgiSubApp(_exporter.asgi_app()),
            methods=["GET", "HEAD"],
            name="corp_llm_gateway_metrics",
        ),
    )
    _app.router.routes.insert(
        0,
        Mount(
            _MOUNT_HEALTHZ,
            app=_AsgiSubApp(build_health_router(), _MOUNT_HEALTHZ),
            name="corp_llm_gateway_healthz",
        ),
    )


_mount_gateway_routes()

gate = RouteGateMiddleware(
    _app,
    metrics=_exporter,
    audit_logger=AuditLogger(get_sink(), gateway_version=gateway_version()),
    extras=config.route_gate_extras(),
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
        if _guardrail_registered():
            gate.arm()
        else:
            log.error(
                "no CorpLlmGuardrail in litellm.callbacks after startup — the config at %s "
                "did not register corp_llm_gateway.bootstrap.guardrail. Refusing to serve.",
                CONFIG_PATH,
            )
            # _exit, not sys.exit: this runs inside uvicorn's lifespan task,
            # where an exception is caught and logged and the server keeps going.
            os._exit(EXIT_NO_CALLBACK)
        yield


_app.router.lifespan_context = _armed_lifespan
if _app.router.lifespan_context is _litellm_lifespan:
    raise RuntimeError("the arming lifespan did not install; the gate would never arm")

# ── 5. the gate, outermost ───────────────────────────────────────────────────

app = gate

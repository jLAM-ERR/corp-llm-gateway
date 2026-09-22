"""The parts of litellm's `proxy_cli.py` the gateway's entrypoint has to repeat.

Separate from ``asgi.py`` because importing that module boots the server; this
one is inert, so tests (and anything else) can read the argument lists without
starting anything.
"""

from __future__ import annotations

from typing import Any

# Exactly the keywords `proxy_cli.py:1061-1079` passes to `save_worker_config`.
# tests/test_asgi_entrypoint.py reads that call with `ast` and fails if the two
# drift, because a key litellm adds would otherwise silently keep its
# `initialize()` default instead of the CLI's.
WORKER_CONFIG_KEYS: tuple[str, ...] = (
    "model",
    "alias",
    "api_base",
    "api_version",
    "debug",
    "detailed_debug",
    "temperature",
    "max_tokens",
    "request_timeout",
    "max_budget",
    "telemetry",
    "drop_params",
    "add_function_to_prompt",
    "headers",
    "save",
    "config",
    "use_queue",
)

# The flags the Prisma sequence reads (`proxy_cli.py:1338-1375`).
# `--use_prisma_db_push` has no envvar; the other two do, and click resolves
# them — so `ENFORCE_PRISMA_MIGRATION_CHECK=true` keeps working exactly as it
# does under the `litellm` CLI today.
PRISMA_KEYS: tuple[str, ...] = (
    "use_prisma_db_push",
    "use_v2_migration_resolver",
    "enforce_prisma_migration_check",
)


def option_values(names: tuple[str, ...]) -> dict[str, Any]:
    """litellm's own click values for ``litellm --config <path> --port 4000``.

    Read off ``run_server.params`` rather than copied: click owns flag
    conversion and envvar resolution (``DEBUG``, ``DETAILED_DEBUG``,
    ``USE_V2_MIGRATION_RESOLVER``, ``ENFORCE_PRISMA_MIGRATION_CHECK``), and a
    hand-copied default would drift on the next bump with nothing saying so. A
    renamed option raises ``KeyError`` here, which is the boot failure we want.
    """
    import click
    from litellm.proxy.proxy_cli import run_server

    params = {param.name: param for param in run_server.params}
    ctx = click.Context(run_server)
    values: dict[str, Any] = {}
    for name in names:
        param = params[name]
        raw = (
            param.value_from_envvar(ctx)
            if param.resolve_envvar_value(ctx) is not None
            else param.default
        )
        values[name] = param.type_cast_value(ctx, raw)
    return values

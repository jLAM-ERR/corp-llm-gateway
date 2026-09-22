"""``python -m corp_llm_gateway.serve`` — the gateway's only launch command.

Replaces ``litellm --config /etc/litellm/config.yaml --port 4000`` in the image
ENTRYPOINT, compose and Helm. It mirrors the uvicorn arguments litellm's CLI
passes (``proxy_cli.py:243-282``): ``server_header=False`` and, when the config
sets ``json_logs: true``, litellm's JSON ``log_config`` — Vector consumes the
container's stdout, so uvicorn's own access and error lines have to be JSON too.

``workers=1``, deliberately. Each uvicorn worker is a separate process that
imports ``corp_llm_gateway.asgi`` for itself, so a second one would run the
entrypoint's Prisma schema sequence concurrently against the same database, and
would build its own Prometheus registry — ``/metrics`` would then report
whichever worker happened to answer the scrape. Scale out with replicas.

The app is passed as an import string, and `json_logs` is read from the YAML (or
litellm's own `JSON_LOGS` env var) rather than from `litellm.json_logs`, because
uvicorn configures logging before it imports the app.
"""

from __future__ import annotations

import logging
from typing import Any

from corp_llm_gateway import config, litellm_config, settings

log = logging.getLogger(__name__)

APP = "corp_llm_gateway.asgi:app"

# The container's own interface, as litellm's CLI defaults to.
_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT = 4000

PORT_KEY = "CORP_LLM_SERVE_PORT"


def _port() -> int:
    raw = (config.get(PORT_KEY, str(_DEFAULT_PORT)) or str(_DEFAULT_PORT)).strip()
    problems: list[str] = []
    # The same check `gateway-admin config check` runs, so a port the CLI would
    # have rejected fails here with the same message and exit code instead of a
    # ValueError traceback out of `int()`.
    settings.check_serve_port({PORT_KEY: raw}, problems)
    if problems:
        for problem in problems:
            log.error("%s", problem)
        raise SystemExit(litellm_config.EXIT_CONFIG)
    return int(raw)


def uvicorn_arguments() -> dict[str, Any]:
    """Everything `uvicorn.run` is given, so a test can read it without binding."""
    arguments: dict[str, Any] = {
        "app": APP,
        "host": config.get("CORP_LLM_SERVE_HOST", _DEFAULT_HOST) or _DEFAULT_HOST,
        "port": _port(),
        "server_header": False,
        "workers": 1,
    }
    if litellm_config.json_logs(litellm_config.config_path()):
        from litellm._logging import _get_uvicorn_json_log_config

        arguments["log_config"] = _get_uvicorn_json_log_config()
    return arguments


def main() -> None:
    import uvicorn

    uvicorn.run(**uvicorn_arguments())


if __name__ == "__main__":
    main()

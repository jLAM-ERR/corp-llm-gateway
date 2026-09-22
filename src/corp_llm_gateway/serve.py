"""``python -m corp_llm_gateway.serve`` — the gateway's only launch command.

Replaces ``litellm --config /etc/litellm/config.yaml --port 4000`` in the image
ENTRYPOINT, compose and Helm. It mirrors the uvicorn arguments litellm's CLI
passes (``proxy_cli.py:243-282``): ``server_header=False`` and, when the config
sets ``json_logs: true``, litellm's JSON ``log_config`` — Vector consumes the
container's stdout, so uvicorn's own access and error lines have to be JSON too.

``workers=1``, deliberately: the route gate arms inside the lifespan of the
process that serves, so a second worker would have to arm itself, and an unarmed
worker answers 503 on every rewritten route. Scale out with replicas.

The app is passed as an import string, and `json_logs` is read from the YAML
rather than from `litellm.json_logs`, because uvicorn configures logging before
it imports the app.
"""

from __future__ import annotations

from typing import Any

from corp_llm_gateway import config, litellm_config

APP = "corp_llm_gateway.asgi:app"

# The container's own interface, as litellm's CLI defaults to.
_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT = 4000


def uvicorn_arguments() -> dict[str, Any]:
    """Everything `uvicorn.run` is given, so a test can read it without binding."""
    arguments: dict[str, Any] = {
        "app": APP,
        "host": config.get("CORP_LLM_SERVE_HOST", _DEFAULT_HOST) or _DEFAULT_HOST,
        "port": int(config.get("CORP_LLM_SERVE_PORT", str(_DEFAULT_PORT)) or _DEFAULT_PORT),
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

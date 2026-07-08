"""Demo-only shim over the production composition root.

Production wiring lives in `corp_llm_gateway.bootstrap`. This module delegates
to `build_guardrail()` with in-memory overrides (a seeded demo token store +
in-memory mapping store) so the laptop demo (`scripts/demo.sh up`) works with no
Postgres/Redis backend. LiteLLM references this module's `guardrail` instance:

    callbacks: ["corp_llm_gateway._demo_guardrail.guardrail"]

All settings resolve through `corp_llm_gateway.config` (env → config.toml →
default); there are no direct `os.environ` reads.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta

from corp_llm_gateway import config
from corp_llm_gateway.bootstrap import build_guardrail
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail
from corp_llm_gateway.storage import InMemoryMappingStore
from corp_llm_gateway.tokens import AuthMiddleware, InMemoryTokenStore, TokenInfo


# Demo-only: silence the container's every-5s healthcheck probe in the logs so
# the demo stream shows the sanitize/desanitize flow, not health-probe spam.
class _DropHealthcheckAccessLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/health/" not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(_DropHealthcheckAccessLogs())

# Claude Code defaults to max_tokens=64000, which overshoots the corp vLLM's
# 65536-token context window on any non-trivial prompt. Clamp for the demo.
_DEMO_MAX_OUTPUT_TOKENS = 4096
_DEMO_LOG_HANDLER_NAME = "corp-demo-stdout"


def _configure_demo_logging() -> None:
    """Surface the gateway's per-request lifecycle in the demo container logs.

    litellm's logging setup suppresses our INFO by default; configure the
    package's parent logger so every child logger inherits it. Overridable via
    ``CORP_LLM_LOG_LEVEL`` (default ``INFO``).
    """
    level_name = (config.get("CORP_LLM_LOG_LEVEL", "INFO") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    pkg_logger = logging.getLogger("corp_llm_gateway")
    pkg_logger.setLevel(level)
    # Idempotent: don't stack handlers if this module is imported twice.
    if not any(h.get_name() == _DEMO_LOG_HANDLER_NAME for h in pkg_logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.set_name(_DEMO_LOG_HANDLER_NAME)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        pkg_logger.addHandler(handler)
    pkg_logger.propagate = False


def _demo_auth() -> AuthMiddleware:
    """In-memory token store seeded with the demo team token."""
    demo_token = config.get("DEMO_TEAM_TOKEN", "demo-team-token") or "demo-team-token"
    store = InMemoryTokenStore()
    now = datetime.now(UTC)
    store.upsert(
        TokenInfo(
            corp_token=demo_token,
            user_id="demo-user",
            team_id="demo-team",
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=365),
        )
    )
    return AuthMiddleware(store)


def _build_demo_guardrail() -> CorpLlmGuardrail:
    return build_guardrail(
        auth_middleware=_demo_auth(),
        mapping_store=InMemoryMappingStore(),
        max_output_tokens_cap=_DEMO_MAX_OUTPUT_TOKENS,
        # hosted_vllm forwards proxy_server_request headers (incl. Host:
        # 127.0.0.1:4000) to the corp ingress, which 503s the unknown vhost.
        strip_inbound_headers_to_upstream=True,
    )


# Configure logging before building so the build's own log lines are captured.
_configure_demo_logging()

# The name the demo litellm-config.yaml references.
guardrail = _build_demo_guardrail()

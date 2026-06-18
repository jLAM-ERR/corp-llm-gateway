"""Demo-only singleton wiring for CorpLlmGuardrail.

LiteLLM's yaml-driven callback registration imports the path you give
it via `spec_from_file_location` and then calls methods on whatever
that path resolves to. If you point it at a class, it tries to call
methods unbound and the call fails with "missing 'self' argument".
The canonical pattern (see
`litellm/proxy/example_config_yaml/custom_callbacks1.py`) is to
register a **module-level instance variable**:

    callbacks: ["corp_llm_gateway._demo_guardrail.guardrail"]

That's what this module provides. Production wires CorpLlmGuardrail
directly in code with real auth/orchestrator/audit; this file exists
so the laptop demo (`scripts/demo.sh up`) can do the same thing
without writing a startup script.

Reads from env at module load:
- ``CORP_LLM_ENDPOINT``         — the corp vLLM URL (required)
- ``CORP_LLM_AUTH_TOKEN``       — Bearer for the corp LLM (optional)
- ``DEMO_TEAM_TOKEN``           — X-Corp-Auth value the demo presents
                                  (default: "demo-team-token"; must match
                                  ``scripts/demo.sh presenter-env``)
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import httpx

from corp_llm_gateway.audit import AuditLogger, StdoutSink
from corp_llm_gateway.auth import BearerAuthProvider, NoopAuthProvider
from corp_llm_gateway.corp_llm import CorpLlmClient
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail
from corp_llm_gateway.rules import Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.storage import InMemoryMappingStore
from corp_llm_gateway.tokens import AuthMiddleware, InMemoryTokenStore, TokenInfo


class _NoTeamRules(RulesLoader):
    """No team-specific rules for the demo; engine still applies the
    PII regex floor + general detector tier."""

    async def load(self, team_id: str) -> Rules:
        return Rules(rules=())


# Per-request output cap. Claude Code defaults to max_tokens=64000
# (Claude Opus value). The corp vLLM has a 65536-token total context
# window, so any non-trivial prompt + 64000 output overshoots. Pass
# this to CorpLlmGuardrail via max_output_tokens_cap.
#
# Why not subclass? litellm's proxy dispatcher checks
# `"async_pre_call_hook" in vars(_callback.__class__)` — that returns
# only attributes defined DIRECTLY on the class, not inherited ones.
# A subclass that overrides only `pre_call` (without overriding
# `async_pre_call_hook`) gets silently skipped.
_DEMO_MAX_OUTPUT_TOKENS = 4096


def _build_demo_guardrail() -> CorpLlmGuardrail:
    corp_endpoint = os.environ.get(
        "CORP_LLM_ENDPOINT", "https://corp-llm.example/v1"
    )
    # CorpLlmClient hardcodes /v1/chat/completions onto its base_url.
    # litellm-config.yaml needs CORP_LLM_ENDPOINT to include /v1 (so
    # hosted_vllm hits .../v1/chat/completions correctly). We want the
    # bare host for CorpLlmClient so it doesn't construct /v1/v1/...
    # Strip a trailing /v1 if present.
    corp_endpoint_root = corp_endpoint.rstrip("/").removesuffix("/v1")
    demo_token = os.environ.get("DEMO_TEAM_TOKEN", "demo-team-token")

    token_store = InMemoryTokenStore()
    now = datetime.now(UTC)
    token_store.upsert(
        TokenInfo(
            corp_token=demo_token,
            user_id="demo-user",
            team_id="demo-team",
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=365),
        )
    )
    auth = AuthMiddleware(token_store)

    # TLS verification defaults to ON. The demo opts out by setting
    # SSL_VERIFY=False on the litellm process env, because the corp
    # LLM uses an internal CA the container doesn't trust. PROD MUST
    # leave SSL_VERIFY unset (or =True) and mount the corp CA bundle
    # into the container's trust store — disabling verification
    # globally opens MITM risk.
    ssl_verify = os.environ.get("SSL_VERIFY", "true").lower() != "false"
    http = httpx.AsyncClient(timeout=30.0, verify=ssl_verify)
    corp_auth_token = os.environ.get("CORP_LLM_AUTH_TOKEN", "")
    auth_provider = (
        BearerAuthProvider(token=corp_auth_token)
        if corp_auth_token
        else NoopAuthProvider()
    )
    corp_llm = CorpLlmClient(
        base_url=corp_endpoint_root,
        model="GLM-5.1-AWQ",
        http=http,
        auth_provider=auth_provider,
    )
    orchestrator = SanitizationOrchestrator(
        corp_llm,
        InMemoryMappingStore(),
        _NoTeamRules(),
    )

    audit_logger = AuditLogger(StdoutSink(), gateway_version="demo")

    return CorpLlmGuardrail(
        orchestrator,
        auth,
        audit_logger,
        max_output_tokens_cap=_DEMO_MAX_OUTPUT_TOKENS,
        # litellm's hosted_vllm provider forwards proxy_server_request
        # headers (including Host: 127.0.0.1:4000) to the corp ingress,
        # which 503s the unknown vhost. Strip them.
        strip_inbound_headers_to_upstream=True,
    )


# This is the name the demo litellm-config.yaml references.
guardrail = _build_demo_guardrail()

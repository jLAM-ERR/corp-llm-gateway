"""LiteLLM hook adapter (M1-7 pre_call + M1-8 post_call wiring).

This is the integration boundary between LiteLLM's proxy and the
corp-llm-gateway sanitization pipeline. The pure logic lives in
SanitizationOrchestrator, AuthMiddleware, AuditLogger, and
StreamingDesanitizer; this file is the thin adapter that plugs them
into LiteLLM's expected callback shape.

LiteLLM's proxy invokes:
  - async_pre_call_hook(user_api_key_dict, cache, data, call_type)
  - async_post_call_success_hook(user_api_key_dict, cache, data, response)
  - async_post_call_streaming_iterator_hook(user_api_key_dict, response, request_data)
  - async_log_success_event(kwargs, response_obj, start_time, end_time)

We register the class via LiteLLM proxy config:
  litellm_settings:
    callbacks: ["corp_llm_gateway.litellm_hook.CorpLlmGuardrail"]

The class is duck-typed; LiteLLM doesn't require strict subclassing.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from corp_llm_gateway.audit import AuditEvent, AuditLogger
from corp_llm_gateway.audit.event import Provider
from corp_llm_gateway.config import get as _config_get
from corp_llm_gateway.corp_llm import CorpLlmHttpError
from corp_llm_gateway.detectors import NerUnavailableError
from corp_llm_gateway.metrics import MetricsExporter, NoopExporter
from corp_llm_gateway.payload.classifier import classify_block
from corp_llm_gateway.payload.size_threshold import OversizeContentError, should_skip_sanitization
from corp_llm_gateway.providers import detect_provider
from corp_llm_gateway.sanitizer import (
    OpenAiToolCallDesanitizer,
    ResponsesStreamDesanitizer,
    SanitizationOrchestrator,
    SanitizeResult,
    SseStreamDesanitizer,
    StrategyResult,
    StreamingDesanitizer,
)
from corp_llm_gateway.sanitizer.content_blocks import (
    ContentTooDeepError,
    UnsanitizableContentBlockError,
    UnsanitizableToolArgumentsError,
    collect_raw_text_leaves,
    collect_responses_item_text,
    collect_text,
    collect_tool_call_text,
    desanitize_content,
    desanitize_responses_payload,
    desanitize_tool_calls,
    message_has_tool_calls,
    sanitize_content,
    sanitize_message,
    sanitize_responses_item,
)
from corp_llm_gateway.sanitizer.dlp_guard import DlpEgressGuard
from corp_llm_gateway.sanitizer.engine import AllStrategiesFailedError
from corp_llm_gateway.sanitizer.identity_preamble import (
    is_identity_preamble,
    leading_identity_block_index,
)
from corp_llm_gateway.sanitizer.placeholder import (
    StaleSpanError,
    add_unwrapped_response_aliases,
    apply_pairs,
    apply_spans,
    build_reverse_substituter,
    find_placeholder_literals,
    find_unwrapped_placeholder_literals,
    placeholder_family,
)
from corp_llm_gateway.sanitizer.placeholder_allocator import (
    RequestPlaceholderAllocator,
)
from corp_llm_gateway.sanitizer.profile_orchestrator import (
    PROFILE_ERRORS,
    ProfileAwareOrchestrator,
    ResolvedProfile,
    passthrough_resolved,
)
from corp_llm_gateway.sanitizer.streaming import _json_string_escape, coerce_tool_index
from corp_llm_gateway.tokens import (
    AuthError,
    AuthMiddleware,
    MissingTokenError,
)

# litellm v1.85's proxy dispatcher filters callbacks via
# `isinstance(cb, CustomLogger)` before invoking any hook method.
# Without the inheritance, our pre_call/post_call hooks are silently
# skipped for /v1/messages requests. Import optionally so unit tests
# that don't have litellm installed still work — production always
# has it.
try:
    from litellm.integrations.custom_logger import (
        CustomLogger as _LitellmCustomLogger,
    )
except ImportError:  # pragma: no cover
    _LitellmCustomLogger = object  # type: ignore[assignment,misc]

# litellm selects its Anthropic OAuth branch by testing the api_key against
# ANTHROPIC_OAUTH_TOKEN_PREFIX (litellm/types/llms/anthropic.py, consumed by
# llms/anthropic/common_utils.py). `_anthropic_upstream_headers` must gate on the
# SAME value, so read it from the installed litellm rather than keep a copy.
# Optional for the same reason as the import above.
try:
    from litellm.types.llms.anthropic import (
        ANTHROPIC_OAUTH_TOKEN_PREFIX as _LITELLM_ANTHROPIC_OAUTH_TOKEN_PREFIX,
    )
except ImportError:  # pragma: no cover
    _LITELLM_ANTHROPIC_OAUTH_TOKEN_PREFIX = None

logger = logging.getLogger(__name__)

# Cap on the audit-idempotency set (bounded FIFO). The two possible audit() calls
# for one request (inline block-audit + any litellm event) happen ms apart, so a
# small window suffices; this just prevents unbounded growth over process life.
_AUDIT_DEDUP_CAP = 4096

# `role` is echoed straight from the request body into a log line. Only these
# known message roles are logged verbatim; anything else logs as "invalid" so a
# caller cannot inject arbitrary (or newline-bearing) text into pod stdout.
_KNOWN_MESSAGE_ROLES = frozenset({"user", "assistant", "system", "tool", "function", "developer"})


def _safe_role_for_log(msg: dict[str, Any]) -> str:
    role = msg.get("role")
    if role is None:
        return "unknown"
    if isinstance(role, str) and role in _KNOWN_MESSAGE_ROLES:
        return role
    return "invalid"


class GuardrailHttpException(Exception):  # noqa: N818 — intentional name; LiteLLM-facing API
    """Raised to signal LiteLLM that the request must be rejected.

    LiteLLM's proxy maps this to an HTTP error response. We carry both
    the status code and a stable error_code so the audit record can
    pin down the failure mode without leaking exception text upstream.
    """

    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        super().__init__(f"{status_code} {error_code}: {message}")
        self.status_code = status_code
        self.error_code = error_code


class CorpLlmGuardrail(_LitellmCustomLogger):
    """LiteLLM custom-callback adapter wiring the sanitization pipeline.

    Inherits from `litellm.integrations.custom_logger.CustomLogger` so
    that litellm's proxy dispatcher recognises this as a hook-eligible
    callback (`isinstance(cb, CustomLogger)` check in
    `proxy/utils.py::pre_call_hook`). Without it, our hook methods are
    silently dropped.
    """

    def __init__(
        self,
        orchestrator: SanitizationOrchestrator | ProfileAwareOrchestrator,
        auth_middleware: AuthMiddleware,
        audit_logger: AuditLogger,
        *,
        max_output_tokens_cap: int | None = None,
        strip_inbound_headers_to_upstream: bool = False,
        forward_chatgpt_auth: bool = False,
        forward_anthropic_auth: bool = False,
        dlp_guard: DlpEgressGuard | None = None,
        metrics: MetricsExporter | None = None,
    ) -> None:
        # Best-effort super().__init__ — when litellm is installed this
        # initializes CustomLogger's internal state; when it isn't
        # (object), this is a no-op kwargs-only call.
        with contextlib.suppress(TypeError):  # pragma: no cover
            super().__init__()
        self._orch = orchestrator
        self._auth = auth_middleware
        self._audit = audit_logger
        # Optional clamp on `max_tokens` in the inbound request, applied
        # before sanitization + upstream call. Used by the laptop demo
        # to keep Claude Code's default 64000 from exceeding the corp
        # vLLM's 65536-token total context window. Default None = no
        # clamp; behaviour is unchanged in production.
        self._max_output_tokens_cap = max_output_tokens_cap
        # When True, strip inbound HTTP client headers from `data` before
        # litellm's provider layer forwards them to the upstream LLM.
        # Some providers (e.g. hosted_vllm) silently pass
        # `proxy_server_request.headers` through, so the upstream sees
        # `Host: 127.0.0.1:4000` and the corp ingress 503s on the unknown
        # vhost. Off by default to preserve existing behaviour.
        self._strip_inbound_headers_to_upstream = strip_inbound_headers_to_upstream
        # Opt-in bridge for a Codex custom provider with
        # ``requires_openai_auth = true``. LiteLLM consumes the client
        # Authorization header at its proxy boundary, so copy only the headers
        # required by the ChatGPT Codex backend into per-request extra_headers.
        self._forward_chatgpt_auth = forward_chatgpt_auth
        # Opt-in bridge for an Anthropic subscription (OAuth) token. Same
        # mechanism as the Codex one — litellm consumes the client
        # Authorization header at its proxy boundary — but gated on the request
        # looking Anthropic-routed so the token cannot be copied onto another
        # provider's upstream call.
        self._forward_anthropic_auth = forward_anthropic_auth
        self._dlp_guard = dlp_guard if dlp_guard is not None else DlpEgressGuard()
        # Pluggable metrics exporter (B4). Default Noop = nothing emitted; a
        # PrometheusExporter (config-selected in the composition root) exposes the
        # block/failure/latency series the shipped alerts + runbook reference.
        self._metrics = metrics if metrics is not None else NoopExporter()
        # Per-request state. Keyed by request_id; cleared in post_call.
        self._req_state: dict[str, _RequestState] = {}
        # Idempotency guard for audit(): litellm does NOT fire
        # async_log_failure_event for a pre_call GuardrailHttpException (confirmed
        # live, v1.85), so Stage-0/Stage-5 blocks audit INLINE. This bounded set
        # keeps audit() exactly-once even if a future litellm version also fires
        # the failure event for the same request_id.
        self._audited_ids: OrderedDict[str, None] = OrderedDict()

    # ---- LiteLLM hook entry points ----------------------------------------

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict[str, Any],
        call_type: str,
    ) -> dict[str, Any]:
        return await self.pre_call(data, call_type=call_type)

    async def async_post_call_streaming_iterator_hook(
        self,
        user_api_key_dict: Any,
        response: AsyncIterator[Any],
        request_data: dict[str, Any],
    ) -> AsyncGenerator[Any, None]:
        async for chunk in self.post_call_stream(request_data, response):
            yield chunk

    async def async_post_call_success_hook(
        self,
        data: dict[str, Any],
        user_api_key_dict: Any,
        response: Any,
    ) -> Any:
        # NOTE: litellm v1.85 dropped `cache` from this hook's signature
        # and reordered to (data, user_api_key_dict, response). Earlier
        # litellm versions had (user_api_key_dict, cache, data, response).
        # If you upgrade or downgrade litellm and see "missing positional
        # argument" errors here, that's the signature drift to check.
        return await self.post_call_unary(data, response)

    async def async_log_success_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: float,
        end_time: float,
    ) -> None:
        request_data = _resolve_request_data(kwargs)
        await self.audit(request_data, response_obj, start_time, end_time, status="ok")

    async def async_log_failure_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: float,
        end_time: float,
    ) -> None:
        request_data = _resolve_request_data(kwargs)
        await self.audit(request_data, response_obj, start_time, end_time, status="failed")

    # ---- Pure logic (unit-testable without LiteLLM) -----------------------

    async def pre_call(
        self, data: dict[str, Any], *, call_type: str | None = None
    ) -> dict[str, Any]:
        """Sanitize a request body in-place; return the mutated dict.

        If `max_output_tokens_cap` was passed to __init__, clamp the
        request's `max_tokens` before any other step. This stops
        Claude-Code-style requests with a huge default output budget
        from overshooting the upstream model's context window.

        Order: auth → strip corp token → sanitize messages → return.
        Failures are mapped to GuardrailHttpException with stable
        error_code so post-call audit can attribute the failure.

        *call_type*: litellm's per-endpoint call type. `data["input"]`
        means something completely different across endpoints — a Responses
        API items list, but ALSO the raw text/tokens `/v1/embeddings` and
        `/v1/moderations` send. Without this, both got routed through the
        Responses item walker: embeddings got vectorized on placeholder text,
        moderation got scored on redacted text. `None` (every existing direct
        `pre_call()` call site/test) preserves today's behavior; only the real
        `async_pre_call_hook` wiring supplies a call_type.
        """
        request_id = self._ensure_request_id(data)
        model = str(data.get("model") or "unknown")
        raw_messages, request_shape = _request_items(data, call_type)
        message_count = len(raw_messages) if isinstance(raw_messages, list) else 0
        logger.info(
            "litellm_pre_call_received request_id=%s model=%s message_count=%d",
            request_id,
            model,
            message_count,
        )

        # Optional max_tokens clamp — first thing so litellm's own
        # validation and the upstream call both see the capped value.
        if self._max_output_tokens_cap is not None:
            mt = data.get("max_tokens")
            if isinstance(mt, int) and mt > self._max_output_tokens_cap:
                logger.info(
                    "litellm_pre_call_max_tokens_clamped request_id=%s requested=%d capped=%d",
                    request_id,
                    mt,
                    self._max_output_tokens_cap,
                )
                data["max_tokens"] = self._max_output_tokens_cap

        inbound_headers = _extract_auth_headers(data)
        try:
            ctx = await self._auth.authenticate_headers(inbound_headers)
        except MissingTokenError:
            logger.info(
                "litellm_pre_call_auth_failed request_id=%s error_code=E_MISSING_TOKEN",
                request_id,
            )
            self._record_failure(request_id, error_code="E_MISSING_TOKEN")
            _now = datetime.now(UTC)
            await self.audit(data, None, _now, _now, status="failed", error_code="E_MISSING_TOKEN")
            raise GuardrailHttpException(401, "E_MISSING_TOKEN", "missing X-Corp-Auth") from None
        except AuthError as exc:
            error_code = _classify_auth_error(exc)
            logger.info(
                "litellm_pre_call_auth_failed request_id=%s error_code=%s",
                request_id,
                error_code,
            )
            self._record_failure(request_id, error_code=error_code)
            _now = datetime.now(UTC)
            await self.audit(data, None, _now, _now, status="failed", error_code=error_code)
            raise GuardrailHttpException(401, error_code, str(exc)) from exc

        logger.info(
            "litellm_pre_call_auth_ok request_id=%s team_id=%s user_id=%s",
            request_id,
            ctx.team_id,
            ctx.user_id,
        )

        data["headers"] = self._auth.strip_corp_token(_extract_headers(data))
        # The corp token arrives duplicated across every header-bearing location
        # and some providers forward proxy_server_request.headers upstream, so
        # strip it from ALL of them, not just data["headers"] (invariant 4).
        _strip_corp_token_everywhere(data)
        logger.info(
            "litellm_pre_call_corp_token_stripped request_id=%s",
            request_id,
        )

        # Optional: strip inbound HTTP wire headers from data so litellm
        # providers (notably hosted_vllm) don't forward them upstream.
        # `Host: 127.0.0.1:4000` going to the corp ingress earns a
        # vhost-not-found 503. Only nuke the hop-by-hop / wire-level
        # headers; preserve protocol-meaningful ones like
        # `anthropic-version` and `authorization` (BYOK passthrough).
        if self._strip_inbound_headers_to_upstream:
            _drop_wire_headers(data.get("headers"))
            proxy_req = data.get("proxy_server_request")
            if isinstance(proxy_req, dict):
                _drop_wire_headers(proxy_req.get("headers"))
            md = data.get("litellm_metadata")
            if isinstance(md, dict):
                _drop_wire_headers(md.get("headers"))

        # Pure function of data["model"] — hoisted above both auth bridges so the
        # Anthropic bridge can gate on it and either bridge's rejection can
        # attribute its audit record; reused for _RequestState below.
        provider = _detect_provider(data)

        if self._forward_chatgpt_auth:
            try:
                upstream_headers = _chatgpt_upstream_headers(inbound_headers)
                authorization = next(
                    value
                    for name, value in upstream_headers.items()
                    if name.lower() == "authorization"
                )
                # The OpenAI provider writes its configured api_key after
                # extra_headers. Override that placeholder per request and let
                # LiteLLM construct the upstream Authorization header.
                data["api_key"] = authorization[7:].strip()
                data["extra_headers"] = {
                    name: value
                    for name, value in upstream_headers.items()
                    if name.lower() != "authorization"
                }
                # LiteLLM injects request/accounting metadata into every proxy
                # call. The ChatGPT Codex backend rejects this otherwise valid
                # Responses API parameter. Correlation remains available via
                # the top-level and litellm_metadata request-id copies.
                data.pop("metadata", None)
                _scrub_retained_request_metadata(data)
            except ValueError:
                self._seed_request_state(
                    request_id,
                    user_id=ctx.user_id,
                    team_id=ctx.team_id,
                    provider=provider,
                    model=model,
                )
                self._record_failure(request_id, error_code="E_PROVIDER_AUTH")
                logger.info(
                    "litellm_pre_call_provider_auth_failed request_id=%s "
                    "error_code=E_PROVIDER_AUTH",
                    request_id,
                )
                _now = datetime.now(UTC)
                await self.audit(
                    data,
                    None,
                    _now,
                    _now,
                    status="failed",
                    error_code="E_PROVIDER_AUTH",
                )
                raise GuardrailHttpException(
                    401,
                    "E_PROVIDER_AUTH",
                    "missing or invalid OpenAI bearer authentication",
                ) from None

        # Anthropic subscription (OAuth) bridge. Publishing the developer's
        # sk-ant-oat token as the per-request api_key is what selects litellm's
        # own OAuth branch (llms/anthropic/common_utils.py), which then emits
        # `Authorization: Bearer` plus the oauth beta header upstream and
        # suppresses x-api-key.
        #
        # LIMITATION: this gate reads the client-visible model alias, NOT the
        # resolved deployment — litellm picks that after the hook runs, so on a
        # wildcard route a `claude-…` alias can still reach a non-Anthropic
        # upstream. It is defence-in-depth against copying the token onto an
        # OpenAI/vLLM call, not a guarantee; the binding control is deploying
        # this flag only against a config whose every route is `anthropic/`.
        if self._forward_anthropic_auth and provider == "anthropic":
            try:
                upstream_headers = _anthropic_upstream_headers(inbound_headers)
                authorization = next(
                    value
                    for name, value in upstream_headers.items()
                    if name.lower() == "authorization"
                )
                data["api_key"] = authorization[7:].strip()
                data["extra_headers"] = {
                    name: value
                    for name, value in upstream_headers.items()
                    if name.lower() != "authorization"
                }
                # `metadata` and a top-level `user` are the two request fields
                # that egress to Anthropic without ever passing through
                # sanitization: on the chat-completions adapter litellm maps
                # `user` to metadata.user_id and copies metadata.user_id into
                # the outbound body, rejecting only complete email/phone shapes.
                # Dropped rather than sanitized — litellm's proxy fills
                # `metadata` with dozens of internal, non-wire keys, so there is
                # nothing to narrow to, and neither field is desanitized on the
                # response path. Request correlation survives via the top-level
                # and litellm_metadata request-id copies.
                # The primary /v1/messages route uses the pass-through
                # transformer, which does not support `metadata` at all; this
                # scrub matters because the bridge is gated by provider, not by
                # call_type, so a claude-* request arriving on
                # /v1/chat/completions still reaches the leaking adapter.
                data.pop("metadata", None)
                data.pop("user", None)
                _scrub_retained_request_metadata(data)
            except ValueError:
                self._seed_request_state(
                    request_id,
                    user_id=ctx.user_id,
                    team_id=ctx.team_id,
                    provider=provider,
                    model=model,
                )
                self._record_failure(request_id, error_code="E_PROVIDER_AUTH")
                logger.info(
                    "litellm_pre_call_provider_auth_failed request_id=%s "
                    "error_code=E_PROVIDER_AUTH",
                    request_id,
                )
                _now = datetime.now(UTC)
                await self.audit(
                    data,
                    None,
                    _now,
                    _now,
                    status="failed",
                    error_code="E_PROVIDER_AUTH",
                )
                raise GuardrailHttpException(
                    401,
                    "E_PROVIDER_AUTH",
                    "missing or invalid Anthropic OAuth bearer authentication",
                ) from None

        messages = raw_messages

        def _item_text(msg: dict[str, Any] | str) -> list[str]:
            # A bare string element of the list (e.g. data["input"] = ["...", {...}])
            # IS the text itself — not a dict to route by shape.
            if isinstance(msg, str):
                return [msg] if msg else []
            # Chat Completions/Anthropic messages stay on the item-type-keyed
            # walkers (unchanged behavior). Responses items (function_call,
            # custom_tool_call, reasoning, …) route through the field-name-keyed
            # walker so every text-bearing field is visible here, not just the
            # enumerated item types (see content_blocks.sanitize_responses_item).
            if request_shape == "messages":
                return collect_text(msg.get("content")) + collect_tool_call_text(msg)
            return collect_responses_item_text(msg)

        if not isinstance(messages, list):
            logger.info(
                "litellm_pre_call_bad_request request_id=%s error_code=E_BAD_REQUEST",
                request_id,
            )
            self._record_failure(request_id, error_code="E_BAD_REQUEST")
            _now = datetime.now(UTC)
            await self.audit(data, None, _now, _now, status="failed", error_code="E_BAD_REQUEST")
            raise GuardrailHttpException(
                400,
                "E_BAD_REQUEST",
                "messages/input must be a list or input must be a string",
            )

        state = _RequestState(
            request_id=request_id,
            user_id=ctx.user_id,
            team_id=ctx.team_id,
            provider=provider,
            model=model,
            redaction_count=0,
            placeholders=[],
            cache_a_hit=False,
            mapping=StrategyResult(pairs=()),
        )
        self._req_state[request_id] = state

        # A client fully controls the request body: {"messages": [], "input": [...]}
        # made `_request_items` pick the empty `messages` list while `input` egressed
        # untouched, bypassing sanitize, Stage 0 and Stage 5 entirely. Refuse the
        # ambiguous shape outright rather than guess which one is real.
        if "messages" in data and "input" in data:
            state.block_reason = "request:ambiguous_shape"
            self._record_failure(request_id, error_code="E_POLICY_BLOCKED")
            self._metrics.record_block(state.block_reason)
            logger.info(
                "litellm_pre_call_blocked request_id=%s block_reason=%s",
                request_id,
                state.block_reason,
            )
            _now = datetime.now(UTC)
            await self.audit(data, None, _now, _now, status="failed")
            raise GuardrailHttpException(
                422,
                "E_POLICY_BLOCKED",
                "request blocked: payload carries both messages and input",
            )

        # D4: resolve the team's merged profile once per request. Empty
        # profile_ids → passthrough (default policy, no fingerprint) == today.
        # A misconfigured profile fails CLOSED — never fall through to
        # un-profiled egress (invariant 6).
        try:
            resolved = await self._resolve_profile(ctx.team_id)
        except PROFILE_ERRORS as exc:
            self._record_failure(request_id, error_code="E_PROFILE_UNAVAILABLE")
            logger.warning(
                "litellm_pre_call_profile_unavailable request_id=%s team_id=%s exception=%s",
                request_id,
                ctx.team_id,
                type(exc).__name__,
            )
            _now = datetime.now(UTC)
            await self.audit(data, None, _now, _now, status="failed")
            raise GuardrailHttpException(
                503,
                "E_PROFILE_UNAVAILABLE",
                "profile configuration unavailable",
            ) from exc
        state.profile_ids = resolved.profile_ids

        # allowed_providers gate: the merged policy may restrict egress targets
        # (None == unrestricted). Reject a banned provider before any content
        # processing — a clean policy denial, no raw body.
        allowed_providers = resolved.policy.allowed_providers
        if allowed_providers is not None and provider not in allowed_providers:
            state.block_reason = "provider:not_allowed"
            self._record_failure(request_id, error_code="E_PROVIDER_BLOCKED")
            self._metrics.record_block("provider:not_allowed")
            logger.info(
                "litellm_pre_call_provider_blocked request_id=%s provider=%s",
                request_id,
                provider,
            )
            _now = datetime.now(UTC)
            await self.audit(data, None, _now, _now, status="failed")
            raise GuardrailHttpException(
                403,
                "E_PROVIDER_BLOCKED",
                "request blocked: provider not allowed by policy",
            )

        # One allocator per request. The corp-LLM numbers placeholders from
        # [LABEL_001] independently for each segment's sanitize() call, so
        # distinct originals across segments (e.g. a system-blob email and a
        # message email) collide on the same token. Remap every segment to a
        # request-canonical placeholder: the same original reuses one token and
        # different originals never share one — otherwise de-sanitization
        # (keyed by placeholder) can only restore one of them. See
        # project_placeholder_collision_cross_segment.
        allocator = RequestPlaceholderAllocator()

        # SECURITY: forbid any placeholder a real redaction might reuse that the
        # user already typed literally in the input — otherwise the user's literal
        # is reversed to the original, and it can be a sanitizer-probing attempt.
        input_literals: list[str] = []
        input_unwrapped_literals: set[str] = set()
        # Minor: response_alias_exclusions is only ever read by _response_mapping
        # when include_bare_aliases is True (forward_chatgpt_auth on) — skip the
        # extra find_unwrapped_placeholder_literals scan over every segment when
        # the flag is off, since the result would never be used.
        for _m in messages:
            if isinstance(_m, (dict, str)):
                for _seg in _item_text(_m):
                    input_literals.extend(find_placeholder_literals(_seg))
                    if self._forward_chatgpt_auth:
                        input_unwrapped_literals.update(find_unwrapped_placeholder_literals(_seg))
        for _prompt_field in ("system", "instructions"):
            for _seg in collect_text(data.get(_prompt_field)):
                input_literals.extend(find_placeholder_literals(_seg))
                if self._forward_chatgpt_auth:
                    input_unwrapped_literals.update(find_unwrapped_placeholder_literals(_seg))
        state.response_alias_exclusions = input_unwrapped_literals
        if input_literals:
            allocator.forbid(input_literals)
            logger.warning(
                "litellm_pre_call_input_placeholder_literal_detected request_id=%s count=%d",
                request_id,
                len(input_literals),
            )

        # Stage 0: payload classifier — block config/log dumps before egress (R10/R11).
        # Runs after auth + _RequestState so the block carries user/team attribution.
        # The merged profile policy can only ADD the block (monotone-tightening).
        if _config_get("CORP_LLM_BLOCK_PAYLOADS", "1") != "0" or resolved.policy.block_payloads:
            _s0_texts: list[str] = []
            for _s0_msg in messages:
                if isinstance(_s0_msg, (dict, str)):
                    _s0_texts.extend(_item_text(_s0_msg))
            if request_shape == "unmanaged" and "input" in data:
                # Same blind spot Stage 5 already closed: a non-chat call_type
                # (embeddings/moderations/pass-through) never gets `input`
                # rewritten, but a config/secret dump there must still be
                # visible to the pre-egress classifier, not just the DLP guard.
                _s0_unmanaged_texts = collect_raw_text_leaves(data.get("input"))
                await self._guard_unmanaged_input_size(
                    request_id, state, data, resolved, _s0_unmanaged_texts
                )
                _s0_texts.extend(_s0_unmanaged_texts)
            for _prompt_field in ("system", "instructions"):
                _s0_texts.extend(collect_text(data.get(_prompt_field)))
            _s0_reason = classify_block("\n".join(_s0_texts))
            if _s0_reason is not None:
                state.block_reason = _s0_reason
                self._record_failure(request_id, error_code="E_POLICY_BLOCKED")
                self._metrics.record_block(_s0_reason)
                logger.info(
                    "litellm_pre_call_blocked request_id=%s block_reason=%s",
                    request_id,
                    _s0_reason,
                )
                # litellm does NOT fire async_log_failure_event for a pre_call
                # rejection (confirmed live, v1.85), so audit the block INLINE —
                # otherwise it is never recorded (R13). audit() is idempotent via
                # self._audited_ids, so this stays exactly-once even if a future
                # litellm version also fires the failure event.
                _now = datetime.now(UTC)
                await self.audit(data, None, _now, _now, status="failed")
                raise GuardrailHttpException(
                    422,
                    "E_POLICY_BLOCKED",
                    "request blocked by content policy",
                )

        async def sanitize_one(text: str) -> SanitizeResult:
            # Route through the resolved inner orchestrator; it folds the D3
            # profile fingerprint into Cache A (None for the no-profile case).
            result = await resolved.sanitize(
                text,
                team_id=ctx.team_id,
                conversation_id=request_id,
            )
            if not result.pairs:
                return result
            # Rule-derived originals (case-insensitive matches sharing one
            # CONFIGURED replacement) are exempt from the bijection re-mint —
            # an operator-configured replacement is intentionally many-to-one.
            canonical_pairs = allocator.remap(
                result.pairs, exempt_from_bijection=result.rule_originals
            )
            if canonical_pairs == result.pairs:
                return result
            # Re-derive from the ORIGINAL segment using the exact selected spans.
            # Legacy/custom orchestrators without span metadata retain the old
            # longest-original-first fallback.
            sanitized_text = (
                apply_spans(text, result.applied_spans, canonical_pairs)
                if result.applied_spans
                else apply_pairs(text, canonical_pairs)
            )
            return SanitizeResult(
                sanitized_text=sanitized_text,
                pairs=canonical_pairs,
                cache_a_hit=result.cache_a_hit,
                skipped=result.skipped,
                block_reason=result.block_reason,
                applied_spans=result.applied_spans,
                rule_originals=result.rule_originals,
            )

        for i, msg in enumerate(messages):
            if not isinstance(msg, (dict, str)):
                continue
            # A bare string element of the list (e.g. input=["leak here", {...}])
            # IS the text itself — not a dict, but still sanitizable, not skippable.
            content = msg.get("content") if isinstance(msg, dict) else msg
            content_empty = content is None or (isinstance(content, str) and not content)
            # A tool-call-only assistant message (content=None) still carries
            # sanitizable data in tool_calls[].function.arguments (F4) — process it.
            # Same for a Responses item with no "content" at all (function_call,
            # custom_tool_call, reasoning, …) — _item_text sees its other
            # text-bearing fields (arguments/output/input/summary/refusal).
            if not isinstance(msg, dict):
                has_sanitizable_data = False
            elif request_shape == "messages":
                has_sanitizable_data = message_has_tool_calls(msg)
            else:
                # any(), not bool(): _item_text can return [""] for an explicitly
                # empty text field, which is truthy as a list but carries no data.
                has_sanitizable_data = any(_item_text(msg))
            if content_empty and not has_sanitizable_data:
                logger.info(
                    "litellm_pre_call_message_skipped request_id=%s "
                    "message_index=%d reason=empty_or_non_string",
                    request_id,
                    i,
                )
                continue

            # Compute content byte size for logging (never logs content bodies).
            if isinstance(content, str):
                content_bytes = len(content.encode("utf-8"))
            elif content is not None:
                content_bytes = len(json.dumps(content).encode("utf-8"))
            else:
                # content is None only reachable via msg.get("content") above,
                # i.e. msg is a dict (a bare string msg IS its own content, a
                # str, caught by the first branch).
                assert isinstance(msg, dict)
                content_bytes = len(
                    json.dumps(
                        msg.get("tool_calls") or msg.get("function_call") or msg, default=str
                    ).encode("utf-8")
                )

            logger.info(
                "litellm_pre_call_message_sanitize_start request_id=%s "
                "message_index=%d role=%s content_bytes=%d",
                request_id,
                i,
                _safe_role_for_log(msg) if isinstance(msg, dict) else "unknown",
                content_bytes,
            )
            new_msg: str | dict[str, Any]
            results: list[Any]
            try:
                if isinstance(msg, str):
                    _str_result = await sanitize_one(msg)
                    new_msg, results = _str_result.sanitized_text, [_str_result]
                elif request_shape == "messages":
                    new_msg, results = await sanitize_message(msg, sanitize_one)
                else:
                    new_msg, results = await sanitize_responses_item(msg, sanitize_one)
            except ContentTooDeepError as exc:
                self._record_failure(request_id, error_code="E_BAD_REQUEST")
                _now = datetime.now(UTC)
                await self.audit(data, None, _now, _now, status="failed")
                raise GuardrailHttpException(
                    400,
                    "E_BAD_REQUEST",
                    "request content nesting too deep",
                ) from exc
            except UnsanitizableToolArgumentsError as exc:
                # Fail closed: a tool-call arguments shape we cannot scan must not egress.
                self._record_failure(request_id, error_code="E_BAD_REQUEST")
                _now = datetime.now(UTC)
                await self.audit(data, None, _now, _now, status="failed")
                raise GuardrailHttpException(
                    400,
                    "E_BAD_REQUEST",
                    "unsupported tool-call arguments shape",
                ) from exc
            except UnsanitizableContentBlockError as exc:
                # Fail closed: an unrecognized content block type must not egress unscanned.
                self._record_failure(request_id, error_code="E_BAD_REQUEST")
                _now = datetime.now(UTC)
                await self.audit(data, None, _now, _now, status="failed")
                raise GuardrailHttpException(
                    400,
                    "E_BAD_REQUEST",
                    "unsupported content block type",
                ) from exc
            except OversizeContentError as exc:
                # F1: fail-closed on an oversize leaf. Never forward the original.
                state.block_reason = "oversize:blocked"
                self._record_failure(request_id, error_code="E_OVERSIZE_BLOCKED")
                self._metrics.record_block("oversize:blocked")
                logger.info(
                    "litellm_pre_call_oversize_blocked request_id=%s message_index=%d "
                    "error_code=E_OVERSIZE_BLOCKED content_bytes=%d threshold_bytes=%d",
                    request_id,
                    i,
                    exc.content_bytes,
                    exc.threshold_bytes,
                )
                _now = datetime.now(UTC)
                await self.audit(data, None, _now, _now, status="failed")
                raise GuardrailHttpException(
                    422,
                    "E_OVERSIZE_BLOCKED",
                    "request blocked: oversize content",
                ) from exc
            except (CorpLlmHttpError, AllStrategiesFailedError) as exc:
                # Fail-policy matrix (plan M4 / docs/ops/runbook.md): a
                # corp-LLM sanitization failure is fail-CLOSED. We must
                # NEVER forward un-sanitized content upstream when the
                # sanitizer can't run, so map it to the documented
                # 503 E_CORP_LLM_DOWN. Without this, the raw exception
                # escaped pre_call and litellm wrapped it as a generic
                # 500 — and httpx timeouts stringify to '', so the leaked
                # body read "corp-llm transport error: " (no detail).
                # Log the exception TYPE; keep the client message stable
                # and content-free.
                logger.warning(
                    "litellm_pre_call_corp_llm_failed request_id=%s "
                    "message_index=%d error_code=E_CORP_LLM_DOWN exception=%s",
                    request_id,
                    i,
                    type(exc).__name__,
                )
                self._record_failure(request_id, error_code="E_CORP_LLM_DOWN")
                _now = datetime.now(UTC)
                await self.audit(data, None, _now, _now, status="failed")
                raise GuardrailHttpException(
                    503,
                    "E_CORP_LLM_DOWN",
                    "corp sanitization LLM unavailable",
                ) from exc
            except NerUnavailableError as exc:
                # F2 fail-closed (M4): a REQUIRED NER engine's model is absent in
                # this build. Refuse egress — never forward content a PERSON/ORG
                # detector would have redacted. 503, distinct from E_CORP_LLM_DOWN
                # so a missing NER model is not confused with an oracle outage.
                logger.warning(
                    "litellm_pre_call_ner_unavailable request_id=%s "
                    "message_index=%d error_code=E_NER_UNAVAILABLE exception=%s",
                    request_id,
                    i,
                    type(exc).__name__,
                )
                self._record_failure(request_id, error_code="E_NER_UNAVAILABLE")
                _now = datetime.now(UTC)
                await self.audit(data, None, _now, _now, status="failed")
                raise GuardrailHttpException(
                    503,
                    "E_NER_UNAVAILABLE",
                    "NER detector unavailable",
                ) from exc
            except StaleSpanError as exc:
                # M4 fail-policy matrix: the pre-selected replacement span pool no
                # longer matches this segment's text (e.g. a stale Cache-A/allocator
                # remap). Fails closed — never forward content we can't safely
                # reconstruct. Log the exception TYPE only; the message itself is
                # already content-free (no original text) by construction.
                logger.warning(
                    "litellm_pre_call_stale_span request_id=%s message_index=%d "
                    "error_code=%s exception=%s",
                    request_id,
                    i,
                    exc.error_code,
                    type(exc).__name__,
                )
                self._record_failure(request_id, error_code=exc.error_code)
                _now = datetime.now(UTC)
                await self.audit(data, None, _now, _now, status="failed")
                raise GuardrailHttpException(
                    500,
                    exc.error_code,
                    "internal sanitization error",
                ) from exc

            messages[i] = new_msg
            # Merge every segment result; emit one done-log per MESSAGE (D).
            msg_placeholders: set[str] = set()
            for result in results:
                self._merge_into_state(state, result)
                msg_placeholders.update(ph for _, ph in result.pairs)
            logger.info(
                "litellm_pre_call_message_sanitize_done request_id=%s "
                "message_index=%d redaction_count=%d",
                request_id,
                i,
                len(msg_placeholders),
            )

        _store_request_items(data, messages, request_shape)

        # Chat Completions uses ``system`` while Responses uses ``instructions``.
        # A well-formed request carries at most one, but nothing rejects a
        # payload carrying both — sanitize whichever are present rather than
        # picking one via a ternary (defect #2: the other egressed raw).
        for prompt_field in ("system", "instructions"):
            await self._sanitize_prompt_field(data, prompt_field, request_id, state, sanitize_one)

        # Stage 5: DLP egress guard — re-scan the SANITIZED outbound request.
        # Defence-in-depth: catches canaries / raw secrets that survived the
        # primary sanitizer. Audit the block INLINE (idempotent) — litellm does
        # not fire the failure event for pre_call rejections; see Stage 0.
        # The merged profile policy can only ADD scanning: dlp_guard forces the
        # stage on and canary_patterns are enforced on top of the base guard.
        _s5_policy = resolved.policy
        if (
            _config_get("CORP_LLM_DLP_GUARD", "1") != "0"
            or _s5_policy.dlp_guard
            or _s5_policy.canary_patterns
        ):
            _s5_texts: list[str] = []
            _s5_messages, _s5_shape = _request_items(data, call_type)
            for _s5_msg in _s5_messages or []:
                if isinstance(_s5_msg, (dict, str)):
                    _s5_texts.extend(_item_text(_s5_msg))
            if _s5_shape == "unmanaged" and "input" in data:
                # A non-chat endpoint (embeddings/moderations) never gets its
                # `input` rewritten, but it must still be visible to this
                # scan — an unmanaged call_type must not become a DLP blind
                # spot just because its content is never sanitized.
                _s5_unmanaged_texts = collect_raw_text_leaves(data.get("input"))
                await self._guard_unmanaged_input_size(
                    request_id, state, data, resolved, _s5_unmanaged_texts
                )
                _s5_texts.extend(_s5_unmanaged_texts)
            for _prompt_field in ("system", "instructions"):
                _s5_texts.extend(collect_text(data.get(_prompt_field)))
            _s5_joined = "\n".join(_s5_texts)
            _s5_reason = self._dlp_guard.scan(_s5_joined)
            if _s5_reason is None and _s5_policy.canary_patterns:
                _s5_reason = DlpEgressGuard(
                    canary_patterns=list(_s5_policy.canary_patterns),
                    secret_rescan=False,
                ).scan(_s5_joined)
            if _s5_reason is not None:
                state.block_reason = _s5_reason
                self._record_failure(request_id, error_code="E_DLP_BLOCKED")
                self._metrics.record_block(_s5_reason)
                logger.info(
                    "litellm_egress_blocked request_id=%s block_reason=%s",
                    request_id,
                    _s5_reason,
                )
                _now = datetime.now(UTC)
                await self.audit(data, None, _now, _now, status="failed")
                raise GuardrailHttpException(
                    422,
                    "E_DLP_BLOCKED",
                    "request blocked by DLP egress policy",
                )

        logger.info(
            "litellm_pre_call_complete request_id=%s team_id=%s provider=%s "
            "model=%s total_redactions=%d placeholder_count=%d",
            request_id,
            ctx.team_id,
            provider,
            model,
            state.redaction_count,
            len(state.placeholders),
        )
        return data

    async def _sanitize_prompt_field(
        self,
        data: dict[str, Any],
        prompt_field: str,
        request_id: str,
        state: _RequestState,
        sanitize_one: Callable[[str], Awaitable[SanitizeResult]],
    ) -> None:
        """Sanitize ``data[prompt_field]`` in place (``"system"`` or ``"instructions"``).

        Extracted so `pre_call` can drive both fields through the identical
        fail-closed/audit behavior instead of picking one via a ternary.

        Anthropic's OAuth ``/v1/messages`` route accepts a request as a Claude
        Code request on the strength of the LEADING ``system`` block, matched by
        exact equality, so that one block is exempt from rewriting (see
        ``sanitizer/identity_preamble``). The exemption is claimed here, not in
        the per-leaf orchestrator: the orchestrator has no field or position
        context, so claiming it there also exempted an identity literal pasted
        into ``instructions``, a user message, a ``tool_result`` or a
        ``document`` — leaves an operator rule or gazetteer entry must still be
        able to redact.
        """
        system = data.get(prompt_field)
        if not system:
            return
        if isinstance(system, str):
            system_bytes = len(system.encode("utf-8"))
        else:
            system_bytes = len(json.dumps(system).encode("utf-8"))
        logger.info(
            "litellm_pre_call_system_sanitize_start request_id=%s field=%s content_bytes=%d",
            request_id,
            prompt_field,
            system_bytes,
        )
        exempt_index: int | None = None
        if prompt_field == "system":
            if isinstance(system, str) and is_identity_preamble(system):
                logger.info(
                    "litellm_pre_call_identity_preamble_passthrough request_id=%s field=%s",
                    request_id,
                    prompt_field,
                )
                return
            exempt_index = leading_identity_block_index(system)
        try:
            if exempt_index is None:
                new_system, results = await sanitize_content(system, sanitize_one)
            else:
                logger.info(
                    "litellm_pre_call_identity_preamble_passthrough request_id=%s "
                    "field=%s block_index=%d",
                    request_id,
                    prompt_field,
                    exempt_index,
                )
                exempt_block = system[exempt_index]
                rest = list(system)
                del rest[exempt_index]
                new_rest, results = await sanitize_content(rest, sanitize_one)
                new_system = [
                    *new_rest[:exempt_index],
                    exempt_block,
                    *new_rest[exempt_index:],
                ]
        except ContentTooDeepError as exc:
            self._record_failure(request_id, error_code="E_BAD_REQUEST")
            _now = datetime.now(UTC)
            await self.audit(data, None, _now, _now, status="failed")
            raise GuardrailHttpException(
                400,
                "E_BAD_REQUEST",
                "request content nesting too deep",
            ) from exc
        except UnsanitizableContentBlockError as exc:
            # Fail closed: an unrecognized content block type must not egress unscanned.
            self._record_failure(request_id, error_code="E_BAD_REQUEST")
            _now = datetime.now(UTC)
            await self.audit(data, None, _now, _now, status="failed")
            raise GuardrailHttpException(
                400,
                "E_BAD_REQUEST",
                "unsupported content block type",
            ) from exc
        except OversizeContentError as exc:
            # F1: fail-closed on an oversize leaf. Never forward the original.
            state.block_reason = "oversize:blocked"
            self._record_failure(request_id, error_code="E_OVERSIZE_BLOCKED")
            self._metrics.record_block("oversize:blocked")
            logger.info(
                "litellm_pre_call_oversize_blocked request_id=%s field=%s "
                "error_code=E_OVERSIZE_BLOCKED content_bytes=%d threshold_bytes=%d",
                request_id,
                prompt_field,
                exc.content_bytes,
                exc.threshold_bytes,
            )
            _now = datetime.now(UTC)
            await self.audit(data, None, _now, _now, status="failed")
            raise GuardrailHttpException(
                422,
                "E_OVERSIZE_BLOCKED",
                "request blocked: oversize content",
            ) from exc
        except (CorpLlmHttpError, AllStrategiesFailedError) as exc:
            logger.warning(
                "litellm_pre_call_corp_llm_failed request_id=%s "
                "field=%s error_code=E_CORP_LLM_DOWN exception=%s",
                request_id,
                prompt_field,
                type(exc).__name__,
            )
            self._record_failure(request_id, error_code="E_CORP_LLM_DOWN")
            _now = datetime.now(UTC)
            await self.audit(data, None, _now, _now, status="failed")
            raise GuardrailHttpException(
                503,
                "E_CORP_LLM_DOWN",
                "corp sanitization LLM unavailable",
            ) from exc
        except NerUnavailableError as exc:
            # F2 fail-closed (M4): required NER unavailable on this field.
            logger.warning(
                "litellm_pre_call_ner_unavailable request_id=%s "
                "field=%s error_code=E_NER_UNAVAILABLE exception=%s",
                request_id,
                prompt_field,
                type(exc).__name__,
            )
            self._record_failure(request_id, error_code="E_NER_UNAVAILABLE")
            _now = datetime.now(UTC)
            await self.audit(data, None, _now, _now, status="failed")
            raise GuardrailHttpException(
                503,
                "E_NER_UNAVAILABLE",
                "NER detector unavailable",
            ) from exc
        except StaleSpanError as exc:
            # M4 fail-policy matrix: same fail-closed mapping as the messages loop.
            logger.warning(
                "litellm_pre_call_stale_span request_id=%s field=%s error_code=%s exception=%s",
                request_id,
                prompt_field,
                exc.error_code,
                type(exc).__name__,
            )
            self._record_failure(request_id, error_code=exc.error_code)
            _now = datetime.now(UTC)
            await self.audit(data, None, _now, _now, status="failed")
            raise GuardrailHttpException(
                500,
                exc.error_code,
                "internal sanitization error",
            ) from exc
        data[prompt_field] = new_system
        for result in results:
            self._merge_into_state(state, result)
            if result.skipped:
                # Reachable only via the opt-in oversize deliver-flag policy
                # (the old size-skip is gone — oversize now fails closed or
                # chunks by default). The original was delivered on purpose
                # after a clean full rescan; flagged for the audit trail.
                logger.warning(
                    "litellm_pre_call_system_oversize_delivered request_id=%s "
                    "field=%s content_bytes=%d block_reason=%s",
                    request_id,
                    prompt_field,
                    system_bytes,
                    result.block_reason,
                )
        logger.info(
            "litellm_pre_call_system_sanitize_done request_id=%s field=%s total_redaction_count=%d",
            request_id,
            prompt_field,
            state.redaction_count,
        )

    async def post_call_stream(
        self,
        request_data: dict[str, Any],
        response: AsyncIterator[Any],
    ) -> AsyncIterator[Any]:
        """Wrap an async iterator of SSE chunks with de-sanitization."""
        request_id = self._ensure_request_id(request_data)
        state = self._req_state.get(request_id)
        if state is None or not state.mapping.pairs:
            logger.info(
                "litellm_post_call_stream_passthrough request_id=%s reason=%s",
                request_id,
                "no_state" if state is None else "no_mapping",
            )
            async for chunk in response:
                yield chunk
            return

        response_mapping = _response_mapping(state, include_bare_aliases=self._forward_chatgpt_auth)
        logger.info(
            "litellm_post_call_stream_desanitize_start request_id=%s pairs=%d aliases=%d",
            request_id,
            len(state.mapping.pairs),
            len(response_mapping.pairs) - len(state.mapping.pairs),
        )
        # SSE bytes/str path: Anthropic passthrough emits raw SSE events.
        sse = SseStreamDesanitizer(response_mapping)
        # Dict path: OpenAI-dict chunks use the classic feed/flush interface.
        dict_desanitizer = StreamingDesanitizer(response_mapping)
        # Dict path: OpenAI tool_calls[].function.arguments deltas (F4), per index.
        dict_tool_calls = OpenAiToolCallDesanitizer(response_mapping)
        # Dict path: legacy OpenAI function_call.arguments deltas (singular).
        dict_function_call = StreamingDesanitizer(response_mapping, escape=_json_string_escape)
        # Responses API path: typed Pydantic ``response.*`` events.
        responses_desanitizer = ResponsesStreamDesanitizer(response_mapping)
        chunk_count = 0
        async for chunk in response:
            chunk_count += 1
            if isinstance(chunk, (bytes, str)):
                for out_chunk in sse.feed(chunk):
                    yield out_chunk
            elif _is_responses_event(chunk):
                for out_chunk in responses_desanitizer.feed(chunk):
                    yield out_chunk
            elif isinstance(chunk, dict):
                chunk, had_tc = _desanitize_chunk_tool_calls(chunk, dict_tool_calls)
                chunk, had_fc = _desanitize_chunk_function_call(chunk, dict_function_call)
                text = _extract_chunk_text(chunk)
                if text is None:
                    yield chunk
                    continue
                out = dict_desanitizer.feed(text)
                # Held-back/empty content must not drop a tool_call/function_call
                # riding in the same delta (its id/name/args would be lost).
                if out or had_tc or had_fc:
                    yield _replace_chunk_text(chunk, out)
            else:
                yield chunk
        # Flush SSE desanitizer (handles truncated streams / held-back tail).
        for out_chunk in sse.flush():
            yield out_chunk
        # Flush dict desanitizer tail.
        tail = dict_desanitizer.flush()
        if tail:
            yield _replace_chunk_text(_make_text_chunk(), tail)
        # Flush any held-back tool_calls arguments tails.
        for tc_index, tc_tail in dict_tool_calls.flush():
            yield _make_tool_call_chunk(tc_index, tc_tail)
        # Flush any held-back legacy function_call arguments tail.
        fc_tail = dict_function_call.flush()
        if fc_tail:
            yield _make_function_call_chunk(fc_tail)
        for responses_tail in responses_desanitizer.flush():
            yield responses_tail
        logger.info(
            "litellm_post_call_stream_desanitize_done request_id=%s chunk_count=%d",
            request_id,
            chunk_count,
        )

    async def post_call_unary(
        self,
        request_data: dict[str, Any],
        response: Any,
    ) -> Any:
        """De-sanitize a single (non-streaming) response."""
        request_id = self._ensure_request_id(request_data)
        state = self._req_state.get(request_id)
        if state is None or not state.mapping.pairs:
            logger.info(
                "litellm_post_call_unary_passthrough request_id=%s reason=%s",
                request_id,
                "no_state" if state is None else "no_mapping",
            )
            return response
        response_mapping = _response_mapping(state, include_bare_aliases=self._forward_chatgpt_auth)
        logger.info(
            "litellm_post_call_unary_desanitize request_id=%s pairs=%d aliases=%d",
            request_id,
            len(state.mapping.pairs),
            len(response_mapping.pairs) - len(state.mapping.pairs),
        )
        return _apply_reverse_to_response(response, response_mapping)

    async def audit(
        self,
        request_data: dict[str, Any],
        response: Any,
        start_time: Any,
        end_time: Any,
        *,
        status: str,
        error_code: str | None = None,
    ) -> None:
        request_id = self._ensure_request_id(request_data)
        if request_id in self._audited_ids:
            logger.debug("litellm_audit_deduped request_id=%s status=%s", request_id, status)
            return
        self._audited_ids[request_id] = None
        if len(self._audited_ids) > _AUDIT_DEDUP_CAP:
            self._audited_ids.popitem(last=False)
        state = self._req_state.pop(request_id, None)
        # litellm v1.85 passes datetime objects for start_time / end_time
        # to async_log_*_event; older versions used floats. Handle both.
        delta = end_time - start_time
        if hasattr(delta, "total_seconds"):
            latency_ms = max(0, int(delta.total_seconds() * 1000))
        else:
            latency_ms = max(0, int(delta * 1000))
        # Once-per-request (audit() is deduped above) request-latency observation.
        self._metrics.observe_request_latency(latency_ms / 1000.0, status=status)
        prompt_tokens, completion_tokens = _extract_token_counts(response)

        event = AuditEvent(
            timestamp=datetime.now(UTC),
            request_id=request_id,
            user_id=state.user_id if state else "unknown",
            team_id=state.team_id if state else "unknown",
            provider=(state.provider if state else "anthropic"),
            model=(state.model if state else str(request_data.get("model") or "unknown")),
            latency_ms=latency_ms,
            prompt_token_count=prompt_tokens,
            completion_token_count=completion_tokens,
            redaction_count=(state.redaction_count if state else 0),
            finding_label_counts=(_label_counts(state.placeholders) if state else {}),
            cache_a_hit=(state.cache_a_hit if state else False),
            status=status,  # type: ignore[arg-type]
            placeholder_list=(
                tuple(sorted(state.placeholders)) if (state and state.placeholders) else None
            ),
            error_code=(
                error_code if error_code is not None else (state.error_code if state else None)
            ),
            block_reason=(state.block_reason if state else None),
            profile_ids=(state.profile_ids if state else ()),
        )
        await self._audit.emit(event)
        logger.info(
            "litellm_audit_emitted request_id=%s status=%s latency_ms=%d "
            "redaction_count=%d cache_a_hit=%s prompt_tokens=%d completion_tokens=%d",
            request_id,
            status,
            latency_ms,
            event.redaction_count,
            event.cache_a_hit,
            prompt_tokens,
            completion_tokens,
        )

    # ---- internals --------------------------------------------------------

    @staticmethod
    def _ensure_request_id(data: dict[str, Any]) -> str:
        """Return a stable id that survives the pre_call → log-event handoff.

        litellm's own per-call id, ``litellm_call_id``, is the one identifier
        present and IDENTICAL on both sides (confirmed for litellm v1.85:
        ``data["litellm_call_id"]`` in ``async_pre_call_hook`` ==
        ``kwargs["litellm_call_id"]`` in ``async_log_*_event``). We key
        per-request state on it.

        litellm does NOT carry our own ``_corp_gateway_request_id`` through to
        the log-event kwargs (and drops the top-level ``metadata`` dict it
        passed to pre_call), so that scatter mechanism is only a FALLBACK — for
        the unit tests and any path/version where ``litellm_call_id`` is
        absent. The read order is therefore:

          0. ``data["litellm_call_id"]`` (litellm's per-call id; preferred)
          1. ``data["_corp_gateway_request_id"]`` (set by pre_call)
          2. ``data["metadata"]["_corp_gateway_request_id"]``
          3. ``data["litellm_metadata"]["_corp_gateway_request_id"]``
          4. ``data["litellm_params"]["metadata"]["_corp_gateway_request_id"]``

        On a total miss, generate a UUID. In all cases scatter the chosen id so
        the fallback lookup paths keep working.

        Every candidate is shape-validated (``_valid_request_id``) before use:
        this id is interpolated into ~30 log lines and the audit ``request_id``
        field, and it arrives from caller-controlled input (``litellm_call_id``
        or nested ``metadata``), so an unvalidated newline-bearing value would
        let a caller forge log lines. A candidate that fails validation is
        treated as absent, falling through to the next lookup path or the
        generated UUID.
        """
        call_id = data.get("litellm_call_id")
        if isinstance(call_id, str) and _valid_request_id(call_id):
            _scatter(data, call_id)
            return call_id
        for path in _REQUEST_ID_LOOKUP_PATHS:
            rid = _dig(data, path)
            if isinstance(rid, str) and _valid_request_id(rid):
                _scatter(data, rid)
                return rid
        rid = str(uuid.uuid4())
        _scatter(data, rid)
        return rid

    @staticmethod
    def _merge_into_state(state: _RequestState, result: SanitizeResult) -> None:
        # Count DISTINCT secrets: one canonical placeholder per distinct original.
        # The reverse mapping still keeps every pair so de-sanitization is complete.
        for _, placeholder in result.pairs:
            if placeholder not in state.placeholders:
                state.placeholders.append(placeholder)
        state.redaction_count = len(state.placeholders)
        state.cache_a_hit = state.cache_a_hit or result.cache_a_hit
        state.mapping = StrategyResult(pairs=state.mapping.pairs + result.pairs)
        # M1: surface an oversize deliver-flag egress in the audit record so an
        # operator can find every delivered oversize original. Only the deliver
        # path sets this; normal results leave it None.
        if result.block_reason is not None:
            state.block_reason = result.block_reason

    def _seed_request_state(
        self,
        request_id: str,
        *,
        user_id: str,
        team_id: str,
        provider: Provider,
        model: str,
    ) -> _RequestState:
        """Register the per-request state audit() reads identity from.

        A rejection raised before pre_call builds the main state would otherwise
        audit as user_id/team_id "unknown" even though corp auth already
        succeeded — the failure could not be attributed to a developer or team.
        """
        state = _RequestState(
            request_id=request_id,
            user_id=user_id,
            team_id=team_id,
            provider=provider,
            model=model,
            redaction_count=0,
            placeholders=[],
            cache_a_hit=False,
            mapping=StrategyResult(pairs=()),
        )
        self._req_state[request_id] = state
        return state

    def _record_failure(self, request_id: str, *, error_code: str) -> None:
        if request_id in self._req_state:
            self._req_state[request_id].error_code = error_code
        # gateway_failure{component} — the single failure choke point. Fires even
        # when no _RequestState exists yet (e.g. an auth failure before state is built).
        self._metrics.record_failure(_failure_component(error_code))

    async def _guard_unmanaged_input_size(
        self,
        request_id: str,
        state: _RequestState,
        data: dict[str, Any],
        resolved: ResolvedProfile,
        texts: list[str],
    ) -> None:
        """F1 parity for the unmanaged (embeddings/moderations/pass-through/
        speech) scan path: `sanitize_one` never runs for this content (it is
        never rewritten by design), so its internal oversize check never
        runs either. Without this, Stage 0/Stage 5 joined and regex-scanned
        an unbounded blob on the event loop. Same threshold + fail-closed
        policy as the managed path, applied BEFORE the expensive scan."""
        threshold = resolved.policy.size_threshold_bytes
        content_bytes = sum(len(t.encode("utf-8")) for t in texts)
        if not should_skip_sanitization(content_bytes, threshold_bytes=threshold):
            return
        state.block_reason = "oversize:blocked"
        self._record_failure(request_id, error_code="E_OVERSIZE_BLOCKED")
        self._metrics.record_block("oversize:blocked")
        logger.info(
            "litellm_pre_call_oversize_blocked request_id=%s field=unmanaged_input "
            "error_code=E_OVERSIZE_BLOCKED content_bytes=%d threshold_bytes=%d",
            request_id,
            content_bytes,
            threshold,
        )
        _now = datetime.now(UTC)
        await self.audit(data, None, _now, _now, status="failed")
        raise GuardrailHttpException(
            422,
            "E_OVERSIZE_BLOCKED",
            "request blocked: oversize content",
        )

    async def _resolve_profile(self, team_id: str) -> ResolvedProfile:
        """Resolve the team's merged profile (policy + inner orchestrator + D3
        fingerprint). A plain orchestrator has no profiles → the passthrough
        resolution (default policy, no fingerprint) keeps today's behavior."""
        orch = self._orch
        if isinstance(orch, ProfileAwareOrchestrator):
            return await orch.resolve(team_id)
        return passthrough_resolved(orch)


# ---- helpers --------------------------------------------------------------


def _label_counts(placeholders: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ph in placeholders:
        family = placeholder_family(ph) or "UNKNOWN"
        counts[family] = counts.get(family, 0) + 1
    return counts


_REQUEST_ID_KEY = "_corp_gateway_request_id"

# Locations where litellm may or may not preserve our request id across
# the pre→post handoff. Read in this order, write to all of them.
_REQUEST_ID_LOOKUP_PATHS: tuple[tuple[str, ...], ...] = (
    (_REQUEST_ID_KEY,),
    ("metadata", _REQUEST_ID_KEY),
    ("litellm_metadata", _REQUEST_ID_KEY),
    ("litellm_params", "metadata", _REQUEST_ID_KEY),
)

# Generous cap — real litellm_call_id / UUID values are well under 100 chars;
# this only bounds a hostile value, never a legitimate one.
_REQUEST_ID_MAX_LEN = 256


def _valid_request_id(value: str) -> bool:
    """Reject empty/oversize/control-character request ids.

    This id is interpolated into ~30 log lines and the audit ``request_id``
    field; a newline or other control character would let a caller forge log
    lines. Rejected candidates fall back to the next lookup path or a
    generated UUID — see ``_ensure_request_id``.
    """
    if not value or len(value) > _REQUEST_ID_MAX_LEN:
        return False
    return all(ord(ch) >= 0x20 and ch != "\x7f" for ch in value)


def _dig(d: Any, path: tuple[str, ...]) -> Any:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _scatter(data: dict[str, Any], rid: str) -> None:
    """Write the request id into every supported location."""
    data[_REQUEST_ID_KEY] = rid
    for top in ("metadata", "litellm_metadata"):
        bucket = data.get(top)
        if not isinstance(bucket, dict):
            bucket = {}
            data[top] = bucket
        bucket[_REQUEST_ID_KEY] = rid
    lparams = data.get("litellm_params")
    if isinstance(lparams, dict):
        meta = lparams.get("metadata")
        if not isinstance(meta, dict):
            meta = {}
            lparams["metadata"] = meta
        meta[_REQUEST_ID_KEY] = rid


def _resolve_request_data(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Pick the dict litellm hands us in async_log_*_event and surface the
    join key ``_ensure_request_id`` needs.

    litellm's logging callbacks receive a ``kwargs`` envelope that does NOT
    carry our scattered ``_corp_gateway_request_id`` (and has no top-level
    ``metadata``), but it DOES carry litellm's own ``litellm_call_id`` — the
    same value pre_call saw. Surface that (plus the legacy metadata locations
    as a fallback) so ``_ensure_request_id`` keys on the SAME id pre_call used.
    """
    base = kwargs.get("data") or kwargs.get("optional_params") or {}
    if not isinstance(base, dict):
        base = {}
    out: dict[str, Any] = dict(base)
    # Primary join key: litellm's per-call id (top-level, else nested in
    # litellm_params). Surfacing it lets _ensure_request_id recover the
    # per-request state stored under the same id in pre_call.
    call_id = kwargs.get("litellm_call_id")
    if not (isinstance(call_id, str) and call_id):
        lparams_in = kwargs.get("litellm_params")
        if isinstance(lparams_in, dict) and isinstance(lparams_in.get("litellm_call_id"), str):
            call_id = lparams_in["litellm_call_id"]
    if isinstance(call_id, str) and call_id:
        out["litellm_call_id"] = call_id
    # Fallback: legacy scatter locations (older litellm / unit tests).
    for top in ("metadata", "litellm_metadata"):
        if isinstance(kwargs.get(top), dict) and top not in out:
            out[top] = kwargs[top]
    lparams = kwargs.get("litellm_params")
    if isinstance(lparams, dict) and "litellm_params" not in out:
        out["litellm_params"] = lparams
    return out


# The internal corp auth header — never egress it (invariant 4, M1-14).
_CORP_AUTH_HEADER_LOWER = AuthMiddleware.HEADER_NAME.lower()

# Inbound HTTP wire-level headers that must NOT be forwarded to upstream
# LLMs by any provider. Mostly hop-by-hop or request-scoped values that
# describe the LiteLLM proxy's own connection from the client. Includes
# x-corp-auth as defense-in-depth; the unconditional strip below is the
# primary guard. Never add `authorization` here — BYOK passthrough (invariant 3).
_WIRE_HEADERS_TO_DROP = frozenset(
    {
        "host",
        "user-agent",
        "content-length",
        "accept",
        "connection",
        "content-type",
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-host",
        "x-real-ip",
        _CORP_AUTH_HEADER_LOWER,
    }
)


def _drop_wire_headers(headers: Any) -> None:
    if not isinstance(headers, dict):
        return
    for key in list(headers):
        if isinstance(key, str) and key.lower() in _WIRE_HEADERS_TO_DROP:
            del headers[key]


def _drop_corp_token(headers: Any) -> None:
    if not isinstance(headers, dict):
        return
    for key in list(headers):
        if isinstance(key, str) and key.lower() == _CORP_AUTH_HEADER_LOWER:
            del headers[key]


def _strip_corp_token_everywhere(data: dict[str, Any]) -> None:
    """Remove the corp token from every header dict litellm may forward OR log.

    Covers ``data["headers"]`` plus the ``headers`` sub-dict of
    proxy_server_request / metadata / litellm_metadata (locations litellm forwards
    upstream), AND ``litellm_params["metadata"]["headers"]`` /
    ``litellm_params["proxy_server_request"]["headers"]``. The former is
    litellm's logging-metadata dict — the same dict ``_scatter`` threads the
    request id through and litellm hands to log callbacks — so a corp token
    mirrored there would reach the audit/logging pipeline, which invariant 4
    forbids. The rule: every bucket ``_extract_auth_headers`` reads for auth must
    be strippable here too. ``_drop_corp_token`` only removes ``x-corp-auth``;
    the developer's BYOK ``Authorization`` header is left untouched (invariant 3).
    """
    _drop_corp_token(data.get("headers"))
    for bucket_key in ("proxy_server_request", "metadata", "litellm_metadata"):
        bucket = data.get(bucket_key)
        if isinstance(bucket, dict):
            _drop_corp_token(bucket.get("headers"))
    lparams = data.get("litellm_params")
    if isinstance(lparams, dict):
        meta = lparams.get("metadata")
        if isinstance(meta, dict):
            _drop_corp_token(meta.get("headers"))
        proxy_request = lparams.get("proxy_server_request")
        if isinstance(proxy_request, dict):
            _drop_corp_token(proxy_request.get("headers"))
    secret_fields = data.get("secret_fields")
    if isinstance(secret_fields, dict):
        _drop_corp_token(secret_fields.get("raw_headers"))


def _scrub_retained_request_metadata(data: dict[str, Any]) -> None:
    """Drop ``metadata``/``user`` from litellm's logging object as well as *data*.

    litellm builds the logging object BEFORE it invokes this hook and keeps its
    own copy of the request in ``model_call_details`` (``litellm_params`` and the
    top level). Popping the keys off *data* therefore leaves the unsanitized
    values reachable by every configured logging callback — the logger surface of
    invariant 1. Every step is guarded: the key may be absent, the attribute may
    not exist, and unit tests pass stub objects, so this must never raise out of
    the hook.
    """
    details = getattr(data.get("litellm_logging_obj"), "model_call_details", None)
    if not isinstance(details, dict):
        return
    for bucket in (details, details.get("litellm_params")):
        if isinstance(bucket, dict):
            bucket.pop("metadata", None)
            bucket.pop("user", None)


_CHATGPT_HEADER_ALLOWLIST = frozenset(
    {
        "authorization",
        "chatgpt-account-id",
        "originator",
        "session-id",
        "thread-id",
        "user-agent",
        "x-openai-internal-codex-responses-lite",
    }
)


def _chatgpt_upstream_headers(inbound: dict[str, str]) -> dict[str, str]:
    """Select Codex subscription headers without forwarding corp credentials."""
    selected: dict[str, str] = {}
    authorization: str | None = None
    for name, value in inbound.items():
        lower = name.lower()
        if lower == _CORP_AUTH_HEADER_LOWER:
            continue
        if lower in _CHATGPT_HEADER_ALLOWLIST or lower.startswith("x-codex-"):
            selected[name] = value
        if lower == "authorization":
            authorization = value
    if authorization is None or not authorization.lower().startswith("bearer "):
        raise ValueError("missing bearer authorization")
    if not authorization[7:].strip() or "\n" in authorization or "\r" in authorization:
        raise ValueError("invalid bearer authorization")
    return selected


# Last-resort value for installs without litellm (e.g. the local 3.14 venv). A
# stale copy is a credential-path hazard: a token that clears this prefix but not
# litellm's own would reach upstream through litellm's `x-api-key` branch, i.e.
# two competing auth schemes on one request — the exact failure the selector
# exists to prevent. Source of truth stays litellm/types/llms/anthropic.py, which
# the resolver below prefers whenever it is importable, and which the drift test
# pins this literal to.
_ANTHROPIC_OAUTH_TOKEN_PREFIX_FALLBACK = "sk-ant-oat"


def _resolve_anthropic_oauth_prefix() -> str:
    resolved = _LITELLM_ANTHROPIC_OAUTH_TOKEN_PREFIX
    # An empty or non-str prefix makes `startswith` always true, which would turn
    # the OAuth-only gate into a pass-through.
    if isinstance(resolved, str) and resolved:
        return resolved
    return _ANTHROPIC_OAUTH_TOKEN_PREFIX_FALLBACK


_ANTHROPIC_OAUTH_TOKEN_PREFIX = _resolve_anthropic_oauth_prefix()

_ANTHROPIC_HEADER_ALLOWLIST = frozenset(
    {
        "authorization",
        "anthropic-beta",
        "anthropic-version",
        "user-agent",
    }
)


def _anthropic_upstream_headers(inbound: dict[str, str]) -> dict[str, str]:
    """Select Anthropic subscription headers without forwarding corp credentials.

    OAuth tokens only. litellm picks its OAuth branch by prefix
    (llms/anthropic/common_utils.py); any other value falls through to the
    ``x-api-key`` branch while the inbound Authorization is still merged in,
    which would put two competing auth schemes on one upstream request.
    """
    selected: dict[str, str] = {}
    authorization: str | None = None
    for name, value in inbound.items():
        lower = name.lower()
        if lower == _CORP_AUTH_HEADER_LOWER:
            continue
        if lower in _ANTHROPIC_HEADER_ALLOWLIST:
            selected[name] = value
        if lower == "authorization":
            authorization = value
    if authorization is None or not authorization.lower().startswith("bearer "):
        raise ValueError("missing bearer authorization")
    bearer = authorization[7:].strip()
    if not bearer or "\n" in authorization or "\r" in authorization:
        raise ValueError("invalid bearer authorization")
    if not bearer.startswith(_ANTHROPIC_OAUTH_TOKEN_PREFIX):
        raise ValueError("authorization is not an anthropic oauth token")
    return selected


# litellm.types.utils.CallTypes values whose `input` field means raw
# text/tokens to embed or score, NOT a Responses items list. This is a
# DENYLIST, not an allowlist: `data["input"]` is treated as Responses-shaped
# (and sanitized) by default, for every call_type not named here — including
# `None` and any call_type this gateway has never seen (e.g. litellm's
# `/v1/responses/compact` route, "acompact_responses", which is a real
# Responses-input endpoint but isn't a member of litellm's own CallTypes
# enum). A positive allowlist here previously defaulted every unrecognized
# call_type to unmanaged pass-through, egressing a real Responses `input`
# items list unsanitized. Only endpoints confirmed to repurpose `input` for
# non-conversational data are listed; the cost of over-sanitizing some other
# unlisted endpoint is a bug report, the cost of under-sanitizing one is a
# leak.
#
# `speech`/`aspeech` (POST /v1/audio/speech): `input` is the plain text to
# synthesize. There is no reverse path for an audio response — a redacted
# `input` would make the synthesized speech say the placeholder token aloud,
# permanently (the `call_type="aspeech"` / `proxy_server.py:9440` routing was
# verified in a scratch venv against a litellm 1.94.1 install — a version this
# repo neither pins nor ships: `pyproject.toml` pins `litellm>=1.40,<2.0`,
# helm/build-image ship v1.85.0, `docker/chatgpt-codex/Dockerfile` ships
# v1.89.3).
#
# `pass_through_endpoint` (litellm's admin-configured arbitrary passthrough,
# e.g. proxying to Voyage AI): the body shape is entirely backend-defined and
# unvalidated by litellm, so an `input` key there means whatever THAT
# backend's API says it means — often the same raw-embed-text semantics as
# `/v1/embeddings` (Voyage's own embeddings endpoint uses `input` this way).
# Decision: treat it the same as embeddings/moderations rather than guess a
# per-backend shape we can't see; Stage 5's `collect_raw_text_leaves` still
# DLP-scans it via the "unmanaged" shape, so this is never a blind spot, only
# a no-rewrite path (same design as embeddings).
_NON_CHAT_INPUT_CALL_TYPES = frozenset(
    {
        "embedding",
        "aembedding",
        "moderation",
        "amoderation",
        "speech",
        "aspeech",
        "pass_through_endpoint",
    }
)


def _request_items(data: dict[str, Any], call_type: str | None = None) -> tuple[Any, str]:
    """Return a mutable message-like view for Chat Completions or Responses.

    `data["input"]` is NOT unique to chat/Responses — `/v1/embeddings`
    and `/v1/moderations` also carry an `input` field, but it means raw
    text/tokens to embed or score, not a Responses items list. Gate the
    Responses `input` interpretation on `call_type` so those endpoints are
    left untouched (matching release, which had no `input` handling at all).
    """
    if "messages" in data:
        messages = data.get("messages")
        return ([] if messages is None else messages), "messages"
    if call_type in _NON_CHAT_INPUT_CALL_TYPES:
        return [], "unmanaged"
    if "input" not in data:
        return [], "messages"
    response_input = data.get("input")
    if response_input is None:
        # Minor: an explicit `{"input": null}` used to reach the
        # `isinstance(messages, list)` bad-request check below and 400 —
        # release passed it through untouched. Nothing to sanitize either way.
        return [], "unmanaged"
    if isinstance(response_input, str):
        return [{"role": "user", "content": response_input}], "input_string"
    return response_input, "input_list"


def _store_request_items(data: dict[str, Any], items: list[Any], shape: str) -> None:
    if shape == "unmanaged":
        return
    if shape == "messages":
        data["messages"] = items
    elif shape == "input_string":
        first = items[0] if items else {}
        data["input"] = first.get("content", "") if isinstance(first, dict) else ""
    else:
        data["input"] = items


def _is_responses_event(chunk: Any) -> bool:
    if isinstance(chunk, dict):
        return str(chunk.get("type") or "").startswith("response.")
    event_type = getattr(chunk, "type", None)
    return isinstance(event_type, str) and event_type.startswith("response.")


class _RequestState:
    __slots__ = (
        "block_reason",
        "cache_a_hit",
        "error_code",
        "mapping",
        "model",
        "placeholders",
        "profile_ids",
        "provider",
        "redaction_count",
        "request_id",
        "response_alias_exclusions",
        "team_id",
        "user_id",
    )

    def __init__(
        self,
        *,
        request_id: str,
        user_id: str,
        team_id: str,
        provider: Provider,
        model: str,
        redaction_count: int,
        placeholders: list[str],
        cache_a_hit: bool,
        mapping: StrategyResult,
    ) -> None:
        self.request_id = request_id
        self.user_id = user_id
        self.team_id = team_id
        self.provider = provider
        self.model = model
        self.redaction_count = redaction_count
        self.placeholders = placeholders
        self.cache_a_hit = cache_a_hit
        self.mapping = mapping
        self.response_alias_exclusions: set[str] = set()
        self.error_code: str | None = None
        self.block_reason: str | None = None
        # Resolved profile layer-key (D4) — metadata for the audit trail; set
        # after profile resolution in pre_call. Empty == no profile applied.
        self.profile_ids: tuple[str, ...] = ()


def _response_mapping(state: _RequestState, *, include_bare_aliases: bool) -> StrategyResult:
    """Mapping used only on model output, including safe bracketless aliases.

    Bracketless aliases (defect #6) exist only to restore identifiers a
    ChatGPT Codex model mangles by stripping placeholder brackets. Adding
    them unconditionally, for every provider, is what made the corruption
    ship on the default (non-Codex) path — so gate on the same flag that
    enables the Codex bridge; Anthropic and OpenAI-chat responses then see
    exactly ``state.mapping.pairs``, byte-identical to release/1.0.x.
    """
    if not include_bare_aliases:
        return state.mapping
    return StrategyResult(
        pairs=add_unwrapped_response_aliases(
            state.mapping.pairs,
            forbidden=state.response_alias_exclusions,
        )
    )


def _extract_headers(data: dict[str, Any]) -> dict[str, str]:
    """Byte-identical to release/1.0.x's ``_extract_headers`` — the WRITE path.

    Used ONLY to compute what gets written back to ``data["headers"]``
    (litellm forwards/logs that bucket). An allowlist projection here would
    risk dropping BYOK ``Authorization`` (invariant 3), ``anthropic-version``,
    or whatever ``_drop_wire_headers`` deliberately preserves, so this stays
    exactly release's "first non-empty bucket" logic rather than the merge
    ``_extract_auth_headers`` below does for auth resolution.
    """
    raw = data.get("headers") or data.get("proxy_server_request") or {}
    if isinstance(raw, dict):
        if "headers" in raw and isinstance(raw["headers"], dict):
            return {str(k): str(v) for k, v in raw["headers"].items()}
        return {str(k): str(v) for k, v in raw.items()}
    return {}


def _extract_auth_headers(data: dict[str, Any]) -> dict[str, str]:
    """Merge the header copies LiteLLM exposes to callbacks, for AUTH ONLY.

    Recent LiteLLM releases keep proxy credentials in ``data["headers"]`` but
    retain the client Authorization header under ``proxy_server_request`` or
    logging metadata. Reading only the first non-empty bucket loses OAuth. The
    merge is case-insensitive so a later, more complete wire-request copy
    replaces an earlier normalized copy instead of creating duplicate headers.

    Used ONLY by ``authenticate_headers`` and ``_chatgpt_upstream_headers`` —
    NEVER for the ``data["headers"]`` write path (see ``_extract_headers``
    above), because litellm deliberately excludes some of these buckets
    (``secret_fields.raw_headers``) from logs and upstream request snapshots;
    writing the merge back there would declassify them.
    """
    buckets: list[Any] = [data.get("headers")]
    for key in ("metadata", "litellm_metadata", "proxy_server_request"):
        bucket = data.get(key)
        if isinstance(bucket, dict):
            buckets.append(bucket.get("headers"))
    litellm_params = data.get("litellm_params")
    if isinstance(litellm_params, dict):
        metadata = litellm_params.get("metadata")
        if isinstance(metadata, dict):
            buckets.append(metadata.get("headers"))
        proxy_request = litellm_params.get("proxy_server_request")
        if isinstance(proxy_request, dict):
            buckets.append(proxy_request.get("headers"))
    secret_fields = data.get("secret_fields")
    if isinstance(secret_fields, dict):
        # LiteLLM 1.89 stores the unmodified HTTP headers here and explicitly
        # excludes this object from logging and upstream request snapshots.
        buckets.append(secret_fields.get("raw_headers"))

    merged: dict[str, tuple[str, str]] = {}
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        for key, value in bucket.items():
            name = str(key)
            merged[name.lower()] = (name, str(value))
    return dict(merged.values())


def _detect_provider(data: dict[str, Any]) -> Provider:
    return detect_provider(str(data.get("model") or ""))


def _classify_auth_error(exc: AuthError) -> str:
    name = type(exc).__name__
    if name == "ExpiredTokenError":
        return "E_TOKEN_EXPIRED"
    if name == "RevokedTokenError":
        return "E_TOKEN_REVOKED"
    if name == "InvalidTokenError":
        return "E_TOKEN_INVALID"
    return "E_AUTH"


# error_code → coarse component for the gateway_failure{component} series. The
# runbook queries component="corp_llm"/"pre_pass"; unmapped codes fall back to "other".
_FAILURE_COMPONENT: dict[str, str] = {
    "E_CORP_LLM_DOWN": "corp_llm",
    "E_NER_UNAVAILABLE": "ner",
    "E_PROFILE_UNAVAILABLE": "profile",
    "E_MISSING_TOKEN": "auth",
    "E_TOKEN_EXPIRED": "auth",
    "E_TOKEN_REVOKED": "auth",
    "E_TOKEN_INVALID": "auth",
    "E_AUTH": "auth",
    "E_PROVIDER_AUTH": "auth",
    "E_PROVIDER_BLOCKED": "provider",
    "E_POLICY_BLOCKED": "policy",
    "E_OVERSIZE_BLOCKED": "oversize",
    "E_DLP_BLOCKED": "dlp",
    "E_BAD_REQUEST": "request",
    "E_SPAN_INVALID": "sanitize",
}


def _failure_component(error_code: str) -> str:
    return _FAILURE_COMPONENT.get(error_code, "other")


def _extract_chunk_text(chunk: Any) -> str | None:
    """Pull text out of an SSE chunk in a shape-tolerant way."""
    if isinstance(chunk, str):
        return chunk
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8", errors="replace")
    if isinstance(chunk, dict):
        choices = chunk.get("choices") or []
        if choices and isinstance(choices, list):
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str):
                return content
        delta_top = chunk.get("delta")
        if isinstance(delta_top, dict):
            text = delta_top.get("text")
            if isinstance(text, str):
                return text
    return None


def _replace_chunk_text(chunk: Any, new_text: str) -> Any:
    if isinstance(chunk, str):
        return new_text
    if isinstance(chunk, bytes):
        return new_text.encode("utf-8")
    if isinstance(chunk, dict):
        out = {**chunk}
        choices = out.get("choices")
        if isinstance(choices, list) and choices:
            new_choices = list(choices)
            first = {**(new_choices[0] or {})}
            delta = {**(first.get("delta") or {})}
            delta["content"] = new_text
            first["delta"] = delta
            new_choices[0] = first
            out["choices"] = new_choices
            return out
        delta_top = out.get("delta")
        if isinstance(delta_top, dict):
            new_delta = {**delta_top, "text": new_text}
            out["delta"] = new_delta
            return out
        out["content"] = new_text
        return out
    return new_text


def _make_text_chunk() -> dict[str, Any]:
    return {"choices": [{"delta": {"content": ""}}]}


def _make_tool_call_chunk(index: int, arguments: str) -> dict[str, Any]:
    return {
        "choices": [
            {"delta": {"tool_calls": [{"index": index, "function": {"arguments": arguments}}]}}
        ]
    }


def _make_function_call_chunk(arguments: str) -> dict[str, Any]:
    return {"choices": [{"delta": {"function_call": {"arguments": arguments}}}]}


def _desanitize_chunk_tool_calls(
    chunk: dict[str, Any], desanitizer: OpenAiToolCallDesanitizer
) -> tuple[dict[str, Any], bool]:
    """Rewrite placeholders in an OpenAI dict chunk's tool_calls argument deltas.

    Returns ``(chunk, had_tool_calls)`` — ``had_tool_calls`` tells the caller to
    keep emitting the chunk even when its content is held back. A garbage index is
    skipped rather than crashing the stream."""
    choices = chunk.get("choices")
    if not (isinstance(choices, list) and choices and isinstance(choices[0], dict)):
        return chunk, False
    delta = choices[0].get("delta")
    if not isinstance(delta, dict) or not isinstance(delta.get("tool_calls"), list):
        return chunk, False
    new_calls: list[Any] = []
    changed = False
    for tc in delta["tool_calls"]:
        fn = tc.get("function") if isinstance(tc, dict) else None
        if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
            idx = coerce_tool_index(tc.get("index", 0))
            if idx is None:
                new_calls.append(tc)
                continue
            rewritten = desanitizer.feed(idx, fn["arguments"])
            new_calls.append({**tc, "function": {**fn, "arguments": rewritten}})
            changed = True
        else:
            new_calls.append(tc)
    if not changed:
        return chunk, False
    new_delta = {**delta, "tool_calls": new_calls}
    new_first = {**choices[0], "delta": new_delta}
    return {**chunk, "choices": [new_first, *choices[1:]]}, True


def _desanitize_chunk_function_call(
    chunk: dict[str, Any], desanitizer: StreamingDesanitizer
) -> tuple[dict[str, Any], bool]:
    """Rewrite placeholders in an OpenAI dict chunk's legacy function_call args delta.

    Returns ``(chunk, had_function_call)``."""
    choices = chunk.get("choices")
    if not (isinstance(choices, list) and choices and isinstance(choices[0], dict)):
        return chunk, False
    delta = choices[0].get("delta")
    if not isinstance(delta, dict):
        return chunk, False
    fc = delta.get("function_call")
    if not isinstance(fc, dict) or not isinstance(fc.get("arguments"), str):
        return chunk, False
    rewritten = desanitizer.feed(fc["arguments"])
    new_delta = {**delta, "function_call": {**fc, "arguments": rewritten}}
    new_first = {**choices[0], "delta": new_delta}
    return {**chunk, "choices": [new_first, *choices[1:]]}, True


def _apply_reverse_to_response(response: Any, mapping: StrategyResult) -> Any:
    _reverse = build_reverse_substituter(mapping.pairs)

    if isinstance(response, str):
        return _reverse(response)
    if isinstance(response, dict):
        out = {**response}
        if "output" in out or str(out.get("type") or "").startswith("response."):
            out = desanitize_responses_payload(out, _reverse)
        choices = out.get("choices")
        if isinstance(choices, list):
            out["choices"] = [_reverse_choice(c, _reverse) for c in choices]
        # Handle Anthropic-native top-level content str or list (no choices).
        elif "content" in out and isinstance(out["content"], (str, list)):
            out["content"] = desanitize_content(out["content"], _reverse)
        return out
    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        try:
            payload = model_dump(mode="python", exclude_none=False)
        except TypeError:
            payload = model_dump(exclude_none=False)
        if isinstance(payload, dict):
            rewritten = desanitize_responses_payload(payload, _reverse)
            validator = getattr(type(response), "model_validate", None)
            if callable(validator):
                try:
                    restored = validator(rewritten)
                except Exception as exc:
                    # The rewritten payload failed to validate back into its own
                    # type. Falling through to model_copy() would bypass that
                    # validation entirely and return an object we never checked
                    # is well-formed. Surface the failure instead of degrading
                    # quietly: log it and hand back the untouched (still
                    # validated, just not desanitized) original response.
                    # `rewritten` here is already DESANITIZED (originals
                    # restored), so `exc_info=True` must never be used: a real
                    # pydantic ValidationError embeds the offending
                    # `input_value` in its own message, which would put the
                    # original on pod stdout (M1-14). Log only the exception
                    # TYPE and a stable error_code.
                    logger.warning(
                        "litellm_post_call_response_reconstruct_failed "
                        "response_type=%s exception_type=%s "
                        "error_code=E_RESPONSE_RECONSTRUCT_FAILED",
                        type(response).__name__,
                        type(exc).__name__,
                    )
                    return response
                # model_validate() builds a BRAND NEW instance from the dumped
                # dict, which never carries pydantic private attrs litellm
                # relies on — `_hidden_params` (cost tracking), and the sibling
                # `_response_headers`/`_response_ms` (x-litellm-* headers,
                # litellm/types/utils.py:1989-1991, checked against a real
                # litellm 1.94.1 install) — model_dump omits all three
                # (confirmed against the real litellm.ModelResponse).
                # model_copy() (the other branch below) doesn't have this
                # problem — it copies the existing instance instead of
                # reconstructing one, so private attrs survive naturally.
                # Restoring them here is deliberately OUTSIDE the reconstruction
                # try/except above: a failure restoring one of these must not
                # discard the already-validated, already-desanitized `restored`
                # object and fall back to the still-placeholdered original —
                # that would be the exact silent degradation the reconstruction
                # failure path above refuses.
                for _attr in ("_hidden_params", "_response_headers", "_response_ms"):
                    _value = getattr(response, _attr, None)
                    if _value is None or not hasattr(restored, _attr):
                        continue
                    try:
                        setattr(restored, _attr, _value)
                    except Exception as _exc:
                        # Surface the failure instead of degrading quietly —
                        # `restored` (already validated + desanitized) is kept
                        # either way; this is observability only, no fallback.
                        logger.warning(
                            "litellm_post_call_hidden_params_restore_failed "
                            "response_type=%s attr=%s exception_type=%s",
                            type(response).__name__,
                            _attr,
                            type(_exc).__name__,
                        )
                return restored
            copier = getattr(response, "model_copy", None)
            if callable(copier):
                return copier(update=rewritten, deep=True)
    return response


def _reverse_choice(choice: Any, reverse_fn: Any) -> Any:
    if not isinstance(choice, dict):
        return choice
    out = {**choice}
    msg = out.get("message")
    if isinstance(msg, dict):
        new_msg = {**msg}
        content = new_msg.get("content")
        if isinstance(content, str):
            new_msg["content"] = reverse_fn(content)
        elif isinstance(content, list):
            new_msg["content"] = desanitize_content(content, reverse_fn)
        # OpenAI tool-call arguments in the assistant response (F4).
        new_msg = desanitize_tool_calls(new_msg, reverse_fn)
        out["message"] = new_msg
    return out


def _extract_token_counts(response: Any) -> tuple[int, int]:
    """Pull (prompt, completion) token counts from a response, shape-tolerant.

    litellm hands ``async_log_*_event`` a ``ModelResponse`` OBJECT whose
    ``.usage`` is a ``Usage`` object (attribute access), not a dict — the old
    dict-only path bailed and every audit logged 0/0. Handle both a dict
    response (``response["usage"]``) and an object response
    (``response.usage``), where ``usage`` itself may be a dict or an object,
    and accept both the OpenAI (``prompt_tokens``/``completion_tokens``) and
    Anthropic (``input_tokens``/``output_tokens``) field names.
    """
    usage = (
        response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    )
    if usage is None:
        return 0, 0

    def _field(name: str) -> Any:
        if isinstance(usage, dict):
            return usage.get(name)
        return getattr(usage, name, None)

    prompt = _field("prompt_tokens")
    if prompt is None:
        prompt = _field("input_tokens")
    completion = _field("completion_tokens")
    if completion is None:
        completion = _field("output_tokens")
    return int(prompt or 0), int(completion or 0)


# Suppress unused import warning for `time` (kept for downstream callers).
_ = time

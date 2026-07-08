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
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from corp_llm_gateway.audit import AuditEvent, AuditLogger
from corp_llm_gateway.audit.event import Provider
from corp_llm_gateway.config import get as _config_get
from corp_llm_gateway.corp_llm import CorpLlmHttpError
from corp_llm_gateway.detectors import NerUnavailableError
from corp_llm_gateway.payload.classifier import classify_block
from corp_llm_gateway.payload.size_threshold import OversizeContentError
from corp_llm_gateway.providers import detect_provider
from corp_llm_gateway.sanitizer import (
    OpenAiToolCallDesanitizer,
    SanitizationOrchestrator,
    SanitizeResult,
    SseStreamDesanitizer,
    StrategyResult,
    StreamingDesanitizer,
)
from corp_llm_gateway.sanitizer.content_blocks import (
    ContentTooDeepError,
    UnsanitizableToolArgumentsError,
    collect_text,
    collect_tool_call_text,
    desanitize_content,
    desanitize_tool_calls,
    message_has_tool_calls,
    sanitize_content,
    sanitize_message,
)
from corp_llm_gateway.sanitizer.dlp_guard import DlpEgressGuard
from corp_llm_gateway.sanitizer.engine import AllStrategiesFailedError
from corp_llm_gateway.sanitizer.placeholder import (
    apply_pairs,
    find_placeholder_literals,
    placeholder_family,
)
from corp_llm_gateway.sanitizer.placeholder_allocator import (
    RequestPlaceholderAllocator,
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

logger = logging.getLogger(__name__)

# Cap on the audit-idempotency set (bounded FIFO). The two possible audit() calls
# for one request (inline block-audit + any litellm event) happen ms apart, so a
# small window suffices; this just prevents unbounded growth over process life.
_AUDIT_DEDUP_CAP = 4096


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
        orchestrator: SanitizationOrchestrator,
        auth_middleware: AuthMiddleware,
        audit_logger: AuditLogger,
        *,
        max_output_tokens_cap: int | None = None,
        strip_inbound_headers_to_upstream: bool = False,
        dlp_guard: DlpEgressGuard | None = None,
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
        self._dlp_guard = dlp_guard if dlp_guard is not None else DlpEgressGuard()
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
        user_api_key_dict: dict[str, Any] | None,
        cache: Any,
        data: dict[str, Any],
        call_type: str,
    ) -> dict[str, Any]:
        return await self.pre_call(data)

    async def async_post_call_streaming_iterator_hook(
        self,
        user_api_key_dict: dict[str, Any] | None,
        response: AsyncIterator[Any],
        request_data: dict[str, Any],
    ) -> AsyncIterator[Any]:
        async for chunk in self.post_call_stream(request_data, response):
            yield chunk

    async def async_post_call_success_hook(
        self,
        data: dict[str, Any],
        user_api_key_dict: dict[str, Any] | None,
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

    async def pre_call(self, data: dict[str, Any]) -> dict[str, Any]:
        """Sanitize a request body in-place; return the mutated dict.

        If `max_output_tokens_cap` was passed to __init__, clamp the
        request's `max_tokens` before any other step. This stops
        Claude-Code-style requests with a huge default output budget
        from overshooting the upstream model's context window.

        Order: auth → strip corp token → sanitize messages → return.
        Failures are mapped to GuardrailHttpException with stable
        error_code so post-call audit can attribute the failure.
        """
        request_id = self._ensure_request_id(data)
        model = str(data.get("model") or "unknown")
        raw_messages = data.get("messages") or []
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

        try:
            ctx = await self._auth.authenticate_headers(_extract_headers(data))
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

        messages = data.get("messages") or []
        if not isinstance(messages, list):
            logger.info(
                "litellm_pre_call_bad_request request_id=%s error_code=E_BAD_REQUEST",
                request_id,
            )
            self._record_failure(request_id, error_code="E_BAD_REQUEST")
            _now = datetime.now(UTC)
            await self.audit(data, None, _now, _now, status="failed", error_code="E_BAD_REQUEST")
            raise GuardrailHttpException(400, "E_BAD_REQUEST", "messages must be a list")

        provider = _detect_provider(data)
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
        for _m in messages:
            if isinstance(_m, dict):
                for _seg in collect_text(_m.get("content")):
                    input_literals.extend(find_placeholder_literals(_seg))
                for _seg in collect_tool_call_text(_m):
                    input_literals.extend(find_placeholder_literals(_seg))
        for _seg in collect_text(data.get("system")):
            input_literals.extend(find_placeholder_literals(_seg))
        if input_literals:
            allocator.forbid(input_literals)
            logger.warning(
                "litellm_pre_call_input_placeholder_literal_detected request_id=%s count=%d",
                request_id,
                len(input_literals),
            )

        # Stage 0: payload classifier — block config/log dumps before egress (R10/R11).
        # Runs after auth + _RequestState so the block carries user/team attribution.
        if _config_get("CORP_LLM_BLOCK_PAYLOADS", "1") != "0":
            _s0_texts: list[str] = []
            for _s0_msg in messages:
                if isinstance(_s0_msg, dict):
                    _s0_texts.extend(collect_text(_s0_msg.get("content")))
                    _s0_texts.extend(collect_tool_call_text(_s0_msg))
            _s0_texts.extend(collect_text(data.get("system")))
            _s0_reason = classify_block("\n".join(_s0_texts))
            if _s0_reason is not None:
                state.block_reason = _s0_reason
                self._record_failure(request_id, error_code="E_POLICY_BLOCKED")
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
            result = await self._orch.sanitize(
                text,
                team_id=ctx.team_id,
                conversation_id=request_id,
            )
            if not result.pairs:
                return result
            canonical_pairs = allocator.remap(result.pairs)
            if canonical_pairs == result.pairs:
                return result
            # Re-derive the sanitized text from the ORIGINAL segment text using
            # the canonical labels. Re-applying to the original (rather than
            # renaming labels in the already-substituted text) avoids any
            # chained-replacement hazard when a minted label coincides with
            # another segment's token.
            return SanitizeResult(
                sanitized_text=apply_pairs(text, canonical_pairs),
                pairs=canonical_pairs,
                cache_a_hit=result.cache_a_hit,
                skipped=result.skipped,
                block_reason=result.block_reason,
            )

        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            content_empty = content is None or (isinstance(content, str) and not content)
            # A tool-call-only assistant message (content=None) still carries
            # sanitizable data in tool_calls[].function.arguments (F4) — process it.
            if content_empty and not message_has_tool_calls(msg):
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
                content_bytes = len(
                    json.dumps(msg.get("tool_calls") or msg.get("function_call")).encode("utf-8")
                )

            logger.info(
                "litellm_pre_call_message_sanitize_start request_id=%s "
                "message_index=%d role=%s content_bytes=%d",
                request_id,
                i,
                str(msg.get("role") or "unknown"),
                content_bytes,
            )
            try:
                new_msg, results = await sanitize_message(msg, sanitize_one)
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
            except OversizeContentError as exc:
                # F1: fail-closed on an oversize leaf. Never forward the original.
                state.block_reason = "oversize:blocked"
                self._record_failure(request_id, error_code="E_OVERSIZE_BLOCKED")
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

        # Sanitize system field if present and non-empty (E: truthy guard skips ""/[]).
        system = data.get("system")
        if system:
            if isinstance(system, str):
                system_bytes = len(system.encode("utf-8"))
            else:
                system_bytes = len(json.dumps(system).encode("utf-8"))
            logger.info(
                "litellm_pre_call_system_sanitize_start request_id=%s content_bytes=%d",
                request_id,
                system_bytes,
            )
            try:
                new_system, results = await sanitize_content(system, sanitize_one)
            except ContentTooDeepError as exc:
                self._record_failure(request_id, error_code="E_BAD_REQUEST")
                _now = datetime.now(UTC)
                await self.audit(data, None, _now, _now, status="failed")
                raise GuardrailHttpException(
                    400,
                    "E_BAD_REQUEST",
                    "request content nesting too deep",
                ) from exc
            except OversizeContentError as exc:
                # F1: fail-closed on an oversize system leaf. Never forward the original.
                state.block_reason = "oversize:blocked"
                self._record_failure(request_id, error_code="E_OVERSIZE_BLOCKED")
                logger.info(
                    "litellm_pre_call_oversize_blocked request_id=%s field=system "
                    "error_code=E_OVERSIZE_BLOCKED content_bytes=%d threshold_bytes=%d",
                    request_id,
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
                    "field=system error_code=E_CORP_LLM_DOWN exception=%s",
                    request_id,
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
                # F2 fail-closed (M4): required NER unavailable on the system field.
                logger.warning(
                    "litellm_pre_call_ner_unavailable request_id=%s "
                    "field=system error_code=E_NER_UNAVAILABLE exception=%s",
                    request_id,
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
            data["system"] = new_system
            for result in results:
                self._merge_into_state(state, result)
                if result.skipped:
                    # Reachable only via the opt-in oversize deliver-flag policy
                    # (the old size-skip is gone — oversize now fails closed or
                    # chunks by default). The original was delivered on purpose
                    # after a clean full rescan; flagged for the audit trail.
                    logger.warning(
                        "litellm_pre_call_system_oversize_delivered request_id=%s "
                        "content_bytes=%d block_reason=%s",
                        request_id,
                        system_bytes,
                        result.block_reason,
                    )
            logger.info(
                "litellm_pre_call_system_sanitize_done request_id=%s total_redaction_count=%d",
                request_id,
                state.redaction_count,
            )

        # Stage 5: DLP egress guard — re-scan the SANITIZED outbound request.
        # Defence-in-depth: catches canaries / raw secrets that survived the
        # primary sanitizer. Audit the block INLINE (idempotent) — litellm does
        # not fire the failure event for pre_call rejections; see Stage 0.
        if _config_get("CORP_LLM_DLP_GUARD", "1") != "0":
            _s5_texts: list[str] = []
            for _s5_msg in data.get("messages") or []:
                if isinstance(_s5_msg, dict):
                    _s5_texts.extend(collect_text(_s5_msg.get("content")))
                    _s5_texts.extend(collect_tool_call_text(_s5_msg))
            _s5_texts.extend(collect_text(data.get("system")))
            _s5_reason = self._dlp_guard.scan("\n".join(_s5_texts))
            if _s5_reason is not None:
                state.block_reason = _s5_reason
                self._record_failure(request_id, error_code="E_DLP_BLOCKED")
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

        logger.info(
            "litellm_post_call_stream_desanitize_start request_id=%s pairs=%d",
            request_id,
            len(state.mapping.pairs),
        )
        # SSE bytes/str path: Anthropic passthrough emits raw SSE events.
        sse = SseStreamDesanitizer(state.mapping)
        # Dict path: OpenAI-dict chunks use the classic feed/flush interface.
        dict_desanitizer = StreamingDesanitizer(state.mapping)
        # Dict path: OpenAI tool_calls[].function.arguments deltas (F4), per index.
        dict_tool_calls = OpenAiToolCallDesanitizer(state.mapping)
        # Dict path: legacy OpenAI function_call.arguments deltas (singular).
        dict_function_call = StreamingDesanitizer(state.mapping, escape=_json_string_escape)
        chunk_count = 0
        async for chunk in response:
            chunk_count += 1
            if isinstance(chunk, (bytes, str)):
                for out_chunk in sse.feed(chunk):
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
        logger.info(
            "litellm_post_call_unary_desanitize request_id=%s pairs=%d",
            request_id,
            len(state.mapping.pairs),
        )
        return _apply_reverse_to_response(response, state.mapping)

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
        """
        call_id = data.get("litellm_call_id")
        if isinstance(call_id, str) and call_id:
            _scatter(data, call_id)
            return call_id
        for path in _REQUEST_ID_LOOKUP_PATHS:
            rid = _dig(data, path)
            if isinstance(rid, str) and rid:
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

    def _record_failure(self, request_id: str, *, error_code: str) -> None:
        if request_id in self._req_state:
            self._req_state[request_id].error_code = error_code


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


# Inbound HTTP wire-level headers that must NOT be forwarded to upstream
# LLMs by any provider. Mostly hop-by-hop or request-scoped values that
# describe the LiteLLM proxy's own connection from the client.
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
    }
)


def _drop_wire_headers(headers: Any) -> None:
    if not isinstance(headers, dict):
        return
    for key in list(headers):
        if isinstance(key, str) and key.lower() in _WIRE_HEADERS_TO_DROP:
            del headers[key]


class _RequestState:
    __slots__ = (
        "block_reason",
        "cache_a_hit",
        "error_code",
        "mapping",
        "model",
        "placeholders",
        "provider",
        "redaction_count",
        "request_id",
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
        self.error_code: str | None = None
        self.block_reason: str | None = None


def _extract_headers(data: dict[str, Any]) -> dict[str, str]:
    raw = data.get("headers") or data.get("proxy_server_request") or {}
    if isinstance(raw, dict):
        if "headers" in raw and isinstance(raw["headers"], dict):
            return {str(k): str(v) for k, v in raw["headers"].items()}
        return {str(k): str(v) for k, v in raw.items()}
    return {}


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
    by_placeholder = {placeholder: original for original, placeholder in mapping.pairs}
    sorted_placeholders = sorted(by_placeholder, key=lambda p: -len(p))

    def _reverse(text: str) -> str:
        for ph in sorted_placeholders:
            text = text.replace(ph, by_placeholder[ph])
        return text

    if isinstance(response, str):
        return _reverse(response)
    if isinstance(response, dict):
        out = {**response}
        choices = out.get("choices")
        if isinstance(choices, list):
            out["choices"] = [_reverse_choice(c, _reverse) for c in choices]
        # Handle Anthropic-native top-level content str or list (no choices).
        elif "content" in out and isinstance(out["content"], (str, list)):
            out["content"] = desanitize_content(out["content"], _reverse)
        return out
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

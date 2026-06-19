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

import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from corp_llm_gateway.audit import AuditEvent, AuditLogger
from corp_llm_gateway.audit.event import Provider
from corp_llm_gateway.sanitizer import (
    SanitizationOrchestrator,
    SanitizeResult,
    StreamingDesanitizer,
    StrategyResult,
)
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


class GuardrailHttpException(Exception):
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
    ) -> None:
        # Best-effort super().__init__ — when litellm is installed this
        # initializes CustomLogger's internal state; when it isn't
        # (object), this is a no-op kwargs-only call.
        try:
            super().__init__()
        except TypeError:  # pragma: no cover
            pass
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
        # Per-request state. Keyed by request_id; cleared in post_call.
        self._req_state: dict[str, _RequestState] = {}

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
        await self.audit(
            request_data, response_obj, start_time, end_time, status="failed"
        )

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
                    "litellm_pre_call_max_tokens_clamped request_id=%s "
                    "requested=%d capped=%d",
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
            raise GuardrailHttpException(401, "E_MISSING_TOKEN", "missing X-Corp-Auth")
        except AuthError as exc:
            error_code = _classify_auth_error(exc)
            logger.info(
                "litellm_pre_call_auth_failed request_id=%s error_code=%s",
                request_id,
                error_code,
            )
            self._record_failure(request_id, error_code=error_code)
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

        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content:
                logger.info(
                    "litellm_pre_call_message_skipped request_id=%s "
                    "message_index=%d reason=empty_or_non_string",
                    request_id,
                    i,
                )
                continue
            logger.info(
                "litellm_pre_call_message_sanitize_start request_id=%s "
                "message_index=%d role=%s content_bytes=%d",
                request_id,
                i,
                str(msg.get("role") or "unknown"),
                len(content.encode("utf-8")),
            )
            result = await self._orch.sanitize(
                content,
                team_id=ctx.team_id,
                conversation_id=request_id,
            )
            messages[i] = {**msg, "content": result.sanitized_text}
            self._merge_into_state(state, result)
            logger.info(
                "litellm_pre_call_message_sanitize_done request_id=%s "
                "message_index=%d redaction_count=%d cache_a_hit=%s skipped=%s",
                request_id,
                i,
                len(result.pairs),
                result.cache_a_hit,
                result.skipped,
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
        desanitizer = StreamingDesanitizer(state.mapping)
        chunk_count = 0
        async for chunk in response:
            chunk_count += 1
            text = _extract_chunk_text(chunk)
            if text is None:
                yield chunk
                continue
            out = desanitizer.feed(text)
            if out:
                yield _replace_chunk_text(chunk, out)
        tail = desanitizer.flush()
        if tail:
            yield _replace_chunk_text(_make_text_chunk(), tail)
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
    ) -> None:
        request_id = self._ensure_request_id(request_data)
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
            cache_a_hit=(state.cache_a_hit if state else False),
            status=status,  # type: ignore[arg-type]
            placeholder_list=(
                tuple(state.placeholders) if (state and state.placeholders) else None
            ),
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
        """Round-trip a stable request_id across the pre/post handoff.

        LiteLLM passes a different ``data`` dict reference to
        ``async_log_*_event`` than to ``async_pre_call_hook`` (it
        re-builds the kwargs envelope around the call). A naive
        top-level write in pre_call wouldn't survive — the post hook
        would generate a fresh UUID and the per-request state lookup
        would miss.

        Read order on entry:
          1. ``data["_corp_gateway_request_id"]`` (set by pre_call)
          2. ``data["metadata"]["_corp_gateway_request_id"]``
          3. ``data["litellm_metadata"]["_corp_gateway_request_id"]``
          4. ``data["litellm_params"]["metadata"]["_corp_gateway_request_id"]``

        On miss, generate a UUID and write it to ALL of those
        locations so whichever envelope litellm hands the post hooks
        will find it. Defensive but cheap; the dicts are small.
        """
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
        state.redaction_count += len(result.pairs)
        state.placeholders.extend(p for _, p in result.pairs)
        state.cache_a_hit = state.cache_a_hit or result.cache_a_hit
        merged_pairs = state.mapping.pairs + result.pairs
        state.mapping = StrategyResult(pairs=merged_pairs)

    def _record_failure(self, request_id: str, *, error_code: str) -> None:
        if request_id in self._req_state:
            self._req_state[request_id].error_code = error_code


# ---- helpers --------------------------------------------------------------


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
    """Pick the dict litellm hands us in async_log_*_event and merge any
    nested metadata into a flat surface ``_ensure_request_id`` can read.

    LiteLLM's logging callbacks receive a ``kwargs`` envelope whose
    ``data`` may NOT be the same dict we mutated in pre_call (the
    anthropic-passthrough path rebuilds it). The request id we scattered
    via ``_scatter`` is also present at ``kwargs["metadata"]`` and
    ``kwargs["litellm_params"]["metadata"]`` — copy those into the
    returned dict so ``_ensure_request_id`` finds the id without having
    to know about litellm's call envelope.
    """
    base = kwargs.get("data") or kwargs.get("optional_params") or {}
    if not isinstance(base, dict):
        return {}
    out: dict[str, Any] = dict(base)
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
_WIRE_HEADERS_TO_DROP = frozenset({
    "host", "user-agent", "content-length", "accept", "connection",
    "content-type", "x-forwarded-for", "x-forwarded-proto",
    "x-forwarded-host", "x-real-ip",
})


def _drop_wire_headers(headers: Any) -> None:
    if not isinstance(headers, dict):
        return
    for key in list(headers):
        if isinstance(key, str) and key.lower() in _WIRE_HEADERS_TO_DROP:
            del headers[key]


class _RequestState:
    __slots__ = (
        "request_id",
        "user_id",
        "team_id",
        "provider",
        "model",
        "redaction_count",
        "placeholders",
        "cache_a_hit",
        "mapping",
        "error_code",
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


def _extract_headers(data: dict[str, Any]) -> dict[str, str]:
    raw = data.get("headers") or data.get("proxy_server_request") or {}
    if isinstance(raw, dict):
        if "headers" in raw and isinstance(raw["headers"], dict):
            return {str(k): str(v) for k, v in raw["headers"].items()}
        return {str(k): str(v) for k, v in raw.items()}
    return {}


def _detect_provider(data: dict[str, Any]) -> Provider:
    model = str(data.get("model") or "")
    if model.startswith("claude") or "anthropic" in model.lower():
        return "anthropic"
    return "openai"


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
        return out
    return response


def _reverse_choice(choice: Any, reverse_fn: Any) -> Any:
    if not isinstance(choice, dict):
        return choice
    out = {**choice}
    msg = out.get("message")
    if isinstance(msg, dict):
        new_msg = {**msg}
        if isinstance(new_msg.get("content"), str):
            new_msg["content"] = reverse_fn(new_msg["content"])
        out["message"] = new_msg
    return out


def _extract_token_counts(response: Any) -> tuple[int, int]:
    if not isinstance(response, dict):
        return 0, 0
    usage = response.get("usage") or {}
    if not isinstance(usage, dict):
        return 0, 0
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    return prompt, completion


# Suppress unused import warning for `time` (kept for downstream callers).
_ = time

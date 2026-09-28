"""The dispatch harness: litellm 1.101.0's own proxy app, in-process, behind our route gate.

A request goes through the real gate and in-flight limiter, litellm's real route handler
(``add_litellm_data_to_request``, ``ProxyLogging.pre_call_hook``, the router, the provider
transform, ``post_call_success_hook``, ``post_call_response_headers_hook`` and the SSE
generators) and out over a real socket to :class:`StubUpstream`, which records what
egressed and echoes back the placeholder it received. Around our guardrail sit two
capture callbacks that override every response-side hook litellm dispatches.

Import only after ``pytest.importorskip("litellm.proxy.proxy_server")``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import litellm
import pytest
from litellm import Router
from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy import proxy_server
from litellm.proxy.guardrails.guardrail_registry import IN_MEMORY_GUARDRAIL_HANDLER
from litellm.proxy.policy_engine import attachment_registry, policy_registry
from litellm.proxy.utils import ProxyLogging
from litellm.types.guardrails import GuardrailEventHooks

from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail, GuardrailHttpException
from corp_llm_gateway.metrics import NoopExporter
from corp_llm_gateway.route_gate import RouteGateMiddleware, arm_checks
from corp_llm_gateway.route_gate.inflight import InflightLimiter, RequestTicket, current_ticket
from tests.test_litellm_hook import _build_guardrail

EMAIL = "alice.secret@corp.example"
PLACEHOLDER = "[EMAIL_1]"
# What survives any split of the reply across stream chunks: an original's head, or a
# placeholder's head (the stub splits right after "[EM").
ORIGINAL_MARK = "alice.secret"
PLACEHOLDER_MARK = "[EM"
TOKEN = "tok-1"
GUARDRAIL_NAME = "corp-llm-sanitizer"

CHAT_MODEL = "corp-chat"
MESSAGES_MODEL = "corp-claude"
RESPONSES_MODEL = "corp-responses"

_PLACEHOLDER_RE = re.compile(r"\[[A-Z]+(?:_[A-Z]+)*_\d+\]")


# ── the stub upstream ────────────────────────────────────────────────────────


def reply_text(echo: str) -> str:
    return f"mail {echo} today"


def _split(text: str) -> tuple[str, str]:
    # Mid-placeholder, so a reversal that works per chunk would miss it.
    cut = text.index("[") + 3 if "[" in text else len(text) // 2
    return text[:cut], text[cut:]


def _chat_sse(text: str) -> list[str]:
    head, tail = _split(text)

    def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
        return "data: " + json.dumps(
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion.chunk",
                "created": 1758500000,
                "model": "gpt-4o-mini",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
        )

    return [
        chunk({"role": "assistant", "content": head}),
        chunk({"content": tail}),
        chunk({}, "stop"),
        "data: [DONE]",
    ]


def _anthropic_sse(text: str) -> list[str]:
    head, tail = _split(text)

    def event(name: str, data: dict[str, Any]) -> str:
        return f"event: {name}\ndata: {json.dumps(data)}"

    return [
        event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_stub",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 7, "output_tokens": 1},
                },
            },
        ),
        event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": head},
            },
        ),
        event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": tail},
            },
        ),
        event("content_block_stop", {"type": "content_block_stop", "index": 0}),
        event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 2},
            },
        ),
        event("message_stop", {"type": "message_stop"}),
    ]


def _response_body(text: str) -> dict[str, Any]:
    return {
        "id": "resp_stub",
        "object": "response",
        "created_at": 1758500000,
        "status": "completed",
        "model": "gpt-4o-mini",
        "output": [
            {
                "id": "msg_stub",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
    }


def _responses_sse(text: str) -> list[str]:
    head, tail = _split(text)

    def event(data: dict[str, Any]) -> str:
        return f"event: {data['type']}\ndata: {json.dumps(data)}"

    delta = {"item_id": "msg_stub", "output_index": 0, "content_index": 0}
    return [
        event(
            {
                "type": "response.created",
                "sequence_number": 0,
                "response": {**_response_body(""), "status": "in_progress", "output": []},
            }
        ),
        event({"type": "response.output_text.delta", "sequence_number": 1, **delta, "delta": head}),
        event({"type": "response.output_text.delta", "sequence_number": 2, **delta, "delta": tail}),
        event({"type": "response.output_text.done", "sequence_number": 3, **delta, "text": text}),
        event(
            {"type": "response.completed", "sequence_number": 4, "response": _response_body(text)}
        ),
    ]


def _unary(path: str, text: str) -> dict[str, Any]:
    path = path.split("?", 1)[0]
    if path.endswith("/messages"):
        return {
            "id": "msg_stub",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 7, "output_tokens": 2},
        }
    if "/responses" in path:
        return _response_body(text)
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "created": 1758500000,
        "model": "gpt-4o-mini",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
    }


def _stream(path: str, text: str) -> list[str]:
    path = path.split("?", 1)[0]
    if path.endswith("/messages"):
        return _anthropic_sse(text)
    if "/responses" in path:
        return _responses_sse(text)
    return _chat_sse(text)


def _parsed(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


class StubUpstream:
    """A provider on a local socket: records each body, replies naming what it received.

    The reply echoes the first placeholder in the request, or the original when one
    egressed instead: a leak therefore shows up in the provider-bound body AND in
    every later hop, which is what the capture assertions need to see.
    """

    def __init__(self, *, event_delay: float = 0.0, error_status: int | None = None) -> None:
        self.bodies: list[str] = []
        # One entry per streamed reply: "completed", or "broken" when the gateway hung up.
        self.stream_outcomes: list[str] = []
        self.event_delay = event_delay
        # Answer every request with this status and a provider-style error body.
        self.error_status = error_status
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                if _parsed(raw) is None:
                    # litellm forwards the CLIENT's content-length next to its own,
                    # longer body; read on until the body parses.
                    raw += self._drain(raw)
                    self.close_connection = True
                text = raw.decode("utf-8", "replace")
                upstream.bodies.append(text)
                found = _PLACEHOLDER_RE.search(text)
                echo = found.group(0) if found else (EMAIL if EMAIL in text else "nothing")
                parsed = _parsed(raw)
                stream = isinstance(parsed, dict) and bool(parsed.get("stream"))
                if upstream.error_status is not None:
                    self._send_error(upstream.error_status)
                elif stream:
                    self._send_stream(_stream(self.path, reply_text(echo)))
                else:
                    payload = json.dumps(_unary(self.path, reply_text(echo))).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            def _send_error(self, status: int) -> None:
                payload = json.dumps(
                    {"type": "error", "error": {"type": "invalid_request_error", "message": "no"}}
                ).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _drain(self, prefix: bytes) -> bytes:
                rest = b""
                self.connection.settimeout(1.0)
                try:
                    while len(rest) < (1 << 20):
                        chunk = self.rfile.read1(65536)
                        if not chunk:
                            break
                        rest += chunk
                        if _parsed(prefix + rest) is not None:
                            break
                except OSError:
                    pass
                finally:
                    self.connection.settimeout(None)
                return rest

            def _send_stream(self, events: list[str]) -> None:
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("transfer-encoding", "chunked")
                self.end_headers()
                try:
                    for item in events:
                        payload = (item + "\n\n").encode()
                        self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
                        self.wfile.flush()
                        if upstream.event_delay:
                            time.sleep(upstream.event_delay)
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    upstream.stream_outcomes.append("broken")
                    self.close_connection = True
                    return
                upstream.stream_outcomes.append("completed")

            def log_message(self, fmt: str, *args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# ── what a capture sees ──────────────────────────────────────────────────────


def serialize(obj: Any) -> str:
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    if isinstance(obj, str):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return json.dumps(dump(mode="json"), default=str)
        except Exception:
            return repr(obj)
    if isinstance(obj, (dict, list, tuple)):
        return json.dumps(obj, default=serialize)
    return repr(obj)


@dataclass
class Seen:
    """Every value one capture callback received, per hook, serialised."""

    success: list[str] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    iterator: list[str] = field(default_factory=list)
    per_chunk: list[str] = field(default_factory=list)
    logged: list[str] = field(default_factory=list)

    def hooks(self) -> dict[str, list[str]]:
        return {
            "success": self.success,
            "headers": self.headers,
            "iterator": self.iterator,
            "per_chunk": self.per_chunk,
            "logged": self.logged,
        }

    def holding(self, needle: str) -> set[str]:
        return {name for name, values in self.hooks().items() if any(needle in v for v in values)}

    def clear(self) -> None:
        for values in self.hooks().values():
            values.clear()


def _capture_namespace(per_chunk: bool) -> dict[str, Any]:
    # litellm detects overrides on the LEAF class's __dict__ (proxy/utils.py:2143-2175),
    # so each hook is placed there directly rather than inherited from a mixin.
    async def async_post_call_success_hook(
        self: Any, data: dict[str, Any], user_api_key_dict: Any, response: Any
    ) -> None:
        self.seen.success.append(serialize(response))

    async def async_post_call_response_headers_hook(
        self: Any,
        data: dict[str, Any],
        user_api_key_dict: Any,
        response: Any,
        request_headers: dict[str, str] | None = None,
        litellm_call_info: dict[str, Any] | None = None,
    ) -> None:
        self.seen.headers.append(serialize(response))

    async def async_post_call_streaming_iterator_hook(
        self: Any,
        user_api_key_dict: Any,
        response: AsyncIterator[Any],
        request_data: dict[str, Any],
    ) -> AsyncIterator[Any]:
        async for chunk in response:
            self.seen.iterator.append(serialize(chunk))
            yield chunk

    async def async_post_call_streaming_hook(
        self: Any, user_api_key_dict: Any, response: Any
    ) -> None:
        self.seen.per_chunk.append(serialize(response))

    async def async_log_success_event(
        self: Any, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self.seen.logged.append(serialize(kwargs.get("standard_logging_object")))
        self.seen.logged.append(serialize(response_obj))

    def init(self: Any, name: str) -> None:
        CustomLogger.__init__(self)
        self.name = name
        self.seen = Seen()

    namespace: dict[str, Any] = {
        "__init__": init,
        "async_post_call_success_hook": async_post_call_success_hook,
        "async_post_call_response_headers_hook": async_post_call_response_headers_hook,
        "async_post_call_streaming_iterator_hook": async_post_call_streaming_iterator_hook,
        "async_log_success_event": async_log_success_event,
    }
    if per_chunk:
        namespace["async_post_call_streaming_hook"] = async_post_call_streaming_hook
    return namespace


# Overrides every response-side hook, the per-chunk one included.
Capture = type("Capture", (CustomLogger,), _capture_namespace(per_chunk=True))
# The same without the per-chunk override: with only these registered, the per-chunk
# hooks run only because some CustomGuardrail is present (hazard 12).
CaptureNoChunkHook = type(
    "CaptureNoChunkHook", (CustomLogger,), _capture_namespace(per_chunk=False)
)


# ── our guardrail, today and migrated ────────────────────────────────────────


def build_ours() -> tuple[CorpLlmGuardrail, ListSink]:
    guardrail, sink = _build_guardrail([(EMAIL, PLACEHOLDER)])
    # The composition root's default (CORP_LLM_STRIP_INBOUND_HEADERS=1): without it
    # litellm forwards the client's Content-Length and truncates the upstream body.
    guardrail._strip_inbound_headers_to_upstream = True
    return guardrail, sink


_ENGINE: list[CorpLlmGuardrail] = []


def use_engine(engine: CorpLlmGuardrail) -> None:
    """The engine a migrated fixture built by litellm's registry (no engine argument) wraps."""
    _ENGINE[:] = [engine]


def _leaf_hooks(cls: type) -> type:
    async def async_pre_call_hook(
        self: Any, user_api_key_dict: Any, cache: Any, data: dict[str, Any], call_type: str
    ) -> Any:
        self.calls.append("pre_call")
        return await self.engine.async_pre_call_hook(user_api_key_dict, cache, data, call_type)

    async def async_post_call_success_hook(
        self: Any, data: dict[str, Any], user_api_key_dict: Any, response: Any
    ) -> Any:
        self.calls.append("post_call_success")
        return await self.engine.async_post_call_success_hook(data, user_api_key_dict, response)

    async def async_post_call_streaming_iterator_hook(
        self: Any,
        user_api_key_dict: Any,
        response: AsyncIterator[Any],
        request_data: dict[str, Any],
    ) -> AsyncIterator[Any]:
        self.calls.append("streaming_iterator")
        async for chunk in self.engine.async_post_call_streaming_iterator_hook(
            user_api_key_dict, response, request_data
        ):
            yield chunk

    cls.async_pre_call_hook = async_pre_call_hook  # type: ignore[attr-defined]
    cls.async_post_call_success_hook = async_post_call_success_hook  # type: ignore[attr-defined]
    cls.async_post_call_streaming_iterator_hook = async_post_call_streaming_iterator_hook  # type: ignore[attr-defined]
    return cls


class _Migrated(CustomGuardrail):
    def __init__(
        self,
        engine: CorpLlmGuardrail | None = None,
        *,
        guardrail_name: str = GUARDRAIL_NAME,
        event_hook: Any = None,
        default_on: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            guardrail_name=guardrail_name,
            event_hook=event_hook
            if event_hook is not None
            else [GuardrailEventHooks.pre_call, GuardrailEventHooks.post_call],
            default_on=default_on,
            **kwargs,
        )
        self.engine = engine if engine is not None else _ENGINE[0]
        self.calls: list[str] = []


@_leaf_hooks
class UnsafeMigratedGuardrail(_Migrated):
    """What Task 1's base-class switch would build, WITHOUT the sentinel or any arm check.

    Our engine behind ``CustomGuardrail``: ``guardrail_name`` ours, native lifecycle hooks,
    no ``apply_guardrail``. Throwaway: the hazard probes show what this can be bypassed by.
    """

    use_native_lifecycle_hooks = True


@_leaf_hooks
class RefusingMigratedGuardrail(_Migrated):
    """The migrated fixture with Task 1's constructor refusal of the unsafe flags."""

    use_native_lifecycle_hooks = True

    def __init__(self, engine: CorpLlmGuardrail | None = None, **kwargs: Any) -> None:
        for flag in ("scan_raw_request", "run_in_parallel"):
            if kwargs.get(flag):
                raise ValueError(f"{GUARDRAIL_NAME} refuses {flag}")
        super().__init__(engine, **kwargs)


@_leaf_hooks
class ApplyGuardrailMigrated(_Migrated):
    """The migrated fixture plus an ``apply_guardrail`` — the dispatch flip of hazard 7."""

    apply_calls = 0

    async def apply_guardrail(
        self,
        inputs: Any,
        request_data: dict[str, Any],
        input_type: Any,
        logging_obj: Any = None,
    ) -> Any:
        type(self).apply_calls += 1
        return inputs


@_leaf_hooks
class NativeApplyGuardrailMigrated(ApplyGuardrailMigrated):
    """Hazard 7's escape hatch: ``apply_guardrail`` present, native hooks forced back on."""

    use_native_lifecycle_hooks = True


class StandInGuardrail(CustomGuardrail):
    """A second guardrail that answers to our name and passes content through (hazard 15b)."""

    def __init__(self) -> None:
        super().__init__(
            guardrail_name=GUARDRAIL_NAME,
            event_hook=[GuardrailEventHooks.pre_call, GuardrailEventHooks.post_call],
            default_on=True,
        )
        self.pre_calls = 0

    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict[str, Any], call_type: str
    ) -> Any:
        self.pre_calls += 1
        return data


E_SANITIZER_SKIPPED = "E_SANITIZER_SKIPPED"


class SentinelCallback(CustomLogger):
    """Registered after ours; refuses any request whose ticket our pre-call never marked.

    The marker is the call id ``_pre_call_impl`` binds onto the route gate's ticket
    (``bind_call_id``, ``litellm_hook.py:456``) before it rewrites anything: a skipped or
    substituted guardrail never binds it. A plain ``CustomLogger`` with its own
    ``async_pre_call_hook``, so it runs in the branch litellm never skips.
    """

    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict[str, Any], call_type: str
    ) -> Any:
        ticket = current_ticket()
        call_id = data.get("litellm_call_id")
        if ticket is None or call_id not in ticket.call_ids:
            raise GuardrailHttpException(503, E_SANITIZER_SKIPPED, "sanitizer did not run")
        return data


# ── the arm checks, plus the ones only a base-class switch would need ───────


def arm_problems(
    callbacks: Iterable[Any],
    *,
    router: Any = None,
    registry: Any = None,
    proxy_logger: Any = None,
    set_verbose: bool = False,
) -> list[str]:
    """What the arm step refuses to serve with (``route_gate.arm_checks``, the code
    ``asgi.py`` runs), plus name uniqueness and pipeline membership. Empty = arm."""
    problems = arm_checks.guardrail_problems(callbacks, is_ours=_is_ours)
    named = [g for g in getattr(router, "guardrail_list", None) or () if _names_us(g)]
    if len(named) > 1:
        problems.append("guardrail_name_not_unique")
    if registry is not None and pipeline_names_us(registry):
        problems.append("pipeline_manages_us")
    loggers = (proxy_logger,) if proxy_logger is not None else ()
    return problems + arm_checks.debug_problems(loggers=loggers, set_verbose=set_verbose)


def _is_ours(cb: Any) -> bool:
    # By type, never by name. _Migrated stands for CorpLlmGuardrail after a base-class switch.
    return isinstance(cb, (CorpLlmGuardrail, _Migrated))


def _names_us(entry: Mapping[str, Any]) -> bool:
    return entry.get("guardrail_name") == GUARDRAIL_NAME


def pipeline_names_us(registry: Any) -> bool:
    policies = list(registry.get_all_policies().values())
    policies += [policy for _, policy in getattr(registry, "_policies_by_id", {}).values()]
    return any(
        step.guardrail == GUARDRAIL_NAME
        for policy in policies
        if policy.pipeline is not None
        for step in policy.pipeline.steps
    )


def body_names_policies(body: bytes) -> bool:
    """The gate-level refusal of 14b: an admitted body with a top-level ``policies`` key."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return False
    return isinstance(parsed, dict) and "policies" in parsed


# ── the harness ──────────────────────────────────────────────────────────────


@dataclass
class Exchange:
    status: int
    headers: httpx.Headers
    text: str
    provider_bodies: list[str]
    ticket: RequestTicket | None


ROUTES: dict[str, tuple[str, str]] = {
    "chat": ("/v1/chat/completions", CHAT_MODEL),
    "messages": ("/v1/messages", MESSAGES_MODEL),
    "responses": ("/v1/responses", RESPONSES_MODEL),
}


def request_body(route: str, *, stream: bool, content: str | None = None) -> dict[str, Any]:
    _, model = ROUTES[route]
    text = content if content is not None else f"write to {EMAIL}"
    body: dict[str, Any] = {"model": model, "stream": stream}
    if route == "responses":
        body["input"] = text
    else:
        body["messages"] = [{"role": "user", "content": text}]
    if route == "messages":
        body["max_tokens"] = 64
    return body


class _TicketProbe:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.tickets: list[RequestTicket | None] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            self.tickets.append(current_ticket())
        await self.app(scope, receive, send)


class DispatchHarness:
    """litellm's app behind our gate, pointed at a stub upstream; monkeypatch restores globals."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        upstream: StubUpstream,
        callbacks: list[Any],
        *,
        general_settings: dict[str, Any] | None = None,
        guardrail_list: list[dict[str, Any]] | None = None,
        wrap: Callable[[Any], Any] | None = None,
    ) -> None:
        self.upstream = upstream
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        # Router.__init__ and request handling append to these in place: patch them first.
        for name in _CALLBACK_LISTS:
            monkeypatch.setattr(litellm, name, list(getattr(litellm, name)))
        clients = litellm.in_memory_llm_clients_cache
        for name in ("cache_dict", "ttl_dict", "expiration_heap"):
            monkeypatch.setattr(clients, name, type(getattr(clients, name))(getattr(clients, name)))
        monkeypatch.setattr(ProxyLogging, "_callback_capabilities_cache", {})
        self.router = Router(
            model_list=[
                _deployment(CHAT_MODEL, "openai/gpt-4o-mini", f"{upstream.base}/v1"),
                _deployment(MESSAGES_MODEL, "anthropic/claude-sonnet-4-5", upstream.base),
                _deployment(RESPONSES_MODEL, "openai/gpt-4o-mini", f"{upstream.base}/v1"),
            ],
            guardrail_list=guardrail_list,  # type: ignore[arg-type]
        )
        monkeypatch.setattr(proxy_server, "llm_router", self.router)
        monkeypatch.setattr(proxy_server, "master_key", None)
        monkeypatch.setattr(proxy_server, "general_settings", dict(general_settings or {}))
        monkeypatch.setattr(litellm, "callbacks", list(callbacks))
        for name in ("IN_MEMORY_GUARDRAILS", "guardrail_id_to_custom_guardrail", "_sources"):
            monkeypatch.setattr(IN_MEMORY_GUARDRAIL_HANDLER, name, {})
        monkeypatch.setattr(IN_MEMORY_GUARDRAIL_HANDLER, "guardrail_id_to_sibling_callbacks", {})
        monkeypatch.setattr(policy_registry, "_policy_registry", None)
        monkeypatch.setattr(attachment_registry, "_attachment_registry", None)
        inner = wrap(proxy_server.app) if wrap is not None else proxy_server.app
        self.probe = _TicketProbe(inner)
        self.gate = RouteGateMiddleware(
            self.probe,
            metrics=NoopExporter(),
            audit_logger=AuditLogger(ListSink(), gateway_version="harness"),
            limiter=InflightLimiter(0, metrics=NoopExporter()),
        )
        self.gate.arm()

    async def send(
        self,
        route: str,
        *,
        stream: bool,
        extra: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        content: str | None = None,
        path: str | None = None,
        token: str | None = TOKEN,
    ) -> Exchange:
        body = request_body(route, stream=stream, content=content) | dict(extra or {})
        sent = {"X-Corp-Auth": token} if token is not None else {}
        before = len(self.upstream.bodies)
        known_clients = set(litellm.in_memory_llm_clients_cache.cache_dict)
        transport = httpx.ASGITransport(app=self.gate)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            response = await client.post(
                path or ROUTES[route][0], json=body, headers={**sent, **dict(headers or {})}
            )
        await _drain_logging()
        await _close_clients_added_since(known_clients)
        return Exchange(
            status=response.status_code,
            headers=response.headers,
            text=response.text,
            provider_bodies=self.upstream.bodies[before:],
            ticket=self.probe.tickets[-1] if self.probe.tickets else None,
        )


_CALLBACK_LISTS = (
    "input_callback",
    "success_callback",
    "failure_callback",
    "service_callback",
    "_async_input_callback",
    "_async_success_callback",
    "_async_failure_callback",
)


def _deployment(name: str, model: str, api_base: str) -> dict[str, Any]:
    return {
        "model_name": name,
        "litellm_params": {"model": model, "api_base": api_base, "api_key": "sk-stub-key"},
    }


async def _close_clients_added_since(known: set[str]) -> None:
    """Close the provider clients a request cached, on the loop that made them: undoing the
    harness drops them from litellm's cache, and they would otherwise be collected open."""
    cache = litellm.in_memory_llm_clients_cache
    for key in [key for key in cache.cache_dict if key not in known]:
        client = cache.cache_dict.pop(key)
        cache.ttl_dict.pop(key, None)
        close = getattr(client, "close", None) or getattr(client, "aclose", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result


async def _drain_logging() -> None:
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

    # Success/failure events run on litellm's logging worker; let them land.
    for _ in range(3):
        await asyncio.sleep(0.05)
        await GLOBAL_LOGGING_WORKER.flush()


def register_response_mapping(
    engine: CorpLlmGuardrail, mappings: Any, data: dict[str, Any]
) -> None:
    """Hand the response mapping of the request our pre-call just rewrote to the ticket."""
    from corp_llm_gateway.litellm_hook import _response_mapping

    ticket = current_ticket()
    state = engine._req_state.get(CorpLlmGuardrail._ensure_request_id(data))
    if ticket is not None and state is not None and state.mapping.pairs:
        mappings.register(
            ticket, _response_mapping(state, include_bare_aliases=engine._forward_chatgpt_auth)
        )


class OptionAPreCall(CustomLogger):
    """Our guardrail with the response reversal moved out (Option A): pre-call and audit only.

    The pre-call hands the response mapping to the middleware's ticket-keyed store — the
    one line Task 1 adds to ``_pre_call_impl``.
    """

    def __init__(self, engine: CorpLlmGuardrail, mappings: Any) -> None:
        super().__init__()
        self.engine = engine
        self.mappings = mappings

    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict[str, Any], call_type: str
    ) -> Any:
        out = await self.engine.async_pre_call_hook(user_api_key_dict, cache, data, call_type)
        register_response_mapping(self.engine, self.mappings, data)
        return out

    async def async_log_success_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        await self.engine.async_log_success_event(kwargs, response_obj, start_time, end_time)

    async def async_log_failure_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        await self.engine.async_log_failure_event(kwargs, response_obj, start_time, end_time)


class MetadataSpy(CustomLogger):
    """Records what the policy engine wrote into the request metadata, before any guardrail."""

    def __init__(self) -> None:
        super().__init__()
        self.managed: list[frozenset[str]] = []

    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict[str, Any], call_type: str
    ) -> Any:
        found: set[str] = set()
        for key in ("metadata", "litellm_metadata"):
            bucket = data.get(key)
            if isinstance(bucket, dict):
                found |= set(bucket.get("_pipeline_managed_guardrails") or ())
        self.managed.append(frozenset(found))
        return data


def policy_config(*, attachment: dict[str, Any] | None) -> dict[str, Any]:
    """One policy whose post_call pipeline names our guardrail; attached as given."""
    config: dict[str, Any] = {
        "policies": {
            POLICY_NAME: {
                "guardrails": {"add": [GUARDRAIL_NAME]},
                "pipeline": {"mode": "post_call", "steps": [{"guardrail": GUARDRAIL_NAME}]},
            }
        }
    }
    if attachment is not None:
        config["policy_attachments"] = [{"policy": POLICY_NAME, **attachment}]
    return config


POLICY_NAME = "corp-post-call-pipeline"


async def load_policies(config: dict[str, Any]) -> None:
    """litellm's own config path for ``policies:`` (``proxy_server.py:5990`` → ``:6068``)."""
    await proxy_server.ProxyConfig()._init_policy_engine(
        config=config, prisma_client=None, llm_router=None
    )


async def until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait for litellm's asynchronous logging to deliver; fail loudly past ``timeout``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached within the logging deadline")
        await _drain_logging()

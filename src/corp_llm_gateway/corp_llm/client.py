"""Corp-LLM HTTP client.

Targets vLLM (corp's chosen LLM serving framework — see openapi.json).
The wire format is OpenAI-compatible chat/completions with full tool-call
support, which we use for the FunctionCallStrategy in M1-5.

This client is intentionally thin — no retries, no streaming. The
guardrail layer composes retries via the failure-policy matrix; the
caller decides streaming. We just speak the protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from corp_llm_gateway.auth import CorpLlmAuthProvider, NoopAuthProvider

SANITIZE_TOOL_NAME = "report_pii_findings"

SANITIZE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SANITIZE_TOOL_NAME,
        "description": (
            "Report each PII / corp-confidential occurrence found in the input "
            "as an (original, replacement) pair. Use placeholder format "
            "`[LABEL_NNN]` (e.g. `[NAME_001]`, `[EMAIL_002]`). If nothing "
            "needs redaction, return an empty `pairs` array."
        ),
        "parameters": {
            "type": "object",
            "required": ["pairs"],
            "additionalProperties": False,
            "properties": {
                "pairs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["original", "replacement"],
                        "additionalProperties": False,
                        "properties": {
                            "original": {"type": "string"},
                            "replacement": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}


class CorpLlmHttpError(Exception):
    pass


@dataclass(frozen=True)
class ChatCompletionResponse:
    raw: dict[str, Any]

    @property
    def first_message(self) -> dict[str, Any]:
        choices = self.raw.get("choices") or []
        if not choices:
            raise CorpLlmHttpError("response has no choices")
        return choices[0].get("message") or {}

    @property
    def first_tool_calls(self) -> list[dict[str, Any]]:
        tc = self.first_message.get("tool_calls") or []
        if not isinstance(tc, list):
            raise CorpLlmHttpError("malformed tool_calls (not a list)")
        return tc

    @property
    def text_content(self) -> str:
        content = self.first_message.get("content")
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        # OpenAI's content-parts variant: stitch the text parts.
        parts = []
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        return "".join(parts)


class CorpLlmClient:
    def __init__(
        self,
        base_url: str,
        *,
        model: str,
        http: httpx.AsyncClient | None = None,
        auth_provider: CorpLlmAuthProvider | None = None,
        timeout: float = 30.0,
        verify: bool | str = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._auth = auth_provider or NoopAuthProvider()
        # When no client is injected we own (and must close) one, so
        # ``aclose()`` actually frees the connection pool. ``verify`` applies
        # only to that owned client; an injected client carries its own TLS
        # config and its lifecycle belongs to the caller.
        self._http = http or httpx.AsyncClient(timeout=timeout, verify=verify)
        self._owned_http = http is None

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        extra: dict[str, Any] | None = None,
    ) -> ChatCompletionResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if extra:
            payload.update(extra)

        artifacts = self._auth.artifacts()
        url = f"{self._base_url}/v1/chat/completions"
        try:
            resp = await self._http.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", **artifacts.headers},
            )
        except httpx.HTTPError as exc:
            # httpx timeout exceptions (Connect/ReadTimeout) stringify to
            # '' — interpolating bare ``{exc}`` yields an undiagnosable
            # "corp-llm transport error: " with nothing after the colon
            # (exactly the empty 500 body seen in the field). Prepend the
            # exception type so logs/audit show WHAT failed.
            raise CorpLlmHttpError(
                f"corp-llm transport error: {type(exc).__name__}: {exc}"
            ) from exc

        if resp.status_code >= 400:
            raise CorpLlmHttpError(f"corp-llm returned {resp.status_code}: {resp.text[:500]}")
        return ChatCompletionResponse(raw=resp.json())

    async def aclose(self) -> None:
        if self._owned_http:
            await self._http.aclose()

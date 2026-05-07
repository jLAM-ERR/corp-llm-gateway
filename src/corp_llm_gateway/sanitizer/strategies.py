"""Three-tier output strategies for the corp-LLM sanitizer.

Targets the vLLM OpenAI-compatible chat/completions response shape per
openapi.json. Order: function-call → JSON → regex.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME


@dataclass(frozen=True)
class StrategyResult:
    pairs: tuple[tuple[str, str], ...]


class StrategyError(Exception):
    pass


class SanitizerStrategy(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def extract(self, raw_llm_output: Any) -> StrategyResult: ...


class FunctionCallStrategy(SanitizerStrategy):
    """Best path: corp LLM was asked with `tools=[SANITIZE_TOOL_SCHEMA]` and
    `tool_choice={"type":"function","function":{"name": SANITIZE_TOOL_NAME}}`,
    so the response contains a tool_calls[0] with JSON arguments.

    Accepts either the full ChatCompletionResponse or the parsed dict.
    """

    @property
    def name(self) -> str:
        return "function_call"

    async def extract(self, raw_llm_output: Any) -> StrategyResult:
        tool_calls = _extract_tool_calls(raw_llm_output)
        if not tool_calls:
            raise StrategyError("no tool_calls in response")
        target = next(
            (
                tc
                for tc in tool_calls
                if (tc.get("function") or {}).get("name") == SANITIZE_TOOL_NAME
            ),
            None,
        )
        if target is None:
            raise StrategyError(
                f"tool_call for {SANITIZE_TOOL_NAME!r} not present"
            )
        args_raw = (target.get("function") or {}).get("arguments")
        if not isinstance(args_raw, str):
            raise StrategyError("tool_call arguments missing or not a string")
        try:
            args = json.loads(args_raw)
        except json.JSONDecodeError as exc:
            raise StrategyError(f"tool_call arguments not valid JSON: {exc}") from exc
        return _parse_pairs(args)


class JsonStrategy(SanitizerStrategy):
    """Fallback: corp LLM didn't tool-call, but emitted JSON as message text."""

    @property
    def name(self) -> str:
        return "json"

    async def extract(self, raw_llm_output: Any) -> StrategyResult:
        text = _extract_text(raw_llm_output)
        if not text:
            raise StrategyError("no message text")
        snippet = _extract_json_snippet(text)
        if snippet is None:
            raise StrategyError("no JSON object detected in message text")
        try:
            parsed = json.loads(snippet)
        except json.JSONDecodeError as exc:
            raise StrategyError(f"malformed JSON in message text: {exc}") from exc
        return _parse_pairs(parsed)


_REGEX_PAIR = re.compile(
    r"^\s*[-*]?\s*(?:`)?(?P<orig>[^`]+?)(?:`)?\s*(?:→|->)\s*(?:`)?(?P<rep>[^`]+?)(?:`)?\s*$"
)


class RegexStrategy(SanitizerStrategy):
    """Last resort: corp LLM emitted a bullet list of pairs in prose."""

    @property
    def name(self) -> str:
        return "regex"

    async def extract(self, raw_llm_output: Any) -> StrategyResult:
        text = _extract_text(raw_llm_output)
        if not text:
            raise StrategyError("no message text")
        pairs: list[tuple[str, str]] = []
        for line in text.splitlines():
            m = _REGEX_PAIR.match(line)
            if not m:
                continue
            original = m.group("orig").strip()
            replacement = m.group("rep").strip()
            if original and replacement:
                pairs.append((original, replacement))
        if not pairs:
            raise StrategyError("no `original -> replacement` lines found")
        return StrategyResult(pairs=tuple(pairs))


# ---- helpers --------------------------------------------------------------


def _extract_tool_calls(raw: Any) -> list[dict[str, Any]]:
    if hasattr(raw, "first_tool_calls"):
        return list(raw.first_tool_calls)
    if isinstance(raw, dict):
        choices = raw.get("choices") or []
        if not choices:
            return []
        msg = (choices[0].get("message") or {})
        tc = msg.get("tool_calls") or []
        return tc if isinstance(tc, list) else []
    return []


def _extract_text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if hasattr(raw, "text_content"):
        return str(raw.text_content)
    if isinstance(raw, dict):
        choices = raw.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
            return "".join(parts)
    return ""


def _extract_json_snippet(text: str) -> str | None:
    """Find the first balanced top-level JSON object in `text`."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_pairs(parsed: Any) -> StrategyResult:
    if not isinstance(parsed, dict):
        raise StrategyError("expected JSON object")
    pairs_raw = parsed.get("pairs")
    if not isinstance(pairs_raw, list):
        raise StrategyError("missing or non-list `pairs` field")
    out: list[tuple[str, str]] = []
    for i, item in enumerate(pairs_raw):
        if not isinstance(item, dict):
            raise StrategyError(f"pair[{i}] is not an object")
        original = item.get("original")
        replacement = item.get("replacement")
        if not isinstance(original, str) or not isinstance(replacement, str):
            raise StrategyError(f"pair[{i}] missing string original/replacement")
        if original and replacement:
            out.append((original, replacement))
    return StrategyResult(pairs=tuple(out))

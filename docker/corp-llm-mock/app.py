"""Mock corp-LLM (vLLM-compatible) for e2e testing.

Speaks just enough of /v1/chat/completions to drive the gateway's
SanitizationOrchestrator end-to-end. Returns canned tool_calls for the
sanitization tool; everything else is a stub.

Environment:
  MOCK_PAIRS  JSON list of {"original":..., "replacement":...} pairs
              the mock will report for every request. Default: redact
              "alice" -> "[NAME_001]" and "alice@corp.lan" -> "[EMAIL_001]".
"""
import json
import os
import uuid
from typing import Any

from fastapi import FastAPI, Request

DEFAULT_PAIRS = [
    {"original": "alice@corp.lan", "replacement": "[EMAIL_001]"},
    {"original": "alice", "replacement": "[NAME_001]"},
]


def _load_pairs() -> list[dict[str, str]]:
    raw = os.environ.get("MOCK_PAIRS")
    if not raw:
        return DEFAULT_PAIRS
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass
    return DEFAULT_PAIRS


app = FastAPI(title="corp-llm-mock")


@app.get("/healthz/live")
async def live() -> dict[str, str]:
    return {"status": "live"}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> dict[str, Any]:
    body = await request.json()
    tool_choice = body.get("tool_choice")

    pairs = _load_pairs()

    if isinstance(tool_choice, dict):
        # Forced tool call (this is what the sanitization orchestrator does).
        tool_name = tool_choice.get("function", {}).get("name", "report_pii_findings")
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "model": body.get("model", "mock"),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": f"call_{uuid.uuid4().hex[:8]}",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps({"pairs": pairs}),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    # Fallback: plain assistant message (unused by sanitizer in normal flow).
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "model": body.get("model", "mock"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }

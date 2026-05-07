import json

import httpx
import pytest

from corp_llm_gateway.corp_llm import (
    CorpLlmClient,
    CorpLlmHttpError,
    SANITIZE_TOOL_NAME,
    SANITIZE_TOOL_SCHEMA,
)


def _mock_transport(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_chat_completion_posts_to_v1_chat_completions() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    http = _mock_transport(handler)
    client = CorpLlmClient("https://corp-llm.example", model="m", http=http)
    resp = await client.chat_completion(
        messages=[{"role": "user", "content": "hi"}], max_tokens=10
    )
    assert captured["method"] == "POST"
    assert captured["url"] == "https://corp-llm.example/v1/chat/completions"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "m"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["max_tokens"] == 10
    assert body["temperature"] == 0.0
    assert resp.text_content == "ok"


async def test_strips_trailing_slash_in_base_url() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    http = _mock_transport(handler)
    client = CorpLlmClient("https://corp-llm.example/", model="m", http=http)
    await client.chat_completion(messages=[])
    assert captured["url"] == "https://corp-llm.example/v1/chat/completions"


async def test_passes_tools_and_tool_choice() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {}}]})

    http = _mock_transport(handler)
    client = CorpLlmClient("https://x", model="m", http=http)
    await client.chat_completion(
        messages=[],
        tools=[SANITIZE_TOOL_SCHEMA],
        tool_choice={"type": "function", "function": {"name": SANITIZE_TOOL_NAME}},
    )
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["tools"][0]["function"]["name"] == SANITIZE_TOOL_NAME
    assert body["tool_choice"]["function"]["name"] == SANITIZE_TOOL_NAME


async def test_raises_on_4xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    http = _mock_transport(handler)
    client = CorpLlmClient("https://x", model="m", http=http)
    with pytest.raises(CorpLlmHttpError, match="401"):
        await client.chat_completion(messages=[])


async def test_raises_on_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    http = _mock_transport(handler)
    client = CorpLlmClient("https://x", model="m", http=http)
    with pytest.raises(CorpLlmHttpError, match="500"):
        await client.chat_completion(messages=[])


async def test_raises_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    http = _mock_transport(handler)
    client = CorpLlmClient("https://x", model="m", http=http)
    with pytest.raises(CorpLlmHttpError, match="transport error"):
        await client.chat_completion(messages=[])


async def test_response_first_tool_calls_extracts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"id": "c1", "function": {"name": "foo", "arguments": "{}"}}
                            ]
                        }
                    }
                ]
            },
        )

    http = _mock_transport(handler)
    client = CorpLlmClient("https://x", model="m", http=http)
    resp = await client.chat_completion(messages=[])
    assert resp.first_tool_calls[0]["function"]["name"] == "foo"


async def test_text_content_handles_content_parts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "alpha "},
                                {"type": "text", "text": "beta"},
                            ]
                        }
                    }
                ]
            },
        )

    http = _mock_transport(handler)
    client = CorpLlmClient("https://x", model="m", http=http)
    resp = await client.chat_completion(messages=[])
    assert resp.text_content == "alpha beta"

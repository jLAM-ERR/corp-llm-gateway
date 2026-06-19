import json

import httpx
import pytest

from corp_llm_gateway.corp_llm import (
    SANITIZE_TOOL_NAME,
    SANITIZE_TOOL_SCHEMA,
    CorpLlmClient,
    CorpLlmHttpError,
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
    resp = await client.chat_completion(messages=[{"role": "user", "content": "hi"}], max_tokens=10)
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


async def test_transport_error_message_names_the_exception_type() -> None:
    """httpx timeout exceptions stringify to '' — the error message must
    still name the exception TYPE so a "corp-llm transport error: " with
    nothing after it (the field incident) can't recur."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("", request=request)

    http = _mock_transport(handler)
    client = CorpLlmClient("https://x", model="m", http=http)
    with pytest.raises(CorpLlmHttpError, match="ConnectTimeout"):
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


async def test_owned_client_is_closed_by_aclose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no injected client, CorpLlmClient OWNS its httpx client, so
    aclose() must actually close it — otherwise a one-shot caller (the
    gateway-admin `sanitize` CLI) would leak the connection pool."""
    # Build the owned client independent of the dev's ambient proxy env —
    # httpx parses HTTP(S)_PROXY at construction, and a malformed value there
    # would fail this test for reasons unrelated to close behavior.
    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    client = CorpLlmClient("https://x", model="m")
    assert client._http.is_closed is False
    await client.aclose()
    assert client._http.is_closed is True

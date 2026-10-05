"""Pre-call header handling: corp-token auth and strip from every header location, header
declassification, and the ChatGPT auth bridge."""

from typing import Any

import pytest

from corp_llm_gateway.litellm_hook import GuardrailHttpException
from tests.hook_fixtures import _build_guardrail, _data_all_header_locations, _data_with_token


async def test_pre_call_missing_token_rejected() -> None:
    g, _ = _build_guardrail()
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call({"messages": [], "headers": {}})
    assert ei.value.status_code == 401
    assert ei.value.error_code == "E_MISSING_TOKEN"


async def test_pre_call_chatgpt_auth_bridge_forwards_only_required_headers() -> None:
    g, _ = _build_guardrail(forward_chatgpt_auth=True)
    data = {
        "model": "gpt-5.6-sol",
        "input": "hello",
        "metadata": {"gateway": "internal"},
        "headers": {
            "X-Corp-Auth": "tok-1",
            "Authorization": "Bearer oauth-value",
            "ChatGPT-Account-Id": "account-id",
            "Originator": "codex_cli_rs",
            "Session-Id": "session-id",
            "Thread-Id": "thread-id",
            "X-Codex-Beta-Features": "feature",
            "X-OpenAI-Internal-Codex-Responses-Lite": "true",
            "X-OpenAI-Unrelated": "do-not-forward",
            "Host": "127.0.0.1:4000",
            "X-Unrelated": "do-not-forward",
        },
    }

    out = await g.pre_call(data)
    upstream = {key.lower(): value for key, value in out["extra_headers"].items()}

    assert out["api_key"] == "oauth-value"
    assert "authorization" not in upstream
    assert upstream["chatgpt-account-id"] == "account-id"
    assert upstream["originator"] == "codex_cli_rs"
    assert upstream["session-id"] == "session-id"
    assert upstream["thread-id"] == "thread-id"
    assert upstream["x-codex-beta-features"] == "feature"
    assert upstream["x-openai-internal-codex-responses-lite"] == "true"
    assert "x-corp-auth" not in upstream
    assert "host" not in upstream
    assert "x-unrelated" not in upstream
    assert "x-openai-unrelated" not in upstream
    assert "metadata" not in out
    assert out["litellm_metadata"]["_corp_gateway_request_id"]


async def test_pre_call_chatgpt_auth_bridge_merges_litellm_header_buckets() -> None:
    g, _ = _build_guardrail(forward_chatgpt_auth=True)
    data = {
        "model": "gpt-5.6-sol",
        "input": "hello",
        "headers": {"X-Corp-Auth": "tok-1"},
        "proxy_server_request": {
            "headers": {
                "authorization": "Bearer oauth-value",
                "chatgpt-account-id": "account-id",
            }
        },
    }

    out = await g.pre_call(data)
    upstream = {key.lower(): value for key, value in out["extra_headers"].items()}

    assert out["api_key"] == "oauth-value"
    assert "authorization" not in upstream
    assert upstream["chatgpt-account-id"] == "account-id"
    assert "x-corp-auth" not in upstream


async def test_pre_call_chatgpt_auth_bridge_reads_litellm_secret_headers() -> None:
    g, _ = _build_guardrail(forward_chatgpt_auth=True)
    data = {
        "model": "gpt-5.6-sol",
        "input": "hello",
        "headers": {"X-Corp-Auth": "tok-1"},
        "secret_fields": {
            "raw_headers": {
                "Authorization": "Bearer oauth-value",
                "ChatGPT-Account-Id": "account-id",
                "X-Corp-Auth": "tok-1",
            }
        },
    }

    out = await g.pre_call(data)
    upstream = {key.lower(): value for key, value in out["extra_headers"].items()}

    assert out["api_key"] == "oauth-value"
    assert "authorization" not in upstream
    assert upstream["chatgpt-account-id"] == "account-id"
    assert "x-corp-auth" not in upstream
    assert "X-Corp-Auth" not in out["secret_fields"]["raw_headers"]


async def test_pre_call_chatgpt_auth_bridge_scrubs_litellm_logging_object_metadata() -> None:
    """The bridge's `data.pop("metadata")` is not enough: litellm builds its
    logging object before invoking this hook and keeps the request metadata in
    `model_call_details["litellm_params"]`, from where every configured logging
    callback still reads it (invariant 1, logger surface)."""

    class _LoggingObj:
        def __init__(self) -> None:
            self.model_call_details: dict[str, Any] = {
                "user": "alice@corp.example",
                "litellm_params": {
                    "metadata": {"user_id": "alice@corp.example"},
                    "user": "alice@corp.example",
                    "api_base": "https://chatgpt.example",
                },
            }

    g, _ = _build_guardrail(forward_chatgpt_auth=True)
    logging_obj = _LoggingObj()
    data = {
        "model": "gpt-5.6-sol",
        "input": "hello",
        "metadata": {"user_id": "alice@corp.example"},
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth-value"},
        "litellm_logging_obj": logging_obj,
    }

    await g.pre_call(data)

    details = logging_obj.model_call_details
    assert "metadata" not in details["litellm_params"]
    assert "user" not in details["litellm_params"]
    assert "user" not in details
    assert details["litellm_params"]["api_base"] == "https://chatgpt.example"


async def test_pre_call_chatgpt_auth_bridge_requires_bearer() -> None:
    g, sink = _build_guardrail(forward_chatgpt_auth=True)
    data = {
        "model": "gpt-5.6-sol",
        "input": "hello",
        "headers": {"X-Corp-Auth": "tok-1"},
    }

    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)

    assert ei.value.status_code == 401
    assert ei.value.error_code == "E_PROVIDER_AUTH"
    # Corp auth succeeded before the bridge ran, so the rejection is
    # attributable to the developer and team rather than audited as "unknown".
    assert len(sink.records) == 1
    rec = sink.records[0]
    assert rec["status"] == "failed"
    assert rec["error_code"] == "E_PROVIDER_AUTH"
    assert rec["user_id"] == "alice"
    assert rec["team_id"] == "t1"
    assert rec["provider"] == "openai"
    assert rec["model"] == "gpt-5.6-sol"


async def test_pre_call_invalid_token_rejected() -> None:
    g, _ = _build_guardrail()
    data = _data_with_token("nonexistent")
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 401
    assert ei.value.error_code == "E_TOKEN_INVALID"


async def test_pre_call_strips_corp_token_from_headers() -> None:
    g, _ = _build_guardrail()
    data = _data_with_token("tok-1")
    out = await g.pre_call(data)
    assert "X-Corp-Auth" not in out["headers"]
    assert out["headers"]["Authorization"] == "Bearer byok"


async def test_pre_call_secret_fields_raw_headers_not_declassified_into_data_headers() -> None:
    """secret_fields.raw_headers carries credentials litellm deliberately keeps
    out of logs and upstream request snapshots (Cookie, X-Internal-Credential).
    The auth-header merge that reads it must not leak those into data["headers"],
    which litellm DOES forward and log (defect #3)."""
    g, _ = _build_guardrail()
    data = {
        "model": "claude",
        "messages": [{"role": "user", "content": "hi"}],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
        "secret_fields": {
            "raw_headers": {
                "X-Corp-Auth": "tok-1",
                "Cookie": "session=secret",
                "X-Internal-Credential": "cred",
            }
        },
    }
    out = await g.pre_call(data)
    assert "Cookie" not in out["headers"], "secret_fields.raw_headers declassified"
    assert "X-Internal-Credential" not in out["headers"]
    assert out["headers"]["Authorization"] == "Bearer byok"


async def test_pre_call_strips_corp_token_from_litellm_params_proxy_server_request() -> None:
    """`_extract_auth_headers` reads litellm_params.proxy_server_request.headers
    for auth, so the strip must cover it too, or the corp token authenticates the
    request and then survives into the dict litellm hands to log callbacks
    (defect #4, invariant 4)."""
    g, _ = _build_guardrail()
    data = {
        "model": "claude",
        "messages": [{"role": "user", "content": "hi"}],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
        "litellm_params": {
            "proxy_server_request": {
                "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"}
            }
        },
    }
    out = await g.pre_call(data)
    proxy_headers = out["litellm_params"]["proxy_server_request"]["headers"]
    assert "X-Corp-Auth" not in proxy_headers
    assert proxy_headers["Authorization"] == "Bearer byok"


def test_extract_headers_write_path_matches_release_semantics() -> None:
    """The WRITE-path `_extract_headers` (data["headers"] at pre_call ~:312) must
    stay byte-identical to release/1.0.x: first non-empty of data["headers"] then
    proxy_server_request, unwrapping a nested "headers" key — no merge across
    buckets. Verified against `git show release/1.0.x:src/corp_llm_gateway/litellm_hook.py`."""
    from corp_llm_gateway.litellm_hook import _extract_headers

    # data["headers"] wins even when other buckets carry MORE headers -- no merge.
    assert _extract_headers(
        {
            "headers": {"Authorization": "Bearer byok"},
            "proxy_server_request": {"headers": {"Cookie": "session=secret"}},
            "secret_fields": {"raw_headers": {"X-Internal-Credential": "cred"}},
        }
    ) == {"Authorization": "Bearer byok"}

    # data["headers"] empty/missing falls back to proxy_server_request, unwrapping
    # its own nested "headers" key.
    assert _extract_headers(
        {"proxy_server_request": {"headers": {"Authorization": "Bearer byok"}}}
    ) == {"Authorization": "Bearer byok"}

    # proxy_server_request without a nested "headers" key IS the header dict itself.
    assert _extract_headers({"proxy_server_request": {"Authorization": "Bearer byok"}}) == {
        "Authorization": "Bearer byok"
    }

    assert _extract_headers({}) == {}


async def test_pre_call_handles_proxy_server_request_headers_shape() -> None:
    """LiteLLM passes headers via `proxy_server_request.headers` in some paths."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = {
        "model": "claude",
        "messages": [{"role": "user", "content": "hi alice"}],
        "proxy_server_request": {
            "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"}
        },
    }
    out = await g.pre_call(data)
    assert "X-Corp-Auth" not in out["headers"]
    assert out["messages"][0]["content"] == "hi [N1]"


async def test_pre_call_strips_corp_token_from_all_header_locations() -> None:
    """F6: the corp token must not survive in ANY header-bearing location, and the
    BYOK Authorization header must pass through untouched (invariant 3)."""
    g, _ = _build_guardrail()
    data = _data_all_header_locations("tok-1")
    out = await g.pre_call(data)
    for loc in (
        out["headers"],
        out["proxy_server_request"]["headers"],
        out["metadata"]["headers"],
        out["litellm_metadata"]["headers"],
    ):
        assert not any(k.lower() == "x-corp-auth" for k in loc)
        assert loc["Authorization"] == "Bearer byok"


async def test_pre_call_strips_corp_token_from_proxy_server_request_headers() -> None:
    """The token in proxy_server_request.headers is stripped in place (pre-fix it survived)."""
    g, _ = _build_guardrail()
    data = {
        "model": "claude",
        "messages": [{"role": "user", "content": "hi"}],
        "proxy_server_request": {
            "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"}
        },
    }
    out = await g.pre_call(data)
    assert "X-Corp-Auth" not in out["proxy_server_request"]["headers"]
    assert out["proxy_server_request"]["headers"]["Authorization"] == "Bearer byok"


async def test_pre_call_strips_corp_token_case_insensitive_all_locations() -> None:
    """litellm normalizes header case; a lower-cased `x-corp-auth` is stripped everywhere."""
    g, _ = _build_guardrail()
    hdrs = {"x-corp-auth": "tok-1", "authorization": "Bearer byok"}
    data = {
        "model": "claude",
        "messages": [{"role": "user", "content": "hello"}],
        "headers": dict(hdrs),
        "proxy_server_request": {"headers": dict(hdrs)},
        "metadata": {"headers": dict(hdrs)},
        "litellm_metadata": {"headers": dict(hdrs)},
    }
    out = await g.pre_call(data)
    for loc in (
        out["headers"],
        out["proxy_server_request"]["headers"],
        out["metadata"]["headers"],
        out["litellm_metadata"]["headers"],
    ):
        assert not any(k.lower() == "x-corp-auth" for k in loc)
        assert loc["authorization"] == "Bearer byok"


async def test_pre_call_strips_corp_token_from_litellm_params_metadata_headers() -> None:
    """F6 completeness: litellm_params["metadata"] is litellm's logging-metadata dict
    (fed to log callbacks + threaded by _scatter); a corp token mirrored into its
    `headers` must be stripped (invariant 4) while BYOK Authorization survives (3)."""
    g, _ = _build_guardrail()
    data = {
        "model": "claude",
        "messages": [{"role": "user", "content": "hello"}],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
        "litellm_params": {
            "metadata": {"headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"}}
        },
    }
    out = await g.pre_call(data)
    hdrs = out["litellm_params"]["metadata"]["headers"]
    assert not any(k.lower() == "x-corp-auth" for k in hdrs)
    assert hdrs["Authorization"] == "Bearer byok"

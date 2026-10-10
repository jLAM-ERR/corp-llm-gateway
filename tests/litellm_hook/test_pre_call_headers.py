"""Pre-call header handling: corp-token auth and strip from every header location, header
declassification, and the ChatGPT auth bridge."""

import json
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
    # Nothing else either: the developer's bearer reaches no upstream through it.
    assert out["headers"] == {}


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
    assert out["headers"] == {}


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
    """F6: the corp token must not survive in ANY header-bearing location. litellm's
    own copies keep the developer's Authorization; the upstream bucket holds nothing."""
    g, _ = _build_guardrail()
    data = _data_all_header_locations("tok-1")
    out = await g.pre_call(data)
    assert out["headers"] == {}
    for loc in (
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
    assert out["headers"] == {}
    for loc in (
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


# Every kind of inbound header a client can send: the corp token, the provider
# credentials, litellm's own key header, protocol headers, wire headers, and an
# arbitrary one whose value is PII the sanitizer never sees.
_CLIENT_HEADERS = {
    "X-Corp-Auth": "tok-1",
    "Authorization": "Bearer byok",
    "Cookie": "session=secret",
    "X-Probe-Secret": "alice.probe@corp.lan",
    "x-api-key": "sk-client-key",
    "x-litellm-api-key": "sk-litellm-key",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "beta-1",
    "User-Agent": "claude-cli/1.0",
    "Content-Length": "42",
    "Host": "127.0.0.1:4000",
}


def _request_with_headers_in(bucket: str, model: str) -> dict[str, Any]:
    data: dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    if bucket == "headers":
        data["headers"] = dict(_CLIENT_HEADERS)
    elif bucket == "proxy_server_request":
        # litellm's own forwarding off: the logging copy is the only inbound copy.
        data["proxy_server_request"] = {"headers": dict(_CLIENT_HEADERS)}
    else:
        data["headers"] = {"X-Corp-Auth": "tok-1"}
        data["secret_fields"] = {"raw_headers": dict(_CLIENT_HEADERS)}
    return data


@pytest.mark.parametrize("strip_inbound", [True, False], ids=["strip-on", "strip-off"])
@pytest.mark.parametrize("bucket", ["headers", "proxy_server_request", "secret_fields"])
async def test_pre_call_forwards_no_client_header_through_data_headers(
    bucket: str, strip_inbound: bool
) -> None:
    """litellm sends ``data["headers"]`` upstream on every provider, unsanitized and
    outside the Stage-5 scan, so the hook puts no client header there, whichever
    bucket the headers came in and whatever the wire-header strip says."""
    g, _ = _build_guardrail()
    g._strip_inbound_headers_to_upstream = strip_inbound

    out = await g.pre_call(_request_with_headers_in(bucket, "corp-glm-5.1"))

    assert out["headers"] == {}


@pytest.mark.parametrize(
    "bridge,model,api_key,forwarded",
    [
        pytest.param(
            "forward_anthropic_auth",
            "claude-sonnet-4-5",
            "sk-ant-oat01-unit",
            {"anthropic-version", "anthropic-beta", "user-agent"},
            id="anthropic",
        ),
        pytest.param(
            "forward_chatgpt_auth", "gpt-5.6-sol", "oauth-unit", {"user-agent"}, id="codex"
        ),
    ],
)
async def test_a_bridge_carries_its_allowlist_in_extra_headers_and_nothing_in_data_headers(
    bridge: str, model: str, api_key: str, forwarded: set[str]
) -> None:
    g, _ = _build_guardrail(**{bridge: True})
    data = _request_with_headers_in("proxy_server_request", model)
    data["proxy_server_request"]["headers"]["Authorization"] = f"Bearer {api_key}"

    out = await g.pre_call(data)

    assert out["headers"] == {}
    assert out["api_key"] == api_key
    assert {name.lower() for name in out["extra_headers"]} == forwarded


def test_the_upstream_header_allowlist_is_empty() -> None:
    """Adding a name here sends that client header's value to every provider,
    unsanitized: a security decision, like a route-table row, not a config change."""
    from corp_llm_gateway.litellm_hook import _UPSTREAM_HEADER_ALLOWLIST

    assert frozenset() == _UPSTREAM_HEADER_ALLOWLIST


def test_the_upstream_projection_keeps_only_allowlisted_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from corp_llm_gateway import litellm_hook

    monkeypatch.setattr(
        litellm_hook, "_UPSTREAM_HEADER_ALLOWLIST", frozenset({"anthropic-version", "x-corp-auth"})
    )

    projected = litellm_hook._upstream_headers(dict(_CLIENT_HEADERS))

    # Case-insensitive on the name, value untouched; the corp token never, listed or not.
    assert projected == {"anthropic-version": "2023-06-01"}
    assert litellm_hook._upstream_headers({}) == {}


_BODY_SECRET = "alice.bodykey@corp.lan"
_CONTROL_BODY_VALUES: dict[str, Any] = {
    "extra_headers": {"x-body-probe": _BODY_SECRET},
    "provider_specific_header": {
        "custom_llm_provider": "anthropic",
        "extra_headers": {"x-psh-probe": _BODY_SECRET},
    },
    "api_key": _BODY_SECRET,
    "organization": _BODY_SECRET,
}
_BRIDGED_REQUESTS = {
    "corp": ({}, "corp-glm-5.1", "Bearer corp-client"),
    "anthropic": (
        {"forward_anthropic_auth": True},
        "claude-sonnet-4-5",
        "Bearer sk-ant-oat01-unit",
    ),
    "codex": ({"forward_chatgpt_auth": True}, "gpt-5.6-sol", "Bearer oauth-unit"),
}


@pytest.mark.parametrize("route", list(_BRIDGED_REQUESTS))
@pytest.mark.parametrize("key", list(_CONTROL_BODY_VALUES))
async def test_a_client_control_body_key_is_dropped_without_refusing(key: str, route: str) -> None:
    """litellm turns these body keys into upstream headers or the upstream credential;
    the client's values go, silently, and the request goes on."""
    kwargs, model, bearer = _BRIDGED_REQUESTS[route]
    g, _ = _build_guardrail(**kwargs)
    data = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": bearer},
        key: _CONTROL_BODY_VALUES[key],
    }

    out = await g.pre_call(data)

    assert _BODY_SECRET not in json.dumps(
        {k: v for k, v in out.items() if k in _CONTROL_BODY_VALUES}
    )
    if route == "corp":
        assert all(k not in out for k in _CONTROL_BODY_VALUES)
    else:
        assert out["api_key"] == bearer[7:]


def test_the_control_body_keys_are_exactly_the_four_litellm_reads() -> None:
    from corp_llm_gateway.litellm_hook import _CLIENT_CONTROL_BODY_KEYS

    assert (
        frozenset({"extra_headers", "provider_specific_header", "api_key", "organization"})
        == _CLIENT_CONTROL_BODY_KEYS
    )


_FREE_TEXT = "alice.header@corp.lan"


_ANTHROPIC = ("forward_anthropic_auth", "claude-sonnet-4-5", "sk-ant-oat01-unit")
_CODEX = ("forward_chatgpt_auth", "gpt-5.6-sol", "oauth-unit")
# A value each format must keep (a real recorded sample where the repo has one).
_FORMAT_CASES: dict[str, tuple[tuple[str, str, str], str, str]] = {
    "anthropic-version": (_ANTHROPIC, "anthropic-version", "2023-01-01"),
    "anthropic-beta": (
        _ANTHROPIC,
        "anthropic-beta",
        "oauth-2025-04-20, interleaved-thinking-2025-05-14",
    ),
    "anthropic-user-agent": (_ANTHROPIC, "user-agent", "claude-cli/2.1.220 (external, cli)"),
    "codex-user-agent": (_CODEX, "user-agent", "codex_cli_rs/0.46.0 (Mac OS 15.6.1; arm64)"),
    "originator": (_CODEX, "originator", "codex_cli_rs"),
    "session-id": (_CODEX, "session-id", "0199a0f2-6d1c-7c43-9a6e-2f4b8c1d5e60"),
    "thread-id": (_CODEX, "thread-id", "0199a0f2-6d1c-7c43-9a6e-2f4b8c1d5e61"),
    "responses-lite": (_CODEX, "x-openai-internal-codex-responses-lite", "true"),
}


@pytest.mark.parametrize("case", list(_FORMAT_CASES))
async def test_a_bridge_drops_a_header_value_outside_its_format(case: str) -> None:
    (bridge, model, bearer), header, good = _FORMAT_CASES[case]
    g, _ = _build_guardrail(**{bridge: True})
    for value, kept in ((good, True), (_FREE_TEXT, False)):
        data = {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "headers": {"X-Corp-Auth": "tok-1", "Authorization": f"Bearer {bearer}", header: value},
        }

        out = await g.pre_call(data)

        assert (out["extra_headers"].get(header) == value) is kept, (header, value)


async def test_the_codex_bridge_forwards_exact_names_only() -> None:
    g, _ = _build_guardrail(forward_chatgpt_auth=True)
    data = {
        "model": "gpt-5.6-sol",
        "input": "hi",
        "headers": {
            "X-Corp-Auth": "tok-1",
            "Authorization": "Bearer oauth-unit",
            "x-codex-beta-features": "unified_exec",
            "x-codex-anything": _FREE_TEXT,
        },
    }

    out = await g.pre_call(data)

    assert set(out["extra_headers"]) == {"x-codex-beta-features"}

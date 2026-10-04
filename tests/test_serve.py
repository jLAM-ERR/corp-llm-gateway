"""`python -m corp_llm_gateway.serve` — the uvicorn arguments, without binding."""

from __future__ import annotations

from pathlib import Path

import pytest

from corp_llm_gateway import config, serve

JSON_CONFIG = """
model_list: []
litellm_settings:
  json_logs: true
"""

PLAIN_CONFIG = """
model_list: []
litellm_settings:
  json_logs: false
"""


@pytest.fixture
def litellm_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(PLAIN_CONFIG)
    monkeypatch.setenv("CORP_LLM_LITELLM_CONFIG", str(path))
    config.reset_cache()
    return path


def test_the_served_target_is_the_gated_app(litellm_config_file: Path) -> None:
    # Never `litellm.proxy.proxy_server:app`: that is litellm's router with
    # nothing in front of it.
    assert serve.APP == "corp_llm_gateway.asgi:app"
    assert serve.uvicorn_arguments()["app"] == serve.APP


def test_the_defaults_match_the_litellm_cli(litellm_config_file: Path) -> None:
    arguments = serve.uvicorn_arguments()

    assert arguments["host"] == "0.0.0.0"
    assert arguments["port"] == 4000
    # litellm's CLI passes this; without it uvicorn advertises its version.
    assert arguments["server_header"] is False


def test_one_worker_only(litellm_config_file: Path) -> None:
    # A second uvicorn worker is a second process importing the entrypoint: it
    # would re-run the Prisma schema sequence against the same database and build
    # a second Prometheus registry, so /metrics would report one worker's counts.
    assert serve.uvicorn_arguments()["workers"] == 1


@pytest.mark.parametrize("port", ["four-thousand", "0", "70000", "-1", "80.5"])
def test_a_bad_serve_port_exits_78(
    litellm_config_file: Path, monkeypatch: pytest.MonkeyPatch, port: str
) -> None:
    # The same refusal `gateway-admin config check` gives, not a ValueError
    # traceback out of int().
    monkeypatch.setenv("CORP_LLM_SERVE_PORT", port)
    config.reset_cache()

    with pytest.raises(SystemExit) as raised:
        serve.uvicorn_arguments()

    assert raised.value.code == 78


def test_host_and_port_come_from_config(
    litellm_config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_LLM_SERVE_HOST", "127.0.0.1")
    monkeypatch.setenv("CORP_LLM_SERVE_PORT", "8443")
    config.reset_cache()

    arguments = serve.uvicorn_arguments()

    assert (arguments["host"], arguments["port"]) == ("127.0.0.1", 8443)


def test_no_json_log_config_when_the_litellm_config_does_not_ask_for_one(
    litellm_config_file: Path,
) -> None:
    assert "log_config" not in serve.uvicorn_arguments()


@pytest.mark.requires_litellm
def test_json_logs_selects_litellms_json_log_config(litellm_config_file: Path) -> None:
    # Vector consumes container stdout, so uvicorn's own access and error lines
    # have to be JSON too — the CLI does this at proxy_cli.py:269-271.
    litellm_config_file.write_text(JSON_CONFIG)

    log_config = serve.uvicorn_arguments()["log_config"]

    assert isinstance(log_config, dict)
    formatters = log_config.get("formatters", {})
    assert any("json" in str(value).lower() for value in formatters.values())

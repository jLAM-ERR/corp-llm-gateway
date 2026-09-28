"""The shipped litellm configs turn on no request-content logging (plan 20260926 hazard 17)
and load no policies or guardrails from litellm's database (hazard 14a); what litellm's
config-table overlay can still add (hazard 14c).

litellm hands every success/failure logger the request its logging object holds, and
its DEBUG output prints the request before any pre-call hook runs. Our pre-call keeps
the first sanitised and the arm step refuses the second; these configs are the layer
under both: no extra success/failure logger, no prompts stored in spend logs, no
``set_verbose``, no DEBUG switch in the environment the gateway runs with.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.helm.test_chart_render import _first_of_kind, _litellm_configmap, _render

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose"
OUR_CALLBACK = "corp_llm_gateway.bootstrap.guardrail"

LITELLM_CONFIGS = {
    "compose": COMPOSE / "litellm" / "config.yaml",
    "compose-oauth": COMPOSE / "litellm" / "config.oauth.yaml",
}
COMPOSE_FILES = sorted(COMPOSE.glob("docker-compose*.yml"))

# litellm's own DEBUG switches (proxy_server.initialize, proxy_cli.py) and ours.
DEBUG_ENV_KEYS = ("DETAILED_DEBUG", "DEBUG", "CORP_LLM_ALLOW_LITELLM_DEBUG")

needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm binary not on PATH")


def _helm_docs() -> list[dict[str, Any]]:
    result = _render()
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _helm_litellm_config() -> dict[str, Any]:
    return yaml.safe_load(_litellm_configmap(_helm_docs())["data"]["config.yaml"])


def _assert_logs_no_content(config: dict[str, Any]) -> None:
    litellm_settings = config.get("litellm_settings") or {}
    general = config.get("general_settings") or {}
    assert litellm_settings.get("callbacks") == [OUR_CALLBACK]
    assert "success_callback" not in litellm_settings
    assert litellm_settings.get("failure_callback", []) in ([], [OUR_CALLBACK])
    assert "set_verbose" not in litellm_settings
    for section in (litellm_settings, general):
        assert "store_prompts_in_spend_logs" not in section


# litellm loads every DB object type when the key is absent (proxy_server.py
# should_load_db_object): the pin must be there, and must name neither.
DB_OBJECTS = ["models"]


def _assert_db_objects_pinned(config: dict[str, Any]) -> None:
    general = config.get("general_settings") or {}
    objects = general.get("supported_db_objects")
    assert objects == DB_OBJECTS
    assert "policies" not in objects and "guardrails" not in objects


def _assert_no_debug_env(env: dict[str, Any]) -> None:
    assert "DEBUG" not in str(env.get("LITELLM_LOG") or "").upper()
    for key in DEBUG_ENV_KEYS:
        assert key not in env, key


@pytest.mark.parametrize("name", sorted(LITELLM_CONFIGS))
def test_compose_litellm_config_logs_no_request_content(name: str) -> None:
    _assert_logs_no_content(yaml.safe_load(LITELLM_CONFIGS[name].read_text()))


@needs_helm
def test_helm_litellm_config_logs_no_request_content() -> None:
    _assert_logs_no_content(_helm_litellm_config())


# The pre-call asks every ticketed chat stream for its usage chunk without litellm's
# per-model `stream_options` check; `drop_params` drops the key where a provider has no
# such param (without it Anthropic via chat would answer 400).
@pytest.mark.parametrize("name", sorted(LITELLM_CONFIGS))
def test_compose_litellm_config_drops_params_a_provider_does_not_take(name: str) -> None:
    config = yaml.safe_load(LITELLM_CONFIGS[name].read_text())
    assert config["litellm_settings"]["drop_params"] is True


@needs_helm
def test_helm_litellm_config_drops_params_a_provider_does_not_take() -> None:
    assert _helm_litellm_config()["litellm_settings"]["drop_params"] is True


@pytest.mark.parametrize("name", sorted(LITELLM_CONFIGS))
def test_compose_litellm_config_loads_no_policies_from_the_db(name: str) -> None:
    _assert_db_objects_pinned(yaml.safe_load(LITELLM_CONFIGS[name].read_text()))


@needs_helm
def test_helm_litellm_config_loads_no_policies_from_the_db() -> None:
    _assert_db_objects_pinned(_helm_litellm_config())


def test_litellm_reads_the_pin_as_models_only() -> None:
    """litellm's own reader of the key (what its DB reconcile consults)."""
    litellm_proxy = pytest.importorskip("litellm.proxy.proxy_server")
    config = yaml.safe_load(LITELLM_CONFIGS["compose"].read_text())
    patched = pytest.MonkeyPatch()
    try:
        patched.setattr(litellm_proxy, "general_settings", config["general_settings"])
        loads = {
            name: litellm_proxy.should_load_db_object(object_type=name)
            for name in ("models", "policies", "guardrails", "config_overrides")
        }
    finally:
        patched.undo()
    assert loads == {
        "models": True,
        "policies": False,
        "guardrails": False,
        "config_overrides": False,
    }


# ── hazard 14c: litellm's config-table overlay ──────────────────────────────
# With a database and ``store_model_in_db`` (env ``STORE_MODEL_IN_DB``, or a DB
# ``general_settings`` row saying so), litellm's reconcile job re-reads the
# ``LiteLLM_Config`` table every 30 s and applies two rows: ``litellm_settings``
# (``_update_config_fields`` + ``_add_callbacks_from_db_config``) and ``general_settings``
# (``_update_general_settings``); ``supported_db_objects`` gates neither. Compose gives
# litellm both; the Helm chart gives it no database. The served-stack side (what such a
# callback sees, the spend-log row, a pass-through route) is
# ``tests/test_desanitize_served_stack.py``.

GUARDRAIL_INITIALISERS = frozenset({"initialize_guardrails", "init_guardrails_v2"})


def test_compose_litellm_reads_its_config_table() -> None:
    services = yaml.safe_load((COMPOSE / "docker-compose.yml").read_text())["services"]
    env = _compose_env(services["litellm"].get("environment"))

    assert "DATABASE_URL" in env
    assert env.get("STORE_MODEL_IN_DB") == "True"


@needs_helm
def test_helm_litellm_has_no_database_to_read_a_config_table_from() -> None:
    docs = _helm_docs()
    pod = _first_of_kind(docs, "Deployment")["spec"]["template"]["spec"]
    names = {
        e["name"]
        for c in pod["containers"] + pod.get("initContainers", [])
        for e in c.get("env", [])
    }
    secret = set((_first_of_kind(docs, "Secret").get("stringData") or {}).keys())

    for key in ("DATABASE_URL", "DIRECT_URL", "STORE_MODEL_IN_DB"):
        assert key not in names | secret, key


def test_a_db_litellm_settings_row_appends_callbacks_and_starts_no_guardrail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the reconcile does with a DB ``litellm_settings`` row: a ``callbacks`` name
    litellm does not know is appended to ``litellm.callbacks`` as a string that resolves to
    nothing (a known integration's name is instantiated into the success/failure lists
    instead, log events only); the arm check never sees it, and ``guardrails`` starts
    nothing."""
    litellm = pytest.importorskip("litellm")
    proxy = pytest.importorskip("litellm.proxy.proxy_server")
    litellm_logging = pytest.importorskip("litellm.litellm_core_utils.litellm_logging")
    from corp_llm_gateway.route_gate.arm_checks import guardrail_problems

    ours = object()
    monkeypatch.setattr(litellm, "callbacks", [ours])
    config = yaml.safe_load(LITELLM_CONFIGS["compose"].read_text())
    row = {
        "callbacks": ["db-row-callback"],
        "guardrails": [{"guardrail_name": "db-row", "litellm_params": {"guardrail": "x"}}],
    }

    merged = proxy.proxy_config._update_config_fields(
        current_config=config, param_name="litellm_settings", db_param_value=row
    )
    proxy.proxy_config._add_callbacks_from_db_config(merged)

    assert merged["litellm_settings"]["callbacks"] == ["db-row-callback"]
    assert litellm.callbacks == [ours, "db-row-callback"]
    assert "db-row-callback" not in litellm._known_custom_logger_compatible_callbacks
    assert litellm_logging.get_custom_logger_compatible_class("db-row-callback") is None
    assert guardrail_problems(litellm.callbacks, is_ours=lambda cb: cb is ours) == []


@pytest.mark.parametrize("name", sorted(LITELLM_CONFIGS))
async def test_a_db_general_settings_row_turns_on_prompts_in_spend_logs(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """The shipped configs do not set ``store_prompts_in_spend_logs``, so a DB row's value
    is the one litellm uses (YAML wins only for a key it sets)."""
    proxy = pytest.importorskip("litellm.proxy.proxy_server")
    general = dict(yaml.safe_load(LITELLM_CONFIGS[name].read_text())["general_settings"])
    monkeypatch.setattr(proxy, "general_settings", general)
    monkeypatch.setattr(proxy.proxy_config, "_yaml_general_settings_keys", set(general))

    await proxy.proxy_config._update_general_settings({"store_prompts_in_spend_logs": True})

    assert proxy.general_settings["store_prompts_in_spend_logs"] is True


def test_only_litellms_startup_config_load_starts_guardrails() -> None:
    """``load_config`` runs before litellm has its database client, so a DB
    ``guardrails`` row is merged into a config nothing starts guardrails from again."""
    import ast
    import inspect

    proxy = pytest.importorskip("litellm.proxy.proxy_server")
    tree = ast.parse(inspect.getsource(proxy))
    callers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(call, ast.Call)
            and getattr(call.func, "id", getattr(call.func, "attr", None)) in GUARDRAIL_INITIALISERS
            for call in ast.walk(node)
        )
    }

    assert callers == {"load_config"}


def _compose_env(entries: Any) -> dict[str, Any]:
    if isinstance(entries, dict):
        return dict(entries)
    env: dict[str, Any] = {}
    for item in entries or ():
        key, sep, value = item.partition("=")
        env[key] = value if sep else None
    return env


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_compose_gateway_env_turns_no_litellm_debug_on(path: Path) -> None:
    services = (yaml.safe_load(path.read_text()) or {}).get("services") or {}
    gateway = services.get("litellm")
    if gateway is None:
        pytest.skip(f"{path.name} does not define the gateway service")
    _assert_no_debug_env(_compose_env(gateway.get("environment")))


@needs_helm
def test_helm_gateway_env_turns_no_litellm_debug_on() -> None:
    pod = _first_of_kind(_helm_docs(), "Deployment")["spec"]["template"]["spec"]
    containers = pod["containers"] + pod.get("initContainers", [])
    for container in containers:
        _assert_no_debug_env({e["name"]: e.get("value") for e in container.get("env", [])})


def test_the_compose_file_set_is_the_one_this_guard_expects() -> None:
    # A new overlay is picked up by the glob; this pins that the glob still finds them.
    assert {p.name for p in COMPOSE_FILES} >= {
        "docker-compose.yml",
        "docker-compose.oauth.yml",
        "docker-compose.issuance.yml",
        "docker-compose.build.yml",
    }


def test_the_guard_catches_what_it_guards_against() -> None:
    with pytest.raises(AssertionError):
        _assert_logs_no_content(
            {"litellm_settings": {"callbacks": [OUR_CALLBACK], "success_callback": ["langfuse"]}}
        )
    with pytest.raises(AssertionError):
        _assert_logs_no_content(
            {"litellm_settings": {"callbacks": [OUR_CALLBACK], "set_verbose": True}}
        )
    with pytest.raises(AssertionError):
        _assert_logs_no_content(
            {
                "litellm_settings": {"callbacks": [OUR_CALLBACK]},
                "general_settings": {"store_prompts_in_spend_logs": True},
            }
        )
    with pytest.raises(AssertionError):
        _assert_no_debug_env(_compose_env(["LITELLM_LOG=${LITELLM_LOG:-DEBUG}"]))
    with pytest.raises(AssertionError):
        _assert_no_debug_env(_compose_env(["DETAILED_DEBUG"]))
    with pytest.raises(AssertionError):
        _assert_db_objects_pinned({"general_settings": {}})
    with pytest.raises(AssertionError):
        _assert_db_objects_pinned({"general_settings": {"supported_db_objects": ["policies"]}})

"""The startup invariants of `corp_llm_gateway.asgi`, proven by importing it.

Every case runs in a subprocess: importing the module IS the boot sequence, so
two scenarios cannot share an interpreter. Each script prints one sentinel line
of JSON, which is all this file reads — litellm writes a banner to stdout.

Skips only when litellm is absent (the 3.14 venv). litellm present without
fastapi FAILS: the `asgi` extra is part of the dev install from this task on, and
a skipped startup-invariant test is not a passed gate.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from importlib.util import find_spec
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    find_spec("litellm") is None, reason="litellm is not installed in this interpreter"
)

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "@@RESULT@@"

VALID_CONFIG = """
model_list:
  - model_name: "corp-*"
    litellm_params:
      model: "hosted_vllm/probe"
      api_base: "http://127.0.0.1:1"
      api_key: "probe"
litellm_settings:
  callbacks: ["corp_llm_gateway.bootstrap.guardrail"]
  drop_params: true
  json_logs: true
"""

STRANGER_CONFIG = """
model_list:
  - model_name: "corp-*"
    litellm_params:
      model: "hosted_vllm/probe"
      api_base: "http://127.0.0.1:1"
      api_key: "probe"
litellm_settings:
  callbacks: ["stranger_callback.handler"]
  drop_params: true
"""

STRANGER_MODULE = """
from litellm.integrations.custom_logger import CustomLogger

handler = CustomLogger()
"""

PASS_THROUGH_CONFIG = (
    VALID_CONFIG
    + """
general_settings:
  pass_through_endpoints:
    - path: "/adapter"
      target: "https://example.invalid"
"""
)

DATABASE_URL_CONFIG = (
    VALID_CONFIG
    + """
general_settings:
  database_url: "postgresql://u:p@db.invalid:5432/litellm"
"""
)

NO_SCHEMA_UPDATE_CONFIG = (
    VALID_CONFIG
    + """
general_settings:
  disable_prisma_schema_update: true
"""
)


def _require_fastapi() -> None:
    if find_spec("fastapi") is None:
        pytest.fail(
            "litellm is installed but fastapi is not: install the asgi extra "
            '(pip install -e ".[dev,asgi]"). These invariants must run, not skip.'
        )


# The boot subprocess gets a whitelist, never `os.environ`: importing the module
# IS the boot, and a developer shell carrying DEBUG, REDIS_URL, CORP_LLM_PG_DSN,
# CORP_METRICS_EXPORTER, DATABASE_URL or CORP_LLM_GATEWAY_CONFIG_FILE would change
# what boots — these assertions would then describe that shell, not the gateway.
_INHERITED = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")


def _run(
    script: str,
    config_path: Path | str,
    *,
    extra_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    _require_fastapi()
    search = [str(ROOT / "src")] + ([str(extra_path)] if extra_path else [])
    child_env = {name: os.environ[name] for name in _INHERITED if name in os.environ}
    child_env.update(
        {
            "PYTHONPATH": os.pathsep.join(search),
            "CORP_LLM_LITELLM_CONFIG": str(config_path),
            # Offline and deterministic: no oracle client, local-first floor on.
            "CORP_LLM_ORACLE_ENABLED": "0",
            "CORP_LLM_LOCAL_FIRST": "1",
            "CORP_AUDIT_SINK": "stdout",
            # litellm downloads its model cost map from raw.githubusercontent.com
            # at import unless this is set; these boots must not need the network.
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        }
    )
    child_env.update(env or {})
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        env=child_env,
        timeout=300,
        check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith(SENTINEL)]
    assert lines, (
        f"the boot script printed no result line\n--- stdout ---\n{completed.stdout}\n"
        f"--- stderr ---\n{completed.stderr}"
    )
    payload = json.loads(lines[-1][len(SENTINEL) :])
    payload["returncode"] = completed.returncode
    payload["stdout"] = completed.stdout
    return payload


@pytest.fixture(scope="module")
def valid_config(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("litellm") / "config.yaml"
    path.write_text(VALID_CONFIG)
    return path


# ── step 1: the config gate, before litellm's app is imported ────────────────

_REFUSAL_SCRIPT = f"""
    import json, sys
    code = None
    try:
        import corp_llm_gateway.asgi  # noqa: F401
    except SystemExit as exc:
        code = exc.code
    print("{SENTINEL}" + json.dumps({{
        "exit_code": code,
        "proxy_imported": "litellm.proxy.proxy_server" in sys.modules,
    }}))
"""


def test_a_missing_config_exits_78_before_litellms_app_is_imported(tmp_path: Path) -> None:
    result = _run(_REFUSAL_SCRIPT, tmp_path / "absent.yaml")

    assert result["exit_code"] == 78
    assert result["proxy_imported"] is False


def test_a_config_that_is_not_yaml_exits_78(tmp_path: Path) -> None:
    path = tmp_path / "config.txt"
    path.write_text(VALID_CONFIG)

    result = _run(_REFUSAL_SCRIPT, path)

    assert result["exit_code"] == 78
    assert result["proxy_imported"] is False


def test_a_config_only_database_url_exits_78(tmp_path: Path) -> None:
    # litellm's CLI exports general_settings.database_url to DATABASE_URL before
    # its Prisma sequence; this entrypoint reads the environment only, so the
    # config-only spelling would connect litellm to an unmigrated database.
    path = tmp_path / "config.yaml"
    path.write_text(DATABASE_URL_CONFIG)

    result = _run(_REFUSAL_SCRIPT, path)

    assert result["exit_code"] == 78
    assert result["proxy_imported"] is False


def test_a_config_with_pass_through_endpoints_exits_78(tmp_path: Path) -> None:
    # SafeRouteAdder registers those paths at runtime; the route gate cannot
    # classify them, so default-deny would 404 every one. Refuse the config.
    path = tmp_path / "config.yaml"
    path.write_text(PASS_THROUGH_CONFIG)

    result = _run(_REFUSAL_SCRIPT, path)

    assert result["exit_code"] == 78
    assert result["proxy_imported"] is False


# ── step 2: the Prisma sequence, with litellm's four guards ──────────────────

_PRISMA_SCRIPT = f"""
    import json, subprocess

    calls = []

    class _Completed:
        returncode = 0

    def _fake_run(args, **kwargs):
        # litellm's runnability probe. asgi.py holds the module, not the
        # function, so replacing the attribute is what it calls.
        calls.append(["probe", list(args)])
        return _Completed()

    subprocess.run = _fake_run

    from litellm.proxy.db import check_migration, prisma_client

    def _fake_diff(db_url=None):
        calls.append(["diff", db_url])

    check_migration.check_prisma_schema_diff = _fake_diff

    OUTCOME = {{outcome!r}}

    def _fake_setup(use_migrate=None, use_v2_resolver=None):
        calls.append(["setup", use_migrate, use_v2_resolver])
        if OUTCOME == "raise":
            raise RuntimeError("prisma db push against a partitioned LiteLLM_SpendLogs")
        return OUTCOME == "ok"

    prisma_client.PrismaManager.setup_database = staticmethod(_fake_setup)

    code = None
    booted = False
    try:
        import corp_llm_gateway.asgi  # noqa: F401
        booted = True
    except SystemExit as exc:
        code = exc.code
    print("{SENTINEL}" + json.dumps(
        {{{{"exit_code": code, "calls": calls, "booted": booted}}}}
    ))
"""

DSN = "postgresql://u:p@db.invalid:5432/litellm"


def _prisma(config_path: Path, outcome: str, **env: str) -> dict:
    return _run(
        _PRISMA_SCRIPT.format(outcome=outcome),
        config_path,
        env={"DATABASE_URL": DSN, **env},
    )


def test_without_a_database_url_the_prisma_sequence_is_skipped(valid_config: Path) -> None:
    result = _run(_PRISMA_SCRIPT.format(outcome="ok"), valid_config)

    assert result["calls"] == []
    assert result["booted"] is True


def test_the_prisma_setup_runs_with_the_cli_arguments(valid_config: Path) -> None:
    result = _prisma(valid_config, "ok")

    assert ["probe", ["prisma"]] in result["calls"]
    # proxy_cli.py:1349-1352: use_migrate = not --use_prisma_db_push, and the
    # v2 resolver flag as click resolved it.
    assert ["setup", True, False] in result["calls"]
    assert result["booted"] is True


def test_an_unrecoverable_migration_error_exits_2(valid_config: Path) -> None:
    # proxy_cli.py:1353-1362 — RuntimeError out of setup_database is sys.exit(2).
    result = _prisma(valid_config, "raise")

    assert result["exit_code"] == 2
    assert result["booted"] is False


def test_a_failed_setup_exits_1_when_the_check_is_enforced(valid_config: Path) -> None:
    # proxy_cli.py:1364-1370 — ENFORCE_PRISMA_MIGRATION_CHECK, resolved by click.
    result = _prisma(valid_config, "fail", ENFORCE_PRISMA_MIGRATION_CHECK="true")

    assert result["exit_code"] == 1
    assert result["booted"] is False


def test_a_failed_setup_warns_and_continues_by_default(valid_config: Path) -> None:
    # proxy_cli.py:1371-1375 — the same fail-open litellm ships, deliberately
    # replicated: compose's database is optional and the proxy serves without it.
    result = _prisma(valid_config, "fail")

    assert result["exit_code"] is None
    assert result["booted"] is True


def test_disable_prisma_schema_update_checks_the_diff_instead(tmp_path: Path) -> None:
    # proxy_cli.py:1338-1340 — the guard that replaces setup with a diff report.
    path = tmp_path / "config.yaml"
    path.write_text(NO_SCHEMA_UPDATE_CONFIG)

    result = _prisma(path, "ok")

    assert ["diff", None] in result["calls"]
    assert not [call for call in result["calls"] if call[0] == "setup"]
    assert result["booted"] is True


# ── steps 3-5: what a successful import leaves behind ────────────────────────

_IMPORT_SCRIPT = f"""
    import json, os, sys
    import corp_llm_gateway.asgi as asgi
    import corp_llm_gateway.bootstrap as bootstrap
    from litellm.proxy.proxy_server import proxy_startup_event
    from corp_llm_gateway.route_gate import RouteGateMiddleware

    routes = [getattr(r, "path", None) for r in asgi._app.router.routes]
    metrics_hop = asgi._GATEWAY_ROUTES._fallthrough
    print("{SENTINEL}" + json.dumps({{
        "worker_config": os.environ.get("WORKER_CONFIG") is not None,
        "worker_config_names_our_path": json.loads(
            os.environ["WORKER_CONFIG"]
        )["config"] == os.environ["CORP_LLM_LITELLM_CONFIG"],
        "config_file_path": "CONFIG_FILE_PATH" in os.environ,
        "app_is_gate": isinstance(asgi.app, RouteGateMiddleware),
        "gate_wraps_gateway_routes": asgi.app.app is asgi._GATEWAY_ROUTES,
        "gateway_routes_reach_litellm": metrics_hop._fallthrough is asgi._app,
        "lifespan_replaced": asgi._app.router.lifespan_context is not proxy_startup_event,
        "lifespan_not_original": (
            asgi._app.router.lifespan_context is not asgi._litellm_lifespan
        ),
        "guardrail_built": bootstrap._guardrail is not None,
        "armed": asgi.gate.armed,
        "gateway_routes_not_on_litellms_router": not [
            path for path in routes if path in ("/healthz", "/metrics")
        ],
        "shared_exporter": asgi._exporter is __import__(
            "corp_llm_gateway.metrics", fromlist=["x"]
        ).get_exporter(),
    }}))
"""


@pytest.fixture(scope="module")
def imported(valid_config: Path) -> dict:
    return _run(_IMPORT_SCRIPT, valid_config)


def test_the_import_sets_worker_config_and_not_config_file_path(imported: dict) -> None:
    # CONFIG_FILE_PATH makes litellm's lifespan skip initialize(), which is what
    # applies drop_params, the request timeout, telemetry and the log level.
    assert imported["worker_config"] is True
    assert imported["worker_config_names_our_path"] is True
    assert imported["config_file_path"] is False


def test_the_exported_app_is_the_gate_wrapping_litellms_app(imported: dict) -> None:
    assert imported["app_is_gate"] is True
    assert imported["gate_wraps_gateway_routes"] is True
    assert imported["gateway_routes_reach_litellm"] is True


def test_the_lifespan_is_wrapped(imported: dict) -> None:
    assert imported["lifespan_replaced"] is True
    assert imported["lifespan_not_original"] is True


def test_the_import_does_not_build_the_guardrail(imported: dict) -> None:
    # litellm builds it when the lifespan resolves the callback; importing the
    # entrypoint must stay free of network clients and model loads.
    assert imported["guardrail_built"] is False


def test_the_gate_starts_unarmed(imported: dict) -> None:
    assert imported["armed"] is False


def test_the_gateway_owned_routes_are_served_ahead_of_litellm(imported: dict) -> None:
    # Not on litellm's router: its PrometheusAuthMiddleware wraps the router and
    # 401s any path containing /metrics whenever a master key is set, and the
    # kubelet probes and Helm's ServiceMonitor carry no litellm credential.
    # `_REQUEST_SCRIPT` below proves both paths answer through the chain.
    assert imported["gateway_routes_not_on_litellms_router"] is True


def test_the_gate_and_the_guardrail_share_one_exporter(imported: dict) -> None:
    assert imported["shared_exporter"] is True


# ── the boot window's own log handler ────────────────────────────────────────

PLAIN_LOG_CONFIG = VALID_CONFIG.replace("  json_logs: true\n", "")

_BOOT_LOG_SCRIPT = f"""
    import json, logging
    import corp_llm_gateway.asgi  # noqa: F401

    package = logging.getLogger("corp_llm_gateway")
    print("{SENTINEL}" + json.dumps({{
        "package_handlers": len(package.handlers),
        "propagate": package.propagate,
        "root_handlers": len(logging.getLogger().handlers),
    }}))
"""

STEP_1_LINE = "litellm config accepted:"
STEP_3_LINE = "litellm WORKER_CONFIG set from"


def _lines_carrying(stdout: str, needle: str) -> list[str]:
    return [line for line in stdout.splitlines() if needle in line]


@pytest.fixture(scope="module")
def json_boot(valid_config: Path) -> dict:
    return _run(_BOOT_LOG_SCRIPT, valid_config)


def test_a_boot_line_is_written_once_not_twice(json_boot: dict) -> None:
    # The package logger propagates, and `_turn_on_json` puts a handler on the
    # root: leaving the boot handler in place writes every later line twice.
    assert len(_lines_carrying(json_boot["stdout"], STEP_3_LINE)) == 1


@pytest.mark.parametrize("needle", [STEP_1_LINE, STEP_3_LINE])
def test_the_boot_lines_are_json_when_the_config_asks_for_json_logs(
    json_boot: dict, needle: str
) -> None:
    # Vector parses this stdout. The step-1 line predates litellm's handler, so
    # it is the boot handler's own formatter that has to be JSON.
    carrying = _lines_carrying(json_boot["stdout"], needle)

    assert len(carrying) == 1, carrying
    assert json.loads(carrying[0])["message"].startswith(needle)


def test_the_boot_handler_is_handed_back_in_json_mode(json_boot: dict) -> None:
    assert json_boot["package_handlers"] == 0
    assert json_boot["root_handlers"] >= 1
    assert json_boot["propagate"] is True


def test_plain_mode_keeps_one_handler_and_stops_propagating(tmp_path: Path) -> None:
    # litellm installs no root handler without `json_logs`, so the package keeps
    # its own — and stops propagating, so a root handler added later by anything
    # else cannot double the lines.
    path = tmp_path / "config.yaml"
    path.write_text(PLAIN_LOG_CONFIG)

    result = _run(_BOOT_LOG_SCRIPT, path)

    carrying = _lines_carrying(result["stdout"], STEP_3_LINE)
    assert len(carrying) == 1, carrying
    assert carrying[0].startswith("INFO corp_llm_gateway.asgi")
    assert result["package_handlers"] == 1
    assert result["propagate"] is False


# ── step 4: the arming check ─────────────────────────────────────────────────

_LIFESPAN_SCRIPT = f"""
    import asyncio, contextlib, json, os
    import corp_llm_gateway.asgi as asgi

    exits = []
    os._exit = exits.append

    MODE = {{mode!r}}
    if MODE != "real":
        import litellm

        @contextlib.asynccontextmanager
        async def _no_startup(app):
            # litellm's own startup would re-register the callback; this
            # isolates the check itself.
            yield

        asgi._litellm_lifespan = _no_startup
        litellm.callbacks = [object()] if MODE == "stranger" else []

    async def drive():
        async with asgi._app.router.lifespan_context(asgi._app):
            pass

    asyncio.run(drive())
    print("{SENTINEL}" + json.dumps({{{{"exits": exits, "armed": asgi.gate.armed}}}}))
"""


def test_the_gate_arms_when_the_guardrail_registered(valid_config: Path) -> None:
    result = _run(_LIFESPAN_SCRIPT.format(mode="real"), valid_config)

    assert result["exits"] == []
    assert result["armed"] is True


@pytest.mark.parametrize("mode", ["stranger", "empty"])
def test_the_process_exits_70_when_the_guardrail_is_not_registered(
    valid_config: Path, mode: str
) -> None:
    result = _run(_LIFESPAN_SCRIPT.format(mode=mode), valid_config)

    assert result["exits"] == [70]
    assert result["armed"] is False


_CONFIG_WITHOUT_CALLBACK_SCRIPT = f"""
    import asyncio, json, os
    import corp_llm_gateway.asgi as asgi

    exits = []
    os._exit = exits.append

    async def drive():
        async with asgi._app.router.lifespan_context(asgi._app):
            pass

    asyncio.run(drive())
    import litellm
    print("{SENTINEL}" + json.dumps({{
        "exits": exits,
        "armed": asgi.gate.armed,
        "callbacks": [type(cb).__name__ for cb in litellm.callbacks],
    }}))
"""


def test_a_config_listing_a_different_callback_exits_70(tmp_path: Path) -> None:
    # The fail-open this plan exists to close: litellm starts happily with some
    # other callback registered and sanitizes nothing.
    path = tmp_path / "config.yaml"
    path.write_text(STRANGER_CONFIG)
    (tmp_path / "stranger_callback.py").write_text(STRANGER_MODULE)

    result = _run(_CONFIG_WITHOUT_CALLBACK_SCRIPT, path, extra_path=tmp_path)

    assert result["exits"] == [70]
    assert result["armed"] is False
    assert "CorpLlmGuardrail" not in result["callbacks"]


# ── the gateway-owned routes answer through the gate ─────────────────────────

_REQUEST_SCRIPT = f"""
    import asyncio, json
    import corp_llm_gateway.asgi as asgi

    async def call(method, path):
        messages = []

        async def receive():
            return {{"type": "http.request", "body": b"", "more_body": False}}

        async def send(message):
            messages.append(message)

        await asgi.app(
            {{
                "type": "http",
                "method": method,
                "path": path,
                "raw_path": path.encode(),
                "headers": [],
            }},
            receive,
            send,
        )
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return status, body.decode()

    async def main():
        async with asgi._app.router.lifespan_context(asgi._app):
            live = await call("GET", "/healthz/live")
            metrics = await call("GET", "/metrics")
            issue = await call("POST", "/internal/issue-token")
            counts = await call("POST", "/v1/messages/count_tokens")
        print("{SENTINEL}" + json.dumps({{
            "live": live, "metrics": metrics, "issue": issue[0], "count_tokens": counts[0],
        }}))

    asyncio.run(main())
"""


@pytest.fixture(scope="module")
def served(valid_config: Path) -> dict:
    return _run(_REQUEST_SCRIPT, valid_config)


def test_healthz_live_answers_through_the_gate(served: dict) -> None:
    status, body = served["live"]

    assert status == 200
    assert json.loads(body)["status"] == "healthy"


def test_metrics_answers_through_the_gate(served: dict) -> None:
    status, _ = served["metrics"]

    assert status == 200


def test_issue_token_stays_refused(served: dict) -> None:
    # Not in GATEWAY_ROUTE_TABLE on purpose; issuance is `gateway-admin token issue`.
    assert served["issue"] == 404


def test_a_bypass_route_is_refused_on_the_real_app(served: dict) -> None:
    assert served["count_tokens"] == 403


# ── the worker-config argument list cannot drift ─────────────────────────────


def _save_worker_config_keywords() -> list[str]:
    import litellm.proxy.proxy_cli as proxy_cli

    tree = ast.parse(Path(proxy_cli.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "save_worker_config"
        ):
            return [keyword.arg for keyword in node.keywords if keyword.arg]
    pytest.fail("litellm's CLI no longer calls save_worker_config; re-read proxy_cli.py")


def test_the_worker_config_keys_are_the_ones_the_cli_passes() -> None:
    # A key litellm adds would otherwise silently keep initialize()'s own
    # default instead of the CLI's, and the gateway would boot differently from
    # the proxy it replaces.
    from corp_llm_gateway.litellm_cli import WORKER_CONFIG_KEYS

    assert sorted(_save_worker_config_keywords()) == sorted(WORKER_CONFIG_KEYS)


def test_the_cli_defaults_are_read_off_click_including_envvars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from corp_llm_gateway.litellm_cli import WORKER_CONFIG_KEYS, option_values

    values = option_values(WORKER_CONFIG_KEYS)
    # What `litellm --config … --port 4000` would pass today.
    assert values["telemetry"] is True
    assert values["debug"] is False
    assert values["config"] is None

    monkeypatch.setenv("DEBUG", "true")
    assert option_values(("debug",))["debug"] is True


def test_an_option_litellm_renamed_fails_loudly() -> None:
    from corp_llm_gateway.litellm_cli import option_values

    with pytest.raises(KeyError):
        option_values(("an_option_litellm_does_not_have",))

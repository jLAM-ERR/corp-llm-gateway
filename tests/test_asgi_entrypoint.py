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
from collections.abc import Callable, Iterator
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
        "litellm_imported": "litellm" in sys.modules,
    }}))
"""


@pytest.fixture(scope="module")
def missing_config_boot(tmp_path_factory: pytest.TempPathFactory) -> dict:
    return _run(_REFUSAL_SCRIPT, tmp_path_factory.mktemp("absent") / "absent.yaml")


def test_a_missing_config_exits_78_before_litellms_app_is_imported(
    missing_config_boot: dict,
) -> None:
    assert missing_config_boot["exit_code"] == 78
    assert missing_config_boot["proxy_imported"] is False


def test_step_1_refuses_before_litellm_itself_is_imported(missing_config_boot: dict) -> None:
    # Not just the app: importing `litellm` at all fetches the model cost map and
    # installs its logging. Step 3 is where that belongs, so nothing the boot
    # window needs — the JSON formatter included — may pull it in earlier.
    assert missing_config_boot["litellm_imported"] is False


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


# ── step 1, continued: the gateway's own serving config (issuance) ───────────

ISSUER = "https://keycloak.corp.lan/realms/dev"
ISSUANCE_TOML = '[CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP]\n"/devs" = "t1"\n'
UNREACHABLE_PG = "postgresql://gateway:gateway@127.0.0.1:1/gateway"


def _issuance_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    cfg = tmp_path / "gateway.toml"
    cfg.write_text(ISSUANCE_TOML)
    return {
        "CORP_LLM_GATEWAY_CONFIG_FILE": str(cfg),
        "CORP_GATEWAY_ISSUE_OIDC_ISSUER": ISSUER,
        "CORP_GATEWAY_ISSUE_OIDC_AUDIENCE": "corp-gateway-issuance",
        "CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID": "corp-gateway-cli",
        **extra,
    }


def _require_issuance_extras() -> None:
    # A boot that gets past the checks builds the verifier and the Postgres store.
    for module in ("cryptography", "asyncpg"):
        if find_spec(module) is None:
            pytest.skip(f"{module} is not installed; the issuance boot needs it")


def test_issuance_without_postgres_exits_78_before_litellm_is_imported(
    valid_config: Path, tmp_path: Path
) -> None:
    result = _run(_REFUSAL_SCRIPT, valid_config, env=_issuance_env(tmp_path))

    assert result["exit_code"] == 78
    assert result["litellm_imported"] is False
    assert "CORP_LLM_PG_DSN" in result["stdout"]


def test_a_partial_issuance_config_exits_78(valid_config: Path, tmp_path: Path) -> None:
    env = _issuance_env(tmp_path, CORP_LLM_PG_DSN=UNREACHABLE_PG)
    del env["CORP_GATEWAY_ISSUE_OIDC_AUDIENCE"]

    result = _run(_REFUSAL_SCRIPT, valid_config, env=env)

    assert result["exit_code"] == 78
    assert result["litellm_imported"] is False
    assert "CORP_GATEWAY_ISSUE_OIDC_AUDIENCE" in result["stdout"]


def test_a_boot_refusal_never_prints_the_dsn(valid_config: Path, tmp_path: Path) -> None:
    secret_dsn = "postgresql://gateway:dsn-password-7f1e@127.0.0.1:1/gateway"
    env = _issuance_env(tmp_path, CORP_LLM_PG_DSN=secret_dsn)
    del env["CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID"]

    result = _run(_REFUSAL_SCRIPT, valid_config, env=env)

    assert result["exit_code"] == 78
    assert "dsn-password-7f1e" not in result["stdout"]


@pytest.mark.parametrize(
    ("raw", "needle"),
    [("GET /key/list", "refused"), ("GET without-a-slash", "must start with '/'")],
    ids=["names-a-refused-row", "malformed"],
)
def test_a_bad_route_gate_extra_exits_78_before_litellm_is_imported(
    valid_config: Path, raw: str, needle: str
) -> None:
    result = _run(_REFUSAL_SCRIPT, valid_config, env={"CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH": raw})

    assert result["exit_code"] == 78
    assert result["litellm_imported"] is False
    assert "CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH" in result["stdout"]
    assert needle in result["stdout"]


@pytest.mark.parametrize(
    ("env", "needle"),
    [
        ({"CORP_ENV": "production", "CORP_LLM_MAX_INFLIGHT": "0"}, "turns the in-flight cap off"),
        ({"CORP_ENV": "prod", "CORP_LLM_MAX_INFLIGHT": "0"}, "turns the in-flight cap off"),
        ({"CORP_LLM_MAX_INFLIGHT": "-3"}, "from 0 to 10000"),
        ({"CORP_ENV": "production", "CORP_LLM_MAX_INFLIGHT": "-3"}, "from 0 to 10000"),
        ({"CORP_LLM_MAX_INFLIGHT": "lots"}, "is not an integer"),
        ({"CORP_LLM_CANCEL_GRACE_SECONDS": "0"}, "CORP_LLM_CANCEL_GRACE_SECONDS"),
    ],
    ids=["zero-production", "zero-prod", "negative", "negative-prod", "not-int", "grace"],
)
def test_a_bad_capacity_exits_78_before_litellm_is_imported(
    valid_config: Path, env: dict[str, str], needle: str
) -> None:
    result = _run(_REFUSAL_SCRIPT, valid_config, env=env)

    assert result["exit_code"] == 78
    assert result["litellm_imported"] is False
    assert needle in result["stdout"]


def _serve(config_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """`python -m corp_llm_gateway.serve`, exactly as the image runs it."""
    _require_fastapi()
    pytest.importorskip("uvicorn")
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    child_env = {name: os.environ[name] for name in _INHERITED if name in os.environ}
    child_env.update(
        {
            "PYTHONPATH": str(ROOT / "src"),
            "CORP_LLM_LITELLM_CONFIG": str(config_path),
            "CORP_LLM_ORACLE_ENABLED": "0",
            "CORP_LLM_LOCAL_FIRST": "1",
            "CORP_AUDIT_SINK": "stdout",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "CORP_LLM_SERVE_HOST": "127.0.0.1",
            "CORP_LLM_SERVE_PORT": str(port),
            **env,
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "corp_llm_gateway.serve"],
        capture_output=True,
        text=True,
        env=child_env,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize(
    "env",
    [
        {"CORP_ENV": "production", "CORP_LLM_MAX_INFLIGHT": "0"},
        {"CORP_ENV": "prod", "CORP_LLM_MAX_INFLIGHT": "-1"},
    ],
    ids=["zero-production", "negative-prod"],
)
def test_the_served_entrypoint_exits_78_on_a_bad_capacity(
    valid_config: Path, env: dict[str, str]
) -> None:
    completed = _serve(valid_config, env)

    assert completed.returncode == 78, completed.stdout + completed.stderr
    assert "CORP_LLM_MAX_INFLIGHT" in completed.stdout + completed.stderr


_CAPACITY_WIRING_SCRIPT = f"""
    import asyncio, json
    import corp_llm_gateway.asgi as asgi
    from corp_llm_gateway.route_gate import inflight

    async def main():
        loop = asyncio.get_running_loop()
        before = loop.get_task_factory()
        async with asgi._app.router.lifespan_context(asgi._app):
            from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

            hook = asgi.limiter._cancel_hook
            inside = loop.get_task_factory()
            worker = GLOBAL_LOGGING_WORKER._worker_task
            state = {{
                "armed": asgi.gate.armed,
                "gate_limiter": asgi.gate.limiter is asgi.limiter,
                "max": asgi.limiter.max_inflight,
                "grace": asgi.limiter.cancel_grace_s,
                "hook_bound": getattr(hook, "__name__", None) == "on_request_cancelled"
                and type(getattr(hook, "__self__", None)).__name__ == "CorpLlmGuardrail",
                "factory_installed": inside is not None and inside is not before,
                "worker_running": worker is not None and not worker.done(),
                "worker_untagged": worker not in inflight.pending_request_tasks(),
            }}
        state["factory_restored"] = loop.get_task_factory() is before
        print("{SENTINEL}" + json.dumps(state))

    asyncio.run(main())
"""


def test_the_lifespan_wires_the_limiter(valid_config: Path) -> None:
    result = _run(
        _CAPACITY_WIRING_SCRIPT,
        valid_config,
        env={"CORP_LLM_MAX_INFLIGHT": "7", "CORP_LLM_CANCEL_GRACE_SECONDS": "2.5"},
    )

    assert result["armed"] is True
    assert result["gate_limiter"] is True
    assert (result["max"], result["grace"]) == (7, 2.5)
    assert result["hook_bound"] is True
    assert result["factory_installed"] is True
    assert result["factory_restored"] is True
    assert result["worker_running"] is True
    assert result["worker_untagged"] is True
    assert _boot_record(result["stdout"], "in-flight cap")["message"].endswith(
        "7 rewritten requests per pod"
    )


def _boot_record(stdout: str, needle: str) -> dict:
    carrying = _lines_carrying(stdout, needle)
    assert len(carrying) == 1, stdout
    return json.loads(carrying[0])


def test_issuance_boots_when_postgres_is_unreachable(valid_config: Path, tmp_path: Path) -> None:
    # Readiness reports an unreachable database; the boot does not refuse on it.
    from datetime import datetime

    _require_issuance_extras()
    env = _issuance_env(tmp_path, CORP_LLM_PG_DSN=UNREACHABLE_PG)

    result = _run(_REFUSAL_SCRIPT, valid_config, env=env)

    assert result["exit_code"] is None
    assert result["proxy_imported"] is True
    warning = _boot_record(result["stdout"], "issuance schema check skipped")
    assert warning["level"] == "WARNING"
    assert "(ConnectionRefusedError)" in warning["message"]
    assert "127.0.0.1" not in warning["message"]
    assert "gateway:gateway" not in result["stdout"]
    accepted = _boot_record(result["stdout"], STEP_1_LINE)
    elapsed = datetime.fromisoformat(warning["timestamp"]) - datetime.fromisoformat(
        accepted["timestamp"]
    )
    assert elapsed.total_seconds() < 10


_NOT_A_DSN = "not-a-dsn-secret-5b1f"


def test_a_dsn_postgres_cannot_parse_exits_78_and_never_prints_it(
    valid_config: Path, tmp_path: Path
) -> None:
    _require_issuance_extras()
    env = _issuance_env(tmp_path, CORP_LLM_PG_DSN=_NOT_A_DSN)

    result = _run(_REFUSAL_SCRIPT, valid_config, env=env)

    assert result["exit_code"] == 78
    assert result["litellm_imported"] is False
    assert "Postgres refused CORP_LLM_PG_DSN (ClientConfigurationError)" in result["stdout"]
    assert _NOT_A_DSN not in result["stdout"]


def _blocking(module: str, script: str) -> str:
    """``script``, run with ``module`` unimportable (a None entry in sys.modules)."""
    return f"import sys\nsys.modules[{module!r}] = None\n" + textwrap.dedent(script)


@pytest.mark.parametrize(
    ("module", "extra"),
    [
        ("cryptography", "corp-llm-gateway[oidc]"),
        ("jwt", "corp-llm-gateway[oidc]"),
        ("asyncpg", "corp-llm-gateway[postgres]"),
    ],
)
def test_issuance_without_its_extra_exits_78_naming_it(
    valid_config: Path, tmp_path: Path, module: str, extra: str
) -> None:
    _require_issuance_extras()
    env = _issuance_env(tmp_path, CORP_LLM_PG_DSN=UNREACHABLE_PG)

    result = _run(_blocking(module, _REFUSAL_SCRIPT), valid_config, env=env)

    assert result["exit_code"] == 78
    assert result["litellm_imported"] is False
    assert extra in result["stdout"]


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_an_unreadable_ca_bundle_exits_78_without_printing_its_path(
    valid_config: Path, tmp_path: Path, kind: str
) -> None:
    _require_issuance_extras()
    bundle = tmp_path / "ca-bundle-path-3e9a"
    if kind == "directory":
        bundle.mkdir()
    env = _issuance_env(tmp_path, CORP_LLM_PG_DSN=UNREACHABLE_PG, CORP_LLM_CA_BUNDLE=str(bundle))

    result = _run(_REFUSAL_SCRIPT, valid_config, env=env)

    assert result["exit_code"] == 78
    assert "CORP_LLM_CA_BUNDLE is not readable" in result["stdout"]
    assert "ca-bundle-path-3e9a" not in result["stdout"]


def test_a_ca_bundle_that_is_not_pem_exits_78_not_a_traceback(
    valid_config: Path, tmp_path: Path
) -> None:
    _require_issuance_extras()
    bundle = tmp_path / "ca-bundle-path-3e9a.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\nnot base64\n")
    env = _issuance_env(tmp_path, CORP_LLM_PG_DSN=UNREACHABLE_PG, CORP_LLM_CA_BUNDLE=str(bundle))

    result = _run(_REFUSAL_SCRIPT, valid_config, env=env)

    assert result["exit_code"] == 78
    assert "CORP_LLM_CA_BUNDLE does not load as a PEM CA bundle" in result["stdout"]
    assert "ca-bundle-path-3e9a" not in result["stdout"]


def test_a_loadable_ca_bundle_passes_the_boot_check(valid_config: Path, tmp_path: Path) -> None:
    import certifi

    _require_issuance_extras()
    env = _issuance_env(
        tmp_path, CORP_LLM_PG_DSN=UNREACHABLE_PG, CORP_LLM_CA_BUNDLE=certifi.where()
    )

    result = _run(_REFUSAL_SCRIPT, valid_config, env=env)

    assert result["exit_code"] is None
    assert result["proxy_imported"] is True


_OLD_TOKEN_TABLE = """
CREATE TABLE corp_tokens (
    corp_token TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    team_id    TEXT NOT NULL,
    scopes     TEXT[] NOT NULL DEFAULT '{}',
    issued_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
)
"""


@pytest.fixture
def pg_schema() -> Iterator[tuple[str, Callable[[str], None]]]:
    """A throwaway schema on the test Postgres, and a DSN whose search_path is it."""
    import asyncio
    import secrets

    from tests.postgres_support import pg_dsn, require_asyncpg, skip_or_fail

    require_asyncpg()
    import asyncpg

    base = pg_dsn()
    schema = f"boot_{secrets.token_hex(4)}"

    async def execute(sql: str) -> None:
        conn = await asyncpg.connect(base, timeout=5.0)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    try:
        asyncio.run(execute(f"CREATE SCHEMA {schema}"))
    except Exception as exc:
        skip_or_fail(f"Postgres unreachable: {type(exc).__name__}")

    def in_schema(sql: str) -> None:
        asyncio.run(execute(f"SET search_path TO {schema}; {sql}"))

    separator = "&" if "?" in base else "?"
    try:
        yield f"{base}{separator}search_path={schema}", in_schema
    finally:
        asyncio.run(execute(f"DROP SCHEMA {schema} CASCADE"))


def test_a_token_table_without_the_oidc_columns_exits_78(
    valid_config: Path, tmp_path: Path, pg_schema: tuple[str, Callable[[str], None]]
) -> None:
    dsn, in_schema = pg_schema
    in_schema(_OLD_TOKEN_TABLE)

    result = _run(_REFUSAL_SCRIPT, valid_config, env=_issuance_env(tmp_path, CORP_LLM_PG_DSN=dsn))

    assert result["exit_code"] == 78
    assert result["litellm_imported"] is False
    assert "tokens/schema.sql" in result["stdout"]


def test_a_missing_token_table_exits_78(
    valid_config: Path, tmp_path: Path, pg_schema: tuple[str, Callable[[str], None]]
) -> None:
    dsn, _ = pg_schema

    result = _run(_REFUSAL_SCRIPT, valid_config, env=_issuance_env(tmp_path, CORP_LLM_PG_DSN=dsn))

    assert result["exit_code"] == 78
    assert "tokens/schema.sql" in result["stdout"]


def test_a_migrated_token_table_boots(
    valid_config: Path, tmp_path: Path, pg_schema: tuple[str, Callable[[str], None]]
) -> None:
    _require_issuance_extras()
    dsn, in_schema = pg_schema
    in_schema((ROOT / "src/corp_llm_gateway/tokens/schema.sql").read_text())

    result = _run(_REFUSAL_SCRIPT, valid_config, env=_issuance_env(tmp_path, CORP_LLM_PG_DSN=dsn))

    assert result["exit_code"] is None
    assert result["proxy_imported"] is True


_TOKEN_BASE_TABLE = """
CREATE TABLE corp_tokens_base (
    corp_token   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    team_id      TEXT NOT NULL,
    scopes       TEXT[] NOT NULL DEFAULT '{}',
    issued_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at   TIMESTAMPTZ,
    oidc_issuer  TEXT,
    oidc_subject TEXT,
    oidc_jti     TEXT
)
"""


def test_a_token_view_without_the_jti_index_exits_78(
    valid_config: Path, tmp_path: Path, pg_schema: tuple[str, Callable[[str], None]]
) -> None:
    dsn, in_schema = pg_schema
    in_schema(_TOKEN_BASE_TABLE)
    in_schema("CREATE VIEW corp_tokens AS SELECT * FROM corp_tokens_base")

    result = _run(_REFUSAL_SCRIPT, valid_config, env=_issuance_env(tmp_path, CORP_LLM_PG_DSN=dsn))

    assert result["exit_code"] == 78
    assert result["litellm_imported"] is False


def test_a_jti_unique_index_under_another_name_exits_78(
    valid_config: Path, tmp_path: Path, pg_schema: tuple[str, Callable[[str], None]]
) -> None:
    # The store maps a unique violation to E_ISSUE_REPLAY by the index name.
    dsn, in_schema = pg_schema
    schema = (ROOT / "src/corp_llm_gateway/tokens/schema.sql").read_text()
    in_schema(schema.replace("corp_tokens_oidc_jti_key", "corp_tokens_jti_uniq"))

    result = _run(_REFUSAL_SCRIPT, valid_config, env=_issuance_env(tmp_path, CORP_LLM_PG_DSN=dsn))

    assert result["exit_code"] == 78
    assert "tokens/schema.sql" in result["stdout"]


def test_a_postgres_that_refuses_the_password_exits_78_and_never_prints_it(
    valid_config: Path, tmp_path: Path, pg_schema: tuple[str, Callable[[str], None]]
) -> None:
    # A credential fault does not heal on a restart: refusing beats serving a route
    # whose every request would fail.
    from urllib.parse import urlsplit, urlunsplit

    _require_issuance_extras()
    dsn, _ = pg_schema
    parts = urlsplit(dsn)
    netloc = f"{parts.username}:wrong-pass-4c2d9e@{parts.hostname}:{parts.port or 5432}"
    bad_dsn = urlunsplit(parts._replace(netloc=netloc))

    result = _run(
        _REFUSAL_SCRIPT, valid_config, env=_issuance_env(tmp_path, CORP_LLM_PG_DSN=bad_dsn)
    )

    assert result["exit_code"] == 78
    assert result["litellm_imported"] is False
    assert "Postgres refused CORP_LLM_PG_DSN (InvalidPasswordError)" in result["stdout"]
    assert "issuance schema check skipped" not in result["stdout"]
    assert "wrong-pass-4c2d9e" not in result["stdout"]


def test_a_database_that_does_not_exist_exits_78(
    valid_config: Path, tmp_path: Path, pg_schema: tuple[str, Callable[[str], None]]
) -> None:
    from urllib.parse import urlsplit, urlunsplit

    _require_issuance_extras()
    dsn, _ = pg_schema
    bad_dsn = urlunsplit(urlsplit(dsn)._replace(path="/no_such_db_7a2c"))

    result = _run(
        _REFUSAL_SCRIPT, valid_config, env=_issuance_env(tmp_path, CORP_LLM_PG_DSN=bad_dsn)
    )

    assert result["exit_code"] == 78
    assert "Postgres refused CORP_LLM_PG_DSN (InvalidCatalogNameError)" in result["stdout"]
    assert "no_such_db_7a2c" not in result["stdout"]


def test_a_token_table_with_the_columns_but_no_jti_index_exits_78(
    valid_config: Path, tmp_path: Path, pg_schema: tuple[str, Callable[[str], None]]
) -> None:
    dsn, in_schema = pg_schema
    in_schema(_TOKEN_BASE_TABLE.replace("corp_tokens_base", "corp_tokens"))

    result = _run(_REFUSAL_SCRIPT, valid_config, env=_issuance_env(tmp_path, CORP_LLM_PG_DSN=dsn))

    assert result["exit_code"] == 78
    assert result["litellm_imported"] is False
    assert "tokens/schema.sql" in result["stdout"]


# ── issuance served end to end through the real entrypoint ───────────────────

_ISSUE_SCRIPT = f"""
    import asyncio, json, os
    import corp_llm_gateway.asgi as asgi

    async def lifespan_step(queue, sent, message, expect):
        await queue.put({{"type": message}})
        while not [m for m in sent if m["type"].startswith(expect)]:
            await asyncio.sleep(0.01)

    async def main():
        verifier = asgi._GATEWAY_ROUTES._on_close.__self__
        queue, sent = asyncio.Queue(), []

        async def lifespan_send(message):
            sent.append(message)

        # Through the served app, as uvicorn drives it: gate -> HealthRouter -> litellm.
        lifespan = asyncio.create_task(
            asgi.app({{"type": "lifespan", "asgi": {{"version": "3.0"}}}}, queue.get, lifespan_send)
        )
        await lifespan_step(queue, sent, "lifespan.startup", "lifespan.startup.")
        messages = []

        async def receive():
            return {{"type": "http.request", "body": b"", "more_body": False}}

        async def send(message):
            messages.append(message)

        bearer = os.environ["ISSUE_TEST_BEARER"].encode()
        await asgi.app(
            {{
                "type": "http",
                "method": "POST",
                "path": "/internal/issue-token",
                "raw_path": b"/internal/issue-token",
                "headers": [(b"authorization", b"Bearer " + bearer)],
            }},
            receive,
            send,
        )
        await lifespan_step(queue, sent, "lifespan.shutdown", "lifespan.shutdown.")
        await lifespan
        start = next(m for m in messages if m["type"] == "http.response.start")
        body = json.loads(
            b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        )
        print("{SENTINEL}" + json.dumps({{
            "status": start["status"],
            "keys": sorted(body),
            "error": body.get("error"),
            "armed": asgi.gate.armed,
            "lifespan": [m["type"] for m in sent],
            "jwks_client_closed": verifier._jwks._http.is_closed,
        }}))

    asyncio.run(main())
"""


@pytest.fixture
def jwks_server() -> Iterator[tuple[str, object]]:
    """A loopback JWKS endpoint and the RSA key it publishes."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    pytest.importorskip("cryptography")
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    document = json.dumps(
        {
            "keys": [
                {
                    **jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True),
                    "kid": "kid-entrypoint",
                    "use": "sig",
                    "alg": "RS256",
                }
            ]
        }
    ).encode()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(document)))
            self.end_headers()
            self.wfile.write(document)

        def log_message(self, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/realms/dev", key
    finally:
        server.shutdown()
        server.server_close()


def test_issuance_serves_a_200_through_the_real_entrypoint(
    valid_config: Path,
    tmp_path: Path,
    pg_schema: tuple[str, Callable[[str], None]],
    jwks_server: tuple[str, object],
) -> None:
    import asyncio
    import time

    import jwt

    _require_issuance_extras()
    dsn, in_schema = pg_schema
    in_schema((ROOT / "src/corp_llm_gateway/tokens/schema.sql").read_text())
    in_schema((ROOT / "src/corp_llm_gateway/team_config/schema.sql").read_text())
    in_schema("INSERT INTO team_config (team_id, name) VALUES ('t1', 't1')")
    issuer, key = jwks_server
    now = int(time.time())
    bearer = jwt.encode(
        {
            "iss": issuer,
            "aud": "corp-gateway-issuance",
            "azp": "corp-gateway-cli",
            "sub": "sub-entrypoint",
            "jti": "jti-entrypoint",
            "iat": now,
            "exp": now + 300,
            "preferred_username": "alice.entrypoint",
            "groups": ["/devs"],
        },
        key,
        "RS256",
        headers={"kid": "kid-entrypoint", "typ": "JWT"},
    )
    env = _issuance_env(
        tmp_path,
        CORP_LLM_PG_DSN=dsn,
        CORP_GATEWAY_ISSUE_OIDC_ISSUER=issuer,
        CORP_GATEWAY_ISSUE_OIDC_JWKS_URL=f"{issuer}/certs",
        ISSUE_TEST_BEARER=bearer,
    )

    result = _run(_ISSUE_SCRIPT, valid_config, env=env)

    assert (result["status"], result["error"]) == (200, None), result["stdout"]
    assert result["keys"] == ["corp_token", "expires_at"]
    assert result["armed"] is True
    assert result["lifespan"] == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert result["jwks_client_closed"] is True
    assert bearer not in result["stdout"]
    assert _boot_record(result["stdout"], "gateway routes served ahead of litellm")[
        "message"
    ].endswith("POST /internal/issue-token")

    async def issued_rows() -> int:
        import asyncpg

        conn = await asyncpg.connect(dsn, timeout=5.0)
        try:
            return await conn.fetchval(
                "SELECT count(*) FROM corp_tokens WHERE oidc_jti = 'jti-entrypoint'"
            )
        finally:
            await conn.close()

    assert asyncio.run(issued_rows()) == 1


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


def test_the_boot_log_names_issuance_as_disabled_when_it_is(imported: dict) -> None:
    record = _boot_record(imported["stdout"], "gateway routes served ahead of litellm")

    assert record["message"].endswith("/internal/issue-token (issuance disabled, 404)")
    assert "POST /internal/issue-token" not in record["message"]


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


# JSON mode has two sources and they behave differently inside litellm: with the
# env var set, litellm's import-time branch (`_logging.py:581`) has already put a
# JSON formatter on its own handler before `asgi.py` calls `_turn_on_json()` a
# second time; with the YAML flag only, that call is the first. Both have to end
# with one root handler and every boot line written once.
_JSON_BOOT_MODES = {
    "yaml": (VALID_CONFIG, {}),
    "env": (PLAIN_LOG_CONFIG, {"JSON_LOGS": "true"}),
}


@pytest.fixture(scope="module", params=sorted(_JSON_BOOT_MODES))
def json_boot(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> dict:
    document, env = _JSON_BOOT_MODES[request.param]
    path = tmp_path_factory.mktemp(f"json-boot-{request.param}") / "config.yaml"
    path.write_text(document)
    return _run(_BOOT_LOG_SCRIPT, path, env=env)


def test_a_boot_line_is_written_once_not_twice(json_boot: dict) -> None:
    # The package logger propagates, and `_turn_on_json` puts a handler on the
    # root: leaving the boot handler in place writes every later line twice.
    assert len(_lines_carrying(json_boot["stdout"], STEP_3_LINE)) == 1


@pytest.mark.parametrize("needle", [STEP_1_LINE, STEP_3_LINE])
def test_the_boot_lines_are_json_when_the_config_asks_for_json_logs(
    json_boot: dict, needle: str
) -> None:
    # Vector parses this stdout. The step-1 line predates litellm's handler AND
    # litellm's import, so it is the gateway's own boot formatter that has to
    # write it — in litellm's record shape, which is what this pins.
    carrying = _lines_carrying(json_boot["stdout"], needle)

    assert len(carrying) == 1, carrying
    record = json.loads(carrying[0])
    assert record["message"].startswith(needle)
    assert {"message", "level", "timestamp"} <= set(record)
    assert record["level"] == "INFO"


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
            "live": live, "metrics": metrics, "issue": issue, "count_tokens": counts[0],
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


def test_issue_token_is_a_local_404_when_issuance_is_off(served: dict) -> None:
    # The gate admits it (a GATEWAY_ROUTE_TABLE row) and the HealthRouter answers
    # it: disabled issuance never falls through to litellm.
    status, body = served["issue"]

    assert status == 404
    assert json.loads(body) == {"error": "E_ISSUE_DISABLED"}


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

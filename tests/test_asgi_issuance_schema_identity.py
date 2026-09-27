"""The boot's issuance schema check must prove what its refusal names: a UNIQUE
index on corp_tokens(oidc_jti) called corp_tokens_oidc_jti_key. A relation that
merely carries the name is not the replay backstop. Also: an issuance bound the
boot accepts must not crash the boot later."""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from importlib.util import find_spec
from pathlib import Path

import pytest

from tests import test_asgi_entrypoint as entry
from tests.test_asgi_entrypoint import (
    _REFUSAL_SCRIPT,
    ROOT,
    UNREACHABLE_PG,
    _issuance_env,
    _require_issuance_extras,
    _run,
)

pg_schema = entry.pg_schema
valid_config = entry.valid_config

pytestmark = pytest.mark.skipif(
    find_spec("litellm") is None, reason="litellm is not installed in this interpreter"
)

_SCHEMA = (ROOT / "src/corp_llm_gateway/tokens/schema.sql").read_text()
_UNIQUE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS corp_tokens_oidc_jti_key\n    ON corp_tokens (oidc_jti);"
)


def test_the_schema_file_still_declares_the_index_these_tests_rewrite() -> None:
    assert _UNIQUE_INDEX in _SCHEMA


def test_a_non_unique_index_carrying_the_jti_index_name_exits_78(
    valid_config: Path, tmp_path: Path, pg_schema: tuple[str, Callable[[str], None]]
) -> None:
    dsn, in_schema = pg_schema
    in_schema(
        _SCHEMA.replace(
            _UNIQUE_INDEX,
            "CREATE INDEX IF NOT EXISTS corp_tokens_oidc_jti_key ON corp_tokens (oidc_jti);",
        )
    )

    result = _run(_REFUSAL_SCRIPT, valid_config, env=_issuance_env(tmp_path, CORP_LLM_PG_DSN=dsn))

    assert result["exit_code"] == 78
    assert "tokens/schema.sql" in result["stdout"]


def test_the_jti_index_name_on_another_table_exits_78(
    valid_config: Path, tmp_path: Path, pg_schema: tuple[str, Callable[[str], None]]
) -> None:
    dsn, in_schema = pg_schema
    in_schema(
        _SCHEMA.replace(
            _UNIQUE_INDEX,
            "CREATE TABLE decoy (oidc_jti TEXT);\n"
            "CREATE UNIQUE INDEX corp_tokens_oidc_jti_key ON decoy (oidc_jti);",
        )
    )

    result = _run(_REFUSAL_SCRIPT, valid_config, env=_issuance_env(tmp_path, CORP_LLM_PG_DSN=dsn))

    assert result["exit_code"] == 78
    assert "tokens/schema.sql" in result["stdout"]


def test_a_token_ttl_beyond_what_a_date_can_hold_exits_78_not_a_traceback(
    valid_config: Path, tmp_path: Path
) -> None:
    _require_issuance_extras()
    env = _issuance_env(
        tmp_path, CORP_LLM_PG_DSN=UNREACHABLE_PG, CORP_GATEWAY_ISSUE_TOKEN_TTL_DAYS="1000000000"
    )

    result = _run(_REFUSAL_SCRIPT, valid_config, env=env)

    assert result["exit_code"] == 78
    assert "CORP_GATEWAY_ISSUE_TOKEN_TTL_DAYS" in result["stdout"]


def test_an_invalid_jti_index_exits_78_naming_drop_or_reindex(
    valid_config: Path, tmp_path: Path, pg_schema: tuple[str, Callable[[str], None]]
) -> None:
    # CREATE UNIQUE INDEX IF NOT EXISTS skips an existing INVALID index by name, so
    # "re-apply schema.sql" alone would never clear this.
    dsn, in_schema = pg_schema
    in_schema(_SCHEMA)
    in_schema(
        "UPDATE pg_index SET indisvalid = false "
        "WHERE indexrelid = 'corp_tokens_oidc_jti_key'::regclass"
    )

    result = _run(_REFUSAL_SCRIPT, valid_config, env=_issuance_env(tmp_path, CORP_LLM_PG_DSN=dsn))

    assert result["exit_code"] == 78
    assert result["litellm_imported"] is False
    assert "INVALID" in result["stdout"]
    assert "DROP INDEX corp_tokens_oidc_jti_key" in result["stdout"]
    assert "REINDEX INDEX corp_tokens_oidc_jti_key" in result["stdout"]
    assert "tokens/schema.sql" in result["stdout"]


# ── the boot probe's exception matrix ────────────────────────────────────────

_SECRET = "probe-secret-8d41"


def _probe_raising(expr: str) -> str:
    """The refusal script, with the schema probe's connect raising ``expr``."""
    return (
        "import asyncpg, ssl\n"
        "async def _connect(*args, **kwargs):\n"
        f"    raise {expr}\n"
        "asyncpg.connect = _connect\n"
    ) + textwrap.dedent(_REFUSAL_SCRIPT)


@pytest.mark.parametrize(
    "expr",
    [
        f"ssl.SSLError(1, '{_SECRET}')",
        f"ssl.SSLCertVerificationError(1, 'certificate verify failed: {_SECRET}')",
    ],
    ids=["SSLError", "SSLCertVerificationError"],
)
def test_a_tls_failure_to_postgres_exits_78_naming_the_class_only(
    valid_config: Path, tmp_path: Path, expr: str
) -> None:
    # An OSError, but a certificate does not fix itself on a restart.
    _require_issuance_extras()
    env = _issuance_env(tmp_path, CORP_LLM_PG_DSN=UNREACHABLE_PG)

    result = _run(_probe_raising(expr), valid_config, env=env)

    assert result["exit_code"] == 78
    assert result["litellm_imported"] is False
    name = expr.split("(")[0].split(".")[1]
    assert f"TLS to CORP_LLM_PG_DSN failed ({name})" in result["stdout"]
    assert "issuance schema check skipped" not in result["stdout"]
    assert _SECRET not in result["stdout"]
    assert "gateway:gateway" not in result["stdout"]


def test_a_role_without_select_on_the_token_table_exits_78_with_its_own_remedy(
    valid_config: Path, tmp_path: Path
) -> None:
    _require_issuance_extras()
    env = _issuance_env(tmp_path, CORP_LLM_PG_DSN=UNREACHABLE_PG)
    expr = f"asyncpg.exceptions.InsufficientPrivilegeError('permission denied {_SECRET}')"

    result = _run(_probe_raising(expr), valid_config, env=env)

    assert result["exit_code"] == 78
    assert "role lacks SELECT on corp_tokens (InsufficientPrivilegeError)" in result["stdout"]
    assert "check the DSN syntax" not in result["stdout"]
    assert _SECRET not in result["stdout"]


def test_a_cancelled_probe_statement_exits_78(valid_config: Path, tmp_path: Path) -> None:
    # 57014 sits under OperatorInterventionError, whose other members warn and boot.
    _require_issuance_extras()
    env = _issuance_env(tmp_path, CORP_LLM_PG_DSN=UNREACHABLE_PG)
    expr = f"asyncpg.exceptions.QueryCanceledError('canceling statement {_SECRET}')"

    result = _run(_probe_raising(expr), valid_config, env=env)

    assert result["exit_code"] == 78
    assert "Postgres refused CORP_LLM_PG_DSN (QueryCanceledError)" in result["stdout"]
    assert _SECRET not in result["stdout"]


@pytest.mark.parametrize(
    "name", ["ConnectionFailureError", "AdminShutdownError"], ids=["08006", "57P01"]
)
def test_a_server_sent_transient_failure_warns_and_boots(
    valid_config: Path, tmp_path: Path, name: str
) -> None:
    # What pgbouncer / an HA proxy answers during a pause or a failover.
    _require_issuance_extras()
    env = _issuance_env(tmp_path, CORP_LLM_PG_DSN=UNREACHABLE_PG)

    result = _run(_probe_raising(f"asyncpg.exceptions.{name}('{_SECRET}')"), valid_config, env=env)

    assert result["exit_code"] is None
    assert result["proxy_imported"] is True
    assert (
        f"issuance schema check skipped, Postgres not reachable at boot ({name})"
        in (result["stdout"])
    )
    assert _SECRET not in result["stdout"]

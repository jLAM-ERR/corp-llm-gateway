"""The boot's issuance schema check must prove what its refusal names: a UNIQUE
index on corp_tokens(oidc_jti) called corp_tokens_oidc_jti_key. A relation that
merely carries the name is not the replay backstop. Also: an issuance bound the
boot accepts must not crash the boot later."""

from __future__ import annotations

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

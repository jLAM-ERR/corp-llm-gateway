"""The checks on litellm's own proxy config, and how `config check` reports them."""

from __future__ import annotations

from pathlib import Path

import pytest

from corp_llm_gateway import config, litellm_config, settings
from corp_llm_gateway.settings import ConfigError

VALID = """
model_list:
  - model_name: "corp-*"
    litellm_params:
      model: "hosted_vllm/probe"
litellm_settings:
  callbacks: ["corp_llm_gateway.bootstrap.guardrail"]
  json_logs: true
general_settings:
  disable_prisma_schema_update: true
"""


@pytest.fixture
def valid(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(VALID)
    return path


def test_a_valid_config_has_no_problems(valid: Path) -> None:
    assert litellm_config.problems(valid, require_file=True) == []


def test_a_missing_file_is_fatal_only_when_required(tmp_path: Path) -> None:
    absent = tmp_path / "absent.yaml"

    assert litellm_config.problems(absent, require_file=True)
    # `config check` on a laptop: nothing is mounted at /etc/litellm, and the
    # entrypoint is what refuses to serve without it.
    assert litellm_config.problems(absent, require_file=False) == []


def test_a_directory_is_not_a_config(tmp_path: Path) -> None:
    assert "not a file" in litellm_config.problems(tmp_path, require_file=True)[0]


def test_a_non_yaml_suffix_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "config.txt"
    path.write_text(VALID)

    assert "YAML" in litellm_config.problems(path, require_file=True)[0]


@pytest.mark.parametrize("suffix", [".yaml", ".yml", ".YAML"])
def test_both_yaml_spellings_are_accepted(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"config{suffix}"
    path.write_text(VALID)

    assert litellm_config.problems(path, require_file=True) == []


def test_broken_yaml_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("model_list: [\n  - unterminated")

    assert "not valid YAML" in litellm_config.problems(path, require_file=True)[0]


def test_an_empty_config_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("# nothing\n")

    assert "empty" in litellm_config.problems(path, require_file=True)[0]


def test_a_scalar_document_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("just-a-string\n")

    assert "mapping" in litellm_config.problems(path, require_file=True)[0]


def test_a_scalar_general_settings_is_refused(tmp_path: Path) -> None:
    # litellm's own load_config does `general_settings.get("search_tools")`
    # (proxy_server.py:5118) on anything truthy, so a scalar is an
    # AttributeError mid-startup. Exit 78 with a readable line instead.
    path = tmp_path / "config.yaml"
    path.write_text("model_list: []\ngeneral_settings: 7\n")

    found = litellm_config.problems(path, require_file=True)

    assert found and "general_settings must be a mapping, not int" in found[0]


def test_an_empty_general_settings_is_not_a_problem(tmp_path: Path) -> None:
    # `general_settings:` with nothing under it parses as None — litellm's
    # `or {}` handles that, and so does the check.
    path = tmp_path / "config.yaml"
    path.write_text("model_list: []\ngeneral_settings:\n")

    assert litellm_config.problems(path, require_file=True) == []


@pytest.mark.not_root
def test_an_unreadable_file_is_refused(tmp_path: Path) -> None:
    # A config mounted with the wrong mode must exit 78, not raise PermissionError
    # out of the entrypoint's first statement.
    path = tmp_path / "config.yaml"
    path.write_text(VALID)
    path.chmod(0o000)
    try:
        found = litellm_config.problems(path, require_file=True)
    finally:
        path.chmod(0o600)

    assert found and "unreadable (PermissionError)" in found[0]


DATABASE_URL_IN_CONFIG = """
model_list: []
general_settings:
  database_url: "postgresql://u:p@db:5432/litellm"
"""


def test_a_config_only_database_url_is_refused(tmp_path: Path) -> None:
    # litellm's CLI would export it to DATABASE_URL before running the Prisma
    # schema sequence; the entrypoint reads the environment only, so this config
    # would boot a proxy connected to a database whose schema was never set up.
    path = tmp_path / "config.yaml"
    path.write_text(DATABASE_URL_IN_CONFIG)

    found = litellm_config.problems(path, require_file=True)

    assert found and "general_settings.database_url is refused" in found[0]


def test_a_database_url_elsewhere_in_the_config_is_fine(tmp_path: Path) -> None:
    # Only `general_settings.database_url` is the DSN litellm's CLI reads.
    path = tmp_path / "config.yaml"
    path.write_text('model_list: []\nlitellm_settings:\n  database_url: "not-the-dsn"\n')

    assert litellm_config.problems(path, require_file=True) == []


UNDER_GENERAL_SETTINGS = """
model_list: []
general_settings:
  pass_through_endpoints:
    - path: "/adapter"
      target: "https://x.invalid"
"""

AT_TOP_LEVEL = """
model_list: []
pass_through_endpoints:
  - path: "/adapter"
    target: "https://x.invalid"
"""


@pytest.mark.parametrize(
    "document", [UNDER_GENERAL_SETTINGS, AT_TOP_LEVEL], ids=["general_settings", "top-level"]
)
def test_pass_through_endpoints_is_refused(tmp_path: Path, document: str) -> None:
    # litellm's SafeRouteAdder registers those paths at runtime; the route gate
    # is generated from source and cannot know them, so default-deny would 404
    # every one. Say so at boot instead of shipping dead routes.
    path = tmp_path / "config.yaml"
    path.write_text(document)

    found = litellm_config.problems(path, require_file=True)

    assert found and "pass_through_endpoints is refused" in found[0]


def test_json_logs_is_read_from_the_file(
    valid: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The env var wins over the file, so a shell carrying it would make the
    # two `is False` assertions below describe the shell, not the loader.
    monkeypatch.delenv("JSON_LOGS", raising=False)

    assert litellm_config.json_logs(valid) is True

    without = tmp_path / "plain.yaml"
    without.write_text("litellm_settings:\n  callbacks: []\n")
    assert litellm_config.json_logs(without) is False
    assert litellm_config.json_logs(tmp_path / "absent.yaml") is False


@pytest.mark.parametrize(
    "value,expected",
    [("true", True), ("TRUE", True), ("false", False), ("1", False), ("", False)],
)
def test_litellms_own_json_logs_env_var_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    # litellm reads JSON_LOGS at import with this exact rule
    # (`litellm/_logging.py:374`) and goes JSON whatever the YAML says, so the
    # boot handler and uvicorn's log config have to agree with it or one stdout
    # carries two record shapes.
    without = tmp_path / "plain.yaml"
    without.write_text("litellm_settings:\n  callbacks: []\n")
    monkeypatch.setenv("JSON_LOGS", value)

    assert litellm_config.json_logs(without) is expected


def test_general_settings_are_read_the_way_the_cli_reads_them(valid: Path, tmp_path: Path) -> None:
    assert litellm_config.general_settings(valid) == {"disable_prisma_schema_update": True}
    assert litellm_config.general_settings(tmp_path / "absent.yaml") == {}


def test_the_path_resolves_through_the_config_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_LITELLM_CONFIG", " /tmp/somewhere/config.yaml ")
    config.reset_cache()

    assert litellm_config.config_path() == Path("/tmp/somewhere/config.yaml")


def test_the_default_path_is_where_the_image_mounts_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORP_LLM_LITELLM_CONFIG", raising=False)
    config.reset_cache()

    assert litellm_config.DEFAULT_CONFIG_PATH == "/etc/litellm/config.yaml"


# ── how `gateway-admin config check` reports it ──────────────────────────────


def _validate(monkeypatch: pytest.MonkeyPatch, **env: str) -> list[str]:
    monkeypatch.setenv("CORP_LLM_ORACLE_ENABLED", "0")
    monkeypatch.setenv("CORP_LLM_LOCAL_FIRST", "1")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    config.reset_cache()
    try:
        settings.validate()
    except ConfigError as exc:
        return exc.problems
    return []


def test_config_check_refuses_a_pass_through_endpoints_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(AT_TOP_LEVEL)

    problems = _validate(monkeypatch, CORP_LLM_LITELLM_CONFIG=str(path))

    assert any("pass_through_endpoints is refused" in problem for problem in problems)


def test_config_check_refuses_a_config_only_database_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(DATABASE_URL_IN_CONFIG)

    problems = _validate(monkeypatch, CORP_LLM_LITELLM_CONFIG=str(path))

    assert any("general_settings.database_url is refused" in problem for problem in problems)


def test_config_check_passes_on_a_valid_config(
    valid: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _validate(monkeypatch, CORP_LLM_LITELLM_CONFIG=str(valid)) == []


def test_config_check_is_silent_when_nothing_is_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    problems = _validate(monkeypatch, CORP_LLM_LITELLM_CONFIG=str(tmp_path / "absent.yaml"))

    assert problems == []


@pytest.mark.parametrize("port", ["0", "70000", "four-thousand"])
def test_config_check_refuses_a_bad_serve_port(
    valid: Path, monkeypatch: pytest.MonkeyPatch, port: str
) -> None:
    problems = _validate(monkeypatch, CORP_LLM_LITELLM_CONFIG=str(valid), CORP_LLM_SERVE_PORT=port)

    assert any("CORP_LLM_SERVE_PORT" in problem for problem in problems)


def test_config_check_accepts_the_default_serve_port(
    valid: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        _validate(monkeypatch, CORP_LLM_LITELLM_CONFIG=str(valid), CORP_LLM_SERVE_PORT="4000") == []
    )


# ── concurrency: the gateway owns the cap, litellm's own is dead ─────────────

_ROOT = Path(__file__).resolve().parents[1]
_SERVING_LITELLM_CONFIGS = (
    _ROOT / "compose" / "litellm" / "config.yaml",
    _ROOT / "compose" / "litellm" / "config.oauth.yaml",
)
# Everything a deploy renders litellm's config or env from.
_DEPLOY_INPUTS = ("compose", "docker", "examples", "helm")
_DEPLOY_ROOT_FILES = (
    ".env.demo.example",
    "docker-compose.yml",
    "docker-compose.demo.yml",
    "docker-compose.anthropic-oauth.yml",
    "docker-compose.chatgpt-codex.yml",
    "Dockerfile.gateway",
)
# litellm 1.101.0 honours the first only in its legacy limiter, which the second
# selects; neither is a cap in this deployment (docs/ops/capacity.md).
_DEAD_CONCURRENCY_KNOBS = ("global_max_parallel_requests", "LEGACY_MULTI_INSTANCE_RATE_LIMITING")


@pytest.mark.parametrize("path", _SERVING_LITELLM_CONFIGS, ids=lambda p: p.name)
def test_the_shipped_litellm_configs_arm_cancel_on_disconnect(path: Path) -> None:
    import yaml

    document = yaml.safe_load(path.read_text())

    assert document["general_settings"]["cancel_on_disconnect"] is True
    assert litellm_config.problems(path, require_file=True) == []


def _deploy_files() -> list[Path]:
    files = [_ROOT / name for name in _DEPLOY_ROOT_FILES if (_ROOT / name).is_file()]
    for top in _DEPLOY_INPUTS:
        files.extend(
            path
            for path in sorted((_ROOT / top).rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".md"
        )
    return files


def test_no_deploy_input_sets_litellms_dead_concurrency_knobs() -> None:
    files = _deploy_files()
    assert any(path.name == "configmap-litellm.yaml" for path in files)

    offending = [
        f"{path.relative_to(_ROOT)}: {knob}"
        for path in files
        for knob in _DEAD_CONCURRENCY_KNOBS
        if knob in path.read_text(encoding="utf-8", errors="replace")
    ]

    assert not offending, offending


def test_the_compose_gateway_carries_the_in_flight_cap() -> None:
    import yaml

    compose = yaml.safe_load((_ROOT / "compose" / "docker-compose.yml").read_text())
    environment = compose["services"]["litellm"]["environment"]

    assert "CORP_LLM_MAX_INFLIGHT=${CORP_LLM_MAX_INFLIGHT:-64}" in environment
    assert "CORP_LLM_CANCEL_GRACE_SECONDS=${CORP_LLM_CANCEL_GRACE_SECONDS:-5}" in environment
    # The compose default is production, where a cap of 0 refuses to boot.
    assert "CORP_ENV=${CORP_ENV:-production}" in environment

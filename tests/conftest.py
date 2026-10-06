from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Generator, Iterator
from importlib.util import find_spec
from pathlib import Path

import pytest

from corp_llm_gateway import config
from corp_llm_gateway.metrics import reset_exporter
from tests import logger_state, package_state

# Not a test module, so its asserts are rewritten only if registered before anything imports it.
pytest.register_assert_rewrite("tests.hook_fixtures")


def _ner_missing() -> bool:
    try:
        import natasha  # noqa: F401
        import spacy  # noqa: F401
    except ImportError:
        return True
    return False


SKIP_MARKERS: dict[str, tuple[Callable[[], bool], str]] = {
    "requires_litellm": (lambda: find_spec("litellm") is None, "litellm is not installed"),
    "requires_ner": (_ner_missing, "natasha/spacy not available"),
    "requires_helm": (lambda: shutil.which("helm") is None, "helm binary not on PATH"),
    "requires_shellcheck": (lambda: shutil.which("shellcheck") is None, "shellcheck not on PATH"),
    "not_root": (lambda: os.geteuid() == 0, "needs a non-root user"),
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Turn the dependency markers registered in pyproject.toml into setup-time skips, as
    the per-test ``skipif`` they replaced did (plan 20260926 Task 1c): one condition and
    one reason per dependency instead of a copy in every module."""
    skips: dict[str, bool] = {}
    for item in items:
        for name, (condition, reason) in SKIP_MARKERS.items():
            if item.get_closest_marker(name) is None:
                continue
            if name not in skips:
                skips[name] = condition()
            if skips[name]:
                item.add_marker(pytest.mark.skip(reason=reason))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item) -> Generator[None, None, None]:
    """Fail a test that leaves a ``corp_llm_gateway`` package attribute on a module other
    than the one ``sys.modules`` holds (and put it back). It runs after every fixture's
    teardown, ``monkeypatch``'s undo included, so it sees what the next test will."""
    try:
        result = yield
    except BaseException as exc:
        if stale := package_state.stale_package_attributes():
            package_state.restore_package_attributes()
            exc.add_note(f"test also left stale package attributes: {stale}")
        raise
    if stale := package_state.stale_package_attributes():
        package_state.restore_package_attributes()
        pytest.fail(f"test left package attributes on stale modules: {stale}", pytrace=False)
    return result


@pytest.fixture(autouse=True)
def _fresh_metrics_exporter() -> Iterator[None]:
    """`get_exporter()` caches one exporter per process; drop it between tests
    so a `CORP_METRICS_EXPORTER` a test sets is actually re-read."""
    reset_exporter()
    yield
    reset_exporter()


@pytest.fixture(autouse=True)
def _no_logger_state_left_behind() -> Iterator[None]:
    """Fail a test that leaves a level, handler, ``propagate`` or ``disabled`` on a
    ``corp_llm_gateway`` logger (and put it back), so no later ``caplog`` depends on
    the run order."""
    before = logger_state.snapshot()
    yield
    changed = logger_state.changes(before, logger_state.snapshot())
    if changed:
        logger_state.restore(before)
        pytest.fail(f"test left logger state behind on {changed}", pytrace=False)


# Every config key the composition root reads, plus decoy aliases a naive
# implementation might read instead of the canonical names. Cleared before each
# bootstrap test so process env can't leak into backend selection.
MANAGED_ENV: tuple[str, ...] = (
    "CORP_LLM_PG_DSN",
    "REDIS_URL",
    "CORP_LLM_ENDPOINT",
    "CORP_LLM_MODEL",
    "CORP_LLM_RULES_DIR",
    "CORP_LLM_LOCAL_FIRST",
    "CORP_LLM_ORACLE_ENABLED",
    "CORP_LLM_GAZETTEER",
    "CORP_LLM_DLP_CANARIES",
    "CORP_LLM_CA_BUNDLE",
    "SSL_VERIFY",
    "CORP_NER_ENABLED",
    "CORP_NER_ENDPOINT",
    "CORP_NER_TIMEOUT_S",
    "CORP_NER_MAX_TEXTS",
    "CORP_NER_MAX_INPUT_CHARS",
    "CORP_NER_CA_BUNDLE",
    "CORP_LLM_AUTH_PROVIDER",
    "CORP_LLM_BEARER_TOKEN",
    "CORP_LLM_OIDC_ISSUER",
    "CORP_LLM_OIDC_CLIENT_ID",
    "CORP_LLM_OIDC_CLIENT_SECRET",
    "CORP_AUDIT_SINK",
    "CORP_LANGFUSE_URL",
    "CORP_LANGFUSE_PUBLIC_KEY",
    "CORP_LANGFUSE_SECRET_KEY",
    "DEMO_TEAM_TOKEN",
    "CORP_LLM_DEV_TEAM_TOKEN",
    "CORP_LLM_FORWARD_ANTHROPIC_AUTH",
    "CORP_LLM_FORWARD_CHATGPT_AUTH",
    "LITELLM_MASTER_KEY",
    "CORP_ENV",
    "CORP_LLM_STRIP_INBOUND_HEADERS",
    "CORP_LLM_GATEWAY_CONFIG_FILE",
    "CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH",
    "CORP_LLM_LITELLM_CONFIG",
    "CORP_LLM_SERVE_HOST",
    "CORP_LLM_SERVE_PORT",
    # decoy aliases (must be ignored):
    "DATABASE_URL",
    "POSTGRES_DSN",
    "CORP_LLM_DSN",
    "CORP_LLM_REDIS_URL",
    "CORP_LLM_CACHE_URL",
)


@pytest.fixture
def hermetic_gateway_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate bootstrap config resolution from both process env and a real
    ``~/.corp-llm-gateway/config.toml``: clear every managed key, then point the
    loader at an empty TOML so only a test's own explicit values resolve."""
    for name in MANAGED_ENV:
        monkeypatch.delenv(name, raising=False)
    empty = tmp_path / "hermetic-config.toml"
    empty.write_text("")
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(empty))
    config.reset_cache()
    yield
    config.reset_cache()

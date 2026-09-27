"""Doc claims cheap enough to pin: config keys, error codes, route counts, EN/RU parity."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest
import yaml

from corp_llm_gateway import settings
from corp_llm_gateway.metrics import BLOCK_REASONS
from corp_llm_gateway.route_gate import (
    GATEWAY_ROUTE_TABLE,
    LITELLM_REGEX_TABLE,
    LITELLM_ROUTE_TABLE,
    Verdict,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIGURATION = ROOT / "docs/ops/configuration.md"
SECURITY_EN = ROOT / "docs/security.md"
SECURITY_RU = ROOT / "docs/security.ru.md"
CLAUDE_MD = ROOT / "CLAUDE.md"

# Every doc that names config keys or error codes to an operator or contributor.
KEY_DOCS: tuple[Path, ...] = (
    *sorted((ROOT / "docs/ops").glob("*.md")),
    SECURITY_EN,
    SECURITY_RU,
    ROOT / "docs/audit-schema.md",
    ROOT / "docs/audit-schema.ru.md",
    ROOT / "docs/extending.md",
    ROOT / "docs/extending.ru.md",
    ROOT / "compose/README.md",
    ROOT / "compose/README.ru.md",
    CLAUDE_MD,
)
# Where a name the docs mention may legitimately be defined or read.
SOURCE_TREES = ("src", "tests", "compose", "helm", "scripts", ".github")
SOURCE_SUFFIXES = {".py", ".sh", ".yml", ".yaml", ".toml", ".tpl", ".example", ".service"}

# Named by the Redis runbook entry and §6 of security.md, emitted by nothing: the
# docs predate the code path. Listed so every other code stays pinned.
UNEMITTED_DOC_CODES = frozenset({"E_REDIS_DOWN"})

CAPACITY_KEYS = (
    "CORP_LLM_MAX_INFLIGHT",
    "CORP_LLM_CANCEL_GRACE_SECONDS",
    "CORP_LLM_BODY_READ_SECONDS",
    "CORP_LLM_MAX_DRAINING",
    "CORP_LLM_MAX_DRAINING_BYTES",
)


def _source_text() -> str:
    parts: list[str] = []
    for top in SOURCE_TREES:
        for path in sorted((ROOT / top).rglob("*")):
            if (
                path.is_file()
                and "__pycache__" not in path.parts
                and (path.suffix in SOURCE_SUFFIXES or path.name.startswith(".env"))
                and path.resolve() != Path(__file__).resolve()
            ):
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


@pytest.fixture(scope="module")
def source_text() -> str:
    return _source_text()


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    nxt = text.find("\n## ", start + len(heading))
    return text[start : nxt if nxt != -1 else len(text)]


# ── configuration.md is the full key reference ───────────────────────────────


@pytest.mark.parametrize("key", settings.all_keys())
def test_every_settings_key_is_documented_in_the_configuration_reference(key: str) -> None:
    assert f"`{key}`" in CONFIGURATION.read_text(), f"{key} missing from {CONFIGURATION.name}"


@pytest.mark.parametrize("doc", KEY_DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_corp_name_a_doc_mentions_exists_in_the_tree(doc: Path, source_text: str) -> None:
    # A typo in a key name is a silent misconfiguration for whoever copies it.
    names = set(re.findall(r"\b(CORP_[A-Z0-9_]*[A-Z0-9])\b", doc.read_text()))
    known = set(settings.all_keys())
    unknown = sorted(n for n in names - known if not re.search(rf"\b{n}\b", source_text))

    assert not unknown, f"{doc.name} names keys nothing defines or reads: {unknown}"


@pytest.mark.parametrize("key", CAPACITY_KEYS)
def test_each_capacity_key_is_documented_with_its_default(key: str) -> None:
    rows = [
        line for line in CONFIGURATION.read_text().splitlines() if line.startswith(f"| `{key}`")
    ]
    default = settings._BY_NAME[key].default

    assert len(rows) == 1, rows
    if default:
        assert f"`{default}`" in rows[0], rows[0]


@pytest.mark.parametrize("key", CAPACITY_KEYS)
def test_each_capacity_key_reaches_both_deploy_targets(key: str) -> None:
    compose = yaml.safe_load((ROOT / "compose/docker-compose.yml").read_text())
    environment = compose["services"]["litellm"]["environment"]
    values = yaml.safe_load((ROOT / "helm/corp-llm-gateway/values.yaml").read_text())

    assert any(item == key or item.startswith(f"{key}=") for item in environment), key
    assert key in values["config"], key
    assert key in (ROOT / "compose/.env.example").read_text(), key


def test_the_pgbouncer_boot_refusal_is_quoted_verbatim() -> None:
    from corp_llm_gateway import pg_session

    assert pg_session.STARTUP_PARAMETER_REJECTED in CONFIGURATION.read_text()
    assert (
        "ignore_startup_parameters = " + ",".join(pg_session.KEEPALIVE_SERVER_SETTINGS)
        in CONFIGURATION.read_text()
    )


def test_the_keys_outside_the_registry_section_lists_only_unregistered_keys() -> None:
    section = _section(CONFIGURATION.read_text(), "## Keys read outside `settings.py`")
    bullets = re.findall(r"^- `(CORP_[A-Z0-9_]+)`", section, re.MULTILINE)

    assert bullets == ["CORP_LLM_GATEWAY_CONFIG_FILE"]
    assert not set(bullets) & set(settings.all_keys())


# ── error codes and block reasons ────────────────────────────────────────────


def test_every_issuance_code_the_route_can_answer_is_in_the_security_doc(
    source_text: str,
) -> None:
    src = "\n".join(
        path.read_text() for path in sorted((ROOT / "src").rglob("*.py")) if path.is_file()
    )
    codes = set(re.findall(r'"(E_(?:ISSUE|JWKS)_[A-Z_]+)"', src))
    codes.discard("E_ISSUE_REFUSED")  # the fallback for a policy error with no code
    section = _section(SECURITY_EN.read_text(), "### The issuance route")

    assert codes, "no issuance codes found in src"
    assert not sorted(c for c in codes if c not in section)


@pytest.mark.parametrize("doc", KEY_DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_error_code_a_doc_names_exists_in_the_tree(doc: Path, source_text: str) -> None:
    names = set(re.findall(r"\b(E_[A-Z][A-Z0-9_]*[A-Z0-9])\b", doc.read_text()))
    unknown = sorted(
        n for n in names - UNEMITTED_DOC_CODES if not re.search(rf"\b{n}\b", source_text)
    )

    assert not unknown, f"{doc.name} names error codes nothing emits: {unknown}"


def test_the_russian_audit_schema_lists_every_block_reason() -> None:
    text = (ROOT / "docs/audit-schema.ru.md").read_text()
    row = next(line for line in text.splitlines() if line.startswith("| `block_reason`"))
    reasons = [reason for group in BLOCK_REASONS.values() for reason in group]

    assert not [r for r in reasons if f"`{r}`" not in row]


# ── route counts ─────────────────────────────────────────────────────────────


def _counts() -> tuple[Counter[Verdict], int]:
    litellm = Counter(
        [entry.verdict for entry in LITELLM_ROUTE_TABLE.values()]
        + [row.entry.verdict for row in LITELLM_REGEX_TABLE]
    )
    return litellm, len(GATEWAY_ROUTE_TABLE)


@pytest.mark.parametrize("doc", [CLAUDE_MD, SECURITY_EN, SECURITY_RU], ids=lambda p: p.name)
def test_documented_route_counts_match_the_table(doc: Path) -> None:
    litellm, gateway = _counts()
    text = " ".join(doc.read_text().split())

    assert (
        f"{litellm[Verdict.PASSTHROUGH]} PASSTHROUGH / {litellm[Verdict.REFUSE]} REFUSE / "
        f"{litellm[Verdict.REWRITTEN]} REWRITTEN"
    ) in text
    assert re.search(rf"\b{gateway} (gateway|PASSTHROUGH)", text), doc.name


# ── EN / RU parity where the RU file is a mirror ─────────────────────────────


def _subsections(text: str, heading: str) -> int:
    return _section(text, heading).count("\n### ")


def test_the_route_gate_section_has_the_same_subsections_in_both_languages() -> None:
    en = _subsections(SECURITY_EN.read_text(), "## 14.")
    ru = _subsections(SECURITY_RU.read_text(), "## 14.")

    assert en == ru


def test_the_invariant_table_has_the_same_rows_in_both_languages() -> None:
    def ids(path: Path) -> list[str]:
        section = _section(path.read_text(), "## 9.")
        return [
            line.split("|")[1].strip() for line in section.splitlines() if line.startswith("| ")
        ]

    assert ids(SECURITY_EN) == ids(SECURITY_RU)


# The docs call the nginx `crit` level load-bearing for the corp token and name
# the test that pins it; a renamed test would leave them citing nothing.
CRIT_PIN = (
    "tests/compose/test_nginx_profile.py::test_nginxs_limiting_line_is_below_the_error_log_level"
)
EDGE_DOCS = (
    SECURITY_EN,
    SECURITY_RU,
    ROOT / "docs/ops/capacity.md",
    ROOT / "docs/ops/capacity.ru.md",
)


@pytest.mark.parametrize("doc", EDGE_DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_the_edge_docs_cite_the_crit_pin_and_it_exists(doc: Path) -> None:
    path, _, name = CRIT_PIN.partition("::")

    assert CRIT_PIN in doc.read_text()
    assert re.search(rf"^def {name}\(", (ROOT / path).read_text(), re.MULTILINE)

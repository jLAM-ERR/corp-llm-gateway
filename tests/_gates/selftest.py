"""Self-tests of the gates: each mutation weakens a check without touching its asserts, and
the gate must reject it. The working tree is restored after every mutation.

Inventory gate (run in any venv): (a) a delegated helper check removed, (b) a
``pytest.fail`` path removed, (c) the production-detector builder swapped for a static
one, (d) an assert made unreachable by a changed selector, (e) a predicate changed inside
a script a test runs by ``subprocess``.

Outcome ledger (run with the minimal venv, scoped to the mutated files): a module that
stops being collected, a lost parametrize case, a setup-time skip, a changed
collection-skip reason.

``python -m tests._gates.selftest inventory|ledger``
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    old: str
    new: str
    expect: str


INVENTORY_MUTATIONS = (
    Mutation(
        "a: delegated helper check removed",
        "tests/invariants/test_no_originals_leak.py",
        "    assert got == status\n    _assert_gate_surfaces_are_clean(\n"
        "        body=body, log_text=caplog.text, sink=sink, metrics=metrics, reason=reason\n"
        "    )\n    # (v) nothing forwarded",
        "    assert got == status\n    # (v) nothing forwarded",
        "test_no_originals_leak.py::",
    ),
    Mutation(
        "b: pytest.fail path removed",
        "tests/sanitizer/test_streaming_adversarial.py",
        "            try:\n                json.loads(payload)\n"
        "            except json.JSONDecodeError as exc:\n"
        '                pytest.fail(f"data: line is not valid JSON: {payload!r} — {exc}")\n',
        "            json.loads(payload)\n",
        "test_streaming_adversarial.py::test_framing_integrity_every_data_line_is_valid_json",
    ),
    Mutation(
        "c: production-detector builder swapped for a static one",
        "tests/sanitizer/test_oauth_system_preamble.py",
        "local_detectors=[RegexChecksumDetector(), DualNerDetector()],",
        "local_detectors=[RegexChecksumDetector()],",
        "test_oauth_system_preamble.py::",
    ),
    Mutation(
        "d: $ENV_FILE selector swapped for '.env' (every assert survives)",
        "tests/deploy/test_bootstrap_server_script.py",
        'if "$ENV_FILE" not in stripped or stripped.startswith("#"):',
        'if ".env" not in stripped or stripped.startswith("#"):',
        "test_bootstrap_server_script.py::test_env_file_contents_are_never_read_or_printed",
    ),
    Mutation(
        "e: holding_after returns [] inside the served subprocess script",
        "tests/desanitize_served_script.py",
        "        return sorted({(f, n) for f, n, m in self.records[starts[0] + 1 :] "
        "if needle in m})",
        "        return []",
        "test_desanitize_served_stack.py::",
    ),
)


def _apply(mutation: Mutation, run: Callable[[], tuple[int, str]]) -> tuple[bool, str]:
    path = ROOT / mutation.path
    original = path.read_text()
    if original.count(mutation.old) != 1:
        return False, f"mutation site not found exactly once in {mutation.path}"
    try:
        path.write_text(original.replace(mutation.old, mutation.new))
        code, output = run()
    finally:
        path.write_text(original)
    rejected = code != 0 and mutation.expect in output
    first = next((line for line in output.splitlines() if mutation.expect in line), "")
    return rejected, first[:240]


def _inventory_check() -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, "-m", "tests._gates.inventory", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}"},
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


def _ledger_check(scope: str, env_name: str = "minimal") -> Callable[[], tuple[int, str]]:
    def run() -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ledger.json"
            env = {k: v for k, v in os.environ.items() if k not in {"CI", "CORP_TEST_PG_DSN"}}
            env.update(
                {"PYTHONPATH": "src", "CORP_REQUIRE_PROXY_CAPTURE": "1", "CORP_TEST_ENV": env_name}
            )
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    scope,
                    "-q",
                    "-p",
                    "tests._gates.outcome_ledger",
                    f"--outcome-ledger={out}",
                    "-p",
                    "no:cacheprovider",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            if not out.exists():
                return 1, "no ledger written"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests._gates.ledger",
                    "check",
                    env_name,
                    str(out),
                    "--scope",
                    scope,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            return result.returncode, result.stdout + result.stderr

    return run


LEDGER_SETUP_SKIP = (
    "\n\n@pytest.fixture(autouse=True)\ndef _selftest_setup_skip() -> None:\n"
    '    pytest.skip("selftest: setup-time skip")\n'
)


def ledger_mutations() -> list[tuple[Mutation, Callable[[], tuple[int, str]]]]:
    store = "tests/storage/test_mapping_store.py"
    team = "tests/team_config/test_store.py"
    ner = "tests/detectors/test_ner_en.py"
    team_text = (ROOT / team).read_text()
    return [
        (
            Mutation(
                "lost parametrize case",
                store,
                'params=[_make_in_memory, _make_redis], ids=["in_memory", "redis"]',
                'params=[_make_in_memory], ids=["in_memory"]',
                "missing id",
            ),
            _ledger_check("tests/storage/"),
        ),
        (
            Mutation(
                "setup-time skip",
                team,
                team_text,
                team_text + LEDGER_SETUP_SKIP,
                "selftest: setup-time skip",
            ),
            _ledger_check(team),
        ),
        (
            Mutation(
                "changed collection-skip reason",
                ner,
                'pytest.importorskip("spacy")',
                'pytest.importorskip("spacy", reason="selftest: another reason")',
                "selftest: another reason",
            ),
            _ledger_check(ner),
        ),
    ]


def missing_module() -> tuple[bool, str]:
    """The module is renamed so pytest no longer collects it, then put back."""
    source = ROOT / "tests/team_config/test_store.py"
    hidden = source.with_name("selftest_hidden_store.py")
    os.rename(source, hidden)
    try:
        code, output = _ledger_check("tests/team_config/")()
    finally:
        os.rename(hidden, source)
    expect = "missing id (deselected, lost or not collected): tests/team_config/test_store.py::"
    first = next((line for line in output.splitlines() if expect in line), "")
    return code != 0 and bool(first), first[:240]


def main(argv: list[str] | None = None) -> int:
    which = (argv or sys.argv[1:] or ["inventory"])[0]
    results: list[tuple[str, bool, str]] = []
    if which == "inventory":
        for mutation in INVENTORY_MUTATIONS:
            rejected, evidence = _apply(mutation, _inventory_check)
            results.append((mutation.name, rejected, evidence))
    else:
        rejected, evidence = missing_module()
        results.append(("newly missing module", rejected, evidence))
        for mutation, run in ledger_mutations():
            rejected, evidence = _apply(mutation, run)
            results.append((mutation.name, rejected, evidence))
    for name, rejected, evidence in results:
        print(json.dumps({"self-test": name, "rejected": rejected, "evidence": evidence}))
    return 0 if all(rejected for _, rejected, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())

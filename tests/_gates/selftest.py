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

from tests._gates import inventory

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    old: str
    new: str
    # Every one must appear in the gate's output: each names the node id and the column
    # that has to catch the mutation, so a column the gate stops filling fails here.
    expect: tuple[str, ...]


LEAK = "tests/invariants/test_no_originals_leak.py"
LEAK_TEST = f"{LEAK}::test_a_refused_route_leaks_no_original_on_any_of_the_six_surfaces"
FRAMING = (
    "tests/sanitizer/test_streaming_adversarial.py"
    "::test_framing_integrity_every_data_line_is_valid_json"
)
PREAMBLE = "tests/sanitizer/test_oauth_system_preamble.py"
ENV_FILE_TEST = (
    "tests/deploy/test_bootstrap_server_script.py::test_env_file_contents_are_never_read_or_printed"
)
SERVED_SCRIPT = "tests/desanitize_served_script.py"


def _baseline_users(column: str, name: str, path: str) -> tuple[str, ...]:
    """Recorded tests in ``path`` whose ``column`` names ``name``."""
    tests, _ = inventory.read_checks()
    return tuple(
        sorted(i for i, e in tests.items() if i.startswith(path + "::") and name in e[column])
    )


def _served_script_users() -> tuple[str, ...]:
    recorded = json.loads(inventory.EXTERNAL_PATH.read_text())["tests"]
    return tuple(sorted(i for i, e in recorded.items() if SERVED_SCRIPT in e["files"]))


def inventory_mutations() -> list[Mutation]:
    guardrail_users = _baseline_users("helpers", "_guardrail", PREAMBLE)
    return [
        Mutation(
            "a: delegated helper check removed",
            LEAK,
            "    assert got == status\n    _assert_gate_surfaces_are_clean(\n"
            "        body=body, log_text=caplog.text, sink=sink, metrics=metrics, reason=reason\n"
            "    )\n    # (v) nothing forwarded",
            "    assert got == status\n    # (v) nothing forwarded",
            (f"{LEAK_TEST}: delegated ", f"{LEAK_TEST}: helpers "),
        ),
        Mutation(
            "b: pytest.fail path removed",
            "tests/sanitizer/test_streaming_adversarial.py",
            "            try:\n                json.loads(payload)\n"
            "            except json.JSONDecodeError as exc:\n"
            '                pytest.fail(f"data: line is not valid JSON: {payload!r} — {exc}")\n',
            "            json.loads(payload)\n",
            (f"{FRAMING}: fail 1 -> 0",),
        ),
        Mutation(
            "c: production-detector builder swapped for a static one",
            PREAMBLE,
            "local_detectors=[RegexChecksumDetector(), DualNerDetector()],",
            "local_detectors=[RegexChecksumDetector()],",
            tuple(f"{node_id}: body_hash " for node_id in guardrail_users),
        ),
        Mutation(
            "d: $ENV_FILE selector swapped for '.env' (every assert survives)",
            "tests/deploy/test_bootstrap_server_script.py",
            'if "$ENV_FILE" not in stripped or stripped.startswith("#"):',
            'if ".env" not in stripped or stripped.startswith("#"):',
            (f"{ENV_FILE_TEST}: body_hash ",),
        ),
        Mutation(
            "e: holding_after returns [] inside the served subprocess script",
            SERVED_SCRIPT,
            "        return sorted({(f, n) for f, n, m in self.records[starts[0] + 1 :] "
            "if needle in m})",
            "        return []",
            tuple(f"{node_id}: external {SERVED_SCRIPT} " for node_id in _served_script_users()),
        ),
    ]


def _apply(mutation: Mutation, run: Callable[[], tuple[int, str]]) -> tuple[bool, str]:
    path = ROOT / mutation.path
    original = path.read_text()
    if original.count(mutation.old) != 1:
        return False, f"mutation site not found exactly once in {mutation.path}"
    if not mutation.expect:
        return False, "no expectation: the baseline column it reads is empty"
    try:
        path.write_text(original.replace(mutation.old, mutation.new))
        code, output = run()
    finally:
        path.write_text(original)
    lines = output.splitlines()
    unmet = [e for e in mutation.expect if not any(e in line for line in lines)]
    if code == 0 or unmet:
        return False, f"exit {code}; not reported: {unmet[:3]}"[:240]
    first = next(line for line in lines if mutation.expect[0] in line)
    return True, f"{len(mutation.expect)} expected line(s), e.g. {first}"[:240]


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
                ("missing id",),
            ),
            _ledger_check("tests/storage/"),
        ),
        (
            Mutation(
                "setup-time skip",
                team,
                team_text,
                team_text + LEDGER_SETUP_SKIP,
                ("selftest: setup-time skip",),
            ),
            _ledger_check(team),
        ),
        (
            Mutation(
                "changed collection-skip reason",
                ner,
                'pytest.importorskip("spacy")',
                'pytest.importorskip("spacy", reason="selftest: another reason")',
                ("selftest: another reason",),
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
        for mutation in inventory_mutations():
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

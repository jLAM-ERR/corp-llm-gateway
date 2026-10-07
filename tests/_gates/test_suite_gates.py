"""The test-suite gates hold on the tree as it is (docs/testing/must-keep.md).

Static half of the gates; scripts/test-gates.sh runs the dynamic half (each environment's
run against its committed outcome ledger). A deliberate change to a gated test regenerates
the manifests in the same PR, and the manifest diff is what review reads.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from tests._gates import (
    fingerprint,
    inventory,
    ledger,
    moves,
    must_keep,
    name_pinned,
    negative_logs,
    selftest,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = ROOT / "tests" / "_manifests"
ENV_VAR = "CORP_TEST_ENV"


def test_a_reparsed_module_never_reads_a_stale_cache_entry() -> None:
    rejected, evidence = selftest.cache_reparse(rounds=60)

    assert rejected, evidence


def test_a_helper_reached_by_a_function_local_import_is_in_the_closure() -> None:
    rejected, evidence = selftest.local_import()

    assert rejected, evidence


def test_a_nested_scope_import_never_hides_a_module_level_helper() -> None:
    rejected, evidence = selftest.nested_scope_import()

    assert rejected, evidence


def test_a_name_imported_twice_in_one_scope_reaches_both_helpers() -> None:
    rejected, evidence = selftest.rebound_import()

    assert rejected, evidence


def test_an_unaliased_tests_import_is_refused() -> None:
    rejected, evidence = selftest.unaliased_tests_import()

    assert rejected, evidence


def test_a_global_declaration_reaches_the_module_level_helper() -> None:
    rejected, evidence = selftest.global_in_nested_scope()

    assert rejected, evidence


def test_a_pure_move_changes_no_manifest_with_the_map_and_fails_without_it() -> None:
    rejected, evidence = selftest.pure_move()

    assert rejected, evidence


def test_every_malformed_moves_entry_is_refused_by_name() -> None:
    rejected, evidence = selftest.move_refusals()

    assert rejected, evidence


def test_a_move_neither_adds_nor_removes_a_must_keep_id() -> None:
    rejected, evidence = selftest.membership_move()

    assert rejected, evidence


def test_a_test_moved_with_its_security_helper_stays_a_negative_log_id() -> None:
    rejected, evidence = selftest.security_helper_move()

    assert rejected, evidence


def test_a_security_helper_moved_away_from_its_must_keep_test_is_refused() -> None:
    rejected, evidence = selftest.security_helper_moved_away()

    assert rejected, evidence


def test_moved_ids_inside_a_parametrize_suffix_keep_their_baseline_id() -> None:
    rejected, evidence = selftest.moved_parameters()

    assert rejected, evidence


def test_the_moves_map_names_only_tests_that_moved() -> None:
    assert moves.problems() == []


def test_the_name_pinned_index_is_consistent() -> None:
    recorded = json.loads(name_pinned.INDEX_PATH.read_text())

    assert name_pinned.problems(recorded, name_pinned.build()) == []


def test_git_and_the_tree_resolve_the_same_citation_sources() -> None:
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True, timeout=60
    ).stdout
    tracked = {path for path in listed.decode().split("\0") if path}
    from_tree = set(name_pinned.tree_doc_files())
    from_git = set(name_pinned.git_doc_files())

    # A clean checkout has no ignored files, so there the two sets are equal.
    assert from_tree & tracked == from_git
    assert from_git <= from_tree
    assert not any(path.startswith("docs/testing/") for path in from_git)


def test_every_must_keep_id_is_present_with_an_expected_outcome() -> None:
    assert must_keep.read()
    assert must_keep.problems() == []


def test_dropping_a_must_keep_id_needs_a_rule_change(monkeypatch: pytest.MonkeyPatch) -> None:
    committed = must_keep.read()
    dropped = "tests/test_serve.py::test_one_worker_only"
    monkeypatch.setattr(must_keep, "read", lambda: [i for i in committed if i != dropped])

    assert f"the rules select a test missing from must_keep/: {dropped}" in must_keep.problems()


def test_a_clone_without_the_step1_history_fails_on_ci_and_warns_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(must_keep, "_have_step1_history", lambda: False)
    monkeypatch.setenv("CI", "true")

    assert must_keep.NO_HISTORY in must_keep.problems()

    monkeypatch.delenv("CI")
    with pytest.warns(UserWarning, match="not in this clone"):
        found = must_keep.problems()
    assert must_keep.NO_HISTORY not in found


def test_the_must_keep_cli_fails_without_the_step1_history_outside_ci_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(must_keep, "_have_step1_history", lambda: False)
    monkeypatch.delenv("CI", raising=False)

    assert must_keep.main(["--check"]) == 1
    assert must_keep.NO_HISTORY in capsys.readouterr().err


def test_every_must_keep_file_stays_under_the_commit_limit() -> None:
    shards = sorted(must_keep.DIR.glob("*.txt"))

    assert shards
    assert all(path.stat().st_size < 500_000 for path in shards)
    assert {path.stem for path in shards} == {inventory._area(i) for i in must_keep.read()}


def test_the_test_job_checks_out_the_history_the_must_keep_rule_reads() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    checkout = next(
        step
        for step in workflow["jobs"]["test"]["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    )

    assert checkout.get("with", {}).get("fetch-depth") == 0


def test_every_negative_log_check_is_reviewed() -> None:
    recorded = json.loads(negative_logs.PATH.read_text())
    reviewed = {negative_logs._key(site) for site in recorded["sites"]}
    current = {negative_logs._key(site) for site in negative_logs.sites()}

    assert current == reviewed
    assert {site["class"] for site in recorded["sites"]} <= {
        "security",
        "behaviour",
        "not-a-log-check",
    }
    assert recorded["security_node_ids"] == negative_logs.node_ids(recorded)
    assert negative_logs.lost(recorded["security_node_ids"], recorded) == []


def test_the_ledgers_hold_exactly_the_must_keep_ids() -> None:
    committed = set(must_keep.read())
    functions = {ledger.function_id(node_id) for node_id in committed}
    for env in ledger.ENVS:
        recorded = ledger.ids_with_outcome(env)
        files = {node_id for node_id in recorded if "::" not in node_id}
        cases = recorded - files
        under_files = {i for i in committed if i.split("::", 1)[0] in files}
        # A parametrised test is listed by its function id too; the ledger has only its cases.
        listed_only = {ledger.function_id(i) for i in cases if ledger.split_id(i)[2]}

        assert cases <= committed, env
        assert all(any(f.startswith(path + "::") for f in functions) for path in files), env
        assert committed - cases - under_files <= listed_only, env


def test_the_ledger_check_speaks_for_the_must_keep_ids_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept, cases, other = (
        "tests/x/test_a.py::test_kept",
        "tests/x/test_a.py::test_cases",
        "tests/x/test_a.py::test_other",
    )
    recorded = {kept: "passed", f"{cases}[1]": "passed", f"{cases}[2]": "skipped:no redis"}
    (tmp_path / "minimal.json").write_text(ledger._dump("minimal", ledger.nest(recorded)))
    monkeypatch.setattr(ledger, "expected_path", lambda env: tmp_path / f"{env}.json")
    monkeypatch.setattr(ledger, "not_applicable", dict)
    monkeypatch.setattr(moves, "load", lambda: moves.Moves({}, {}))

    def check(outcomes: dict[str, str], skipped: dict[str, str] | None = None) -> list[str]:
        run = {"outcomes": outcomes, "collection_skipped": skipped or {}, "collection_errors": {}}
        return ledger.compare("minimal", run)

    assert check({**recorded, other: "passed"}) == []
    assert check(recorded) == []
    assert check({**recorded, other: "skipped:gone quiet"}) == []
    assert check({**recorded, "tests/y/test_b.py::test_new": "passed"}) == []
    assert check(recorded, {"tests/y/test_b.py": "no module"}) == []
    assert check({**recorded, other: "failed"}) == [f"minimal: {other} failed"]
    assert check({k: v for k, v in recorded.items() if k != kept}) == [
        f"minimal: missing id (deselected, lost or not collected): {kept}"
    ]
    assert check({k: v for k, v in recorded.items() if k != f"{cases}[2]"}) == [
        f"minimal: missing id (deselected, lost or not collected): {cases}[2]"
    ]
    assert check({**recorded, f"{cases}[3]": "passed"}) == [
        f"minimal: new id not in the expected outcomes: {cases}[3] (passed)"
    ]
    assert check({**recorded, kept: "skipped:flaky"}) == [
        f"minimal: {kept}: expected 'passed', got 'skipped:flaky'"
    ]
    assert check({**recorded, f"{cases}[2]": "skipped:no postgres"}) == [
        f"minimal: {cases}[2]: expected 'skipped:no redis', got 'skipped:no postgres'"
    ]
    assert len(check({}, {"tests/x/test_a.py": "no module"})) == 3


def test_a_module_skipped_whole_that_collects_again_names_its_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = "tests/x/test_m.py"
    recorded = {module: "collection-skipped:no extra"}
    (tmp_path / "minimal.json").write_text(ledger._dump("minimal", ledger.nest(recorded)))
    monkeypatch.setattr(ledger, "expected_path", lambda env: tmp_path / f"{env}.json")
    monkeypatch.setattr(ledger, "not_applicable", dict)
    monkeypatch.setattr(moves, "load", lambda: moves.Moves({}, {}))
    run = {
        "outcomes": {f"{module}::test_a": "passed", "tests/y/test_b.py::test_b": "passed"},
        "collection_skipped": {},
        "collection_errors": {},
    }

    assert ledger.compare("minimal", run) == [
        f"minimal: missing id (deselected, lost or not collected): {module}",
        f"minimal: new id not in the expected outcomes: {module}::test_a (passed)",
    ]


def test_ledger_write_keeps_only_the_tests_it_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept, other = "tests/x/test_a.py::test_kept", "tests/x/test_a.py::test_other"
    monkeypatch.setattr(ledger, "expected_path", lambda env: tmp_path / f"{env}.json")
    monkeypatch.setattr(ledger, "not_applicable", dict)
    monkeypatch.setattr(moves, "load", lambda: moves.Moves({}, {}))
    run = {
        "outcomes": {f"{kept}[1]": "passed", f"{kept}[2]": "passed", other: "passed"},
        "collection_skipped": {"tests/z/test_c.py": "no module"},
        "collection_errors": {},
    }

    assert ledger.write(dict.fromkeys(ledger.ENVS, run), keep={kept}) == []

    for env in ledger.ENVS:
        assert ledger.ids_with_outcome(env) == {f"{kept}[1]", f"{kept}[2]"}


def test_moves_checks_values_against_the_collection_before_the_ledgers_narrowed() -> None:
    full = moves.full_ledger_ids()
    if full is None:
        if os.environ.get("CI"):
            pytest.fail(moves.NO_FULL_LEDGERS)
        pytest.skip(moves.NO_FULL_LEDGERS)

    assert len(full) > len(ledger.ids_with_outcome("minimal"))
    assert set(moves.load().ids.values()) <= {ledger.function_id(i) for i in full if "::" in i}


def test_a_clone_without_the_full_ledgers_fails_moves_on_ci_and_warns_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(moves, "full_ledger_ids", lambda: None)
    monkeypatch.setenv("CI", "true")

    assert moves.NO_FULL_LEDGERS in moves.problems()

    monkeypatch.delenv("CI")
    with pytest.warns(UserWarning, match="not in this clone"):
        found = moves.problems()
    assert found == []


def test_the_moves_cli_fails_without_the_full_ledgers_outside_ci_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(moves, "full_ledger_ids", lambda: None)
    monkeypatch.delenv("CI", raising=False)

    assert moves.main(["--check"]) == 1
    assert moves.NO_FULL_LEDGERS in capsys.readouterr().err


@pytest.mark.parametrize(
    "failure", [subprocess.TimeoutExpired(["git"], 60), FileNotFoundError("git")]
)
def test_a_git_that_hangs_or_is_missing_reads_as_no_full_ledgers(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def run(*args: object, **kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(moves.subprocess, "run", run)
    moves.full_ledger_ids.cache_clear()
    try:
        assert moves.full_ledger_ids() is None
    finally:
        moves.full_ledger_ids.cache_clear()


@pytest.mark.parametrize(
    ("gate", "target", "argv", "prefix", "error"),
    [
        (must_keep, "problems", ["--check"], "MUST-KEEP", moves.MoveCollisionError),
        (moves, "problems", ["--check"], "MOVES", moves.MoveCollisionError),
        (negative_logs, "sites", ["--check"], "NEGATIVE-LOGS", inventory.RebindingImportError),
        (name_pinned, "build", ["--check"], "NAME-PINNED", inventory.UnaliasedTestsImportError),
        (ledger, "compare", ["check", "minimal", "RUN"], "OUTCOMES", moves.MoveCollisionError),
    ],
    ids=["must_keep", "moves", "negative_logs", "name_pinned", "ledger"],
)
def test_a_gate_cli_reports_a_refusal_as_one_line_not_a_traceback(
    gate: object,
    target: str,
    argv: list[str],
    prefix: str,
    error: type[Exception],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise error("tests/x/test_a.py:3: the refusal")

    run = tmp_path / "run.json"
    run.write_text("{}")
    monkeypatch.setattr(gate, target, refuse)

    assert gate.main([str(run) if a == "RUN" else a for a in argv]) == 1
    assert capsys.readouterr().err == f"{prefix}: tests/x/test_a.py:3: the refusal\n"


def test_a_module_level_name_imported_twice_from_tests_is_refused() -> None:
    path = ROOT / "tests" / "_gates" / "selftest_twice.py"
    twice = (
        "try:\n    from tests.a import helper\n"
        "except ImportError:\n    from tests.b import helper\n"
    )
    other = "try:\n    import tomllib\nexcept ImportError:\n    import tomli as tomllib\n"

    with pytest.raises(inventory.RebindingImportError, match=r"selftest_twice.py:4: `helper`"):
        inventory._index("tests._gates.selftest_twice", path, twice)
    assert inventory._index("tests._gates.selftest_twice", path, other).imports == {
        "tomllib": ("tomli", None)
    }


def test_a_lost_security_id_names_an_unreviewed_check_of_its_own() -> None:
    recorded = json.loads(negative_logs.PATH.read_text())
    owner = "tests/detectors/test_shadow.py::test_shadow_exception_does_not_break_canonical"
    sites = [
        {**site, "class": "UNREVIEWED"} if site["owner"] == owner else site
        for site in recorded["sites"]
    ]
    lost = f"security negative-log id lost while its test is still must-keep: {owner}"

    assert negative_logs.lost(recorded["security_node_ids"], {**recorded, "sites": sites}) == [
        lost + negative_logs.UNREVIEWED_HINT
    ]
    behaviour = [
        {**site, "class": "behaviour"} if site["owner"] == owner else site for site in sites
    ]
    assert negative_logs.lost(recorded["security_node_ids"], {**recorded, "sites": behaviour}) == [
        lost
    ]


def test_not_applicable_skips_are_reviewed_and_still_recorded() -> None:
    entries = ledger.not_applicable()
    committed = set(must_keep.read())
    for env in ledger.ENVS:
        outcomes = ledger.flat(json.loads(ledger.expected_path(env).read_text()))
        for node_id, entry in entries.items():
            assert entry["note"]
            if env not in entry:
                continue
            if node_id in committed:
                assert outcomes[node_id] == f"{ledger.NOT_APPLICABLE}{entry['note']}"
            else:
                assert node_id not in outcomes


def test_the_full_constraints_install_the_litellm_pyproject_pins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = json.loads(fingerprint.path_for("full").read_text())

    assert fingerprint.constraints_litellm("full") == fingerprint.litellm_pin()
    assert fingerprint.constraints_litellm("minimal") is None
    assert fingerprint.problems("full", recorded, recorded) == []
    monkeypatch.setattr(fingerprint, "constraints_litellm", lambda env: "0.0.1")
    assert fingerprint.problems("full", recorded, recorded) == [
        f"scripts/test-env.full.txt pins litellm==0.0.1, pyproject.toml pins "
        f"{fingerprint.litellm_pin()}"
    ]


def test_a_different_python_patch_version_still_matches_the_fingerprint() -> None:
    recorded = json.loads(fingerprint.path_for("minimal").read_text())

    assert recorded["python_patch"].startswith(recorded["python"] + ".")
    assert fingerprint.problems("minimal", {**recorded, "python_patch": "3.14.99"}, recorded) == []
    assert fingerprint.problems("minimal", {**recorded, "python": "3.15"}, recorded) == [
        "python: recorded '3.14', now '3.15'"
    ]


def test_the_environment_fingerprints_are_the_recipes() -> None:
    for env, present in fingerprint.EXPECTED_PRESENT.items():
        recorded = json.loads(fingerprint.path_for(env).read_text())
        assert set(recorded["markers"].values()) == {present}, env
        assert recorded["python"] == "3.14"
    env = os.environ.get(ENV_VAR)
    if env:
        recorded = json.loads(fingerprint.path_for(env).read_text())
        assert fingerprint.problems(env, fingerprint.current(), recorded) == []

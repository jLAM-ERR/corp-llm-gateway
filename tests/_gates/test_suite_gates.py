"""The refactor gates of plan 20260926 hold on the tree as it is (docs/testing/must-keep.md).

Static half of the gates; scripts/test-gates.sh runs the dynamic half (each environment's
outcome ledger and coverage against the committed baselines). A deliberate change to a
test regenerates the manifests in the same PR, and the manifest diff is what review reads.
"""

from __future__ import annotations

import json
import os
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


def test_the_check_inventory_matches_the_baseline() -> None:
    checks, _ = inventory.build()
    recorded, _ = inventory.read_checks()

    assert inventory.diff_checks(recorded, checks) == []


def test_every_external_dependency_resolves_and_matches_the_baseline() -> None:
    _, external = inventory.build()
    recorded = json.loads(inventory.EXTERNAL_PATH.read_text())

    assert external["unresolved"] == []
    assert inventory.diff_external(recorded, external) == []


def test_each_test_in_a_module_keeps_its_own_external_files() -> None:
    source = (
        "from pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parents[2]\n"
        "def test_reads_pyproject():\n"
        "    assert (ROOT / 'pyproject.toml').read_text()\n"
        "def test_reads_readme():\n"
        "    assert (ROOT / 'README.md').read_text()\n"
    )
    path = ROOT / "tests" / "_gates" / "two_external_readers.py"
    module = inventory._index("tests._gates.two_external_readers", path, source)

    _, external = inventory.collect(
        (module, qual, node, cls) for qual, node, cls in inventory._tests_in(module)
    )

    files = {node_id.split("::")[1]: entry["files"] for node_id, entry in external["tests"].items()}
    assert files == {
        "test_reads_pyproject": {"pyproject.toml": inventory.file_hash("pyproject.toml")},
        "test_reads_readme": {"README.md": inventory.file_hash("README.md")},
    }


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


def test_a_moved_test_that_lost_an_assert_is_rejected_despite_the_map() -> None:
    rejected, evidence = selftest.weakened_move()

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


def test_the_moves_map_names_only_tests_that_moved() -> None:
    assert moves.problems() == []


def test_a_rename_leaves_a_name_the_function_binds_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    source = (
        "def helper():\n    return 1\n"
        "def test_uses_both():\n"
        "    assert helper() == 1\n"
        "    def inner(helper):\n        return helper\n"
        "    assert [helper for helper in (2,)] == [2]\n"
    )
    path = ROOT / "tests" / "_gates" / "renamed_helper.py"
    module = inventory._index("tests._gates.renamed_helper", path, source)
    monkeypatch.setattr(inventory, "renames", lambda: {"helper": "old_helper"})

    dump = inventory._normalise(module, module.defs["test_uses_both"], False)

    # The module-level call is renamed; the argument and the comprehension variable are not.
    assert dump.count("Name('old_helper'") == 1
    assert dump.count("Name('helper'") == 3


def _override_files(sites: dict[str, object]) -> set[str]:
    """The current module of each override site (sites are keyed by baseline id)."""
    return {moves.from_baseline(site.split("#", 1)[0]).split("::", 1)[0] for site in sites}


def test_an_override_site_of_a_test_moved_by_ids_resolves_to_its_module_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = "tests/_gates/test_suite_gates.py::test_the_check_inventory_matches_the_baseline"
    monkeypatch.setattr(
        moves, "load", lambda: moves.Moves({}, {now: "tests/old_place.py::test_launches"})
    )

    files = _override_files({"tests/old_place.py::test_launches#process0": {}})

    assert files == {"tests/_gates/test_suite_gates.py"}


def test_every_override_names_a_live_site_and_says_why() -> None:
    sites = json.loads(inventory.OVERRIDES_PATH.read_text())["sites"]
    files = _override_files(sites)

    assert all(entry["note"] for entry in sites.values())
    assert all((ROOT / path).is_file() for path in files)


def test_the_name_pinned_index_is_consistent() -> None:
    recorded = json.loads(name_pinned.INDEX_PATH.read_text())

    assert name_pinned.problems(recorded, name_pinned.build()) == []


def test_git_and_the_tree_resolve_the_same_citation_sources() -> None:
    tracked = set(inventory.tracked_files())
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


def test_the_ledgers_cover_every_test_and_count_its_cases() -> None:
    checks, _ = inventory.build()
    _, cases = inventory.read_checks()
    counted: dict[str, dict[str, int]] = {}
    covered_files: set[str] = set()
    for env in ledger.ENVS:
        for node_id in ledger.ids_with_outcome(env):
            if "::" not in node_id:
                covered_files.add(node_id)
                continue
            path, test, _ = ledger.split_id(node_id)
            function = ledger.join_id(path, test, "")
            counted.setdefault(function, {}).setdefault(env, 0)
            counted[function][env] += 1
    uncovered = sorted(
        node_id
        for node_id in checks
        if node_id not in counted and node_id.split("::", 1)[0] not in covered_files
    )

    assert uncovered == []
    assert counted == cases


def test_not_applicable_skips_are_reviewed_and_still_recorded() -> None:
    entries = ledger.not_applicable()
    for env in ledger.ENVS:
        outcomes = ledger.flat(json.loads(ledger.expected_path(env).read_text()))
        for node_id, entry in entries.items():
            assert entry["note"]
            if env in entry:
                assert outcomes[node_id] == f"{ledger.NOT_APPLICABLE}{entry['note']}"


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


def test_the_environment_fingerprints_are_the_recipes() -> None:
    for env, present in fingerprint.EXPECTED_PRESENT.items():
        recorded = json.loads(fingerprint.path_for(env).read_text())
        assert set(recorded["markers"].values()) == {present}, env
        assert recorded["python"] == "3.14"
    env = os.environ.get(ENV_VAR)
    if env:
        recorded = json.loads(fingerprint.path_for(env).read_text())
        assert fingerprint.problems(env, fingerprint.current(), recorded) == []

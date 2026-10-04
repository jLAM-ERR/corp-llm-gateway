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

from tests._gates import fingerprint, inventory, ledger, must_keep, name_pinned, negative_logs

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


def test_every_override_names_a_live_site_and_says_why() -> None:
    sites = json.loads(inventory.OVERRIDES_PATH.read_text())["sites"]
    files = {site.split("::", 1)[0] for site in sites}

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

    assert f"the rules select a test missing from must_keep.txt: {dropped}" in must_keep.problems()


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


def test_the_environment_fingerprints_are_the_recipes() -> None:
    for env, present in fingerprint.EXPECTED_PRESENT.items():
        recorded = json.loads(fingerprint.path_for(env).read_text())
        assert set(recorded["markers"].values()) == {present}, env
        assert recorded["python"] == "3.14"
    env = os.environ.get(ENV_VAR)
    if env:
        recorded = json.loads(fingerprint.path_for(env).read_text())
        assert fingerprint.problems(env, fingerprint.current(), recorded) == []

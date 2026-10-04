"""Must-keep manifest: node ids no prune PR may delete, re-split or reduce.

Built from the rules below (documented in docs/testing/must-keep.md) and expanded to
parametrised ids from both expected-outcome ledgers, so a lost parameter case shows up.

``python -m tests._gates.must_keep --write|--check``
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import subprocess
import sys
import warnings
from functools import cache
from pathlib import Path

from tests._gates import ledger
from tests._gates.inventory import Module, _area, _tests_in, modules

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = ROOT / "tests" / "_manifests"
# One file per test directory (per module for tests/*.py), as baseline_checks/: together
# they are near the 500 KB commit limit.
DIR = MANIFESTS / "must_keep"
HEADER = (
    "# Must-keep node ids (plan 20260926 Task 0, baseline 807831a). Rules and per-file\n"
    "# notes: docs/testing/must-keep.md. Regenerate only on a re-baseline.\n"
)

# Step 1: `git diff --name-status e9e877f..807831a -- tests/` (PR #16-#18), frozen.
STEP1_RANGE = ("e9e877f", "807831a")
STEP1_ADDED = (
    "tests/audit/test_guardrail_information_gate.py",
    "tests/compose/test_issuance_overlay.py",
    "tests/compose/test_nginx_allowlist_routes.py",
    "tests/compose/test_nginx_profile.py",
    "tests/compose/test_nginx_runtime.py",
    "tests/deploy/test_install_device_flow.py",
    "tests/deploy/test_make_selfsigned_certs.py",
    "tests/docs/test_docs_pins.py",
    "tests/healthz/test_issuance_schema_gate.py",
    "tests/healthz/test_issue_token_pg_stall.py",
    "tests/healthz/test_issue_token_route.py",
    "tests/healthz/test_issue_token_served.py",
    "tests/healthz/test_ready_probe_postgres.py",
    "tests/integration/test_gateway_image_links.py",
    "tests/integration/test_gateway_image.py",
    "tests/integration/test_nginx_allowlist_image.py",
    "tests/integration/test_postgres_readiness.py",
    "tests/invariants/test_issuance_error_codes.py",
    "tests/invariants/test_issuance_no_leak.py",
    "tests/litellm_hook/test_acceptance_matrix.py",
    "tests/litellm_hook/test_asgi_desanitize_prototype.py",
    "tests/litellm_hook/test_cancel_call_id_isolation.py",
    "tests/litellm_hook/test_fail_open_probes.py",
    "tests/litellm_hook/test_guardrail_information.py",
    "tests/litellm_hook/test_guardrails_only_dispatch.py",
    "tests/litellm_hook/test_hazard17_logging_snapshot.py",
    "tests/litellm_hook/test_hazard17b_failure_hook_request_data.py",
    "tests/litellm_hook/test_hazard18_corp_token_snapshot.py",
    "tests/litellm_hook/test_hazard19_bearer_hashing.py",
    "tests/litellm_hook/test_logging_surfaces.py",
    "tests/litellm_hook/test_on_request_cancelled_edges.py",
    "tests/litellm_hook/test_on_request_cancelled.py",
    "tests/litellm_hook/test_proxy_dispatch.py",
    "tests/litellm_hook/test_walker_oracle.py",
    "tests/route_gate/test_arm_checks.py",
    "tests/route_gate/test_body_policies.py",
    "tests/route_gate/test_call_id_header.py",
    "tests/route_gate/test_desanitize_usage.py",
    "tests/route_gate/test_extras_and_codes_edges.py",
    "tests/route_gate/test_inflight_boundaries.py",
    "tests/route_gate/test_inflight_faults.py",
    "tests/route_gate/test_inflight.py",
    "tests/route_gate/test_terminal_audit.py",
    "tests/sanitizer/test_streaming_chat_sse.py",
    "tests/sanitizer/test_streaming_responses_terminal.py",
    "tests/sanitizer/test_team_config_outage_edges.py",
    "tests/test_asgi_issuance_schema_identity.py",
    "tests/test_ci_workflow.py",
    "tests/test_desanitize_served_stack.py",
    "tests/test_inflight_served_stack.py",
    "tests/test_issuance_bounds_upper.py",
    "tests/test_issue_token_composition.py",
    "tests/test_litellm_config_guards.py",
    "tests/test_logger_state.py",
    "tests/test_pg_session_edges.py",
    "tests/test_pg_session.py",
    "tests/tokens/test_issuance_policy.py",
    "tests/tokens/test_middleware_single_flight_edges.py",
    "tests/tokens/test_oidc_verifier.py",
)
# Modified (not added) by PR #16-#18 and outside the plan's prunable universe: in full.
STEP1_MODIFIED_IN_FULL = (
    "tests/cli/test_admin.py",
    "tests/compose/test_oauth_overlay.py",
    "tests/healthz/test_server.py",
    "tests/helm/test_chart_render.py",
    "tests/integration/test_route_gate_container.py",
    "tests/invariants/test_no_originals_leak.py",
    "tests/metrics/test_metrics.py",
    "tests/route_gate/test_classify.py",
    "tests/route_gate/test_middleware.py",
    "tests/route_gate/test_table.py",
    "tests/sanitizer/test_profile_orchestrator.py",
    "tests/test_asgi_entrypoint.py",
    "tests/tokens/test_middleware.py",
)
# Modified by PR #16-#18 inside the plan's prunable universe: only the tests the diff
# added or changed (reviewer note per file in docs/testing/must-keep.md).
STEP1_MODIFIED_TOUCHED = (
    "tests/deploy/test_deploy_script.py",
    "tests/team_config/test_postgres_store.py",
    "tests/test_bootstrap.py",
    "tests/test_bootstrap_edges.py",
    "tests/test_litellm_config.py",
    "tests/test_litellm_hook.py",
    "tests/test_litellm_hook_adversarial.py",
    "tests/test_settings.py",
    "tests/tokens/test_postgres_store.py",
    "tests/tokens/test_token_store_contract.py",
)
# tests/test_litellm_hook.py: the plan also names its ticket / terminal-audit section.
HOOK_FILE = "tests/test_litellm_hook.py"
HOOK_SECTION_FROM_LINE = 5975

# Step 2: CLAUDE.md invariants -> the files and ids that assert them.
STEP2_GLOBS = (
    "tests/invariants/*.py",
    "tests/route_gate/*.py",
    "tests/test_desanitize_served_stack.py",
    "tests/test_inflight_served_stack.py",
    "tests/test_launch_command.py",
    "tests/test_litellm_pin.py",
    "tests/test_litellm_config_guards.py",
    "tests/test_ci_workflow.py",
    "tests/test_asgi_entrypoint.py",
    "tests/audit/test_invariants.py",
    "tests/audit/test_block_reason.py",
    "tests/audit/test_guardrail_information_gate.py",
    "tests/sanitizer/test_placeholder_allocator*.py",
    "tests/sanitizer/test_dlp_guard.py",
    "tests/sanitizer/test_oauth_system_preamble.py",
    "tests/litellm_hook/*.py",
    "tests/healthz/test_issue_token_*.py",
    "tests/healthz/test_issuance_schema_gate.py",
    "tests/tokens/test_oidc_verifier.py",
    "tests/tokens/test_issuance_policy.py",
    "tests/tokens/test_middleware*.py",
    "tests/compose/*.py",
    "tests/integration/*.py",
    "tests/docs/test_docs_pins.py",
    "tests/auth/test_rbac.py",
    "tests/test_serve.py",
)
STEP2_LINE_RANGES = (("tests/audit/test_logger.py", 135, 163),)
# Must-keep security checks skipped in both environments: an open finding for the DRI
# (no CI job runs the e2e stack), not a reviewed not-applicable case. Any other must-keep
# id skipped in both fails the gate.
SKIPPED_IN_BOTH_OPEN = (
    "tests/e2e/test_langfuse_pipeline.py::test_no_originals_in_batch_payload",
    "tests/e2e/test_proxy_pipeline.py::test_proxy_forwards_authorization_untouched",
    "tests/e2e/test_proxy_pipeline.py::test_proxy_injects_x_corp_auth",
    "tests/e2e/test_proxy_pipeline.py::test_proxy_401_when_token_missing",
)
STEP2_IDS = (
    "tests/test_litellm_hook.py::test_the_guardrail_defines_no_response_side_hook",
    "tests/test_litellm_hook.py::test_a_ticketed_pre_call_hands_the_mapping_and_the_record_to_the_ticket",
    "tests/sanitizer/test_allowlist.py::test_allowlisted_secret_label_not_dropped",
    "tests/deploy/test_bootstrap_server_script.py::test_env_file_contents_are_never_read_or_printed",
    "tests/deploy/test_deploy_script.py::test_env_file_contents_are_never_read_or_printed",
    *SKIPPED_IN_BOTH_OPEN,
)
# Security-policy defaults that look trivial (plan Context): exempt from pruning.
POLICY_DEFAULTS = (
    "tests/test_config.py::test_corp_llm_verify_defaults_to_true",
    "tests/test_settings.py::test_forward_anthropic_auth_defaults_off",
    "tests/test_settings.py::test_route_gate_extras_default_to_empty",
    "tests/team_config/test_store.py::test_default_fail_policy_matches_matrix",
)


def _spans(module: Module) -> list[tuple[str, int, int]]:
    """[(function-level node id, first line incl. decorators, last line)]."""
    out = []
    for qual, node, _ in _tests_in(module):
        first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        out.append((f"{module.rel}::{qual}", first, node.end_lineno))
    return out


def _functions() -> dict[str, list[tuple[str, int, int]]]:
    return {module.rel: _spans(module) for module in modules().values()}


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False, timeout=60
    )


@cache
def _have_step1_history() -> bool:
    return all(_git("cat-file", "-e", f"{c}^{{commit}}").returncode == 0 for c in STEP1_RANGE)


def _step1_touched(path: str) -> list[tuple[str, int, int]]:
    """The tests of ``path`` as of 807831a that the step-1 diff added or changed (and, in the
    hook file, its ticket section); 807831a's line numbers, so later moves cannot shift them."""
    old, new = STEP1_RANGE
    text = _git("show", f"{new}:{path}")
    diff = _git("diff", "-U0", f"{old}..{new}", "--", path)
    if text.returncode or diff.returncode:
        raise SystemExit(f"git cannot read {path} at {new}: {text.stderr or diff.stderr}")
    touched: set[int] = set()
    for match in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", diff.stdout, re.M):
        start, count = int(match.group(1)), int(match.group(2) or 1)
        touched |= set(range(start, start + max(count, 1)))
    module = Module(path, ROOT / path, ast.parse(text.stdout))
    return [
        (node_id, first, last)
        for node_id, first, last in _spans(module)
        if any(first <= line <= last for line in touched)
        or (path == HOOK_FILE and first >= HOOK_SECTION_FROM_LINE)
    ]


def _committed_in(path: str) -> list[str]:
    return sorted(
        {ledger.join_id(*ledger.split_id(i)[:2], "") for i in read() if i.startswith(path + "::")}
    )


def _rule_ids() -> dict[str, str]:
    """function-level must-keep id -> the rule that put it there (no existence check)."""
    functions = _functions()
    chosen: dict[str, str] = {}

    def add(node_id: str, rule: str) -> None:
        chosen.setdefault(node_id, rule)

    for path in STEP1_ADDED + STEP1_MODIFIED_IN_FULL:
        for node_id, _, _ in functions.get(path, []):
            add(node_id, "step1:file")
    history = _have_step1_history()
    for path in STEP1_MODIFIED_TOUCHED:
        if history:
            for node_id, _, _ in _step1_touched(path):
                add(node_id, "step1:touched")
        else:
            for node_id in _committed_in(path):
                add(node_id, "step1:touched (committed list)")
    for path, ids in functions.items():
        if any(fnmatch.fnmatch(path, pattern) for pattern in STEP2_GLOBS):
            for node_id, _, _ in ids:
                add(node_id, "step2:invariant-file")
    for path, start, end in STEP2_LINE_RANGES:
        for node_id, first, last in functions.get(path, []):
            if first <= end and last >= start:
                add(node_id, "step2:invariant-range")
    for node_id in STEP2_IDS:
        add(node_id, "step2:named")
    for node_id in POLICY_DEFAULTS:
        add(node_id, "policy-default")
    negative = json.loads((MANIFESTS / "negative_log_checks.json").read_text())
    for node_id in negative["security_node_ids"]:
        add(node_id, "negative-log")
    pinned = json.loads((MANIFESTS / "name_pinned.json").read_text())
    for node_id in pinned["ids"]:
        add(node_id, "name-pinned")
    return chosen


def function_ids() -> dict[str, str]:
    """function-level must-keep id -> the rule that put it there."""
    chosen = _rule_ids()
    known = {node_id for ids in _functions().values() for node_id, _, _ in ids}
    missing = sorted(set(chosen) - known)
    if missing:
        raise SystemExit(f"must-keep rule names tests that do not exist: {missing}")
    return chosen


def expand(function_level: dict[str, str]) -> list[str]:
    """Every parametrised id the ledgers record for a must-keep function."""
    expanded: set[str] = set()
    for env in ledger.ENVS:
        for node_id in ledger.ids_with_outcome(env):
            path, test, _ = ledger.split_id(node_id)
            if ledger.join_id(path, test, "") in function_level:
                expanded.add(node_id)
    expanded |= set(function_level)
    return sorted(expanded)


def read() -> list[str]:
    return [
        line
        for path in sorted(DIR.glob("*.txt"))
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def write(ids: list[str]) -> None:
    shards: dict[str, list[str]] = {}
    for node_id in ids:
        shards.setdefault(_area(node_id), []).append(node_id)
    DIR.mkdir(exist_ok=True)
    for stale in DIR.glob("*.txt"):
        if stale.stem not in shards:
            stale.unlink()
    for shard, shard_ids in shards.items():
        (DIR / f"{shard}.txt").write_text(HEADER + "\n".join(sorted(shard_ids)) + "\n")


NO_HISTORY = (
    f"{'..'.join(STEP1_RANGE)} is not in this clone (shallow?): the step-1 touched-test rule "
    "fell back to the ids committed in must_keep/"
)


def _skipped(outcome: str | None) -> bool:
    return outcome is not None and outcome.startswith(("skipped:", ledger.COLLECTION_SKIPPED))


def problems() -> list[str]:
    """Must-keep ids absent from the tree or from either environment's ledger, ids the rules
    select that the committed list lacks, and must-keep ids skipped in both environments."""
    committed = read()
    found = [
        f"the rules select a test missing from must_keep/: {node_id}"
        for node_id in sorted(set(expand(_rule_ids())) - set(committed))
    ]
    if not _have_step1_history():
        if os.environ.get("CI"):
            found.append(NO_HISTORY)
        else:
            warnings.warn(NO_HISTORY, stacklevel=2)
    functions = {node_id for ids in _functions().values() for node_id, _, _ in ids}
    recorded = {env: ledger.ids_with_outcome(env) for env in ledger.ENVS}
    outcomes = {
        env: ledger.flat(json.loads(ledger.expected_path(env).read_text())) for env in ledger.ENVS
    }
    for node_id in committed:
        function = ledger.join_id(*ledger.split_id(node_id)[:2], "")
        if function in SKIPPED_IN_BOTH_OPEN:
            continue
        per_env = [
            outcomes[env].get(node_id, outcomes[env].get(node_id.split("::")[0]))
            for env in ledger.ENVS
        ]
        if all(_skipped(outcome) for outcome in per_env):
            found.append(f"must-keep id is skipped in every environment: {node_id} {per_env}")
    files = {
        env: {node_id.split("::", 1)[0] for node_id in ids if "::" not in node_id}
        for env, ids in recorded.items()
    }
    for node_id in committed:
        path, test, param = ledger.split_id(node_id)
        if ledger.join_id(path, test, "") not in functions:
            found.append(f"must-keep test is gone: {node_id}")
            continue
        for env in ledger.ENVS:
            if param and node_id not in recorded[env] and path not in files[env]:
                found.append(f"must-keep case has no expected outcome in {env}: {node_id}")
            if not param and path not in files[env]:
                has = node_id in recorded[env] or any(
                    i.startswith(node_id + "[") for i in recorded[env]
                )
                if not has:
                    found.append(f"must-keep test has no expected outcome in {env}: {node_id}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.write:
        if not _have_step1_history():
            raise SystemExit(NO_HISTORY + "; refusing to write from it")
        write(expand(function_ids()))
        return 0
    found = problems()
    for line in found:
        print(f"MUST-KEEP: {line}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())

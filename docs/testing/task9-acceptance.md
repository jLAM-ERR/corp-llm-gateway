# Task 9 acceptance check

The acceptance check of plan `docs/plans/20260926-test-suite-refactor-and-prune.md` (rev 19 when measured, rev 20
for the fixes; local only), Task 9. Five commits. Commit 1 (`5b91553`) is this report and changes nothing under
`tests/`. Commit 2 (`cc00051`) adds module docstrings to 8 test files in `tests/litellm_hook/` and
`tests/route_gate/` (criterion 5). Commit 3 (`e0c5680`) refreshes 23 `site` values in
`tests/_manifests/negative_log_checks.json` that those docstrings moved. Commit 4 (`941f78f`) corrects four of the
docstrings after review and refreshes the 18 `site` values they moved again. Commit 5 rewrites `test_classify.py`'s
docstring paragraph after a Codex review on PR #37 (no `site` moves; the file has no negative-log check). Tree:
`release/1.0.x` at `f050208` (PR #36). Baseline: `807831a`, with its manifests committed in Task 0 as `7ae1098` (PR
#19). Every number below was measured on this checkout on 2026-10-07, in `.venv-test-minimal` and `.venv-test-full`
(Python 3.14.8), with the env each recipe sets ([must-keep.md](must-keep.md), "The two environments"). Run outputs
are in `.test-gates/t9-{minimal,full}` (not committed).

| # | criterion | verdict | key numbers |
|---|---|---|---|
| 1 | must-keep guard | met | every gate exits 0 in both envs; acceptance matrix 127 / 127 passed in both; inventory diff vs `7ae1098`: 64 removed, 35 added, 391 changed, all explained |
| 2 | every baseline id mapped 1:1 or ledgered | met, with one note | 5,933 baseline ids per env: 5,346 present, 515 moved, 67 deleted with a row, 4 re-parametrised with a row, 1 Task 0 gate test removed by Task 1a; 0 unaccounted |
| 3 | both envs green, coverage identical | met | minimal 4,695 passed / 419 skipped / 0 failed; full 5,913 / 16 / 0; four areas identical in both envs |
| 4 | wall time not worse | met (per-test reading, rev 20 (1)) | tests present at both commits take the same time (minimal 545.4 s vs 544.7 s). The whole suite is 22-50 s longer (+2-6 %) because of tests added since: `tests/_gates` alone is 18.6-19.6 s |
| 5 | a reader can name each file's behaviour | met (commits 2-5) | 8 of 45 files were flagged at `f050208` (4 with no docstring, 4 whose docstring named a task, hazard or `src/` function instead of a behaviour); commits 2-5 give each a docstring that names the behaviour, with no other change |

## 1. Must-keep guard

Commands (`PYTHONPATH=src:.`, each env's invocation vars):

```
scripts/test-gates.sh minimal --record --out .test-gates/t9-minimal
CORP_TEST_PG_DSN=postgresql://gateway:gateway@localhost:55432/gateway \
  scripts/test-gates.sh full --record --out .test-gates/t9-full
python -m tests._gates.{inventory,must_keep,moves,name_pinned,negative_logs} --check
python -m tests._gates.fingerprint <env> --check
python -m tests._gates.ledger check <env> .test-gates/t9-<env>/ledger.json
python -m tests._gates.coverage_gate check <env> .test-gates/t9-<env>/coverage.json
python -m pytest tests/_gates -q
```

| gate | minimal | full |
|---|---|---|
| record run (pytest exit) | 0 | 0 |
| `inventory --check` (check inventory + external deps) | 0 | 0 |
| `must_keep --check` | 0 | 0 |
| `moves --check` | 0 | 0 |
| `name_pinned --check` | 0 | 0 |
| `negative_logs --check` | 0 | 0 |
| `fingerprint <env> --check` | 0 | 0 |
| `ledger check <env>` on the record run | 0 | 0 |
| `pytest tests/_gates -q` | 33 passed | 33 passed |

No `--check` printed anything. `ledger check` passed on the record run alone: no merge with a scoped re-run was
needed, because nothing changed the manifests since PR #36 regenerated them. No test was red in either run, so
there was nothing to re-run. The known two-clock flake
`tests/test_inflight_served_stack.py::test_idle_bodies_get_408_once_the_deadline_passes[uvloop]` (full only) passed,
as did its `[asyncio]` case.

**Acceptance matrix** (`tests/litellm_hook/test_acceptance_matrix.py`): 127 ids, all `passed` in both envs —
`test_every_listed_test_exists_and_runs` ×105, `test_an_unconditional_skip_disables_a_listed_test` ×8,
`test_a_conditional_skip_does_not` ×4, `test_a_later_rebinding_of_the_base_fails_the_pin` ×4, and one each of
`test_every_hazard_has_a_row`, `test_the_existence_check_catches_a_missing_test`,
`test_no_leaking_set_survives_in_the_tests`, `test_the_base_pin_reads_the_shipped_shape`,
`test_the_guardrail_stays_a_plain_custom_logger`, `test_every_policy_and_guardrail_route_is_refused`.

**Must-keep**: the `must_keep/` diff since `7ae1098` is additions only, 10 lines: the two Task 3b fold tests and
their 7 cases in `litellm_hook.txt` (PR #27), and
`sanitizer/test_orchestrator.py::test_cache_a_disabled_when_policy_fingerprint_cannot_be_computed` (PR #35). The
four e2e security checks are still `SKIPPED_IN_BOTH_OPEN` (`tests/_gates/must_keep.py:167`), as decided in Task 0.

**Expected outcomes**: against `7ae1098`, the two ledgers share 5,861 baseline ids. 15 of them changed, all in
minimal and reason-only: the `tests/detectors/test_detector_contract.py` `[dual_ner|ner_en|ner_ru]` skips,
"natasha/spaCy not installed" → "natasha/spacy not available" (Task 1c, PR #23, recorded there). Full: 0 changed.
The ids that differ are in criterion 2.

**Check inventory vs `7ae1098`.** A script walked `tests/_manifests/baseline_checks/` at every commit from
`7ae1098` to `f050208` and gave each changed entry to the commit that changed it. Entries are keyed by the
baseline function id. 3,578 → 3,549 entries.

| PR (commit) | removed | added | changed, with a `deleted-tests.md` row | changed, no row | why (the PR's record in the plan) |
|---|---|---|---|---|---|
| #20 Task 1a (`533b742`) | 1 | | | | the Task 0 freeze guard `test_task0_changes_no_existing_test`, deleted as Task 0 planned |
| #21 Task 1b (`d9710a5`) | | | 23 | 283 | 26 builder-consolidation rows; 293 tests re-hashed only because `_build_guardrail` gained three keyword args whose defaults are the constructor's. Of those 293, 8 were later deleted (Task 3b rows) and 2 later got PR #35 rows, which leaves 283. Columns: `body_hash` only |
| #23 Task 1c (`c339ed1`) | | | | 22 | skip markers: 24 ids at the PR (`body_hash`, + `constants` for the 18 that reached `needs_helm` / `_ner_available`). One was later deleted and one later got a Task 6a row |
| #24 Task 1d (`892b8f1`) | | 5 | | 48 | the inventory follows function-local `tests.*` imports: 36 non-gate + 12 gate tests, columns `body_hash` / `helpers` / `constants` / `delegated` only; +5 gate self-test mirrors (h-l) |
| #25 Task 1e (`d48e8e9`) | | 9 | | 11 | the gates follow `moves.json`: 16 gate tests re-hashed (these 11 baseline ones + 5 of #24's new ones), and 9 new gate tests (self-tests m-s and the moves checks). Only gate tests change `asserts` / `case_data` (2 of them) |
| #26 Task 2 (`5fda500`) | | | | 3 | `test_every_hazard_has_a_row`, `test_every_listed_test_exists_and_runs` (they read H17-H19) and `test_the_name_pinned_index_is_consistent`, `body_hash` only |
| #27 Task 3b (`2560566`) | 16 | 2 | | | 9 twins pruned + 7 folded into 2 parametrised tests, 16 rows |
| #28 Task 4 (`e3aec88`) | 11 | 2 | | | 2 folds of 11, 11 rows (the 33 moves keep their keys) |
| #29 Task 5a (`f066b10`) | 7 | 2 | | | 7 fail-fast tests folded into 2, 7 rows |
| #30 Task 5b (`c0c4258`) | 5 | | | | 5 config twins, 5 rows |
| #31 Task 6a (`6b8e9bb`) | 4 | | 4 | | 4 bootstrap tests folded into the 4 deploy tests, now parametrised over both scripts; 8 rows |
| #32 Task 6b (`152e57a`) | 14 | | | | 14 store tests, 14 rows |
| #33 Task 7 (`8722122`) | 6 | | | | 6 trivial / twin tests, 6 rows |
| #34 (`b66f8ac`) | | 2 | 4 | | test-order fix, 4 `(changed: …)` rows + 2 new pin tests |
| #35 (`9f9ecf9`) | | | 5 | | structured log asserts, 5 `(changed: …)` rows |
| #36 Task 8 (`f050208`) | | 13 | | | `tests/test_conftest_hooks.py` (guard, slow list, shuffle) |
| **distinct entries** | **64** | **35** | **35** | **356** | |

Some entries were changed by two PRs, so the columns add up to more than the distinct counts. 10 entries were
changed by both #24 and #25, and 1 by both #24 and #26 (283 + 22 + 48 + 11 + 3 − 11 = 356). One entry has rows from
both #21 and #35. Each removed entry but the Task 0 freeze guard has a ledger row (63). No changed entry outside
the gate tests moved `asserts`, `raises`, `fail`, `case_data` or `fixtures` without a row. Every unrowed change
matches a merged PR's record, by count and by column. **Unexplained: none.**

## 2. Every baseline node id mapped 1:1 or ledgered

Baseline ids: both `expected_outcomes.{env}.json` at `7ae1098`, flattened to leaf ids with their parametrize
suffix (`tests._gates.ledger.flat`). Each ledger holds all 5,933 ids (minimal records a collection-skipped
module's ids from full). 20 of them are Task 0's own gate tests in `tests/_gates/test_suite_gates.py`, which
`807831a` does not have. HEAD ids: the union of both record runs' `outcomes`, 5,929 ids. Each HEAD id
went through `moves.claim` (which calls `moves.to_baseline` and refuses two ids landing on one baseline id; none
did).

| category | minimal | full |
|---|---|---|
| present, same id | 5,346 | 5,346 |
| moved (`moves.json`) | 515 | 515 |
| deleted, with a `deleted-tests.md` row | 67 | 67 |
| re-parametrised, with a `(changed: …)` row (Task 6a: `test_deploy_script.py::<name>` is now `<name>[deploy]`) | 4 | 4 |
| Task 0 gate test deleted by Task 1a (`test_task0_changes_no_existing_test`) | 1 | 1 |
| **unaccounted** | **0** | **0** |
| total | 5,933 | 5,933 |

The 67 deletions by PR: Task 3b 16 (3 of them also have Task 1b rows), Task 4 11, Task 5a 11 (7 functions; one
row names the 5 cases of `test_validate_rejects_a_malformed_route_gate_extra`), Task 5b 5, Task 6a 4, Task 6b 14,
Task 7 6. A row matches on the function id, and on the case when it lists `— cases`. `deleted-tests.md` has 102
rows. Every row has a non-empty semantic note, and its survivor column names a `tests/…` id or reads `none — …`
(the one Task 7 trivial row).

Note: the Task 0 freeze guard has no `deleted-tests.md` row. It was never part of the `807831a` suite (no
`tests/_gates/` there). Task 0's record in the plan says Task 1a deletes it, and Task 1a's record says the
ledgers lost only that id. So no baseline-suite id is missing.

New ids since the baseline, 68 (5,933 − 72 + 68 = 5,929):

| PR | new ids |
|---|---|
| #24 Task 1d | 5 gate self-test mirrors in `tests/_gates/test_suite_gates.py` |
| #25 Task 1e | 9 gate tests (moves map, self-tests m-s) |
| #27 Task 3b | `litellm_hook/test_pre_call_shapes.py::test_pre_call_unmanaged_call_type_input_passes_through_untouched` ×4, `litellm_hook/test_stage0_classifier.py::test_stage0_payload_raises_policy_blocked` ×3 (fold tests) |
| #28 Task 4 | `sanitizer/test_streaming.py::test_single_event_passes_through_byte_identical` ×8, `::test_stream_does_not_raise_and_emits_output` ×3 (fold tests) |
| #29 Task 5a | `test_bootstrap_edges.py::test_auth_provider_misconfig_fails_fast_at_build` ×3, `test_settings.py::test_validate_rejects_an_unknown_or_malformed_choice` ×8 (fold tests) |
| #31 Task 6a | `deploy/test_deploy_script.py::{test_bash_syntax_is_valid,test_defines_repo_standard_helpers_writing_to_stderr,test_script_is_executable_bash_with_strict_mode,test_shellcheck_clean}[deploy|bootstrap-server]`, 8 |
| #34 | `extensions/test_registry_boundaries.py::test_the_reimport_check_leaves_the_package_attribute_on_the_live_module`, `test_bootstrap.py::test_a_demo_reimport_test_restores_the_module_and_the_package_attribute` ×3 |
| #36 Task 8 | `tests/test_conftest_hooks.py`, 13 |

## 3. Both environments green; coverage

| env | passed | skipped | failed | collection-skipped modules | pytest time (with coverage) |
|---|---|---|---|---|---|
| minimal | 4,695 | 419 (399 ids + 20 modules) | 0 | 20 (834 ids) | 601.9 s |
| full | 5,913 | 16 | 0 | 0 | 1,258.4 s |

These match the committed expected outcomes at HEAD: minimal 4,695 passed / 389 skipped / 834 collection-skipped
/ 11 not-applicable, full 5,913 / 6 / 0 / 10 (5,929 ids each).

`coverage_gate check` exits 0 in both envs, over all 122 `src/` files. Per area, the run's executed lines and
branch arcs against the baseline manifest `tests/_manifests/coverage.<env>.json`, which has not changed since
`7ae1098`. The script used `coverage_gate.load` and `coverage_gate.from_report`.

| area | files | minimal lines / arcs | minimal | full lines / arcs | full |
|---|---|---|---|---|---|
| `litellm_hook.py` | 1 | 1,064 / 378 | identical | 1,117 / 406 | identical |
| `route_gate/*` | 8 | 1,504 / 419 | identical | 1,536 / 433 | identical |
| `sanitizer/*` | 16 | 1,947 / 761 | identical | 1,949 / 763 | identical |
| `audit/*` | 8 | 284 / 96 | identical | 284 / 96 | identical |

The same comparison against Task 0's own single clean run (`.test-gates/{minimal,full}/coverage.json`, 2026-10-04)
is also identical in all four areas in both envs. Outside the four areas the run covers exactly the baseline,
with one exception: full also took `src/corp_llm_gateway/pg_session.py` arc `266>-265`. That is the
timing-dependent arc removed from the baseline by hand ([must-keep.md](must-keep.md#coverage-baseline)). It is a
gain, not a drop.

## 4. Wall time

The plan's figures (~9 min `.venv`, ~19 min `.venv-bench`) come from the retired venvs. No earlier run is
comparable: Task 0's outputs under `.test-gates/` (`minimal`, `full`, `rec-*`) hold a ledger and a coverage
report, with no timing, and they ran with coverage on. So both commits were measured here, in the same venvs with
the same invocation env. `807831a` ran from a scratch `git worktree` (its own `src/` comes first on
`PYTHONPATH`; the route-gate test image for its build inputs was already cached, as was HEAD's). Command:
`python -m pytest tests/ -q -p no:cacheprovider`, no coverage. One run at a time, in this order: base minimal,
HEAD minimal, base full, HEAD full, then the `-m "not slow"` runs. A second pair, HEAD first, added
`--durations=0` (it costs nothing) to split the time by test.

| env | run | `807831a` pytest s [wall s] | HEAD pytest s [wall s] | delta |
|---|---|---|---|---|
| minimal | 1 | 558.5 [560] — 4,674 passed / 424 skipped | 590.7 [594] — 4,695 / 419 | +32.2 s (+5.8 %) |
| minimal | 2 (`--durations=0`) | 553.2 [555] | 576.4 [580] | +23.2 s (+4.2 %) |
| full | 1 | 1,177.5 [1,196] — 5,897 passed / 16 skipped | 1,199.4 [1,218] — 5,913 / 16 | +21.9 s (+1.9 %) |
| full | 2 (`--durations=0`) | 1,176.5 [1,195] | 1,226.6 [1,246] | +50.2 s (+4.3 %) |

`-m "not slow"` at HEAD: minimal **353.7 s** [357], 4,647 passed / 371 skipped / 96 deselected; full
**672.4 s** [693], 5,781 passed / 16 skipped / 132 deselected. Task 8 measured 344.2 s / 661.1 s.

The time by test, from run 2. These are the sums of setup + call + teardown that pytest shows, so durations under
5 ms are left out. HEAD ids were mapped to baseline ids with `moves.to_baseline`.

| | minimal | full |
|---|---|---|
| tests at both commits, HEAD vs `807831a` | 545.4 s vs 544.7 s (+0.7 s) | 1,175.9 s vs 1,156.0 s (+19.9 s) |
| new: `tests/_gates/` (refactor-time gate tests) | 18.6 s | 19.6 s |
| new: `tests/test_conftest_hooks.py` (Task 8) | 1.6 s | 6.6 s |
| new: other (fold tests, PR #34 pins) | 0.6 s | 2.2 s |
| gone: deleted or folded tests | 0.7 s | 1.2 s |

Verdict: **met (per-test reading, rev 20 (1)).** The tests that exist at both commits run in the same time. In
minimal the gap is +0.7 s on 545 s. In full the common tests are +19.9 s in run 2, spread across subprocess and
docker tests (`tests/deploy` +9.8 s, teardown +11.2 s). Run 1 puts them at about −5 s instead (the +21.9 s total,
less 28.4 s of new tests, plus 1.2 s of deleted ones, taking run 2's per-test times). The two HEAD full runs differ
by 27 s, so this is within run-to-run noise. Two numbers explain why the whole suite is longer than at `807831a`.
First, the gate tests Task 0 to Task 1e added, about 19 s (`test_the_check_inventory_matches_the_baseline` alone is
13.8 s in minimal and 14.2 s in full, run 2; `tests/slow_tests.txt:12`). Task 10 deletes the inventory and its
self-tests. Second, Task 8's guard tests. Read strictly as "whole suite not slower than `807831a`", the criterion
is not met: the suite is +22-50 s (2-6 %).

## 5. Readable layout

For each test module in `tests/litellm_hook/` (28) and `tests/route_gate/` (17), judged only from the file name,
the module docstring and the test names. The helpers `litellm_hook/_dispatch_fixtures.py` and
`route_gate/litellm_routes.py` are not test modules. **Flag** means a reader cannot name the behaviour from those
three alone: no docstring, or a docstring that names a task, hazard or `src/` function rather than a behaviour.
A docstring that starts with a plan / hazard tag and then names the behaviour is not flagged.

| file | tests | docstring, first sentence | behaviour it pins | flag |
|---|---|---|---|---|
| `litellm_hook/test_acceptance_matrix.py` | 10 | Acceptance matrix of the litellm guardrail adoption plan (20260926): each hazard, and each acceptance item, names the tests that fail if it regresses. | every hazard row names tests that exist, run and are not skipped; the guardrail stays a plain `CustomLogger` | |
| `litellm_hook/test_anthropic_upstream_headers.py` | 10 | none | the header allow-list sent to Anthropic, and the OAuth token prefix | **flag** |
| `litellm_hook/test_audit_facts.py` | 14 | The audit facts the hook records: the event, distinct-secret counts, finding labels, and re-entrant audit failures. | the audit record's fields and counts | |
| `litellm_hook/test_bearer_is_hashed_before_logging.py` | 6 | Plan 20260926 hazard 19: litellm hashes the developer's bearer before it logs it. | which bearer shapes litellm logs hashed and which raw | |
| `litellm_hook/test_cancel_call_id_isolation.py` | 13 | A cancel reaches only the request it belongs to. | cancel isolation between requests sharing a call id | |
| `litellm_hook/test_corp_token_never_reaches_logging_surfaces.py` | 5 | Plan 20260926 hazard 18: the corp token never reaches litellm's logging surfaces. | invariant 4 on litellm's log surfaces | |
| `litellm_hook/test_fail_open_probes.py` | 6 | Plan 20260926 Task 0: fail-open probes (hazards 2, 4, 11, 14a, 14b), through litellm's app. | unclear without the plan: "probes" plus hazard numbers; the test names use plan terms (`migrated_pipeline`, `sentinel`, `scan_raw_request`) | **flag** |
| `litellm_hook/test_fail_policy.py` | 22 | The M4 fail-policy matrix and the transport-vs-internal error classification: 503 for an unavailable dependency, an opaque 500 for our own bug. | fail-closed status codes | |
| `litellm_hook/test_failure_hook_request_data_on_refusal.py` | 6 | Plan 20260926 hazard 17b: what a pre-call rejection hands ``async_post_call_failure_hook``. | a refusal hands failure hooks only the sanitised request | |
| `litellm_hook/test_forward_anthropic_auth.py` | 8 | Provider-gated Anthropic OAuth bridge in pre_call (`forward_anthropic_auth`). | the OAuth bridge and its scrubbing | |
| `litellm_hook/test_guardrail_information.py` | 22 | Plan 20260926 Task 5: our content-free entry in litellm's ``guardrail_information``. | the content-free `guardrail_information` entry | |
| `litellm_hook/test_guardrails_only_dispatch.py` | 3 | Plan 20260926 hazard 3: ``enforces_request_content`` on our plain callback. | litellm's guardrails-only scan runs our pre-call (from the test names) | |
| `litellm_hook/test_litellm_entrypoints.py` | 6 | The hook through litellm's own entry points: `async_pre_call_hook` and the log events. | litellm's entry points reach our hook and audit | |
| `litellm_hook/test_litellm_route_assumptions.py` | 9 | Drift guards for the litellm behavior the two auth bridges depend on. | litellm behaviour the auth bridges rely on | |
| `litellm_hook/test_log_hygiene.py` | 10 | What the hook logs: no original, no injected role or request id in a log line. | log-line hygiene | |
| `litellm_hook/test_logging_snapshot_is_content_free.py` | 13 | Plan 20260926 hazard 17: litellm's logging snapshot of the request is sanitised. | the logging snapshot holds placeholders only | |
| `litellm_hook/test_logging_surfaces.py` | 8 | Plan 20260926 Task 0: what litellm's logging surfaces hold (hazards 1, 8, 9, 12, 16). | what litellm's log payloads hold; debug / verbose refused at arm (from the test names) | |
| `litellm_hook/test_on_request_cancelled.py` | 17 | `CorpLlmGuardrail.on_request_cancelled`: the one terminal record of a request whose client left, and the request-id bridge from the route gate's ticket to litellm's call id. | one content-free record per cancelled request | |
| `litellm_hook/test_on_request_cancelled_edges.py` | 4 | `CorpLlmGuardrail.on_request_cancelled` at its edges: many unknown ids, a cancel beside a live request, and a guardrail rebuilt after a failed emit. | the same, at its edges | |
| `litellm_hook/test_payload_limits.py` | 12 | Request size limits in the pre-call: the oversize policy (fail-closed, chunk, deliver-flag) and the max_output_tokens cap. | size limits and the token cap | |
| `litellm_hook/test_pre_call_headers.py` | 16 | Pre-call header handling: corp-token auth and strip from every header location, header declassification, and the ChatGPT auth bridge. | corp-token auth and strip; ChatGPT bridge | |
| `litellm_hook/test_pre_call_shapes.py` | 40 | What the pre-call rewrites in each request shape: messages, system, instructions, Responses `input`, audio, and the call_type gate on `input`. | what is rewritten per request shape | |
| `litellm_hook/test_proxy_dispatch.py` | 10 | Plan 20260926 Task 0: litellm 1.101.0's real dispatch, driven end to end. | the sentence names the harness, not what is asserted; the test names use plan terms (`today`, `migrated`, `sentinel`) | **flag** |
| `litellm_hook/test_stage0_classifier.py` | 8 | Stage 0: the payload classifier refuses config / log shaped requests before egress. | Stage 0 refusals | |
| `litellm_hook/test_stage5_dlp.py` | 8 | Stage 5: the DLP egress guard re-scans the sanitised request and blocks a surviving secret. | Stage 5 refusals | |
| `litellm_hook/test_ticket_handover.py` | 11 | The guardrail has no response-side hook: the pre-call hands the mapping and the audit facts to the request's ticket, and asks a chat stream for usage. | the pre-call → ticket hand-over | |
| `litellm_hook/test_tool_calls.py` | 17 | Tool calls in a request: OpenAI tool_calls / function_call arguments, Anthropic tool_use.input, Responses tool-call items. | tool-call arguments are sanitised | |
| `litellm_hook/test_walker_oracle.py` | 5 | Plan 20260926 Task 4: litellm's guardrail translation handlers as an oracle for our walker. | every leaf litellm exposes is one we rewrite (from the test names) | |
| `route_gate/test_arm_checks.py` | 11 | The arm predicates ``asgi.py`` exits 70 on (``route_gate/arm_checks.py``). | what refuses to arm | |
| `route_gate/test_body_policies.py` | 18 | The gate refuses an admitted rewritten body that names litellm policies, or that is not JSON. | the 403 / 415 body refusals | |
| `route_gate/test_call_id_header.py` | 4 | The gateway owns litellm_call_id: a client's ``x-litellm-call-id`` header never reaches litellm, so two client requests can never share the id the guardrail keys its per-request state and audit record on. | the call-id header is dropped | |
| `route_gate/test_classify.py` | 26 | none | (method, path, scope) → verdict, malformed paths, HEAD inheritance, operator extras | **flag** |
| `route_gate/test_desanitize_middleware.py` | 49 | Plan 20260926 Task 0: the Option A middleware, ``route_gate/desanitize_middleware.py``. | the sentence names a plan option and a `src/` file; the behaviour comes only from the test names | **flag** |
| `route_gate/test_desanitize_stream.py` | 38 | The response reversal on a stream: chat chunks, Anthropic SSE and Responses events restored by the desanitiser (through `tests/response_restore.py`), upstream vs own-bug errors. | stream restoration | |
| `route_gate/test_desanitize_unary.py` | 27 | The response reversal on a unary response: a request sanitised by the hook, its JSON response restored by the desanitiser (through `tests/response_restore.py`). | unary restoration | |
| `route_gate/test_desanitize_usage.py` | 10 | Token counts the ASGI desanitiser reads off a response for the terminal record. | usage read-off and caps | |
| `route_gate/test_extras_and_codes_edges.py` | 7 | Operator extras at scale and at odd spellings, and the limiter's refusal codes end to end: the body a client gets and the audit record agree, and no two refusals share a code. | extras and refusal codes | |
| `route_gate/test_inflight.py` | 81 | The in-flight cap behind the route gate: admission, body replay, disconnect-aware cancellation and exactly-once slot release. | the in-flight cap | |
| `route_gate/test_inflight_boundaries.py` | 16 | The in-flight limiter at its edges: declared lengths that lie, the exact body cap, concurrent reservations that fill the byte budget, send failures at each point of a response, streams that end without a final chunk, a receive that fails, many sequential requests, and task tagging through nested shared tasks, explicit contexts and a chained task factory. | the limiter at its edges | |
| `route_gate/test_inflight_faults.py` | 8 | The in-flight limiter when something it depends on misbehaves: a metrics exporter that raises, and a receive still waiting after the downstream ended. | the limiter under dependency faults | |
| `route_gate/test_litellm_route_guard.py` | 18 | The guard: litellm's installed source against the route table. | every route litellm registers is in the table | |
| `route_gate/test_middleware.py` | 31 | none | refusal shape, passthrough, armed forwarding, websocket refusal, refusal audit / logs, extras | **flag** |
| `route_gate/test_reverse_object_shapes.py` | 6 | ⚠️ `_apply_reverse_to_response` on model objects (`model_dump` / `model_validate` / `model_copy`). | the sentence names a private `src/` function | **flag** |
| `route_gate/test_table.py` | 39 | none | the route table's own rules and verdict counts | **flag** |
| `route_gate/test_terminal_audit.py` | 39 | The terminal-audit contract: facts deposited on the ticket, one record published by whoever ends the response, never by a callback awaiting it. | one terminal record per request | |

Not flagged, but they lead with plan archaeology (`Plan 20260926 Task N:` / `hazard N:`) before naming the
behaviour: `test_bearer_is_hashed_before_logging.py`, `test_corp_token_never_reaches_logging_surfaces.py`,
`test_failure_hook_request_data_on_refusal.py`, `test_guardrail_information.py`, `test_guardrails_only_dispatch.py`,
`test_logging_snapshot_is_content_free.py`, `test_logging_surfaces.py`, `test_walker_oracle.py`.

**The 8 flagged files, with the one-line docstring suggested in commit 1** (as found at `f050208`):

| file | suggested docstring |
|---|---|
| `tests/litellm_hook/test_anthropic_upstream_headers.py` | The headers the hook forwards to Anthropic: an allow-list that drops the corp token and unlisted headers and rejects a bad Authorization, and the OAuth token prefix taken from litellm with a fallback. |
| `tests/litellm_hook/test_fail_open_probes.py` | Fail-open paths through litellm's real app: a metadata opt-out is overwritten, M4 status codes reach the client, `scan_raw_request` / `run_in_parallel` are refused, and a policy row in Postgres makes a migrated guardrail skip and send originals. |
| `tests/litellm_hook/test_proxy_dispatch.py` | Which of our hooks litellm 1.101.0's proxy runs, end to end: the plain callback runs on every flow and the client gets originals back; `apply_guardrail`, a duplicate guardrail name or a migrated pipeline would change that. |
| `tests/route_gate/test_classify.py` | How the route gate turns a request (method, raw path, scope type, upgrade header) into a verdict: malformed paths refused, HEAD inheriting GET, operator extras only ever PASSTHROUGH. |
| `tests/route_gate/test_desanitize_middleware.py` | The response desanitiser middleware: originals restored in unary JSON and SSE by the request's ticket, the mapping released on the final body, and every failure content-free. |
| `tests/route_gate/test_middleware.py` | The route-gate middleware: the refusal shape and codes, byte-identical passthrough, rewritten routes forwarded only once armed, websockets refused, and a refusal's audit record and logs. |
| `tests/route_gate/test_reverse_object_shapes.py` | Response restoration on litellm model objects rather than wire JSON, a path the middleware never takes: placeholders reversed, hidden params kept, a reconstruct failure neither bypasses validation nor logs an original. |
| `tests/route_gate/test_table.py` | The hand-classified route table's own rules: well-formed rows, a reason on every row, exactly eight rewritten spellings, bypass and management routes refused, and the verdict counts. |

### Fix (commits 2-5, plan rev 20 (2))

Each of the 8 files has a module docstring whose first line names the behaviour, with plan terms spelled out (no
"migrated pipeline", "sentinel", "Option A" or "M4" left unexplained). The four files that had a docstring keep its
old text below the new lines. The plan / hazard tag stays as a closing line, and the ⚠️ paragraph of
`test_reverse_object_shapes.py` is kept word for word. Commit 4 fixed four of them after review, so that each says
only what its tests check: `test_table.py` (the table is the collected surface by size floors;
`test_litellm_route_guard.py` holds the check against litellm's routes), `test_desanitize_middleware.py` (a failure
after the response starts sends what was already restored, then closes), `test_fail_open_probes.py` (the opt-out is
stripped or overwritten; the defences the same tests pin), `test_anthropic_upstream_headers.py` (every rejected
Authorization case). Commit 5 answers a Codex review on PR #37 (P2): `test_classify.py`'s second paragraph said the
verdict comes from the method and the raw path; it now names every input `classify()` reads (scope type, Upgrade
header, upper-cased method, decoded path, the raw path for malformed checks only, the tables, then extras), each
clause checked against `route_gate/classify.py` and `route_gate/table.py`. The docstrings at the final commit
(summary and first paragraph):

| file | docstring (summary and first paragraph) |
|---|---|
| `tests/litellm_hook/test_anthropic_upstream_headers.py` | The headers the hook forwards to Anthropic, and the OAuth token prefix it expects. An allow-list keeps the headers Anthropic needs and drops the corp token and every unlisted header. An Authorization that is missing, not a Bearer, empty, carries a line break, or holds no OAuth token (a truncated prefix, a plain API key, any other bearer) is rejected. The OAuth token prefix is read from litellm, with a fixed fallback when litellm's value is missing or unusable, and is never empty. |
| `tests/litellm_hook/test_fail_open_probes.py` | Ways litellm could let an original reach the provider, driven through its real proxy app. A client's guardrail opt-out, in `metadata`, `litellm_metadata` or at the root, is stripped or overwritten; our refusal keeps its status and error code through litellm; litellm's `scan_raw_request` and `run_in_parallel` flags are refused when the gateway arms. A policy row written into litellm's database, or a request body naming one, makes a guardrail registered the litellm way (under `guardrails:`, not as a plain callback, as ours is) skip the request, so the original would leave. The same tests pin the defences: our plain callback still rewrites, a check that runs after our pre-call refuses the request with 503, pinning `supported_db_objects` keeps the row out, and the gate refuses a body with a `policies` key with 403. That after-pre-call check cannot see a `scan_raw_request` run; the arm check refuses the flag instead. |
| `tests/litellm_hook/test_proxy_dispatch.py` | Which of our hooks litellm's real proxy runs, end to end, and what the client gets back. Our plain callback runs on every request flow, even when a litellm policy names it, and the client gets the originals back. The other tests show what would break that: an `apply_guardrail` method, a second guardrail answering to our name, or a guardrail registered the litellm way that a policy pipeline skips. A check that runs after our pre-call refuses a request our pre-call never saw. |
| `tests/route_gate/test_classify.py` | How the route gate turns a request into a verdict: PASSTHROUGH, REWRITTEN or REFUSE. The verdict is decided first by the ASGI scope type and the Upgrade header, then by the upper-cased method and the decoded path, taken as received. The raw (still-encoded) path is read only to spot a malformed one: an encoded slash, NUL or `..`, or a non-ASCII byte; the decoded path is malformed if it holds `..`, `//` or a NUL. A lifespan scope is passed through; a websocket scope, a websocket Upgrade header, an unknown scope type and a malformed path are refused. Otherwise the built-in tables decide (exact rows, then regex rows), and operator extras are consulted only when they have no row: HEAD checks a HEAD row, then the GET row, and a REWRITTEN result is refused; an extra can only add an otherwise-unlisted PASSTHROUGH route — it never reopens a refusal or promises a rewrite. |
| `tests/route_gate/test_desanitize_middleware.py` | The response desanitiser middleware: each request's originals restored, and restore failures. It restores the originals in unary JSON, chat / Anthropic SSE and Responses SSE with the mapping held on the request's ticket (never a client header) and releases that mapping on the final body. A restoration failure before the response starts is a content-free 500; after it starts, the events already restored reach the client and the stream closes cleanly. No failure puts an original in a log line. |
| `tests/route_gate/test_middleware.py` | The route-gate ASGI middleware: what a refused, a passed-through and a rewritten request get. A refusal has a fixed JSON shape and code, echoes no byte of the body, logs no path and writes one audit record. A passthrough is forwarded byte for byte and writes none. A rewritten route is forwarded only once the gate is armed. Websockets are refused, and an operator extra cannot re-admit a refused route. |
| `tests/route_gate/test_reverse_object_shapes.py` | Response restoration on litellm model objects, a path the middleware never takes. On model objects rather than wire JSON, placeholders are reversed, litellm's hidden params are kept, and a failed rebuild neither skips validation nor logs an original. (Then the ⚠️ paragraph, unchanged.) |
| `tests/route_gate/test_table.py` | The hand-classified route table's own rules. Every row is well formed and says why. Exactly eight spellings are REWRITTEN; the bypass, management and non-probe health routes are refused; the table is the whole collected surface (size floors only: `test_litellm_route_guard.py` checks it against litellm's routes), and the verdict counts are pinned. |

Checks at the final commit:

- Only module docstrings changed under `tests/` (besides the manifest below). For each of the 8 files, a script
  compared against `f050208` with `ast.dump`: the module body after the docstring is identical, and so are all
  349 function and class nodes. The files are 5-12 lines longer. `ruff check` and `ruff format --check` are
  clean on `tests/`.
- Manifests: `negative_log_checks.json` `site` values only. `negative_logs --check` keys a site by owner and
  check text, not by line, so it passed after commit 2. Under rev 20 (2), commit 3 ran one `negative_logs --write`
  that refreshed 23 `site` values (`route_gate/test_desanitize_middleware.py` 18 by +6,
  `route_gate/test_middleware.py` 4 by +8, `route_gate/test_reverse_object_shapes.py` 1 by +5). Commit 4 ran one
  more and refreshed `test_desanitize_middleware.py`'s 18 by +2. Each time a script compared the file with the
  previous commit: same top-level keys, `about` and `security_node_ids`; 149 entries in the same order; only
  `site` changed, only in those files; every one of the 149 sites names a line that holds its check. No other
  manifest changed.
- In both venvs, `inventory`, `must_keep`, `moves`, `name_pinned` and `negative_logs --check` all exit 0, and
  `pytest tests/_gates -q` gives 33 passed. The 8 files plus `litellm_hook/test_acceptance_matrix.py` and
  `docs/test_docs_pins.py`: minimal 529 passed / 6 skipped, full 716 passed. The 6 minimal skips are the
  recorded ones: three modules collection-skipped without litellm, `test_reverse_object_shapes.py`'s
  `importorskip`, and two `test_anthropic_upstream_headers.py` runtime skips.
- No full record run: the change is docstrings only, the inventory drops docstrings, and none of the 8 files
  is hashed in `external_deps.json`.
- Citations of a shifted line: one, `docs/testing/task7-caplog-trivial-prune-audit.md`'s
  `route_gate/test_desanitize_middleware.py:1467`, annotated `(now: :1475 …)`. `CLAUDE.md`, `README.md`,
  the other `docs/` files and `tests/` cite no line in the 8 files.

Verdict: **met.**

## Other findings

- [must-keep.md](must-keep.md) line 186 gives the baseline run as 4,693 / 5,916 passed. The committed `7ae1098`
  ledgers record 4,694 / 5,917 passed. Not changed here; rev 20 (3) moves it to Task 10.
- Coverage manifests: `tests/_manifests/coverage.{minimal,full}.json` were last written in `7ae1098` (`runs` 7 /
  4). Every later PR checked against the baseline, not against a re-written file.

# Task 7 caplog, call_count and trivial-test prune audit (phase 1)

The audit of plan `docs/plans/20260926-test-suite-refactor-and-prune.md` (rev 16, local only), Task 7: `caplog`
wording and trivial-test pruning, the last prune PR. Phase 1 decides keep / delete for every id of the universe
below and changes no test. Phase 2 (after review) makes the deletions, the [deleted-tests.md](deleted-tests.md)
rows, the fault injections on the checkout and the manifest regeneration. The gates are in
[must-keep.md](must-keep.md). Tree: `release/1.0.x` at `152e57a`. Every line number here is `152e57a`'s unless it
says "after".

**Phase 2 done** (commits `a40c4cf` deletions + ledger rows + injections + the review fixes, `318b967` manifests, this
commit line refs): the 6 deletions below are made. Rev 17 decided the review questions: the 6 deletions stand; Q1 #3
has a survivor (`::test_nfc_offsets_map_back_to_the_original_string` builds `Finding` through `corp_ner.py:217-223`
and asserts `.label`) and #6 is the one survivor-less row, admitted as `none — trivial (<reason>)` with `n/a (no
survivor; coverage equal: <module>)` (one bullet in `deleted-tests.md`'s completeness list); Q2 the test-side
injection is gate-5 evidence; Q3 #4 / #5 delete; Q4 no rewrite in this PR, the (b) work is one optional strengthening
PR after Task 7; Q5 a prune PR carries no case deletion; Q6-Q8 keep as audited; Q9 `test_orchestrator.py:1302` stays
`behaviour`; Q10-Q12 as proposed (banner and `_NFC` left, the in-line `Finding` import edit is a residual edit,
`moves.json` −1). The only edits besides the function removals are that import edit, the EOF blank-line strip in
`test_checks.py` and the twin's `moves.json` key. The injections were re-run on the checkout in both venvs (I1, I2 /
I2b, I3 / I4 / I3r / I6a / I6b, I5, and I7 for #3: `corp_ner.py:219` `label=label,` → `label="LOCATION",`): every
survivor failed at the line its row cites, I3r and I5 failed nothing, and each was reverted with `src/` clean after.
Line numbers marked "at HEAD" are the pruned tree's, measured; every other line number is `152e57a`'s. At HEAD the
four edited files are 1,682 / 445 / 162 / 337 lines (`test_streaming.py` / `test_corp_ner.py` / `test_profile_ids.py`
/ `test_checks.py`) and hold 80 / 29 / 12 / 29 test functions; they collect 226 ids, all passed in both venvs; the
record runs collect 5,097 ids in minimal and 5,912 in full. The survivors fail at HEAD at `test_streaming.py:429`
(I1), `test_corp_ner.py:88` (I2, I2b, I7) and `:101` (I2), `test_profile_ids.py:55` (I3), `:56` (I4) and `:41`
(`_base_event`, I6a / I6b). The manifest diff (`git diff 152e57a 318b967 -- tests/_manifests`) is the
[prediction below](#predicted-phase-2-manifest-diff), byte-identical to the scratch preview; every gate exits 0 in
both venvs, and `coverage_gate check` finds no dropped line or arc.

**Decision: 6 delete, 91 keep, and 6 asserts in 4 kept tests marked rewrite-candidate.** No log assert, no
`call_count` assert and no must-keep id is deleted. The six deletions:

| # | deleted id | survivor(s) | set |
|---|---|---|---|
| 1 | `tests/sanitizer/test_streaming.py::test_framing_integrity_original_reconstructed_after_split` (baseline `tests/sanitizer/test_streaming_adversarial.py::…`) | `tests/sanitizer/test_streaming.py::test_sse_placeholder_split_across_deltas_reassembled` | D (Task 4 twin) |
| 2 | `tests/detectors/test_corp_ner.py::test_fixture_actually_diverges_in_length_under_nfc` | `tests/detectors/test_corp_ner.py::test_nfc_offsets_map_back_to_the_original_string`, `::test_span_over_a_single_composed_char_covers_both_code_points` | C (fixture self-check) |
| 3 | `tests/detectors/test_corp_ner.py::test_finding_is_the_shared_dataclass` | `tests/detectors/test_corp_ner.py::test_nfc_offsets_map_back_to_the_original_string` (rev 17; phase 1 said none — trivial) | C |
| 4 | `tests/audit/test_profile_ids.py::test_event_defaults_are_empty` | `tests/audit/test_profile_ids.py::test_profile_ids_absent_when_empty` | C (dataclass default) |
| 5 | `tests/audit/test_profile_ids.py::test_event_carries_profile_ids_and_jurisdiction` | `tests/audit/test_profile_ids.py::test_profile_ids_emitted_when_set`, `::test_jurisdiction_emitted_when_set` | C (dataclass field read-back) |
| 6 | `tests/healthz/test_checks.py::test_status_is_immutable_dataclass` | none — trivial (restates `frozen=True`) | C |

Two of them (#3, #6) had no survivor in phase 1; rev 17 named one for #3, so only #6 is survivor-less. The ledger's
column spec did not admit that at phase 1; see
[the ledger question](#the-ledger-column-spec-and-a-deletion-with-no-survivor) and Q1. After phase 2 each
environment's ledger holds 5,912 ids (5,918 today).

## The universe

Recomputed with `tests/_gates/inventory.py`'s `suite_modules()` / `_tests_in()` (module-level `def test*` and
`Test*` methods; there are no test classes) and `must_keep.function_ids()`. **A current id is must-keep when
`moves.to_baseline(id)` is a key of `function_ids()`** — the dict is keyed by baseline ids.

| count | this audit | the manager's sizing (`t7/universe.txt`) | why they differ |
|---|---|---|---|
| test functions | 3,540 | 3,493 | the sizing left out `tests/_gates` (33) and `tests/e2e` (14) |
| must-keep functions | 1,925 | 1,756 | the sizing compared current ids to the baseline-keyed dict without `moves.to_baseline`: 165 moved must-keep tests counted as not must-keep (`route_gate/test_desanitize_middleware.py` 49, `test_desanitize_stream.py` 33, `test_desanitize_unary.py` 27, `litellm_hook/test_logging_snapshot_is_content_free.py` 13, `test_ticket_handover.py` 11, …) |
| not must-keep | 1,615 | 1,737 | 1,737 − the 165 moved must-keep tests + the 43 not-must-keep tests of `tests/_gates` and `tests/e2e` (the other 4 there are the e2e security ids) = 1,615 |
| Set A: tests / log asserts | 13 / 21 (17 positive, 4 negative) | 33 / 54 | 22 of the 33 are must-keep at their baseline id (13 in `test_desanitize_middleware.py`, step-1 added via `litellm_hook/test_asgi_desanitize_prototype.py`; 3 `test_reverse_object_shapes.py`, 2 `test_fail_policy.py`, 1 each `test_desanitize_stream.py`, `test_desanitize_unary.py`, `test_ticket_handover.py`, `test_log_hygiene.py`); 2 more found here through log aliases (`warnings = [r.getMessage() …]`, `warning = next(…getMessage()…)`) |
| Set A: helper-delegated rows | 0 | 4 helpers listed | see below |
| Set B: `call_count` asserts | 37 (36 not must-keep, 1 must-keep) in 37 tests | 5 | the sizing matched only `.call_count` attributes; the AST rule ("an `assert` whose source contains `call_count`") finds the plan's 37 |
| Set C: trivial-test rows | 28 | 12 | widened scan, below |
| Set D: recorded candidates | 20 functions (22 node ids) + 2 helper notes | — | `test_default_ttl_is_30_days` is counted once, in C |
| **universe (not must-keep functions)** | **97** | | 13 + 36 + 28 + 20 |

**Set A, helper-delegated checks.** Every helper or method that mentions `caplog` / `getMessage`:
`route_gate/test_middleware.py::_log_text` (its callers are must-keep: step-1 modified-in-full and the
`tests/route_gate/*` glob), `tokens/test_oidc_verifier.py::_assert_no_leak` / `_rejects` / `_jwks_unavailable`
(step-1 added and step-2 glob), the `emit` / `holding` methods of record collectors in four must-keep modules, and
`tests/desanitize_served_script.py` (a subprocess script). No other module imports any of them. The two
"helpers" the sizing found in `test_bootstrap.py` / `test_bootstrap_edges.py` are logger-restoring fixtures whose
comments mention `caplog` (`test_bootstrap.py:916`, `test_bootstrap_edges.py:35`); they check nothing. So no helper
check reaches a non-must-keep test, and Set A has no delegated rows. No non-must-keep test takes `caplog` without a
log assert.

**Must-keep and the stop conditions.** `must_keep --check` exits 0 at `152e57a` and on the pruned clone. All 139
`security` rows of `negative_log_checks.json` have a must-keep owner (or a helper owner: `_assert_clean`,
`_assert_gate_surfaces_are_clean`, `_assert_no_leak`), and all 151 `security_node_ids` are must-keep. None of the 21
Set A asserts is a `security` row. Fingerprints match in both venvs; Postgres `pg-test` was up. No stop condition
hit.

## Rules applied

- **Set A (item 1).** Each log assert is (a) negative (here: only the reviewed `behaviour` class; no `security`
  row is in the universe), (b) level + event key (an event name, a `key=value` field, a `block_reason`, an env-var
  name, a level filter), or (c) the wording of an informational line (prose fragments). A prune PR deletes ids, not
  asserts, so a test with any other assert is a keep (dual-use). A (c) assert can only go with its test, and only
  under criterion (3). A (b) site gets a recommendation (keep as is / rewrite in a separate PR) and its token is
  checked against `docs/*.md`, `docs/ops/*.md`, `CLAUDE.md`, `README.md`, `compose/*.md`.
- **Set B (item 2).** Deletable only where the count pins an implementation detail with no behavioural consequence,
  and never a dedup / idempotency guarantee. CLAUDE.md's "oracle called ONLY on a deterministic gazetteer hit" and
  the `CORP_LLM_ORACLE_TRIGGER` / `CORP_LLM_ORACLE_ENABLED` switches are behaviour.
- **Set C (item 3).** Policy default → keep (retention, TTL, fail policy, size threshold, TLS, auth, route gate,
  capacity, NEVER-fields, the secret-label set, a registry's membership). A call into `src/` (a factory, a parser,
  a check, a runtime `Protocol` `isinstance`, an exception's `__init__` / `__str__`) → keep. Otherwise delete, with
  a survivor where one exists, and only when coverage stays equal.
- **Set D.** Each recorded candidate by the same rules. A vacuous "does not raise" test is deleted only under
  criterion (3) (a survivor on the same input, equal or stronger); otherwise keep and list for strengthening.
- **Criterion (3) / superset survivors.** Same inputs, same production path, equal-or-stronger predicates,
  outcomes (`expected_outcomes.*`), negative checks and cases. Opaque values the code neither reads nor transforms
  (rev 16 (3)) are recorded in the note. A mapping the desanitizer *does* transform is an input (Q5).
- **Fault injection.** Run on a scratch clone for every deletion, both venvs, each reverted (`git diff --stat`
  empty after each). Rev 10's waiver applies to #1 (same file, same `SseStreamDesanitizer`); it was run anyway.

Columns below: `checks` is the shard line (`a` asserts / `r` raises / `f` fail; every test also carries the
autouse `_no_logger_state_left_behind` fail 1, not repeated); outcomes are `passed / passed` (minimal / full)
unless a row says otherwise.

## Set A — log-output asserts (13 tests, 21 asserts)

| node id · assert | def line | checks | class | decision | survivor | semantic note | fault injection |
|---|---|---|---|---|---|---|---|
| `tests/detectors/test_shadow.py::test_shadow_agreement_does_not_log` · `:57` `'detector_disagreement' not in caplog.text` | :51 | a1 r0 f0 | (a)-shape, reviewed `behaviour` row | keep | — | The only check of the test: agreement (`_equivalent` true) logs no disagreement line. `::test_shadow_returns_canonical_results` runs the same input with no log check. Deleting the test would drop its `behaviour` row silently. | — (K1) |
| `tests/litellm_hook/test_fail_policy.py::test_pre_call_corp_llm_down_on_system_fails_closed_503` (baseline `tests/test_litellm_hook.py::…`) · `:394` `'litellm_pre_call_corp_llm_failed' in caplog.text` | :381 | a3 r1 f0 | (b) event key (WARNING) | rewrite-candidate | — | Dual-use: 503 and `E_CORP_LLM_DOWN` asserted at `:391-392`. The line carries `error_code=E_CORP_LLM_DOWN`, which `docs/ops/runbook.md:38` tells operators to grep; no test pins it in a log line. Note: the oracle fails on `message_index=0` here (measured), so the system-field site `litellm_hook.py:1392` is not reached despite the name. | — |
| `tests/litellm_hook/test_log_hygiene.py::test_pre_call_logs_sanitize_done_per_block` (baseline `tests/test_litellm_hook.py::…`) · `:80` `'litellm_pre_call_message_sanitize_done' in caplog.text` | :62 | a4 r0 f0 | (b) event key (INFO) | keep | — | Dual-use (`:77-78` content asserts). Token not cited in any doc: incidental. "Per block" (two lines for two blocks) is not counted; noted for strengthening. | — |
| ″ · `:81` `'redaction_count' in caplog.text` | | | (b) field key | keep | — | Incidental field name of the same line (`redaction_count` is an audit field in `docs/audit-schema.md`, but this is the log line). | — |
| `tests/litellm_hook/test_payload_limits.py::test_pre_call_max_tokens_clamped_when_over_cap` (baseline `tests/test_litellm_hook.py::…`) · `:169` `'litellm_pre_call_max_tokens_clamped' in caplog.text` | :156 | a4 r0 f0 | (b) event key (INFO) | keep | — | Dual-use: the clamp is `out["max_tokens"] == 4096` at `:168`. Not doc-cited. | — |
| ″ · `:170` `'requested=64000' in caplog.text` | | | (b) `key=value` | keep | — | Same line, incidental. | — |
| ″ · `:171` `'capped=4096' in caplog.text` | | | (b) `key=value` | keep | — | Same line, incidental. | — |
| `tests/litellm_hook/test_payload_limits.py::test_system_oversize_deliver_flag_logs_system_oversize_delivered` (baseline `tests/test_litellm_hook.py::…`) · `:213` `'litellm_pre_call_system_oversize_delivered' in caplog.text` | :196 | a6 r0 f0 | (b) event key (WARNING) | rewrite-candidate | — | Dual-use (`:212`, the audit `block_reason == "oversize:delivered"` at `:223-224`). Contract: `docs/security.md:131` "logged as `litellm_pre_call_system_oversize_delivered`, so every deliver-flag egress is auditable". | — |
| ″ · `:214` `'field=system' in caplog.text` | | | (b) `key=value` | rewrite-candidate | — | Same line; a structured check pins it on that record, not anywhere in the text. | — |
| `tests/litellm_hook/test_stage5_dlp.py::test_stage5_dlp_blocks_canary_survivor` (baseline `tests/test_litellm_hook.py::…`) · `:27` `'litellm_egress_blocked' in caplog.text` | :14 | a4 r1 f0 | (b) event key (INFO) | rewrite-candidate | — | Dual-use (422 / `E_DLP_BLOCKED` at `:25-26`). Contract: `docs/ops/runbook.md:38` names `litellm_egress_blocked` and tells operators to grep `block_reason=`. | — |
| ″ · `:28` `'dlp:canary' in caplog.text` | | | (b) `block_reason` value | rewrite-candidate | — | `dlp:canary` is a `docs/audit-schema.md:65` block reason; the test does not pin the `block_reason=` key the runbook greps. | — |
| `tests/sanitizer/test_orchestrator.py::test_cache_a_disabled_when_policy_fingerprint_cannot_be_computed` · `:1301` `any('cache_a_disabled' in m for m in warnings)` | :1281 | a7 r0 f0; loops 3 | (b) level (≥ WARNING) + event key | rewrite-candidate | — | Dual-use (`:1299`, `:1306-1309`). Contract: `docs/ops/upgrade.md:402` "logs `cache_a_disabled reason=policy_fingerprint_failed` with the exception type only"; the test checks neither `reason=` nor "type only". | — |
| ″ · `:1302` `not any(_R9_TERM in m for m in warnings)` | | | (a)-shape, reviewed `behaviour` row | keep | — | Kept with its test. Its assert message says "M1-14: no user content in logs" while the reviewed class is `behaviour` (Q9). | — |
| `tests/sanitizer/test_streaming.py::test_responses_stream_event_reconstruct_failure_does_not_bypass_validation` · `:694` `'reconstruct_failed' in caplog.text.lower()` | :676 | a2 r0 f0 | (b) event-key substring (WARNING) | keep | — | Dual-use (`out == [event]` at `:693`). `streaming_responses_event_reconstruct_failed` and its `error_code` are cited in no doc. The must-keep sibling `::test_responses_stream_event_reconstruct_failure_log_has_no_original` owns the M1-14 check. | — |
| `tests/test_bootstrap.py::test_oracle_disabled_logs_one_info_at_build_time` · `:302` `'oracle_enabled=false' in caplog.text` | :294 | a1 r0 f0 | (c) informational INFO line (`bootstrap oracle_enabled=false — local-first only`), `key=value` token | keep | — | The only assert: the emission of the line is the behaviour, the token its only probe. `::test_oracle_falsy_spellings_disable_oracle_without_endpoint` checks the oracle is off but not the line. "One INFO" is neither counted nor level-checked; noted for a rewrite. | — (K4) |
| `tests/test_bootstrap.py::test_explicit_kwarg_off_takes_precedence_over_a_conflicting_env_pair` · `:627` `'CORP_LLM_FORWARD_ANTHROPIC_AUTH' in warning` | :607 | a5 r0 f0; loops 1 | (b) env-var key; record already selected by `levelno == WARNING` (`:625`) | keep | — | Dual-use (`:620-621` flag asserts). Already structured (level + key). | — |
| ″ · `:628` `'disarmed CORP_LLM_FORWARD_CHATGPT_AUTH' in warning` | | | (c) wording | keep | — | Kept with its test. No other test checks this warning, so it is the only probe that the divergence is logged; not a criterion-(3) deletion either way. | — |
| ″ · `:629` `'config check' in warning` | | | (c) wording (operator remedy) | keep | — | Same. | — |
| `tests/test_bootstrap.py::test_no_disarm_warning_when_the_env_pair_is_already_legal` · `:641` `not [r for r in caplog.records if 'mutually exclusive' in r.getMessage()]` | :632 | a1 r0 f0; loops 1 | (a)-shape, reviewed `behaviour` row | keep | — | The only check: a legal env pair logs no disarm warning. No survivor makes it. | — (K2) |
| `tests/test_bootstrap_edges.py::test_endpoint_placeholder_default_warns` · `:324` `any('CORP_LLM_ENDPOINT' in r.getMessage() and r.levelno == logging.WARNING …)` | :316 | a1 r0 f0; loops 1 | (b) level + key | keep | — | Already structured. The only probe of the placeholder warning (Task 5b kept it as a sensitive default). | — |
| `tests/test_bootstrap_edges.py::test_endpoint_set_does_not_warn` · `:340` `not any('CORP_LLM_ENDPOINT' in r.getMessage() …)` | :331 | a1 r0 f0; loops 1 | (a)-shape, reviewed `behaviour` row | keep | — | The only check; no survivor. | — (K3) |

Classes: (a)-shape `behaviour` 4, (b) 14, (c) 3 = 21. Decisions: 15 keep, 6 rewrite-candidate, 0 delete; all 13
tests keep.

### The (b) sites and the rewrite question

| site | current check | structured form | token a contract? | recommendation |
|---|---|---|---|---|
| `test_fail_policy.py:394` | event name anywhere in `caplog.text` | the WARNING record whose message starts `litellm_pre_call_corp_llm_failed ` and holds `error_code=E_CORP_LLM_DOWN` | yes: `error_code=` grep, `runbook.md:38` | rewrite in a separate PR |
| `test_log_hygiene.py:80-81` | two substrings in `caplog.text` | INFO records starting `litellm_pre_call_message_sanitize_done`, one per block, each with `redaction_count=` | no | keep as is |
| `test_payload_limits.py:169-171` | three substrings | one INFO record starting `litellm_pre_call_max_tokens_clamped` with `requested=64000 capped=4096` | no | keep as is |
| `test_payload_limits.py:213-214` | two substrings | one WARNING record starting `litellm_pre_call_system_oversize_delivered` with `field=system` and `block_reason=oversize:delivered` | yes: `security.md:131` | rewrite in a separate PR |
| `test_stage5_dlp.py:27-28` | two substrings | one record starting `litellm_egress_blocked` with `block_reason=dlp:canary` | yes: `runbook.md:38`, `audit-schema.md:65` | rewrite in a separate PR |
| `test_orchestrator.py:1301` | event name in a WARNING+ message | a WARNING record `cache_a_disabled reason=policy_fingerprint_failed exc=RuntimeError`, and the exception text `fingerprint source unavailable` absent | yes: `upgrade.md:402` | rewrite in a separate PR |
| `test_streaming.py:694` | lowercase substring | a WARNING record starting `streaming_responses_event_reconstruct_failed` | no | keep as is |
| `test_bootstrap.py:627` | key in a selected WARNING | — (already level + key) | no | keep as is |
| `test_bootstrap_edges.py:324` | key + level | — (already structured) | no | keep as is |

**Overall recommendation: one small rewrite PR, 4 tests / 6 asserts** (`test_fail_policy.py:394`,
`test_payload_limits.py:213-214`, `test_stage5_dlp.py:27-28`, `test_orchestrator.py:1301`). These are the only sites
whose token an operator doc cites, and no test pins the cited form today: no log assert pins
`error_code=E_CORP_LLM_DOWN`, `block_reason=dlp:canary` or `reason=policy_fingerprint_failed` (two log asserts pin
other `error_code=` / `reason=` values: `extensions/test_corp_ner_hook_codes.py:182`,
`route_gate/test_desanitize_middleware.py:1467` (now: `:1473` after Task 9's docstring); the must-keep
`invariants/test_no_originals_leak.py:1967-1968` pins
`litellm_pre_call_corp_llm_failed` / `field=system`). A rewrite strengthens the check, so it changes the four
`body_hash`es: it is neither a move nor a prune, and needs Task 1b-style ledger rows (`(changed: …)`). Everything else
keeps as is. The plan's "rewritten as structured assertions in a preceding move PR" assumed (b) sites in deletable
tests; every (b) site here sits in a test kept for other asserts, so this PR does not depend on that rewrite. The
reviewer decides at rev 17 whether the rewrite PR exists (Q4).

## Set B — `call_count` asserts (37)

Every count is a local counter over a fake oracle `http` handler (`call_count[0]`, `call_count`) or a fake rules
loader's `.call_count` attribute. The counters in `invariants/test_no_originals_leak.py:1676-1681` and
`litellm_hook/test_fail_policy.py:404-409` drive a fake (fail on the second call) and are asserted nowhere, so they
are not in the 37. None of the 37 pins an implementation detail; all are kept.

| node id · assert | def line | checks | class | decision | survivor | semantic note (what the count pins) | fault injection |
|---|---|---|---|---|---|---|---|
| `tests/rules/test_cached.py::test_cache_hit_within_ttl` · `:44` `inner.call_count["a"] == 1` | :38 | a1 r0 f0 | counter, behaviour | keep | — | Three loads in the TTL reach the inner loader once: the cache. | — |
| `tests/rules/test_cached.py::test_cache_isolated_per_team` · `:59` `inner.call_count == {"a": 1, "b": 1}` | :47 | a3 r0 f0 | counter, behaviour | keep | — | The cache is keyed per team: each team loads once and never gets another team's entry. | — |
| `tests/rules/test_cached.py::test_cache_refresh_after_ttl` · `:68` `inner.call_count["a"] == 2` | :62 | a1 r0 f0 | counter, behaviour | keep | — | Rule updates take effect after the TTL (M1-15 in `cached.py`'s docstring). | — |
| `tests/rules/test_cached.py::test_concurrent_loads_dedupe` · `:76` `inner.call_count == 1` | :71 | a2 r0 f0; loops 2 | counter, dedup guarantee | keep | — | Ten concurrent loads, one inner load: the per-team lock's single-flight. The plan names this class "never". | — |
| `tests/rules/test_cached.py::test_inner_error_propagates_and_does_not_cache` · `:86` `inner.call_count["missing"] == 2` | :79 | a1 r2 f0 | counter, behaviour | keep | — | A failed load is retried, not cached. | — |
| `tests/sanitizer/test_content_blocks.py::test_sanitize_document_base64_source_untouched` · `:770` `call_count == 1` | :752 | a2 r0 f0 | counter, behaviour | keep | — | Exactly one leaf reaches the sanitizer: the `title` is scanned, the base64 `source.data` is not. The identity fake means `data` would pass `:771` even if scanned; the count is the only probe. | — |
| `tests/sanitizer/test_content_blocks.py::test_sanitize_document_url_source_untouched` · `:789` `call_count == 1` | :774 | a2 r0 f0 | counter, behaviour | keep | — | Same for a `url` source: the title is scanned, the URL is never sent to (or rewritten by) the sanitizer. | — |
| `tests/sanitizer/test_local_first.py::test_cache_a_stores_merged_pairs` · `:377` `call_count[0] == 1` | :364 | a4 r0 f0; loops 1 | counter, dedup guarantee | keep | — | The second identical request is a Cache-A hit: one oracle call in total. | — |
| `tests/sanitizer/test_local_first.py::test_oracle_still_called_with_local_pass_enabled` · `:428` `call_count[0] == 1` | :417 | a1 r0 f0 | counter, behaviour | keep | — | No gazetteer + a local pass: the DP-3 path always calls the oracle. Near-twin of `test_oracle_trigger.py::test_default_no_gazetteer_with_local_detectors_oracle_called` (other text, other local findings), not a strict one. | — |
| `tests/sanitizer/test_oauth_system_preamble.py::test_preamble_block_is_byte_identical` · `:190` `call_count[0] == 0` | :183 | a3 r0 f0 | counter, behaviour | keep (must-keep) | — | Must-keep (step-2 glob); listed for the count of 37. No gazetteer hit ⇒ no oracle call. | — |
| `tests/sanitizer/test_oracle_trigger.py::test_gazetteer_hit_calls_oracle` · `:102` `== 1` | :87 | a2 r0 f0 | counter, conditional-oracle rule | keep | — | A gazetteer hit calls the oracle exactly once (one round-trip per hit leaf: ADR-003 latency). | — |
| `…::test_gazetteer_hit_regulated_term` · `:141` `== 1` | :126 | a2 r0 f0 | counter, rule | keep | — | The same for a REGULATED term. | — |
| `…::test_gazetteer_nohit_oracle_not_called` · `:165` `== 0` | :150 | a3 r0 f0 | counter, rule | keep | — | No hit ⇒ no oracle call: the rule's core (no egress to the oracle, the latency win). | — |
| `…::test_gazetteer_nohit_local_findings_still_applied` · `:185` `== 0` | :171 | a3 r0 f0; loops 1 | counter, rule | keep | — | No hit with local findings ⇒ still no call. | — |
| `…::test_gazetteer_hit_bijection_with_local_and_oracle` · `:231` `== 1` | :215 | a5 r0 f0; loops 2 | counter, rule | keep | — | A hit with local findings: one call, merged with local pairs. | — |
| `…::test_gazetteer_hit_cache_a_oracle_called_once` · `:259` `== 1` | :246 | a2 r0 f0 | counter, dedup guarantee | keep | — | A Cache-A hit on the second request saves the second call. | — |
| `…::test_default_no_gazetteer_oracle_always_called` · `:278` `== 1` | :268 | a3 r0 f0 | counter, rule (default branch) | keep | — | `gazetteer=None` ⇒ the legacy path calls the oracle. | — |
| `…::test_default_no_gazetteer_with_local_detectors_oracle_called` · `:295` `== 1` | :283 | a1 r0 f0 | counter, rule | keep | — | The same with local detectors. | — |
| `…::test_gazetteer_nohit_cache_a_oracle_never_called` · `:311` `== 0` | :298 | a2 r0 f0 | counter, rule | keep | — | Two no-hit requests: zero calls (and a Cache-A hit). | — |
| `…::test_confidential_mark_detected_triggers_oracle` · `:335` `== 1` | :320 | a2 r0 f0 | counter, rule | keep | — | A confidentiality marking is a hit. | — |
| `…::test_size_threshold_fails_closed_before_gazetteer` · `:401` `== 0` | :388 | a1 r1 f0 | counter, fail-closed | keep | — | Oversize content is refused before any oracle call: the payload never leaves for the oracle. | — |
| `…::test_gazetteer_without_local_detectors_hit` · `:421` `== 1` | :409 | a2 r0 f0 | counter, rule | keep | — | A hit without local detectors. | — |
| `…::test_gazetteer_without_local_detectors_nohit` · `:436` `== 0` | :425 | a3 r0 f0 | counter, rule | keep | — | No hit without local detectors. | — |
| `…::test_trigger_default_is_gazetteer_hit_local_does_not_wake_oracle` · `:471` `== 0` | :457 | a2 r0 f0 | counter, trigger default | keep | — | The default trigger: a local finding alone does not wake the oracle. | — |
| `…::test_trigger_any_local_finding_local_wakes_oracle` · `:492` `== 1` | :478 | a2 r0 f0 | counter, trigger mode | keep | — | `any_local_finding`: a local finding wakes it. | — |
| `…::test_trigger_any_local_finding_rule_wakes_oracle` · `:509` `== 1` | :496 | a1 r0 f0 | counter, trigger mode | keep | — | `any_local_finding`: a rule match wakes it. | — |
| `…::test_trigger_any_local_finding_clean_skips_oracle` · `:524` `== 0` | :512 | a1 r0 f0 | counter, trigger mode | keep | — | `any_local_finding`: a clean leaf does not. | — |
| `…::test_trigger_always_calls_on_clean_request` · `:565` `== 1` | :553 | a1 r0 f0 | counter, trigger mode | keep | — | `always`: a clean leaf still calls. | — |
| `…::test_trigger_gaz_hit_always_runs_oracle` · `:584` `== 1` (4 cases) | :572 | a1 r0 f0; parametrize 4 | counter, rule | keep | — | A hit calls the oracle under every trigger. | — |
| `…::test_trigger_sampled_zero_never_calls` · `:640` `== 0` | :628 | a1 r0 f0 | counter, trigger mode | keep | — | `sampled:0` never calls on a no-hit leaf. | — |
| `…::test_trigger_sampled_hundred_always_calls` · `:655` `== 1` | :643 | a1 r0 f0 | counter, trigger mode | keep | — | `sampled:100` always calls. | — |
| `…::test_trigger_always_widens_deliver_flag_rescan` · `:679` `== 1` | :661 | a1 r1 f0 | counter, trigger mode | keep | — | `always` makes the deliver-flag rescan consult the oracle. | — |
| `…::test_trigger_gazetteer_hit_deliver_flag_skips_oracle_on_nohit` · `:698` `== 0` | :682 | a2 r0 f0 | counter, trigger default | keep | — | The default skips it on a no-hit oversize leaf. | — |
| `…::test_oracle_disabled_gazetteer_hit_skips_oracle_local_findings_applied` · `:769` `== 0` | :751 | a3 r0 f0; loops 1 | counter, oracle-off gate | keep | — | `oracle_enabled=False`: no call even on a hit. | — |
| `…::test_oracle_disabled_trigger_always_still_zero_calls` · `:787` `== 0` | :774 | a2 r0 f0 | counter, oracle-off gate | keep | — | Off wins over `always`. | — |
| `…::test_oracle_disabled_local_pass_branch_applies_replace_md_rules` · `:822` `== 0` | :802 | a6 r0 f0; loops 2 | counter, oracle-off gate | keep | — | Off on the local-pass branch. | — |
| `…::test_oracle_enabled_local_pass_branch_applies_replace_md_rules_directly` · `:850` `== 1` | :832 | a5 r0 f0; loops 1 | counter, rule | keep | — | On: the local-pass branch calls once. | — |

`…` = `tests/sanitizer/test_oracle_trigger.py`. 37 rows: 36 keep, 1 must-keep. Implementation-detail counts found:
none. The `== 1` (rather than `>= 1`) is the one-round-trip-per-hit-leaf latency contract, not a detail. Two
count-derived asserts sit outside the 37 because their text does not say `call_count`:
`test_oracle_trigger.py:606` (`counts[0] == counts[1]`, sampling is deterministic per request) and `:625` (the
`sampled:50` share of 200 requests is between 30 % and 70 %). Both are behaviour.

## Set C — trivial tests (28)

The sizing's heuristic (body ≤ 4 statements, asserts and assignments only, no call but constructors / `isinstance`
/ `len`) gave 12. Widened here three ways, every hit read by hand:

1. **Shape**: ≤ 6 statements, only asserts / assignments / imports, only constructor (`CapitalName(…)`) or pure
   builtin calls; or a single-assert body comparing an attribute / constant to a literal or by `is`; or `repr(`.
   Adds 11.
2. **By name** (`default`, `repr`, `export`, `expos`, `constant`, `registered`, `singleton`, `dataclass`,
   `frozen`, `immutable`, `version`, `__all__`) with ≤ 8 statements. About 190 hits; all but 4 call a factory /
   registry / parser / check in `src/` (a branch, so not item 3). Adds the 4 frozen / dataclass-default tests.
3. **A dataclass built by a test helper** (`x = _helper()` then attribute asserts against literals). Adds
   `test_event_carries_profile_ids_and_jurisdiction`. Its other hits are decided elsewhere (`test_team_config_defaults`,
   the `test_bootstrap.py` `*_unset_defaults_*` tests — Task 5b rule A) or call `src/` (`parse_manifest`,
   `BearerAuthProvider(…).artifacts()`, the deploy script runner).

| node id | def line | checks | class | decision | survivor | semantic note | fault injection |
|---|---|---|---|---|---|---|---|
| `tests/audit/test_profile_ids.py::test_profile_ids_not_a_never_field` | :100 | a2 r0 f0 | trivial (set membership) | keep | — | NEVER-fields policy: `profile_ids` / `jurisdiction` are not in `NEVER_FIELDS`, so they reach the sinks. | — |
| `tests/cli/test_proxy.py::test_defaults_match_install_sh_layout` | :30 | a3 r0 f0 | trivial (constant = literal) | keep | — | Policy defaults: loopback listen `127.0.0.1:9999` (not network-exposed), an `https://` upstream (TLS), the token-file path (auth). It does not read `scripts/install.sh` and has no `external_deps.json` entry. | — |
| `tests/detectors/test_corp_ner.py::test_fixture_actually_diverges_in_length_under_nfc` | :82 | a3 r0 f0 | trivial (test-constant self-check) | **delete** | `tests/detectors/test_corp_ner.py::test_nfc_offsets_map_back_to_the_original_string`, `::test_span_over_a_single_composed_char_covers_both_code_points` | Asserts test constants only (`len(_NFC) != len(_DECOMPOSED)`, `(16, 14)`, `_NFC == "Андрей Королёв"`); no `src/` line. It guards the fixture premise of the NFC tests; those tests themselves fail when the premise breaks: `:94` expects original offsets `(0, 7)` / `(8, 16)` on `_DECOMPOSED` and NFC texts `Андрей` / `Королёв`, `:107` expects `(5, 7)`. | I2 / I2b (test-side, below): both survivors fail |
| `tests/detectors/test_corp_ner.py::test_finding_is_the_shared_dataclass` | :393 | a1 r0 f0 | trivial (dataclass field read-back) | **delete** | `tests/detectors/test_corp_ner.py::test_nfc_offsets_map_back_to_the_original_string` (rev 17) | Constructs `detectors.base.Finding` and reads `.label` back: the generated `__init__`, no `src/` line beyond the class body executed at import. Despite the name it checks nothing of `corp_ner`. The survivor builds `Finding` on the production path (`corp_ner.py:217-223`) and asserts `.label` with the offsets and `.text`. Coverage equal: `detectors/base.py` 11 / 0 both venvs. | I7 (phase 2): `corp_ner.py:219` `label=label,` → `label="LOCATION",` fails the survivor |
| `tests/detectors/test_detector_contract.py::test_registry_covers_every_known_detector` | :46 | a1 r0 f0 | trivial (set equality) | keep | — | A registry's membership (policy). | — |
| `tests/detectors/test_detector_contract.py::test_built_detector_is_pii_detector` | :56 | a1 r0 f0; fixture `detector` (4 cases) | trivial-shaped | keep | — | A branch: the fixture builds each entry through `build_detectors` (the production path). Outcomes: minimal `passed` / `skipped:natasha/spacy not available`, full `passed` ×4. | — |
| `tests/detectors/test_dual_ner_policy_signature.py::test_dual_ner_satisfies_the_policy_signature_protocol` | :51 | a1 r0 f0 | trivial-shaped | keep | — | A runtime `Protocol` `isinstance` and `DualNerDetector.__init__`: a call into `src/`. | — |
| `tests/extensions/test_registry.py::test_module_exposes_singleton_and_api_version` | :163 | a2 r0 f0 | trivial (re-export + constant = literal) | keep | — | `EXTENSION_API_VERSION == "1"` is the api-version gate's reference value, cited as `"1"` in `docs/extending.md:192`; no other test pins the literal (Q7). | — |
| `tests/payload/test_size_threshold.py::test_default_threshold_is_100kb` | :14 | a1 r0 f0 | trivial (constant = literal) | keep | — | Size-threshold policy default. | — |
| `tests/profiles/test_registry.py::test_corp_ner_is_registered_for_profile_bundles` | :46 | a1 r0 f0 | trivial (membership) | keep | — | A registry's membership. It is a strict subset of `test_detector_contract.py::test_registry_covers_every_known_detector` (the same `profiles.registry.DETECTOR_REGISTRY` object); kept by analogy with rev 14 (1) (a sensitive default stays even with a strict twin) (Q6). | — |
| `tests/sanitizer/test_allowlist.py::test_secret_labels_set_exactly_matches_spec` | :113 | a1 r0 f0; fixture `_reset_config` | trivial (set equality) | keep | — | The secret-label set (policy). | — |
| `tests/tokens/test_issuance.py::test_default_ttl_is_30_days` | :25 | a1 r0 f0 | trivial (constant = literal) | keep | — | TTL policy default (and `gateway-admin token issue --ttl-days`'s default). Also a Set D item (Task 6b note). | — |
| `tests/audit/test_factory.py::test_sink_extension_is_not_a_writable_sink` | :398 | a2 r0 f0 | trivial-shaped | keep | — | Security: the registry adapter must not expose `write()`, so nothing writes around the AuditLogger NEVER gate; `SinkExtension.__init__` runs. | — |
| `tests/audit/test_factory.py::test_sink_extension_spec_shape` | :189 | a4 r0 f0 | trivial-shaped | keep | — | Fail policy: M4 audit-sink-down = `continue`. | — |
| `tests/detectors/test_dual_ner_require.py::test_ner_unavailable_is_not_a_runtimeerror` | :87 | a2 r0 f0 | trivial (class hierarchy) | keep | — | Fail policy: `NerUnavailableError` must escape the per-engine `RuntimeError` handler, so it fails closed. | — |
| `tests/extensions/test_registry.py::test_spec_defaults_fail_closed_and_accepts_m4_vocab` | :147 | a3 r0 f0 | trivial (dataclass default) | keep | — | Fail-policy default `fail-closed`. | — |
| `tests/healthz/test_checks.py::test_live_always_healthy` | :19 | a1 r0 f0 | trivial-shaped | keep | — | A branch: `LiveCheck().check()`. | — |
| `tests/payload/test_size_threshold.py::test_oversize_content_error_carries_sizes_not_content` | :62 | a4 r0 f0 | trivial-shaped | keep | — | A branch (the exception's message formatting) and an error-text surface: sizes only. | — |
| `tests/profiles/test_manifest.py::test_parse_extends_string_normalizes_to_tuple` | :24 | a1 r0 f0 | trivial-shaped | keep | — | A branch: `parse_manifest`. | — |
| `tests/profiles/test_manifest.py::test_parse_extends_list` | :28 | a1 r0 f0 | trivial-shaped | keep | — | A branch: `parse_manifest`. | — |
| `tests/sanitizer/test_content_blocks.py::test_responses_block_list_fields_are_registered_text_fields` | :1202 | a1 r0 f0 | constant relation | keep | — | Not a value-vs-literal test: every field the walker sanitizes must be one the reversal restores (otherwise placeholders reach the client). A registry-consistency check, which rev 14 keeps. | — |
| `tests/sanitizer/test_orchestrator.py::test_default_chunk_overlap_covers_longest_entity` | :227 | a2 r0 f0 | trivial (constants) | keep | — | Detection-safety default: the chunk overlap covers the longest entity (a PEM key body), so no secret splits across chunks unseen. | — |
| `tests/test_anthropic_oauth_profile.py::test_merged_stack_arms_only_the_anthropic_bridge_and_sets_no_master_key` | :275 | a5 r0 f0; delegated `_render` f1; fixture `merged_stack` | trivial-shaped | keep | — | Auth policy of the shipped overlay (one bridge, no master key); the fixture runs `docker compose config`. | — |
| `tests/audit/test_profile_ids.py::test_event_defaults_are_empty` | :47 | a2 r0 f0 | trivial (dataclass default) | **delete** | `tests/audit/test_profile_ids.py::test_profile_ids_absent_when_empty` | Same input `_base_event()`. The candidate asserts `event.profile_ids == ()` and `event.jurisdiction is None`; the survivor emits that event through `AuditLogger` and asserts both keys absent from the record (`logger.py:47-50`: `if event.profile_ids:` / `is not None`). Any non-empty `profile_ids` default or non-`None` `jurisdiction` default fails the survivor (I3, I4; `test_langfuse_sink.py::test_profile_metadata_absent_when_not_set` fails too). Residual: the candidate also pins the default's type (`()`); a falsy non-tuple default (`[]`) passes the survivor and changes no record (I3r) (Q3). | I3, I4 required (the survivor runs `AuditLogger.emit`); I3r residual |
| `tests/audit/test_profile_ids.py::test_event_carries_profile_ids_and_jurisdiction` | :53 | a2 r0 f0 | trivial (dataclass field read-back) | **delete** | `tests/audit/test_profile_ids.py::test_profile_ids_emitted_when_set`, `::test_jurisdiction_emitted_when_set` | The candidate builds `_base_event(profile_ids=("core", "ru-152fz"), jurisdiction="ru")` and reads both fields back; no branch. The survivors build the same event class with `profile_ids=("core", "division-x")` and `("ru-152fz",)` + `jurisdiction="ru"` and assert the emitted record's values: they pass the fields through the dataclass and the logger. The tuple strings are opaque (rev 16 (3)). Residual: the record holds `list(event.profile_ids)`, so the attribute's type is not pinned (Q3). | I6a, I6b required |
| `tests/extensions/test_registry.py::test_spec_is_frozen` | :157 | a0 r1 f0 | trivial (frozen dataclass) | keep | — | Fail policy: a registered spec's `fail_policy` cannot be flipped after registration (Q8). | — |
| `tests/providers/test_registry.py::test_provider_spec_is_frozen` | :71 | a0 r1 f0 | trivial (frozen dataclass) | keep | — | `CORP_VLLM_SPEC` is a shared module constant the v1 provider guard validates; a mutable name would bypass it (Q8). | — |
| `tests/healthz/test_checks.py::test_status_is_immutable_dataclass` | :340 | a0 r1 f0 | trivial (frozen dataclass) | **delete** | none — trivial | Restates `@dataclass(frozen=True)` on `HealthStatus`, a per-call value object: `src/` holds no shared `HealthStatus` instance and never assigns `.healthy` / `.detail`. With `frozen=True` removed (I5) only this test fails; the other 415 (minimal, 31 skipped) / 452 (full) tests of `tests/healthz/`, `test_bootstrap.py`, `tests/extensions/`, `tests/providers/` and `audit/test_factory.py` pass. | n/a (no survivor; coverage equal: `healthz/checks.py` 87 / 17 both venvs); I5 run as evidence |

28 rows: 23 keep (policy default / value / membership 11, a branch into `src/` 6, a fail-policy or security
property 5, a constant relation 1), 5 delete.

## Set D — candidates recorded by earlier audits (20 functions, 22 ids, 2 helper notes)

| node id | def line | checks | class | decision | survivor | semantic note | fault injection |
|---|---|---|---|---|---|---|---|
| `tests/sanitizer/test_streaming.py::test_framing_integrity_original_reconstructed_after_split` (baseline `tests/sanitizer/test_streaming_adversarial.py::…`; `moves.json` `ids`) | :857 | a2 r0 f0; loops `out` | twin (Task 4 audit `:180-190`) | **delete** | `tests/sanitizer/test_streaming.py::test_sse_placeholder_split_across_deltas_reassembled` | Re-verified on the current bodies: same `SseStreamDesanitizer(_mapping(("user@example.com", "[EMAIL_001]")))`, same `list(ANTHROPIC_SSE_FIXTURE)`, `_collect_bytes` vs `_collect` (identical bodies, `:807` / `:386`), the same `content_block_delta` / `text_delta` filter and the same two asserts. The survivor's `_data_of` is stricter: a non-JSON `data:` line raises (not skipped) and it decodes strictly; a missing data line is skipped in both. | n/a (same path: `SseStreamDesanitizer`, rev 10 waiver), run anyway: I1 |
| `tests/sanitizer/test_streaming.py::test_stream_does_not_raise_and_emits_output[empty_data_line]` | :1065 | a1 r0 f0; parametrize 3 | vacuous (F2) | keep | — | `len(out) >= 1` on `[b"data: \n\n"]`; no other test feeds an empty `data:` line to `SseStreamDesanitizer`. | — |
| ″ `[tool_use_block]` | | | vacuous (F2) | keep | — | The nearest, `::test_input_json_delta_after_tool_use_block_start_unchanged`, has other `partial_json` and no `_cb_stop(1)`, the event this case is about. | — |
| ″ `[second_text_block]` | | | vacuous (F2) | keep | — | Near-twin `::test_sse_two_text_blocks_no_runtime_error` (`:562`): the same six-event shape (start / delta / stop for blocks 0 and 1) and stronger asserts, but another mapping and other delta texts (`alice`/`[N1]`, `bob`/`[N2]` instead of `x`/`[X]`, `y`/`[Y]`), which the desanitizer reads and rewrites; the literal input rule keeps it (Q5). The case-deletion dry run used this case. | — |
| `tests/sanitizer/test_streaming.py::test_flush_called_twice_does_not_raise` (baseline `…_adversarial.py::…`) | :1337 | a2 r0 f0 | vacuous | keep | — | `isinstance(…, list)` ×2. `::test_flush_idempotent` (`:151`) is the inner `StreamingDesanitizer`, not the SSE wrapper. | — |
| `tests/sanitizer/test_streaming.py::test_feed_after_complete_stream_is_safe` (baseline `…_adversarial.py::…`) | :1348 | a1 r0 f0 | vacuous | keep | — | No survivor; the SSE wrapper's feed-after-flush differs from the inner desanitizer's (`::test_feed_after_flush_raises`). | — |
| `tests/litellm_hook/test_log_hygiene.py::test_pre_call_request_id_stable_across_calls_on_same_data` (baseline `tests/test_litellm_hook.py::…`) | :13 | a1 r0 f0 | vacuous | keep | — | One `pre_call`, `isinstance(rid1, str) and rid1`. The `litellm_call_id` tests (`:135-172`) set a call id (another branch of `_ensure_request_id`); `test_forward_anthropic_auth.py:101` sets the bridge. No survivor on the same input. | — |
| `tests/route_gate/test_desanitize_stream.py::test_post_call_stream_malformed_data_line_does_not_raise` (baseline `tests/test_litellm_hook_adversarial.py::…`) | :1180 | a1 r0 f0 | vacuous | keep | — | Middleware path; Task 4 merged only the algorithm-layer files. No other middleware test feeds `data: NOT JSON`. | — |
| `tests/route_gate/test_desanitize_stream.py::test_post_call_stream_mixed_bytes_and_dict_chunks` (baseline `tests/test_litellm_hook_adversarial.py::…`) | :1238 | a1 r0 f0 | vacuous | keep | — | No other test mixes bytes and dict chunks. | — |
| `tests/test_settings.py::test_all_keys_is_nonempty_and_unique` | :37 | a2 r0 f0 | weak-assert (Task 5b) | keep | — | `len(keys) > 30` is a bound; the uniqueness assert is real. | — |
| `tests/test_bootstrap.py::test_build_guardrail_carries_metrics_exporter_noop_by_default` | :113 | a2 r0 f0 | weak-assert | keep | — | The first `isinstance` is implied by the second; a prune PR does not remove asserts. | — |
| `tests/test_bootstrap.py::test_master_key_is_checked_against_the_resolved_flag_not_the_env_var` | :661 | a1 r1 f0 | weak-assert | keep | — | The `raises(ConfigError, match=…)` half is real; `… is not None` holds for any build that returns. | — |
| `tests/test_bootstrap.py::test_guardrail_attribute_builds_once_and_caches` | :72 | a5 r0 f0 | weak-assert | keep | — | `calls == 0` right after patching cannot fail; the other four asserts are real. Survivor of a Task 5b deletion. | — |
| `tests/test_settings.py::test_validate_uses_pydantic_when_present` | :541 | a1 r1 f0 | weak-assert | keep | — | Nothing shows pydantic ran; survivor of a Task 5b deletion. | — |
| `tests/deploy/test_deploy_script.py::test_unknown_option_and_unknown_subcommand_are_refused` (Task 6a D7) | :272 | a2 r0 f0; loops 1 | weak-assert | keep | — | `argv[-1] in stderr or argv[2] in stderr`: `"up"` can satisfy the first case. | — |
| `tests/deploy/test_bootstrap_server_script.py::test_unit_working_directory_follows_a_custom_target_dir` (B20) | :164 | a1 r0 f0; delegated `_function_body` a1; fixture `script_text` | weak-assert | keep | — | One substring for a substitution claim. | — |
| `tests/deploy/test_bootstrap_server_script.py::test_help_exits_zero_without_touching_the_host` (B5) | :48 | a2 r0 f0 | weak-assert | keep | — | "Without touching the host" is not checked. | — |
| `tests/deploy/test_deploy_script.py::test_empty_ps_output_is_not_mistaken_for_healthy` (D38) | :780 | a1 r0 f0; fixture `tmp_path` | weak-assert | keep | — | Return code only. | — |
| `tests/tokens/test_issuance.py::test_issues_token_with_default_ttl` | :30 | a3 r0 f0 | weak-assert (Task 6b) | keep | — | The `>= 29 days` lower bound also passes a 29-day default. | — |
| `tests/tokens/test_issuance.py::test_custom_ttl_respected` | :79 | a1 r0 f0 | weak-assert | keep | — | Upper bound only; a zero TTL passes. | — |
| `tests/tokens/test_postgres_store.py::test_pg_upsert_and_lookup` (the `isinstance(pg_store, …)` asserts) | :81 | a8 r0 f0; delegated `skip_or_fail` f1; fixture `pg_store`; `skipped:asyncpg not installed / passed` | weak-assert | keep | — | Kept in Task 6b for its 2-element scopes round trip. The `isinstance` narrows the fixture's `object` type; the same assert sits in four must-keep races (not candidates). | — |
| `tests/team_config/test_store.py::test_team_config_defaults` | :21 | a5 r0 f0 | trivial (dataclass defaults) | keep | — | Policy defaults (retention, fail policy), as Task 6b expected. | — |
| `tests/test_config.py::_isolate_from_file` (helper) | :107 | — | helper | not a target | — | Points `CORP_LLM_GATEWAY_CONFIG_FILE` at a nonexistent path, so `config._load_file` (`config.py:42-51`) falls through to `~/.corp-llm-gateway/config.toml` and `/etc/…`. A fix (an existing empty `tmp_path` file, or patching `config._DEFAULT_PATHS`) changes the bodies of its 6 callers (`:112-159`, none must-keep): a body-changing PR, not a prune. | — |
| `tests/tokens/test_postgres_store.py::_info` (helper) | :36 | — | helper | not a target | — | `user_id=` / `revoked_at=` have no caller since Task 6b; editing it changes the `body_hash` of the must-keep races. Left for Task 10. | — |

`test_default_ttl_is_30_days` (Task 6b note) is the Set C row. The Task 4 audit's "all three checks are vacuous"
note (`task4-streaming-audit.md:166`) is the three F2 rows.

## Fault injections and keep evidence

Scratch clone of `152e57a`; each mutation applied, the tests run in both venvs, reverted with `git checkout --
<file>`, `git diff --stat` empty after each. The batch was re-run with `PYTHONDONTWRITEBYTECODE=1` after one
stale-`.pyc` result (I6a and I6b change `event.py` by the same byte count within one second; the first I6b run
executed I6a's bytecode). The re-run reproduced every result below.

| # | mutation | failed (both venvs) | nothing else failed in |
|---|---|---|---|
| I1 | `src/corp_llm_gateway/sanitizer/streaming.py:600` `rewritten = ds.feed(text_in)` → `rewritten = text_in` | candidate `test_streaming.py:869` (`original not restored: ' [EMAIL_001]'`), survivor `:429` (`'user@example.com' in ' [EMAIL_001]'`), and 13 other stream tests | — |
| I2 | `tests/detectors/test_corp_ner.py:33` `_DECOMPOSED = f"Андре{_I_BREVE} Корол{_E_DIAERESIS}в"` → `"Андрей Королёв"` (precomposed) | candidate `:83` (`14 != 14`), survivors `:94` (`[(0, 6, …), (7, 14, …)] == [(0, 7, …), (8, 16, …)]`) and `:107` (`(5, 6) == (5, 7)`) | the other 95 tests of the file |
| I2b | the same line, only `Корол{_E_DIAERESIS}в` → `Королёв` | candidate `:84` (`(15, 14) == (16, 14)`), survivor `:94` (`(8, 15)` vs `(8, 16)`) | 96 |
| I3 | `src/corp_llm_gateway/audit/event.py:47` `profile_ids: tuple[str, ...] = ()` → `= ("core",)` | candidate `test_profile_ids.py:49`, survivor `:67` (`'profile_ids' not in {…}`), `test_langfuse_sink.py::test_profile_metadata_absent_when_not_set` | 186 of `tests/audit/` |
| I4 | `event.py:48` `jurisdiction: str \| None = None` → `= "ru"` | candidate `:50`, survivor `:68`, `::test_jurisdiction_absent_when_none_even_with_profiles`, the langfuse test | 185 |
| I3r | `event.py:47` `= ()` → `= field(default_factory=list)` | candidate `:49` (`[] == ()`) only | 188 — the residual (Q3) |
| I6a | `event.py:47` → `= field(default=(), init=False)` | candidate #5, survivors `::test_profile_ids_emitted_when_set`, `::test_jurisdiction_emitted_when_set` (`unexpected keyword argument 'profile_ids'`) | — |
| I6b | `event.py:48` → `= field(default=None, init=False)` | candidate #5, survivor `::test_jurisdiction_emitted_when_set` (`… 'jurisdiction'`) | — |
| I5 | `src/corp_llm_gateway/healthz/checks.py:11` `@dataclass(frozen=True)` → `@dataclass` | candidate `test_checks.py:342` (`DID NOT RAISE`) only | 415 passed / 31 skipped (minimal), 452 (full) |
| K1 | `src/corp_llm_gateway/detectors/shadow.py:28` `if not _equivalent(…):` → `if True:` | kept `test_shadow.py:57` only | 245 / 24 skipped (minimal), 303 (full) of `tests/detectors/` |
| K2 | `src/corp_llm_gateway/bootstrap.py:543` `if forward_auth_conflict(…) is None:` → `if False:` | kept `test_bootstrap.py:641` only | 375 / 378 (bootstrap, edges, settings, the two profile modules) |
| K3 | `bootstrap.py:202` `if endpoint == _DEFAULT_ENDPOINT:` → `if True:` | kept `test_bootstrap_edges.py:340` only | 472 / 481 (incl. `tests/cli/`) |
| K4 | `bootstrap.py:664` `_log.info(` → `_log.debug(` | kept `test_bootstrap.py:302` only | 359 / 362 |

## Totals

| set | rows | keep | delete | rewrite-candidate (asserts) |
|---|---|---|---|---|
| A — log asserts | 13 tests / 21 asserts | 13 tests (15 asserts) | 0 | 6 asserts in 4 tests |
| B — `call_count` | 37 asserts / 37 tests (36 + 1 must-keep) | 36 (+1 must-keep) | 0 | — |
| C — trivial | 28 | 23 | 5 | — |
| D — recorded | 20 functions (22 ids) + 2 helpers | 19 (+2 helpers left) | 1 | — |
| **universe** | **97 functions** | **91** | **6** | **6 asserts** |

Keep reasons: A — dual-use 8, sole probe of the behaviour 5 (the log line, or a `behaviour` negative check); B —
dedup guarantee 3 (`test_concurrent_loads_dedupe` and the two Cache-A hits), behaviour 33 (cache semantics, the
conditional-oracle rule and its default branch, trigger modes, the oracle-off gate, fail-closed, the walker's leaf
selection); C — policy default / value / membership 11, a branch into `src/` 6, a fail-policy or security property
5, a constant relation 1; D — vacuous without a survivor 6 functions (8 ids: the 3 F2 cases), weak-assert 12, policy
default 1.

## Parametrize-case dry run (`[second_text_block]`, not proposed)

On its own scratch branch the `[second_text_block]` `pytest.param` (13 lines, the comment above it included) was
removed from `test_stream_does_not_raise_and_emits_output`. Ruff clean.

- `inventory --check` (both venvs): exit 1 with **no `missing test` line**, but 3 changed columns on the fold test:
  `body_hash` `944210f2…` → `f715259b…`, `case_data` `cases 3 → 2` (its `values` hash too), `helpers` lose
  `_cb_start` and `_delta` (only that case called them). `must_keep`, `moves`, `name_pinned`, `negative_logs
  --check`: 0 (the fold test is not must-keep: rev 11, `tests/sanitizer/test_streaming.py` is under no step-1 /
  step-2 path).
- `ledger check full` on a plugin run of the file: `missing id … [second_text_block]`.
- After splicing that one id out of both ledgers (5,918 → 5,917) and `inventory --write`:
  `baseline_checks/sanitizer.json` `tests` 570 → 570 and `cases` 570 → 570, **0 gone, 1 changed in each**
  (`tests`: the three columns above; `cases`: `{full: 3, minimal: 3}` → `{2, 2}`); git +2 / −2. Then every
  `--check` exits 0 and the scoped `ledger check` exits 0.
- So a case deletion shows in the inventory as a *changed* test, not a missing one, and the ledger row must
  carry the changed `body_hash` / `case_data` / `helpers` like a Task 1b `(changed: …)` row. It also orphans the
  survivor named in `deleted-tests.md:73` (the Task 4 F2 row for that case). The reviewer decides at rev 17 whether
  a prune PR may carry such a change (Q5).

## Residual files — what phase 2 leaves

- **`tests/sanitizer/test_streaming.py`** 1,698 → 1,682 (`:857-872`, 16). No import or helper loses its last
  user (`_data_obj` is still used at `:1138`, `ANTHROPIC_SSE_FIXTURE` at `:417`). Later tests move −16
  (`test_stream_does_not_raise_and_emits_output` `:1065 → :1049`, `test_flush_called_twice_does_not_raise`
  `:1337 → :1321`, …); the must-keep site `:715` is above the deletion.
- **`tests/detectors/test_corp_ner.py`** 455 → 445 (`:82-87` 6, `:393-396` 4) plus **one residual edit**:
  `:23` `from corp_llm_gateway.detectors.base import BatchPIIDetector, Finding` → `… import BatchPIIDetector`
  (ruff F401 otherwise). An in-line edit of an import statement, the same category as rev 16 (6)'s import line
  (Q11). `_NFC` (`:34`) loses its last code reference; the survivor's comment at `:89` (`:83` after) names it.
  Ruff does not flag a module constant; proposed to leave it (Q10). Drift: the survivors `:88 → :82`,
  `:101 → :95`; the must-keep `test_never_logs_request_or_response_text` `:371 → :365`.
- **`tests/audit/test_profile_ids.py`** 174 → 162 (`:47-58`, 12). The `# AuditEvent carries the fields ---` banner
  (`:44`) is left with no test under it; proposed to leave it, or remove it (comment only, no hash) if the reviewer
  wants (Q10). Every later test moves −12 (`test_profile_ids_absent_when_empty` `:62 → :50`, …,
  `test_vector_vrl_renders_with_profile_ids` `:160 → :148`); their `external_deps.json` entries are keyed by id,
  not line.
- **`tests/healthz/test_checks.py`** 343 → 337 (the last function and the 2 blank lines before it; EOF). `HealthStatus`
  and `pytest` stay in use.
- **`tests/_manifests/moves.json`** 279 → 278 lines: the `ids` key
  `tests/sanitizer/test_streaming.py::test_framing_integrity_original_reconstructed_after_split` goes, or
  `moves --check` refuses ("the key is not in the current tree"). Task 3b removed 16 keys the same way (Q12).

The dry-run diff (`git diff 152e57a t7-dry2`): 5 files, 1 insertion, 46 deletions.

## Negative-log, name-pinned, external-deps, moves and ledger findings

- `negative_log_checks.json`: no deleted id is an `owner`. Two `site` leaves drift (−6, from deletion #2):
  `tests/detectors/test_corp_ner.py:379 → :373` and `:380 → :374`, both owned by the must-keep
  `test_never_logs_request_or_response_text` (`security`). The `test_streaming.py:715` site is above deletion #1.
  The 5 `behaviour` rows: owners not must-keep — `test_shadow.py:57`, `test_orchestrator.py:1302`,
  `test_bootstrap.py:641`, `test_bootstrap_edges.py:340` — all kept, so no row drops; `route_gate/test_inflight.py:884`
  is must-keep. The 4 `not-a-log-check` rows: `test_litellm_config_guards.py::_assert_starts_no_litellm_guardrail`
  (a helper in a must-keep module) and three `tokens/test_oidc_verifier.py` rows (must-keep). `negative_logs --write`
  on the clone changed exactly those two leaves.
- `name_pinned.json`: no deleted id and none of the four files is indexed. `name_pinned --write` prints only
  `UNRESOLVED CLAUDE.md:tests/sanitizer/test_engine.py::test_name ['CLAUDE.md:177']` and writes no diff.
- `external_deps.json`: no entry for any deleted id. `cli/test_proxy.py::test_defaults_match_install_sh_layout` has
  no entry (it compares constants to literals and reads no file); `--write` changes nothing for it. The file is
  unchanged by the preview regeneration.
- `moves.json`: deletion #1 is a moved test: its ledger row names the baseline id with `(current: …)` as Task 3b
  did, and its `ids` key goes (above). No other candidate was moved.
- `deleted-tests.md`: row 49 (Task 3b) names #1 as one of its survivors and cites `test_streaming.py:869`; phase 2
  adds "(now: deleted in Task 7; `test_sse_placeholder_split_across_deltas_reassembled` and the middleware sibling
  remain)". Row 50 cites `test_streaming.py:1164`, which moves to `:1148`; phase 2 adds `(now: …)`. Rows 48 (`:854`)
  and 49 (`:429`) cite lines above the deletion. No row cites a line of the other three files.

## Predicted phase-2 manifest diff

Previewed on the clone (the 6 ids spliced out of both ledgers with `ledger._dump`, then `inventory --write`,
`name_pinned --write`, `negative_logs --write`; `git diff --numstat 152e57a`):

| manifest | change |
|---|---|
| `expected_outcomes.minimal.json` | 5,918 → 5,912 ids (−6, all `passed`), 0 added, 0 changed; git +4 / −4 (4 file lines) |
| `expected_outcomes.full.json` | the same |
| `baseline_checks/audit.json` | `tests` 123 → 121, `cases` 123 → 121 (0 changed as JSON); git −4 |
| `baseline_checks/detectors.json` | 182 → 180 / 182 → 180; git −4 |
| `baseline_checks/healthz.json` | 132 → 131 / 132 → 131; git −2 |
| `baseline_checks/sanitizer.json` | 570 → 569 / 570 → 569 (the twin's baseline id); git −2 |
| `negative_log_checks.json` | the two `site` leaves above; git +2 / −2 |
| `moves.json` | −1 `ids` entry (266 → 265); git −1 |
| `must_keep/`, `name_pinned.json`, `coverage.*.json`, `not_applicable.json`, `external_deps*.json`, `env_fingerprint.*.json` | unchanged |
| `docs/testing/deleted-tests.md` | +6 rows (`Task 7`, `auto-review (pending)`), and the `(now: …)` notes on rows 49 and 50 |

Predicted record runs: minimal collects 5,103 → 5,097 (−6 `passed`), full 5,918 → 5,912. The one expected failure
before regeneration is `tests/_gates/test_suite_gates.py::test_the_check_inventory_matches_the_baseline` (the 6
`missing test` lines).

## Coverage

The four touched files, `--cov=corp_llm_gateway --cov-branch`, before (232 ids) and after (226) the six deletions,
compared with `tests/_gates/coverage_gate.py`'s `from_report` / `drops`:

| env | module | before (lines / arcs) | after | whole-suite baseline |
|---|---|---|---|---|
| minimal / full | `sanitizer/streaming.py` | 401 / 138 | 401 / 138 | 482 / 192 |
| minimal / full | `detectors/corp_ner.py` | 113 / 39 | 113 / 39 | 113 / 39 |
| minimal | `detectors/base.py` | 11 / 0 | 11 / 0 | 16 / 0 |
| full | `detectors/base.py` | 11 / 0 | 11 / 0 | 14 / 0 |
| minimal / full | `audit/event.py` | 18 / 0 | 18 / 0 | 18 / 0 |
| minimal / full | `audit/logger.py` | 25 / 10 | 25 / 10 | 31 / 16 |
| minimal / full | `healthz/checks.py` | 87 / 17 | 87 / 17 | 113 / 26 |

`drops(before, after)` and `drops(after, before)` are empty in both environments over all 122 `src/` files the run
reports (2,409 lines / 279 arcs before and after). Phase 1 proposed two no-survivor deletions (#3, #6); rev 17
named a survivor for #3 (`test_nfc_offsets_map_back_to_the_original_string`), so #6 is the one survivor-less
deletion. Neither executes a line or arc that a surviving test of the same files does not.

## Dry run (scratch clone; nothing in the checkout's `tests/` or `src/` changed)

A `git clone --shared` of the checkout at `152e57a` under the session scratch directory, `PYTHONPATH=src:.`, the
two venvs with the env from [must-keep.md](must-keep.md); Postgres `pg-test` up on 55432. Before any change:
`fingerprint minimal|full --check` 0, and `inventory`, `must_keep`, `moves`, `name_pinned`, `negative_logs --check`
0.

- **Deletions** (branch `t7-dry2`): the 6 functions removed by an AST script (decorator through the blank lines
  after it), EOF blanks stripped, the `Finding` import edit, the `moves.json` key. `ruff check` and `ruff format
  --check` on the four files: clean.
- **Gates, manifests not regenerated**: `inventory --check` exit 1 with exactly 6 `missing test` lines (the 5
  current ids and the twin's baseline id) and nothing else, in both venvs. `must_keep`, `moves`, `name_pinned`,
  `negative_logs --check`: 0. (Without the `moves.json` edit, `moves --check` exits 1 on the dangling key.)
- **After**: the four files 226 passed in each venv (232 before). `tests/_gates`: 32 passed / 1 failed in each
  venv, the failure `test_the_check_inventory_matches_the_baseline` (the 6 lines).
- **Preview regeneration** (branch `t7-regen2`): the diff in the table above. Then `inventory`, `must_keep`,
  `moves`, `name_pinned`, `negative_logs --check` all 0 in both venvs; `tests/_gates` 33 passed in both;
  `ledger check <env> <run> --scope <the four files>` on a plugin run of the four files: 0 in both; `selftest
  inventory` (full) 19 of 19 rejected; `selftest ledger` (minimal) 4 of 4 rejected.
- **Case-deletion scenario** (branch `t7-case`): above.

## Ledger rows (phase 2)

Ready to copy; the fault-injection column is re-run on the checkout in phase 2 and cited at HEAD.

| PR | deleted node id | baseline outcome (minimal / full) | the check it made | survivor node id(s) | semantic note | fault injection | reviewer |
|---|---|---|---|---|---|---|---|
| Task 7 | `tests/sanitizer/test_streaming_adversarial.py::test_framing_integrity_original_reconstructed_after_split` (current: `tests/sanitizer/test_streaming.py::test_framing_integrity_original_reconstructed_after_split`) | passed / passed | asserts 2, raises 0, fail 0; delegated: `_no_logger_state_left_behind` fail 1; loops over `out` | `tests/sanitizer/test_streaming.py::test_sse_placeholder_split_across_deltas_reassembled` | Criterion (3), intra-file twin (Task 4 audit's ready row). Same `SseStreamDesanitizer(_mapping(("user@example.com", "[EMAIL_001]")))`, same `ANTHROPIC_SSE_FIXTURE`, identical collectors (`_collect_bytes` / `_collect`), the same `text_delta` filter and the same two asserts; the survivor's `_data_of` is stricter (a non-JSON `data:` line raises, strict UTF-8). | `src/corp_llm_gateway/sanitizer/streaming.py:600` `rewritten = ds.feed(text_in)` → `rewritten = text_in`: the survivor fails `test_streaming.py:429`; not required (rev 10 same-builder waiver), run anyway | auto-review (pending) |
| Task 7 | `tests/detectors/test_corp_ner.py::test_fixture_actually_diverges_in_length_under_nfc` | passed / passed | asserts 3, raises 0, fail 0; delegated: `_no_logger_state_left_behind` fail 1 | `tests/detectors/test_corp_ner.py::test_nfc_offsets_map_back_to_the_original_string`, `::test_span_over_a_single_composed_char_covers_both_code_points` | A self-check of test constants (`_NFC` / `_DECOMPOSED` lengths and text), no `src/` line. The survivors run the detector on `_DECOMPOSED` and assert the original-string offsets and NFC texts the premise implies, so they fail whenever the fixture stops diverging. The candidate's `(16, 14)` / `_NFC ==` pins are fixture trivia with no production consequence: a fixture that keeps the divergence with extra characters passes the survivors and fails only the candidate; the injection is test-side, accepted as gate-5 evidence (rev 17 (3)). | Test-side (no `src/` line to mutate): `tests/detectors/test_corp_ner.py:33` `_DECOMPOSED` → the precomposed string: the survivors fail `:94` and `:107`; `Корол{_E_DIAERESIS}в` → `Королёв`: `:94` | auto-review (pending) |
| Task 7 | `tests/detectors/test_corp_ner.py::test_finding_is_the_shared_dataclass` | passed / passed | asserts 1, raises 0, fail 0; delegated: `_no_logger_state_left_behind` fail 1 | `tests/detectors/test_corp_ner.py::test_nfc_offsets_map_back_to_the_original_string` (rev 17) | Constructs `detectors.base.Finding` and reads `.label`: the generated `__init__`, no branch, no `src/` line beyond import. The survivor builds `Finding` on the production path (`corp_ner.py:217-223`) and asserts `.label` with the offsets and `.text`. Not a policy default. Coverage of the file set equal (`detectors/base.py` 11 / 0). Residual edit: `Finding` leaves the `:23` import. | `src/corp_llm_gateway/detectors/corp_ner.py:219` `label=label,` → `label="LOCATION",`: the survivor fails `test_corp_ner.py:88` (after); required | auto-review (pending) |
| Task 7 | `tests/audit/test_profile_ids.py::test_event_defaults_are_empty` | passed / passed | asserts 2, raises 0, fail 0; delegated: `_no_logger_state_left_behind` fail 1 | `tests/audit/test_profile_ids.py::test_profile_ids_absent_when_empty` | Same input `_base_event()`. The survivor emits it through `AuditLogger` and asserts `profile_ids` and `jurisdiction` absent, which any non-empty / non-`None` default breaks. The candidate also pinned the default's type (`()`); a falsy non-tuple default passes the survivor and changes no record. | `src/corp_llm_gateway/audit/event.py:47` `= ()` → `= ("core",)`: the survivor fails `:67` (after: `:55`); `:48` `= None` → `= "ru"`: `:68`; required (the survivor runs `AuditLogger.emit`) | auto-review (pending) |
| Task 7 | `tests/audit/test_profile_ids.py::test_event_carries_profile_ids_and_jurisdiction` | passed / passed | asserts 2, raises 0, fail 0; delegated: `_no_logger_state_left_behind` fail 1 | `tests/audit/test_profile_ids.py::test_profile_ids_emitted_when_set`, `::test_jurisdiction_emitted_when_set` | A dataclass field read-back, no branch. The survivors build the same `AuditEvent` with set `profile_ids` (two-element and one-element tuples) and `jurisdiction="ru"` and assert the emitted record's values; the tuple strings are opaque (rev 16 (3)). The record holds `list(event.profile_ids)`, so the attribute's type is not pinned. | `event.py:47` → `field(default=(), init=False)`: both survivors fail (`unexpected keyword argument 'profile_ids'`); `:48` → `field(default=None, init=False)`: `test_jurisdiction_emitted_when_set` fails; required | auto-review (pending) |
| Task 7 | `tests/healthz/test_checks.py::test_status_is_immutable_dataclass` | passed / passed | asserts 0, raises 1, fail 0; delegated: `_no_logger_state_left_behind` fail 1 | none — trivial (restates `frozen=True`) | `HealthStatus` is a per-call value object: `src/` holds no shared instance and never assigns its fields. With `frozen=True` removed only this test fails. Not a policy default. Coverage equal (`healthz/checks.py` 87 / 17). | n/a (no survivor; coverage equal: `healthz/checks.py`); `checks.py:11` → `@dataclass` fails only this test | auto-review (pending) |

### The ledger column spec and a deletion with no survivor

`deleted-tests.md`'s header says a row is complete only when "the surviving test(s) have the same inputs, select the
same production path, …" and, for a disputed replacement, the survivor failed a mutation (gate 5); "How to fill a
row" defines outcome, checks, note and injection, and nothing for a missing survivor. **So at phase 1 the spec
did not admit `none — trivial (<reason>)`**, and phase 1 proposed the rows for #3 and #6 that way, pending a rev 17
ruling (Q1). Rev 17 ruled: #3 names the survivor `test_nfc_offsets_map_back_to_the_original_string`; #6 stays
`none — trivial`, admitted by the header bullet a40c4cf added (a trivial test, not a policy default, with
`n/a (no survivor; coverage equal: <module>)` as fault injection, when the coverage gate shows no dropped line or
arc). #6 is the ledger's only row without a survivor.

## Noted for Task 10 / a strengthening PR

Not deletions here:

- **Vacuous, kept** (no survivor on the same input): the three F2 cases of
  `test_streaming.py::test_stream_does_not_raise_and_emits_output`; `::test_flush_called_twice_does_not_raise`
  (could assert `second == []`); `::test_feed_after_complete_stream_is_safe`;
  `test_log_hygiene.py::test_pre_call_request_id_stable_across_calls_on_same_data` (runs `pre_call` once; the name
  promises stability across calls); `route_gate/test_desanitize_stream.py::test_post_call_stream_malformed_data_line_does_not_raise`,
  `::test_post_call_stream_mixed_bytes_and_dict_chunks`.
- `litellm_hook/test_fail_policy.py::test_pre_call_corp_llm_down_on_system_fails_closed_503` never reaches the
  system-field site: the oracle fails on `message_index=0` first. The system site (`litellm_hook.py:1392`) is
  covered by the must-keep `invariants/test_no_originals_leak.py::test_corp_llm_failed_prompt_field_log_contains_no_raw_content`.
  And `::test_pre_call_corp_llm_down_fails_closed_503` (`:292`, other content) is a strict subset of it — out of
  Task 7's scope.
- `test_log_hygiene.py::test_pre_call_logs_sanitize_done_per_block` does not count one line per block;
  `test_bootstrap.py::test_oracle_disabled_logs_one_info_at_build_time` neither counts the line nor checks INFO.
- The Task 5b weak asserts (5 tests) and `test_config.py::_isolate_from_file` (what a fix takes: Set D).
- The Task 6a weak asserts D7 / B20 / B5 / D38.
- The Task 6b notes: the TTL bounds of `test_issues_token_with_default_ttl` / `test_custom_ttl_respected`, the
  `isinstance(pg_store, …)` asserts, `_info`'s unused keywords.
- `test_local_first.py::test_oracle_still_called_with_local_pass_enabled` vs
  `test_oracle_trigger.py::test_default_no_gazetteer_with_local_detectors_oracle_called`: near-twins (other text,
  other local findings).

**Noted for Task 8:** test-order pollution at `152e57a`: `cli/test_admin.py::test_extensions_health_unhealthy_fail_closed_exits_nonzero`
fails when it runs after `tests/extensions` (`pytest tests/extensions tests/cli/test_admin.py::…`: 1 failed / 43 passed;
alone it passes). The alphabetical whole-suite order (`cli` before `extensions`) hides it; random order or xdist would not.

## Open questions / decisions by rule

Decided by the rules:

- The universe is 97 functions: 13 / 36 / 28 / 20 (A / B / C / D), counted against baseline ids. The sizing's
  1,737 and 33 / 54 counted moved must-keep tests as candidates.
- No log assert is deleted: every (b) and (c) assert sits in a test kept for other asserts or is the only probe of
  its behaviour; the 4 negative ones are reviewed `behaviour` rows with no survivor (K1-K3).
- No `call_count` assert is an implementation detail; all 37 keep.
- 6 deletions: 5 with survivors, 1 with none (#6). That is the Task 4 twin (#1) and 5 trivial deletions: #2 and #4
  with survivors on the same input, #5 with survivors on the record path, #3 with the survivor rev 17 named.
  Coverage equal; every injection fails the survivor where one is named.
- 23 Set C keeps: policy default, a branch into `src/`, a fail-policy or security property, or a registry
  relation.

For the reviewer (rev 17):

- **Q1 — deletions with no survivor (#3, #6).** The ledger spec requires a survivor. (A) add one bullet to "How to
  fill a row": a trivial test (no branch, not a policy default) may have `none — trivial (<reason>)` as survivor,
  with "coverage equal: <module>" in place of an injection; or (B) keep both (93 keep, 4 delete).
  Proposed: A.
- **Q2 — a test-side fault injection (#2).** Deletion #2 checks test constants, so its gate-5 evidence mutates the
  fixture (`test_corp_ner.py:33`), not `src/`. Accept that as the injection, or keep the test.
- **Q3 — the type residual (#4, #5).** The survivors check the emitted record; the candidates also pin the
  attribute's type (`()` / a tuple). Proposed: a superset survivor per rev 16 (3) — no record or sink sees the
  difference (I3r: 188 of 189 `tests/audit/` pass). If the type is a contract, both stay.
- **Q4 — the (b) rewrite PR.** Proposed: one small PR, 6 asserts in 4 tests (the doc-cited log forms:
  `error_code=E_CORP_LLM_DOWN`, `litellm_pre_call_system_oversize_delivered … field=system`,
  `litellm_egress_blocked … block_reason=dlp:canary`, `cache_a_disabled reason=policy_fingerprint_failed`), with
  Task 1b-style `(changed: …)` rows. Alternative: none (keep all 14 (b) asserts as they are).
- **Q5 — case deletions.** The dry run shows a removed `pytest.param` as a *changed* fold test (`body_hash`,
  `case_data`, `helpers`), not a missing id. Rule whether a prune PR may carry that. Separately:
  `[second_text_block]` vs `test_sse_two_text_blocks_no_runtime_error` (`:562`), a stronger test on the same
  event shape with another mapping and other delta texts. Kept by the literal input rule; a delete would need Q5 = yes and the mapping read as
  opaque.
- **Q6 — `profiles/test_registry.py::test_corp_ner_is_registered_for_profile_bundles`** is a strict subset of
  `detectors/test_detector_contract.py::test_registry_covers_every_known_detector` (same dict). Kept by analogy with
  rev 14 (1); a delete would cite that survivor and mutate `profiles/registry.py`'s `corp_ner` entry.
- **Q7 — `extensions/test_registry.py::test_module_exposes_singleton_and_api_version`** kept: `docs/extending.md:192`
  cites `EXTENSION_API_VERSION = "1"` and no other test pins it. Delete if the doc's value is not a contract.
- **Q8 — frozen dataclasses.** `HealthStatus` (#6) is deleted; `ExtensionSpec` / `ProviderSpec` frozenness is kept
  as a fail-policy / provider-guard property. Confirm the line.
- **Q9 — `test_orchestrator.py:1302`** is a reviewed `behaviour` row, but its assert message says "M1-14: no user
  content in logs". Reclassified as `security`, its owner would become must-keep through `security_node_ids`.
  Nothing changes in this PR (the test is kept). Not treated as a stop condition: by `must_keep.py`'s rules, which
  read the reviewed class, the owner is correctly not must-keep.
- **Q10 — cosmetic leftovers.** The `# AuditEvent carries the fields` banner (`test_profile_ids.py:44`) with no
  test under it, and `_NFC` (`test_corp_ner.py:34`) with no code reference. Proposed: leave both (rev 16's residual
  scope is imports and EOF lines); removing either changes no hash.
- **Q11 — the `Finding` import edit** is in-line (`BatchPIIDetector, Finding` → `BatchPIIDetector`), not a whole-line
  removal like rev 16 (6). Proposed: the same category.
- **Q12 — `moves.json` −1.** The brief predicted `moves.json` unchanged; deleting a moved test needs its `ids` key
  removed (Task 3b removed 16). Proposed: as Task 3b.

# Task 5b config prune audit (phase 1)

The audit of plan `docs/plans/20260926-test-suite-refactor-and-prune.md` (rev 13, local only), Task 5b:
config-test pruning, a prune PR over `tests/test_settings.py`, `tests/test_config.py`,
`tests/test_bootstrap.py` and `tests/test_bootstrap_edges.py`. Phase 1 decides keep / delete for
every non-must-keep test function of the four files and changes no test. Phase 2 (after review)
makes the deletions, the [deleted-tests.md](deleted-tests.md) rows, the fault injections and the
manifest regeneration. The gates are in [must-keep.md](must-keep.md). Tree: `release/1.0.x` at
`f066b10`.

**Decision: 5 delete, 149 keep.** All five deletions are criterion-(3) intra-layer twins (rule B).
Rule A (per-key default tests) deletes nothing: every default test in the universe either reads a
sensitive key or has no survivor on the same path. After phase 2 the four files hold 109 / 27 / 77
/ 18 functions (settings / config / bootstrap / edges; 110 / 28 / 80 / 18 today) and 228 / 41 / 110 / 25
collected ids in both environments (229 / 42 / 113 / 25 today).

## The universe: 154 functions

| file | functions at `f066b10` | must-keep functions | universe |
|---|---|---|---|
| `tests/test_settings.py` | 110 | 72 | 38 |
| `tests/test_config.py` | 28 | 1 | 27 |
| `tests/test_bootstrap.py` | 80 | 9 | 71 |
| `tests/test_bootstrap_edges.py` | 18 | 0 (no shard) | 18 |
| **total** | **236** | **82** | **154** |

Functions: module-level `def test*` / `async def test*` by `ast` (the two classes in
`test_bootstrap.py`, `_RecordingCorpNerDetector` and `_RecordingMetrics`, are helpers). Must-keep:
`tests/_gates/must_keep.py`'s `function_ids()` gives 72 / 1 / 9 / 0, which equals the
`must_keep/_root__test_*.txt` shards once the `[param]` suffix is stripped, duplicates are dropped
and each shard's two `#` header lines are left out. **The brief first said 148** (74 / 3 / 11
must-keep): it counted each shard's two header lines as ids. Corrected to 154 before the audit
began; no id was left out. `moves.json` has no entry for the four files, so every current id is its
baseline id, except Task 5a's two fold tests (`test_settings.py::test_validate_rejects_an_unknown_or_malformed_choice`,
`test_bootstrap_edges.py::test_auth_provider_misconfig_fails_fast_at_build`), whose baseline is
their current id. Every id's checks come from `tests/_manifests/baseline_checks/_root__<file>.json`
and its outcomes from `expected_outcomes.{minimal,full}.json`. All 154 passed in both environments,
except `test_bootstrap.py::test_build_guardrail_selects_postgres_token_store` (skipped in minimal,
"PostgresTokenStore requires the 'postgres' extra"; passed in full).

## Rules applied

- **(A) Per-key default test** (plan Task 5b item 1): a test whose asserts read the resolved default
  of one registered key (or of one flag the builder derives from it) with that key unset, through
  `config.get` / a `settings` resolver / a `build_*` attribute, and nothing else. A test that sets
  the key is not a default test. A test that asserts two or more keys' defaults is an existing
  grouping (plan Task 5a item 3): kept, not split. Each default test gets a key and a sensitivity
  class (`policy`, `credential`, `TLS`, `fail-policy`, `route-gate`, `capacity`, `none`) with the
  `settings.py` `KEYS` line that shows what the key governs. Any class but `none` ⇒ **keep**,
  whatever the survivor. A `none` default is deletable only with a named survivor that asserts the
  same default for the same key through the same resolver ("the registry declares it" is not one).
- **(B) Criterion (3), intra-layer twin** (plan rev 10), over the whole universe: the candidate's
  checks are a strict subset of a survivor's on the same input (same env / file values, same builder
  or resolver, same predicates, negative checks and parameter cases). The survivor may be any test
  (must-keep or not) in the four files or elsewhere, and must pass wherever the candidate passed.
  A survivor weaker on any of those means keep. A prune deletes whole functions: a parametrised
  function with one case covered elsewhere is kept.
- **Kept by the plan** (named so no one re-asks; read from the bodies): the KEYS-registry
  consistency tests (`test_settings.py` `test_all_keys_is_nonempty_and_unique`,
  `test_all_keys_contains_core_and_new_knobs`, `test_secret_flag_marks_credentials`,
  `test_corp_ner_keys_are_registered`, `test_master_key_is_registered_as_a_secret_so_config_check_redacts_it`,
  `test_example_toml_documents_every_key`; the must-keep `test_issuance_keys_are_registered`,
  `test_the_store_timeout_key_is_registered_with_its_default`, `test_capacity_keys_are_registered`
  are survivors outside the universe), the resolution-order tests (`test_config.py:17-75`, seven
  functions; `test_settings.py` `test_validate_resolves_endpoint_from_file_with_env_cleared`,
  `test_env_overrides_file_through_the_chain`, `test_existing_accessors_unchanged`; and by body
  `test_config.py` `test_require_ner_reads_from_file`, `test_get_table_does_not_break_scalar_env_override`,
  `test_auth_factory_reads_from_config_file`, `test_bootstrap.py` `test_backends_resolve_from_config_file_without_env`,
  `test_env_wins_over_the_extensions_detector_table`, `test_bootstrap_edges.py`
  `test_config_file_values_win_over_decoy_env_aliases`; the must-keep
  `test_issuance_team_map_keeps_config_file_order` and `test_issuance_bounds_resolve_from_the_config_file`
  are survivors), and `tests/docs/test_docs_pins.py` (outside the universe).
- Fault injection: required when the survivor runs another production path; for a same-file /
  same-builder survivor rev 10's waiver applies, and the row still names the `src/` line. All five
  were **dry-run on the scratch clone** (below), both venvs; phase 2 runs them again on the checkout.

Columns: `checks` is the shard's line (asserts / raises / fail; parametrize cases, loops,
delegated helpers). Every test also carries the delegated `_no_logger_state_left_behind` fail 1
(conftest autouse), not repeated. `def line` is the `def` statement at `f066b10`. In the
`decision` column the keep reason is one of: registry / resolution-order (plan-kept); sensitive
default (class ≠ none); `none`-class default, no same-path survivor; near-twin rejected; not a
default test, no twin; Task 5a fold test; default grouping (≥ 2 keys) kept. In `class`, "other (…)"
names the sensitivity area a non-default test touches, for orientation only.

## Per-file tables

### `tests/test_settings.py` — 38 ids

| node id | def line at f066b10 | checks | class | decision | survivor node id(s) | semantic note | fault injection |
|---|---|---|---|---|---|---|---|
| `test_all_keys_is_nonempty_and_unique` | :37 | a2 r0 f0 | registry | keep (registry / resolution-order (plan-kept)) | — | KEYS-registry consistency test the plan keeps (`all_keys()` non-empty, no duplicate). | — |
| `test_all_keys_contains_core_and_new_knobs` | :43 | a1 r0 f0 | registry | keep (registry / resolution-order (plan-kept)) | — | KEYS-registry consistency test the plan keeps (11 named keys registered). | — |
| `test_secret_flag_marks_credentials` | :60 | a3 r0 f0 | registry | keep (registry / resolution-order (plan-kept)) | — | KEYS-registry consistency (the `secret` flags `config check` redacts by); one of "their like" in the plan's kept list; credential. | — |
| `test_validate_ok_when_endpoint_set` | :69 | a2 r0 f0 | other | keep (near-twin rejected (no survivor as strong)) | — | No survivor as strong: `:549`'s first block has the same resolver and predicate `isinstance(…, Settings)` on another endpoint literal (`https://x/v1`), and no test asserts `result["CORP_LLM_ENDPOINT"]` from env through `validate()` (`:481` reads it from the file). | — |
| `test_validate_hard_fails_on_missing_endpoint` | :76 | a2 r1 f0; loops 1 | other (required-key refusal; fail-policy) | keep (near-twin rejected (no survivor as strong)) | — | Endpoint unset under the oracle's default-on → refusal; asserts the message and `.problems`. Near: `:83` adds `CORP_LLM_OVERSIZE_POLICY=banana` (another input), `:172` sets the oracle flag (another input). | — |
| `test_validate_reports_every_problem_at_once` | :83 | a2 r1 f0 | other | keep (not a default test, no twin) | — | Two problems in one `ConfigError`; no other test makes both. `:83` vs the E2 `[oversize_policy]` case is a Task 5a near-twin (`banana`, no endpoint), not re-litigated. | — |
| `test_corp_ner_keys_are_registered` | :98 | a2 r0 f0; loops 1 | registry | keep (registry / resolution-order (plan-kept)) | — | KEYS-registry consistency test the plan names. | — |
| `test_corp_ner_defaults_leave_existing_deploys_untouched` | :111 | a5 r0 f0 | default grouping: `CORP_NER_ENABLED`, `CORP_NER_TIMEOUT_S`, `CORP_NER_MAX_TEXTS`, `CORP_NER_MAX_INPUT_CHARS`, `CORP_NER_ENDPOINT`; policy + capacity | keep (default grouping (≥ 2 keys) kept) | — | Five keys' defaults in one test: an existing grouping (plan Task 5a item 3, kept, not split); every key is policy or capacity. | — |
| `test_validate_requires_corp_ner_endpoint_when_enabled` | :124 | a2 r1 f0; parametrize 'truthy' ×4; loops 1 | other (sets the key; fail-policy) | keep (not a default test, no twin) | — | Truthy spellings ×4 + the recovery. Near: `test_bootstrap.py::test_corp_ner_enabled_without_endpoint_fails_closed` (`build_guardrail()`, another resolver, `1` only). | — |
| `test_validate_ignores_corp_ner_endpoint_when_disabled` | :139 | a1 r0 f0 | other | keep (not a default test, no twin) | — | File-sourced `CORP_NER_ENABLED = false`; no other test has this file. | — |
| `test_validate_ok_with_oracle_disabled_and_no_endpoint` | :147 | a3 r0 f0 | other (sets the oracle key) | keep (not a default test, no twin) | — | Survivor of `:187[0]`'s checks (see that row); no test makes its three asserts on this input. | — |
| `test_validate_fails_when_oracle_and_local_first_both_disabled` | :157 | a3 r1 f0 | other | keep (not a default test, no twin) | — | Negative `"CORP_LLM_ENDPOINT" not in joined`. Near: `test_bootstrap.py:753` (`build_guardrail()`, another resolver). | — |
| `test_validate_still_requires_endpoint_when_oracle_enabled` | :172 | a2 r1 f0; parametrize 'truthy' ×4; loops 1 | other (fail-policy) | keep (not a default test, no twin) | — | Truthy spellings ×4 + the recovery; no other test on `validate()`. | — |
| `test_validate_lenient_falsy_forms_disable_oracle` | :187 | a1 r0 f0; parametrize 'falsy' ×3 | other (sets the key) | keep (near-twin rejected (no survivor as strong)) | — | Case `[0]` is a subset of `:147` (same input, `:147` also asserts `flag("CORP_LLM_ORACLE_ENABLED") is False`), but `[false]` / `[off]` have no survivor on `validate()` (`test_bootstrap.py:360` runs `build_guardrail()`); a prune deletes whole functions, never a case. | — |
| `test_validate_ok_unless_both_forward_auth_flags_are_on` | :211 | a1 r0 f0; parametrize 'chatgpt,anthropic' ×5 | other (credential) | keep (not a default test, no twin) | — | Five legal pairs; no other `validate()` test on them. | — |
| `test_validate_rejects_both_forward_auth_flags_with_the_shared_message` | :220 | a2 r1 f0 | other (credential) | keep (not a default test, no twin) | — | `"1"` pair + message identity + `"v2"`; `:234` uses other spellings and only `match=`. | — |
| `test_validate_rejects_lenient_truthy_spellings_of_both_flags` | :234 | a0 r1 f0; parametrize 'truthy' ×4 | other (credential) | keep (not a default test, no twin) | — | Four spellings, none `"1"`; no survivor. | — |
| `test_forward_auth_conflict_is_the_single_shared_rule` | :245 | a4 r0 f0 | other | keep (not a default test, no twin) | — | The pure rule over all four pairs; nothing else calls `forward_auth_conflict` directly. | — |
| `test_validate_rejects_a_master_key_next_to_the_anthropic_bridge` | :257 | a3 r1 f0 | other (credential) | keep (not a default test, no twin) | — | Carries the negative `"master-key-fixture" not in str(exc.value)`. | — |
| `test_validate_rejects_a_master_key_next_to_the_chatgpt_bridge` | :274 | a0 r1 f0 | other (credential) | keep (not a default test, no twin) | — | ChatGPT bridge `on`; no other `validate()` test on it. | — |
| `test_validate_rejects_a_blank_master_key_next_to_a_bridge` | :286 | a1 r1 f0; parametrize 'master_key' ×2 | other (credential) | keep (not a default test, no twin) | — | Blank / whitespace master key ×2; no survivor. | — |
| `test_validate_accepts_an_absent_master_key_next_to_a_bridge` | :303 | a1 r0 f0 | other (credential) | keep (near-twin rejected (no survivor as strong)) | — | Near: `:211[0-1]` sets `CORP_LLM_FORWARD_ANTHROPIC_AUTH=1` and also `CORP_LLM_FORWARD_CHATGPT_AUTH=0` (the same resolved `False`, but a set key is another input). Task 5a N3 fold-shaped, out of scope. | — |
| `test_validate_allows_a_blank_master_key_when_no_bridge_is_on` | :313 | a1 r0 f0; parametrize 'master_key' ×2 | other (credential) | keep (not a default test, no twin) | — | No survivor on this input. N3, out of scope. | — |
| `test_validate_allows_a_master_key_when_no_bridge_is_on` | :322 | a1 r0 f0 | other (credential) | keep (not a default test, no twin) | — | No survivor on this input. N3, out of scope. | — |
| `test_master_key_is_registered_as_a_secret_so_config_check_redacts_it` | :332 | a2 r0 f0 | registry | keep (registry / resolution-order (plan-kept)) | — | KEYS-registry consistency test the plan names. | — |
| `test_master_key_conflict_is_the_single_shared_rule` | :337 | a6 r0 f0 | other | keep (not a default test, no twin) | — | The pure rule, six cases; nothing else calls `master_key_conflict` directly. | — |
| `test_validate_rejects_an_unknown_or_malformed_choice` | :393 | a0 r1 f0; parametrize ('key', 'value', 'match') ×8 | other (Task 5a fold E2) | keep (Task 5a fold test) | — | Survivor of the four E2 members three commits ago; deleting it needs a survivor per case (8) and none exists. | — |
| `test_validate_accepts_route_gate_extras_and_resolves_them` | :412 | a3 r0 f0; loops 1 | other (route-gate) | keep (not a default test, no twin) | — | The only test that resolves extras into `PASSTHROUGH` entries. | — |
| `test_validate_accepts_valid_choices` | :434 | a1 r0 f0 | other | keep (not a default test, no twin) | — | `chunk` + `stdout`; no other test on this input. | — |
| `test_bearer_provider_requires_token` | :444 | a1 r1 f0 | other (credential) | keep (not a default test, no twin) | — | Refusal + recovery on `validate()`. Near: `test_bootstrap_edges.py::test_auth_provider_misconfig_fails_fast_at_build[bearer_provider_without_token]` (`build_guardrail()`, another resolver, no recovery). | — |
| `test_langfuse_sink_requires_keys` | :454 | a4 r1 f0 | other (credential) | keep (not a default test, no twin) | — | Three missing keys + recovery; no survivor. | — |
| `test_noop_provider_needs_no_credentials` | :470 | a1 r0 f0 | twin | **delete** | `tests/test_settings.py::test_validate_uses_pydantic_when_present` | Criterion (3), intra-layer twin, same file. The survivor's first block is this test's body: the same `hermetic` + `monkeypatch` fixtures, the same single `monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")`, the same `config.validate()` and the same assert `isinstance(config.validate(), Settings)`; it then adds `CORP_LLM_AUTH_PROVIDER=bogus` → `pytest.raises(ConfigError, match="CORP_LLM_AUTH_PROVIDER")`. Its `pytest.importorskip("pydantic")` does not skip: pydantic is in both recipes and the survivor passed in both ledgers. The deleted test's comment ("default provider is noop") is a default reading, but no assert reads `CORP_LLM_AUTH_PROVIDER`'s value, so it is not a criterion-(A) default test (reviewer question Q2). | n/a (rev 10 same-path waiver: same file, same resolver `config.validate()`); mutation `src/corp_llm_gateway/settings.py:184` `default="noop"` → `default="bearer"`, dry-run on the scratch clone: the deleted test and the survivor both raise `ConfigError` ("CORP_LLM_BEARER_TOKEN: required when CORP_LLM_AUTH_PROVIDER=bearer") at the survivor's `:554` `assert isinstance(config.validate(), Settings)`, minimal and full |
| `test_validate_resolves_endpoint_from_file_with_env_cleared` | :481 | a1 r0 f0 | resolution-order | keep (registry / resolution-order (plan-kept)) | — | Plan-kept resolution-order test. | — |
| `test_env_overrides_file_through_the_chain` | :489 | a0 r1 f0 | resolution-order | keep (registry / resolution-order (plan-kept)) | — | Plan-kept resolution-order test. | — |
| `test_example_toml_documents_every_key` | :501 | a1 r0 f0; loops 1 | registry | keep (registry / resolution-order (plan-kept)) | — | Plan-kept registry test (`config.example.toml` has every key). | — |
| `test_existing_accessors_unchanged` | :514 | a6 r0 f0 | resolution-order + default grouping (`CORP_LLM_OVERSIZE_POLICY`, `SSL_VERIFY`) | keep (registry / resolution-order (plan-kept)) | — | Six accessors through the chain (env over file, file fallback, table, `require_ner` from the file) plus two defaults in one test: resolution-order by body, an existing grouping. | — |
| `test_settings_flag_helper` | :537 | a2 r0 f0 | other | keep (not a default test, no twin) | — | `flag()` over `REQUIRE_NER=1` + `GAZETTEER=0`; no survivor on this input. | — |
| `test_validate_uses_pydantic_when_present` | :549 | a1 r1 f0 | other | keep (not a default test, no twin) | — | Survivor of `:470`. Near: E2 `[auth_provider]` (`kerberos`, its own input). | — |

### `tests/test_config.py` — 27 ids

| node id | def line at f066b10 | checks | class | decision | survivor node id(s) | semantic note | fault injection |
|---|---|---|---|---|---|---|---|
| `test_env_wins_over_file` | :17 | a1 r0 f0 | resolution-order | keep (registry / resolution-order (plan-kept)) | — | Plan-kept (`test_config.py:17-75`). | — |
| `test_file_used_when_env_missing` | :26 | a1 r0 f0 | resolution-order | keep (registry / resolution-order (plan-kept)) | — | Plan-kept (`test_config.py:17-75`). | — |
| `test_default_used_when_neither_source_provides_value` | :35 | a2 r0 f0 | resolution-order | keep (registry / resolution-order (plan-kept)) | — | Plan-kept (`test_config.py:17-75`); the caller default of `config.get`, not a registered key's default. | — |
| `test_missing_config_file_is_silent` | :47 | a1 r0 f0 | resolution-order | keep (registry / resolution-order (plan-kept)) | — | Plan-kept (`test_config.py:17-75`). | — |
| `test_get_required_raises_when_missing` | :54 | a0 r1 f0 | resolution-order | keep (registry / resolution-order (plan-kept)) | — | Plan-kept (`test_config.py:17-75`). | — |
| `test_get_required_uses_file_when_env_missing` | :64 | a1 r0 f0 | resolution-order | keep (registry / resolution-order (plan-kept)) | — | Plan-kept (`test_config.py:17-75`). | — |
| `test_non_string_values_are_stringified` | :75 | a1 r0 f0 | resolution-order | keep (registry / resolution-order (plan-kept)) | — | Plan-kept (`test_config.py:17-75`). | — |
| `test_corp_llm_verify_ca_bundle_set` | :84 | a1 r0 f0 | other (TLS) | keep (near-twin rejected (no survivor as strong)) | — | Bundle set, `SSL_VERIFY` unset. Near: `:91` sets `SSL_VERIFY=false`, `:130` also `CORP_ENV=prod` — the same `ca_bundle` branch on another input. | — |
| `test_corp_llm_verify_ca_bundle_takes_precedence_over_ssl_verify` | :91 | a1 r0 f0 | other (TLS) | keep (near-twin rejected (no survivor as strong)) | — | Near: `:130` adds `CORP_ENV=prod` and file isolation; another input. | — |
| `test_corp_llm_verify_ssl_verify_false` | :100 | a1 r0 f0 | twin (TLS) | **delete** | `tests/test_config.py::test_corp_llm_verify_demo_allows_ssl_verify_false` | Criterion (3), intra-layer twin, same file. Same env values (`CORP_LLM_CA_BUNDLE` deleted, `SSL_VERIFY=false`), same resolver `config.corp_llm_verify()`, same assert `is False`. The survivor also pins the rest of the input the result depends on: `_isolate_from_file` (`CORP_LLM_GATEWAY_CONFIG_FILE=/nonexistent/config.toml` + `reset_cache()`) and `delenv("CORP_ENV")`; this test reads `CORP_ENV` and the TOML from the process (the file has no hermetic fixture), so under `CORP_ENV=prod` it would raise. Same autouse `_reset_config_cache`. Not a criterion-(A) default test (it sets `SSL_VERIFY`); TLS-related, so reviewer question Q3. | n/a (rev 10 same-path waiver: same file, same resolver); mutation `src/corp_llm_gateway/config.py:185` `verify = (get("SSL_VERIFY", "true") or "true").lower() != "false"` → `verify = True`, dry-run on the scratch clone: the survivor failed `test_config.py:146` `assert config.corp_llm_verify() is False` (`True is False`), the deleted test failed its `:104`; minimal and full |
| `test_corp_llm_verify_prod_refuses_ssl_verify_false` | :119 | a0 r1 f0 | other (TLS, fail-policy) | keep (not a default test, no twin) | — | The F9 refusal; no survivor. | — |
| `test_corp_llm_verify_prod_allows_ca_bundle` | :130 | a1 r0 f0 | other (TLS) | keep (not a default test, no twin) | — | Prod + bundle + `SSL_VERIFY=false`; no survivor. | — |
| `test_corp_llm_verify_demo_allows_ssl_verify_false` | :140 | a1 r0 f0 | other (TLS) | keep (not a default test, no twin) | — | Survivor of `:100`. | — |
| `test_corp_llm_verify_prod_true_when_verify_on` | :149 | a1 r0 f0 | default: `SSL_VERIFY` (with `CORP_ENV=prod`), TLS | keep (sensitive default (class ≠ none)) | — | TLS class (`settings.py:182` "'false' disables corp-LLM TLS verification") → keep. Near: must-keep `:107` (no `CORP_ENV`). | — |
| `test_is_prod_true` | :159 | a1 r0 f0; parametrize 'value' ×4 | other | keep (not a default test, no twin) | — | Four prod spellings; `is_prod()` is called directly nowhere else in the suite. | — |
| `test_is_prod_false` | :166 | a1 r0 f0; parametrize 'value' ×4 | other | keep (not a default test, no twin) | — | Four non-prod values (one empty-but-set); no survivor. | — |
| `test_require_ner_defaults_to_false` | :172 | a1 r0 f0 | default: `CORP_LLM_REQUIRE_NER`, fail-policy | keep (sensitive default (class ≠ none)) | — | fail-policy class (`settings.py:141` "fail closed when NER absent (F2)"; `config.py:154-168`) → keep. | — |
| `test_require_ner_truthy_values` | :179 | a1 r0 f0; parametrize 'value' ×5 | other (fail-policy) | keep (not a default test, no twin) | — | Five truthy spellings; `require_ner()` from env is called nowhere else. | — |
| `test_require_ner_falsey_values` | :185 | a1 r0 f0; parametrize 'value' ×5 | other (fail-policy) | keep (not a default test, no twin) | — | Five falsy spellings incl. empty; no survivor. | — |
| `test_require_ner_reads_from_file` | :190 | a1 r0 f0 | resolution-order | keep (registry / resolution-order (plan-kept)) | — | File fallback through `require_ner()` — a resolution-order test by body. Near: `test_settings.py:514` (the same line in a file with three more keys and a table; hermetic instead of `delenv`). | — |
| `test_get_table_reads_nested_table` | :198 | a1 r0 f0 | other | keep (not a default test, no twin) | — | `get_table` nested read; each `get_table` test takes another branch of `config.py:101-106`. | — |
| `test_get_table_dotted_prefix_descends` | :210 | a2 r0 f0 | other | keep (not a default test, no twin) | — | Dotted descent; no survivor. | — |
| `test_get_table_missing_returns_empty_dict` | :221 | a2 r0 f0 | other | keep (not a default test, no twin) | — | Missing key → `{}` at two depths; `:238` uses the same file but a scalar path (another branch). | — |
| `test_get_table_no_file_returns_empty_dict` | :232 | a1 r0 f0 | other | keep (not a default test, no twin) | — | No file → `{}`; no survivor. | — |
| `test_get_table_on_scalar_path_returns_empty_dict` | :238 | a1 r0 f0 | other | keep (not a default test, no twin) | — | Scalar at the path → `{}`; no survivor. | — |
| `test_get_table_does_not_break_scalar_env_override` | :248 | a2 r0 f0 | resolution-order | keep (registry / resolution-order (plan-kept)) | — | Env over file with a table in the file; resolution-order by body. | — |
| `test_auth_factory_reads_from_config_file` | :264 | a1 r0 f0 | resolution-order (credential) | keep (registry / resolution-order (plan-kept)) | — | File fallback through the auth factory; resolution-order by body; no survivor. | — |

### `tests/test_bootstrap.py` — 71 ids

| node id | def line at f066b10 | checks | class | decision | survivor node id(s) | semantic note | fault injection |
|---|---|---|---|---|---|---|---|
| `test_build_guardrail_returns_guardrail_with_in_memory_backends` | :52 | a3 r0 f0 | default grouping: `REDIS_URL`, `CORP_LLM_PG_DSN`; credential | keep (default grouping (≥ 2 keys) kept) | — | Two keys' defaults + the guardrail type in one test (Task 5a: extra check); an existing grouping. | — |
| `test_module_level_guardrail_is_importable_instance` | :61 | a1 r0 f0 | twin | **delete** | `tests/test_bootstrap.py::test_guardrail_attribute_builds_once_and_caches` | Criterion (3), intra-layer twin, same file. The survivor reads the same attribute `bootstrap.guardrail` through the same PEP 562 `__getattr__` (`bootstrap.py:706-717`) and asserts the same `isinstance(…, CorpLlmGuardrail)`, plus build-once / cached identity. Input: the survivor starts from `_guardrail = None` (the first access LiteLLM's `callbacks:` import makes); this test reads whatever `_guardrail` holds when it runs (built here or cached by an earlier test). The survivor wraps `bootstrap.build_guardrail` in a counter that calls the real one, which `__getattr__` looks up as a module global, so the same build runs. Same autouse `_clean_config`. | n/a (rev 10 same-path waiver: same file, same `__getattr__`); mutation `src/corp_llm_gateway/bootstrap.py:717` `return _guardrail` → `return None`, dry-run on the scratch clone: the survivor failed `test_bootstrap.py:94` `assert isinstance(first, CorpLlmGuardrail)`, the deleted test failed its `:63`; minimal and full |
| `test_guardrail_attribute_builds_once_and_caches` | :77 | a5 r0 f0 | other | keep (not a default test, no twin) | — | Survivor of `:61`. | — |
| `test_getattr_raises_for_unknown_attribute` | :97 | a0 r1 f0 | other | keep (not a default test, no twin) | — | The `AttributeError` branch of `__getattr__`; no survivor. | — |
| `test_gateway_version_is_metadata_not_demo_string` | :102 | a2 r0 f0 | other (package metadata, no key) | keep (not a default test, no twin) | — | No other test reads `_audit._gateway_version` from `build_guardrail()`. | — |
| `test_audit_sink_is_stdout` | :109 | a1 r0 f0 | default: `CORP_AUDIT_SINK`, proposed `none` (borderline, Q1) | keep (`none`-class default, no same-path survivor) | — | No survivor on the same path: `tests/audit/test_factory.py::test_get_sink_default_is_stdout` asserts `get_sink()`'s default, not the `build_guardrail()` wiring (`bootstrap.py:684` `sink if sink is not None else get_sink()`); `:958` asserts `StdoutSink` on the demo shim, which passes `sink=StdoutSink()` itself (`_demo_guardrail.py:87`). Coverage would not catch the loss (every build runs `:684`). | — |
| `test_build_guardrail_carries_metrics_exporter_noop_by_default` | :118 | a2 r0 f0 | default: `CORP_METRICS_EXPORTER`, proposed `none` (borderline, Q1) | keep (`none`-class default, no same-path survivor) | — | No single survivor on the same path: `tests/metrics/test_metrics.py::test_get_exporter_default_is_noop` asserts `get_exporter()`'s default, `:137` asserts `guardrail._metrics is get_exporter()`; only the two together make this check. | — |
| `test_build_guardrail_takes_an_explicit_metrics_exporter` | :127 | a1 r0 f0 | other | keep (not a default test, no twin) | — | The `metrics=` kwarg; no survivor. | — |
| `test_build_guardrail_defaults_to_the_shared_exporter` | :137 | a1 r0 f0 | other (kwarg default, no key) | keep (not a default test, no twin) | — | Identity with the process exporter; `:118` asserts the type only. | — |
| `test_the_health_router_is_ready_without_redis_or_postgres` | :199 | a1 r0 f0 | default grouping: `REDIS_URL`, `CORP_LLM_PG_DSN` → readiness; credential | keep (default grouping (≥ 2 keys) kept) | — | Two keys' defaults in one readiness check; no survivor on `/healthz/ready`. | — |
| `test_the_sanitization_probe_reports_a_failing_round_trip` | :218 | a2 r0 f0 | other | keep (not a default test, no twin) | — | The probe's failure branch; no survivor. | — |
| `test_build_guardrail_wraps_orchestrator_in_profile_aware` | :256 | a2 r0 f0 | other | keep (not a default test, no twin) | — | Near: `tests/sanitizer/test_team_config_outage_edges.py:68` asserts `ProfileAwareOrchestrator` on its own `_guardrail` builder, not `build_guardrail()`. | — |
| `test_no_profile_team_passes_through_to_core_unchanged` | :264 | a3 r0 f0 | other (policy) | keep (not a default test, no twin) | — | No survivor. | — |
| `test_team_with_sealed_default_profile_resolves_and_applies` | :276 | a5 r0 f0 | other (policy) | keep (not a default test, no twin) | — | No survivor. | — |
| `test_oracle_disabled_build_guardrail_skips_client_build` | :299 | a2 r0 f0 | twin | **delete** | `tests/test_bootstrap.py::test_oracle_falsy_spellings_disable_oracle_without_endpoint[0]` | Criterion (3), intra-layer twin, same file. The survivor's `[0]` case is this test's body with the literal as its parameter: `monkeypatch.setenv("CORP_LLM_ORACLE_ENABLED", "0")`, the same `_fail_if_called` patch of `bootstrap.build_corp_llm_client`, `bootstrap.build_guardrail()`, and the same two asserts `core._corp_llm is None`, `core._oracle_enabled is False`; its four other cases (`off`, `no`, `false`, `OFF`) add spellings. Same autouse `_clean_config` (`hermetic_gateway_config` clears `CORP_LLM_ENDPOINT`). | n/a (rev 10 same-path waiver: same file, same builder); mutation `src/corp_llm_gateway/bootstrap.py:650` `oracle_enabled = _flag("CORP_LLM_ORACLE_ENABLED")` → `oracle_enabled = True`, dry-run on the scratch clone: all five survivor cases failed at `test_bootstrap.py:369` (`AssertionError: build_corp_llm_client must not run when the oracle is disabled`), the deleted test failed at its `:307`; minimal and full |
| `test_oracle_disabled_logs_one_info_at_build_time` | :318 | a1 r0 f0 | other | keep (not a default test, no twin) | — | The `caplog` INFO check; no survivor. | — |
| `test_oracle_disabled_profiled_team_inner_orchestrator_has_no_client` | :329 | a4 r0 f0 | other (policy) | keep (not a default test, no twin) | — | Inner orchestrator + a sanitize; no survivor. | — |
| `test_oracle_falsy_spellings_disable_oracle_without_endpoint` | :360 | a2 r0 f0; parametrize 'falsy' ×5 | other | keep (not a default test, no twin) | — | Survivor of `:299` (case `[0]`). | — |
| `test_oracle_truthy_spellings_enable_oracle_and_build_client` | :381 | a2 r0 f0; parametrize 'truthy' ×4 | other | keep (not a default test, no twin) | — | Counts the client build; no survivor. | — |
| `test_forward_chatgpt_auth_truthy_spellings_enable_the_flag` | :407 | a1 r0 f0; parametrize 'truthy' ×4 | other (credential) | keep (not a default test, no twin) | — | Task 5a N1, out of scope. Near: `:735[1-0]` also sets `CORP_LLM_FORWARD_ANTHROPIC_AUTH=0`; covers one of four cases. | — |
| `test_forward_chatgpt_auth_falsy_spellings_disable_the_flag` | :418 | a1 r0 f0; parametrize 'falsy' ×5 | other (credential) | keep (not a default test, no twin) | — | Task 5a N2, out of scope. | — |
| `test_forward_chatgpt_auth_unset_defaults_off` | :428 | a1 r0 f0 | default: `CORP_LLM_FORWARD_CHATGPT_AUTH`, credential | keep (sensitive default (class ≠ none)) | — | credential class (`settings.py:104-109`: forwards the developer's Codex OAuth headers upstream) → keep whatever the survivor. It is a strict criterion-(3) subset of `:571` (same no-env build, the same `_forward_chatgpt_auth is False` as `:571`'s second assert): rule (A) keeps it; reviewer question Q4. | — |
| `test_forward_chatgpt_auth_explicit_argument_overrides_config` | :434 | a1 r0 f0 | other (credential) | keep (not a default test, no twin) | — | kwarg over env; no survivor. | — |
| `test_strip_inbound_headers_truthy_spellings_enable_the_flag` | :452 | a1 r0 f0; parametrize 'truthy' ×4 | other (route-gate) | keep (not a default test, no twin) | — | Task 5a N1, out of scope. | — |
| `test_strip_inbound_headers_falsy_spellings_disable_the_flag` | :463 | a1 r0 f0; parametrize 'falsy' ×5 | other (route-gate) | keep (not a default test, no twin) | — | Task 5a N2, out of scope. | — |
| `test_strip_inbound_headers_unset_defaults_on` | :473 | a1 r0 f0 | default: `CORP_LLM_STRIP_INBOUND_HEADERS`, route-gate | keep (sensitive default (class ≠ none)) | — | route-gate class (strip / forward headers; `settings.py:110-119`) → keep. | — |
| `test_a_grown_sanitized_body_goes_upstream_without_the_inbound_length` | :482 | a5 r0 f0; loops 3 | other (route-gate, invariant 3) | keep (not a default test, no twin) | — | `pre_call` with the default strip: no `Content-Length` / `Host` in three buckets, `Authorization` kept; no survivor. | — |
| `test_strip_inbound_headers_explicit_argument_overrides_config` | :534 | a1 r0 f0 | other (route-gate) | keep (not a default test, no twin) | — | kwarg over env; no survivor. | — |
| `test_forward_anthropic_auth_truthy_spellings_enable_the_flag` | :550 | a1 r0 f0; parametrize 'truthy' ×4 | other (credential) | keep (not a default test, no twin) | — | Task 5a N1, out of scope. | — |
| `test_forward_anthropic_auth_falsy_spellings_disable_the_flag` | :561 | a1 r0 f0; parametrize 'falsy' ×5 | other (credential) | keep (not a default test, no twin) | — | Task 5a N2, out of scope. | — |
| `test_forward_anthropic_auth_unset_defaults_off` | :571 | a2 r0 f0 | default grouping: `CORP_LLM_FORWARD_ANTHROPIC_AUTH`, `CORP_LLM_FORWARD_CHATGPT_AUTH`; credential | keep (default grouping (≥ 2 keys) kept) | — | Two keys' defaults in one test (an existing grouping); credential. Survivor-shaped for `:428`. | — |
| `test_forward_anthropic_auth_explicit_argument_overrides_config` | :580 | a2 r0 f0 | other (credential) | keep (not a default test, no twin) | — | Two kwarg-over-env blocks; no survivor. | — |
| `test_both_forward_auth_flags_raise_from_build_guardrail` | :597 | a1 r1 f0 | other (credential) | keep (not a default test, no twin) | — | `"1"` pair + `.problems`; `:610` uses other spellings and only `match=`. | — |
| `test_both_forward_auth_flags_lenient_spellings_raise_from_build_guardrail` | :610 | a0 r1 f0; parametrize 'truthy' ×3 | other (credential) | keep (not a default test, no twin) | — | No survivor. | — |
| `test_both_forward_auth_flags_raise_when_one_comes_from_a_kwarg` | :620 | a0 r1 f0 | other (credential) | keep (not a default test, no twin) | — | The kwarg is the input; no survivor. | — |
| `test_explicit_kwarg_off_takes_precedence_over_a_conflicting_env_pair` | :631 | a5 r0 f0; loops 1 | other (credential) | keep (not a default test, no twin) | — | The disarm warning text; no survivor. | — |
| `test_no_disarm_warning_when_the_env_pair_is_already_legal` | :656 | a1 r0 f0; loops 1 | other | keep (not a default test, no twin) | — | Negative-log owner (`behaviour`, site `tests/test_bootstrap.py:665`); no survivor carries its negative check. | — |
| `test_master_key_next_to_a_bridge_raises_from_build_guardrail` | :672 | a2 r1 f0 | other (credential) | keep (not a default test, no twin) | — | Negative `"master-key-fixture" not in str(exc_info.value)`. | — |
| `test_master_key_is_checked_against_the_resolved_flag_not_the_env_var` | :685 | a1 r1 f0 | other (credential) | keep (not a default test, no twin) | — | No survivor. | — |
| `test_master_key_alone_still_builds` | :700 | a2 r0 f0 | other (credential) | keep (not a default test, no twin) | — | Sets `LITELLM_MASTER_KEY`; `:571` makes the same asserts on another input (no master key). | — |
| `test_master_key_refusal_precedes_any_component_construction` | :709 | a0 r1 f2 | other (credential) | keep (not a default test, no twin) | — | Two `pytest.fail` patches; no survivor. | — |
| `test_one_forward_auth_flag_at_a_time_builds_fine` | :735 | a2 r0 f0; parametrize 'chatgpt,anthropic' ×3 | other (credential) | keep (not a default test, no twin) | — | No survivor. | — |
| `test_oracle_off_and_local_first_off_raises_config_error_naming_both_keys` | :753 | a2 r1 f0; parametrize 'local_first_off' ×2 | other (policy) | keep (not a default test, no twin) | — | No survivor on `build_guardrail()`. | — |
| `test_oracle_off_and_local_first_default_on_builds_fine` | :767 | a1 r0 f0 | default: `CORP_LLM_LOCAL_FIRST` (with the oracle off), policy | keep (sensitive default (class ≠ none)) | — | policy class (`settings.py:99` "enable the local-first cascade"; the no-op-sanitizer floor `bootstrap.py:656`) → keep. | — |
| `test_oracle_off_and_local_first_off_raises_even_with_corp_llm_override` | :778 | a0 r1 f0 | other (policy) | keep (not a default test, no twin) | — | The `corp_llm=` override is the input; no survivor. | — |
| `test_oracle_default_on_and_local_first_off_builds_fine` | :790 | a1 r0 f0 | default: `CORP_LLM_ORACLE_ENABLED` (with local-first off), policy | keep (sensitive default (class ≠ none)) | — | policy class (`settings.py:150-155` oracle switch) → keep. Near: `test_bootstrap_edges.py::test_local_first_flag_toggles_local_detectors[0]` (asserts `_local`, not the guardrail type). | — |
| `test_mapping_store_selects_redis_when_url_set` | :804 | a1 r0 f0 | other | keep (near-twin rejected (no survivor as strong)) | — | Task 5a near-twin (edges `:129` same `REDIS_URL` through `build_guardrail()`; edges `:113` another host), not re-litigated. | — |
| `test_mapping_store_in_memory_when_url_unset` | :811 | a1 r0 f0 | default: `REDIS_URL`, credential | keep (sensitive default (class ≠ none)) | — | credential class: the key is registered `secret=True` (`settings.py:179`, a URL that can carry a password). Under a `none` reading it would be deletable with survivor `test_bootstrap_edges.py::test_postgres_set_redis_absent_selects_mixed_backends` (`REDIS_URL` unset, `isinstance(build_mapping_store(), InMemoryMappingStore)`; its `CORP_LLM_PG_DSN` is never read by `build_mapping_store`) — reviewer question Q1. | — |
| `test_team_config_store_selects_postgres_when_dsn_set` | :815 | a1 r0 f0 | twin | **delete** | `tests/test_bootstrap_edges.py::test_postgres_set_redis_absent_selects_mixed_backends` | Criterion (3), intra-layer twin in another file (both are composition-root tests at the tests root). Same env value `CORP_LLM_PG_DSN=postgresql://gw:gw@pg:5432/gw`, same builder `bootstrap.build_team_config_store()` and the same assert `isinstance(…, PostgresTeamConfigStore)`; the survivor also asserts `build_mapping_store()` is in-memory. The builder is stateless (`bootstrap.py:140-145`: `config.get("CORP_LLM_PG_DSN")`, a new store), so the fixture difference does not reach it: `test_bootstrap.py`'s autouse `_clean_config` also resets `bootstrap._token_store` / `_team_config_store`, which `build_team_config_store()` never reads; both use `hermetic_gateway_config`. The survivor's extra `config.reset_cache()` after the setenv changes no resolved value. (Task 5a's ready row.) | required (other file; rev 10's same-builder waiver would also apply): `src/corp_llm_gateway/bootstrap.py:144` `return PostgresTeamConfigStore(dsn)` → `return InMemoryTeamConfigStore()`, dry-run on the scratch clone: the survivor failed `test_bootstrap_edges.py:150` `assert isinstance(bootstrap.build_team_config_store(), PostgresTeamConfigStore)`, the deleted test failed its `:820`; minimal and full |
| `test_team_config_store_in_memory_when_dsn_unset` | :823 | a1 r0 f0 | default: `CORP_LLM_PG_DSN`, credential | keep (sensitive default (class ≠ none)) | — | credential class: the key is registered `secret=True` (`settings.py:178`, a DSN with a password). Under a `none` reading: survivor `test_bootstrap_edges.py::test_redis_set_postgres_absent_selects_mixed_backends` (`CORP_LLM_PG_DSN` unset, `isinstance(build_team_config_store(), InMemoryTeamConfigStore)`; its `REDIS_URL` is never read by that builder) — Q1. | — |
| `test_build_guardrail_selects_postgres_token_store` | :827 | a1 r0 f0; skipped in minimal (asyncpg), passed in full | other | keep (near-twin rejected (no survivor as strong)) | — | Skipped in minimal, passed in full. Task 5a near-twins (edges `:78`, another host and branch-dependent; must-keep `:861`, extra env + `caplog`), not re-litigated. | — |
| `test_dev_team_token_seeds_local_dev_team` | :841 | a2 r0 f0 | other (credential) | keep (not a default test, no twin) | — | Near: must-keep `:172` seeds through the lazy `bootstrap.guardrail`, not `build_guardrail()`, and asserts no store type. | — |
| `test_dev_team_token_unset_store_stays_empty` | :851 | a0 r2 f0 | default: `CORP_LLM_DEV_TEAM_TOKEN`, credential | keep (sensitive default (class ≠ none)) | — | credential class (`settings.py:393-400`, `secret=True`, seeds an X-Corp-Auth token) → keep. | — |
| `test_bootstrap_does_not_read_process_environment` | :916 | a0 r0 f0; delegated `_assert_no_process_env_reads` a2 r0 f0 | other | keep (not a default test, no twin) | — | AST check of `bootstrap.py` (delegated `_assert_no_process_env_reads`). | — |
| `test_demo_guardrail_does_not_read_process_environment` | :920 | a0 r0 f0; delegated `_assert_no_process_env_reads` a2 r0 f0 | other | keep (not a default test, no twin) | — | The same check on another module (`_demo_guardrail.py`). | — |
| `test_backends_resolve_from_config_file_without_env` | :925 | a2 r0 f0 | resolution-order | keep (registry / resolution-order (plan-kept)) | — | File-only backends; resolution-order by body. Near: edges `:226` (same file values, plus decoy env). | — |
| `test_demo_shim_yields_in_memory_deps_and_working_guardrail` | :958 | a6 r0 f0 | other | keep (not a default test, no twin) | — | The demo builder; no survivor. | — |
| `test_demo_shim_resolves_forward_anthropic_auth_from_config` | :974 | a1 r0 f0 | other (credential) | keep (not a default test, no twin) | — | The demo builder; no survivor. | — |
| `test_demo_boot_path_raises_when_both_forward_auth_flags_set` | :990 | a0 r1 f0 | other (credential) | keep (not a default test, no twin) | — | The demo boot path; no survivor. | — |
| `test_importing_demo_guardrail_with_pg_dsn_and_no_asyncpg_does_not_raise` | :1007 | a1 r0 f0 | other | keep (not a default test, no twin) | — | No survivor. | — |
| `test_corp_ner_disabled_by_default_changes_nothing` | :1051 | a2 r0 f0; loops 1 | default: `CORP_NER_ENABLED`, policy | keep (sensitive default (class ≠ none)) | — | policy class (`settings.py:160` corp NER detector) → keep. | — |
| `test_only_network_backed_detectors_are_kept_off_code_segments` | :1062 | a2 r0 f0; loops 2 | default: `CORP_NER_ENABLED` (code detector lists), policy | keep (sensitive default (class ≠ none)) | — | policy class (which detectors scan CODE segments) → keep. Near: `:1081` asserts the same CODE list with corp NER on (another input). | — |
| `test_corp_ner_enabled_appends_detector_off_the_code_path` | :1081 | a2 r0 f0; loops 2 | other (policy) | keep (not a default test, no twin) | — | No survivor. | — |
| `test_corp_ner_never_receives_code_segment_text` | :1117 | a2 r0 f0; loops 2 | other (policy) | keep (not a default test, no twin) | — | No survivor. | — |
| `test_a_corp_ner_outage_fires_gateway_failure_exactly_once` | :1176 | a2 r1 f0 | other (fail-policy) | keep (not a default test, no twin) | — | No survivor. | — |
| `test_corp_ner_registers_a_detector_extension` | :1204 | a3 r0 f0 | other | keep (not a default test, no twin) | — | No survivor. | — |
| `test_corp_ner_client_reads_its_limits_from_config` | :1218 | a4 r0 f0 | other (capacity) | keep (not a default test, no twin) | — | No survivor. | — |
| `test_corp_ner_enabled_without_endpoint_fails_closed` | :1235 | a0 r1 f0 | other (fail-policy) | keep (not a default test, no twin) | — | Near: `test_settings.py:124` (`validate()`, another resolver). | — |
| `test_corp_ner_reads_the_extensions_detector_table` | :1243 | a2 r0 f0 | other | keep (not a default test, no twin) | — | The extensions table; no survivor. | — |
| `test_env_wins_over_the_extensions_detector_table` | :1264 | a1 r0 f0 | resolution-order | keep (registry / resolution-order (plan-kept)) | — | Env over the file's table; resolution-order by body. | — |
| `test_table_disabled_corp_ner_stays_off` | :1280 | a2 r0 f0 | other (policy) | keep (not a default test, no twin) | — | Table `enabled = false`; `:1051` reaches the same lists with no file (another input). | — |

### `tests/test_bootstrap_edges.py` — 18 ids

| node id | def line at f066b10 | checks | class | decision | survivor node id(s) | semantic note | fault injection |
|---|---|---|---|---|---|---|---|
| `test_auth_provider_misconfig_fails_fast_at_build` | :65 | a0 r1 f0; parametrize ('provider', 'error', 'match') ×3 | other (Task 5a fold E1; credential) | keep (Task 5a fold test) | — | Survivor of the three E1 members; no survivor per case. | — |
| `test_pg_dsn_token_store_selected_or_fastfails_without_asyncpg` | :78 | a1 r1 f0 | other | keep (near-twin rejected (no survivor as strong)) | — | Branch-dependent (raises in minimal, `PostgresTokenStore` in full); Task 5a near-twin of `test_bootstrap.py:827`. | — |
| `test_team_config_store_postgres_constructs_without_io` | :101 | a1 r0 f0 | other | keep (near-twin rejected (no survivor as strong)) | — | DSN host `10.255.255.1` (unreachable): another input than `test_bootstrap.py:815` / edges `:143` (`pg:5432`). | — |
| `test_mapping_store_redis_constructs_without_connect` | :113 | a1 r0 f0 | other | keep (near-twin rejected (no survivor as strong)) | — | Host `10.255.255.1`: another input than `test_bootstrap.py:804`. | — |
| `test_redis_set_postgres_absent_selects_mixed_backends` | :129 | a3 r0 f0 | other | keep (not a default test, no twin) | — | Three stores asserted. | — |
| `test_postgres_set_redis_absent_selects_mixed_backends` | :143 | a2 r0 f0 | other | keep (not a default test, no twin) | — | Survivor of `test_bootstrap.py:815`. | — |
| `test_empty_string_backend_config_falls_back_to_in_memory_and_defaults` | :157 | a4 r0 f0 | other | keep (not a default test, no twin) | — | Empty strings are set values, not unset; no survivor. | — |
| `test_local_first_flag_toggles_local_detectors` | :190 | a1 r0 f0; parametrize ('value', 'local_enabled') ×6 | other (policy) | keep (not a default test, no twin) | — | Six values; no survivor. | — |
| `test_gazetteer_flag_off_disables_gazetteer` | :201 | a1 r0 f0 | other (policy) | keep (not a default test, no twin) | — | Near: `test_settings.py:537` (`validate().flag`, another resolver). | — |
| `test_decoy_env_under_wrong_keys_is_ignored` | :211 | a2 r0 f0 | other (credential) | keep (not a default test, no twin) | — | Decoy aliases; no survivor. | — |
| `test_config_file_values_win_over_decoy_env_aliases` | :226 | a2 r0 f0 | resolution-order | keep (registry / resolution-order (plan-kept)) | — | File over decoy env; resolution-order by body. | — |
| `test_demo_backends_ignore_prod_env` | :249 | a2 r0 f0 | other | keep (not a default test, no twin) | — | The demo builder; no survivor. | — |
| `test_demo_seeded_token_absent_from_prod_guardrail` | :266 | a1 r1 f0 | other (credential) | keep (not a default test, no twin) | — | No survivor. | — |
| `test_dlp_canaries_parsed_from_config` | :282 | a3 r0 f0 | other (policy) | keep (not a default test, no twin) | — | No survivor. | — |
| `test_dlp_secret_rescan_stays_on_without_canaries` | :294 | a1 r0 f0 | default: `CORP_LLM_DLP_CANARIES`, policy | keep (sensitive default (class ≠ none)) | — | policy class (Stage-5 DLP egress guard, `settings.py:102-103`; the always-on secret rescan) → keep. | — |
| `test_malformed_canary_regex_fails_fast_at_build` | :302 | a0 r1 f0 | other (fail-policy) | keep (not a default test, no twin) | — | No survivor. | — |
| `test_endpoint_placeholder_default_warns` | :316 | a1 r0 f0; loops 1 | default: `CORP_LLM_ENDPOINT` (placeholder warning), proposed policy (borderline, Q1) | keep (sensitive default (class ≠ none)) | — | policy class proposed (the oracle endpoint, `settings.py:90-94`); a `none` reading keeps it too — nothing else asserts the warning (R3). | — |
| `test_endpoint_set_does_not_warn` | :331 | a1 r0 f0; loops 1 | other | keep (not a default test, no twin) | — | Negative-log owner (`behaviour`, site `tests/test_bootstrap_edges.py:340`); no survivor carries its negative check. | — |

## Totals

| file | universe | keep | delete |
|---|---|---|---|
| `tests/test_settings.py` | 38 | 37 | 1 |
| `tests/test_config.py` | 27 | 26 | 1 |
| `tests/test_bootstrap.py` | 71 | 68 | 3 |
| `tests/test_bootstrap_edges.py` | 18 | 18 | 0 |
| **total** | **154** | **149** | **5** |

Keep by reason (settings / config / bootstrap / edges = total):

| reason | settings | config | bootstrap | edges | total |
|---|---|---|---|---|---|
| registry / resolution-order (plan-kept) | 9 | 10 | 2 | 1 | 22 |
| sensitive default (class ≠ none) | 0 | 2 | 9 | 2 | 13 |
| `none`-class default, no same-path survivor | 0 | 0 | 2 | 0 | 2 |
| default grouping (≥ 2 keys) kept | 1 | 0 | 3 | 0 | 4 |
| near-twin rejected (no survivor as strong) | 4 | 2 | 2 | 3 | 11 |
| Task 5a fold test | 1 | 0 | 0 | 1 | 2 |
| not a default test, no twin | 22 | 12 | 50 | 11 | 95 |
| **keep** | **37** | **26** | **68** | **18** | **149** |

37 + 26 + 68 + 18 = 149; + 5 deletes = 154.

## Default-key table (rule A)

One row per key whose default a universe test reads alone. The reviewer rules on the classes
here; the four marked Q1 are the borderline ones.

| key | proposed class | what the key governs (`settings.py`) | test(s) | decision |
|---|---|---|---|---|
| `CORP_LLM_REQUIRE_NER` | fail-policy | `:141` "fail closed when NER absent (F2)" | `test_config.py::test_require_ner_defaults_to_false` | keep |
| `SSL_VERIFY` (under `CORP_ENV=prod`) | TLS | `:182` "'false' disables corp-LLM TLS verification" | `test_config.py::test_corp_llm_verify_prod_true_when_verify_on` | keep |
| `CORP_LLM_FORWARD_CHATGPT_AUTH` | credential | `:104-109` forwards Codex OAuth headers upstream | `test_bootstrap.py::test_forward_chatgpt_auth_unset_defaults_off` | keep (a twin of `:571`, Q4) |
| `CORP_LLM_STRIP_INBOUND_HEADERS` | route-gate | `:110-119` strip inbound wire headers before upstream | `test_bootstrap.py::test_strip_inbound_headers_unset_defaults_on` | keep |
| `CORP_LLM_LOCAL_FIRST` | policy | `:99` the local-first cascade | `test_bootstrap.py::test_oracle_off_and_local_first_default_on_builds_fine` | keep |
| `CORP_LLM_ORACLE_ENABLED` | policy | `:150-155` the corp-LLM oracle | `test_bootstrap.py::test_oracle_default_on_and_local_first_off_builds_fine` | keep |
| `CORP_LLM_DEV_TEAM_TOKEN` | credential | `:393-400` `secret=True`, seeds an X-Corp-Auth token | `test_bootstrap.py::test_dev_team_token_unset_store_stays_empty` | keep |
| `CORP_NER_ENABLED` | policy | `:160` the corp NER detector | `test_bootstrap.py::test_corp_ner_disabled_by_default_changes_nothing`, `::test_only_network_backed_detectors_are_kept_off_code_segments` | keep |
| `CORP_LLM_DLP_CANARIES` | policy | `:102-103` the Stage-5 DLP guard and its canaries | `test_bootstrap_edges.py::test_dlp_secret_rescan_stays_on_without_canaries` | keep |
| `REDIS_URL` | credential (Q1) | `:179` `secret=True`, the mapping store (Cache B, which holds originals) | `test_bootstrap.py::test_mapping_store_in_memory_when_url_unset` | keep; under `none`: delete, survivor `test_bootstrap_edges.py::test_postgres_set_redis_absent_selects_mixed_backends` |
| `CORP_LLM_PG_DSN` | credential (Q1) | `:178` `secret=True`, token / team stores | `test_bootstrap.py::test_team_config_store_in_memory_when_dsn_unset` | keep; under `none`: delete, survivor `test_bootstrap_edges.py::test_redis_set_postgres_absent_selects_mixed_backends` |
| `CORP_LLM_ENDPOINT` (placeholder warning) | policy (Q1) | `:90-94` the oracle endpoint, "no routable default" | `test_bootstrap_edges.py::test_endpoint_placeholder_default_warns` | keep under any class (no other test asserts the warning) |
| `CORP_AUDIT_SINK` | `none` (Q1) | `:225` audit sink kind | `test_bootstrap.py::test_audit_sink_is_stdout` | keep: no same-path survivor (`audit/test_factory.py::test_get_sink_default_is_stdout` skips the `build_guardrail()` wiring) |
| `CORP_METRICS_EXPORTER` | `none` (Q1) | `:306-311` metrics exporter, noop default | `test_bootstrap.py::test_build_guardrail_carries_metrics_exporter_noop_by_default` | keep: no single same-path survivor (`metrics/test_metrics.py::test_get_exporter_default_is_noop` + `test_bootstrap.py:137` together) |

Existing groupings, kept and not split (plan Task 5a item 3): `test_settings.py::test_corp_ner_defaults_leave_existing_deploys_untouched`
(5 corp-NER keys), `::test_existing_accessors_unchanged` (`CORP_LLM_OVERSIZE_POLICY`, `SSL_VERIFY`,
inside a resolution-order test), `test_bootstrap.py::test_build_guardrail_returns_guardrail_with_in_memory_backends`
and `::test_the_health_router_is_ready_without_redis_or_postgres` (`REDIS_URL`, `CORP_LLM_PG_DSN`),
`::test_forward_anthropic_auth_unset_defaults_off` (both forward-auth keys). Not a key default:
`test_config.py::test_default_used_when_neither_source_provides_value` (the caller default of
`config.get`, plan-kept), `test_bootstrap.py::test_build_guardrail_defaults_to_the_shared_exporter`
(a kwarg default). The must-keep policy defaults (`test_config.py::test_corp_llm_verify_defaults_to_true`,
`test_settings.py::test_forward_anthropic_auth_defaults_off`, `::test_route_gate_extras_default_to_empty`,
the issuance and capacity defaults) are outside the universe.

Result: no `none`-class default test has a survivor on the same path, so rule A deletes nothing.
If the reviewer moves `REDIS_URL` and `CORP_LLM_PG_DSN` to `none`, rule A would delete two tests,
each with the survivor in the table (other file, same builder; injection sites `bootstrap.py:191`
`return InMemoryMappingStore()` and `:145` `return InMemoryTeamConfigStore()`).

## Twin list (rule B)

Deleted (all five):

| deleted id | survivor | path |
|---|---|---|
| `tests/test_settings.py::test_noop_provider_needs_no_credentials` | `tests/test_settings.py::test_validate_uses_pydantic_when_present` | same file, `config.validate()` |
| `tests/test_config.py::test_corp_llm_verify_ssl_verify_false` | `tests/test_config.py::test_corp_llm_verify_demo_allows_ssl_verify_false` | same file, `config.corp_llm_verify()` |
| `tests/test_bootstrap.py::test_module_level_guardrail_is_importable_instance` | `tests/test_bootstrap.py::test_guardrail_attribute_builds_once_and_caches` | same file, `bootstrap.__getattr__` |
| `tests/test_bootstrap.py::test_oracle_disabled_build_guardrail_skips_client_build` | `tests/test_bootstrap.py::test_oracle_falsy_spellings_disable_oracle_without_endpoint[0]` | same file, `build_guardrail()` |
| `tests/test_bootstrap.py::test_team_config_store_selects_postgres_when_dsn_set` | `tests/test_bootstrap_edges.py::test_postgres_set_redis_absent_selects_mixed_backends` | other file, `build_team_config_store()` (Task 5a's ready row) |

Strict subsets kept by another rule: `test_bootstrap.py::test_forward_chatgpt_auth_unset_defaults_off`
⊂ `::test_forward_anthropic_auth_unset_defaults_off` (rule A, credential; Q4);
`test_settings.py::test_validate_lenient_falsy_forms_disable_oracle[0]` ⊂ `::test_validate_ok_with_oracle_disabled_and_no_endpoint`
(one case of three; functions are deleted whole).

Near-twins checked and kept (another input, another resolver, or a weaker survivor):
`test_settings.py` `:69` vs `:549` (endpoint literal, and `:69`'s value assert has no survivor);
`:76` vs `:83` / `:172`; `:303` vs `:211[0-1]` (a set `CHATGPT=0`); `test_config.py` `:84` / `:91`
vs `:130` (`SSL_VERIFY` / `CORP_ENV` differ); `:190` vs `test_settings.py:514` (file contents);
`test_bootstrap.py` `:109` vs `audit/test_factory.py::test_get_sink_default_is_stdout`, `:118` vs
`metrics/test_metrics.py::test_get_exporter_default_is_noop` (not the `build_guardrail()` wiring);
`:256` vs `sanitizer/test_team_config_outage_edges.py:68` (another builder); `:407[1]` vs `:735[1-0]`;
`:700` vs `:571` (master key set); `:790` vs edges `:190[0]`; `:841` vs must-keep `:172`; `:925` vs
edges `:226` (decoy env; both resolution-order); `:1235` vs `test_settings.py:124`; `:1280` vs
`:1051`; edges `:101` / `:113` vs `test_bootstrap.py:815` / `:804` (unreachable host); edges `:201`
vs `test_settings.py:537`. The Task 5a near-twins (`test_bootstrap.py:804` vs edges `:129`, `:827`
vs edges `:78` and must-keep `:861`, `:811` / `:823` vs `:52` / edges `:157`, `test_settings.py`
E2 `[audit_sink]` vs `:489`, E2 `[oversize_policy]` vs `:83`) are cited, not re-litigated.

## Negative-log and name-pinned findings

- `negative_log_checks.json` owners in the four files: `test_bootstrap.py::test_dev_team_token_ignored_when_corp_env_prod`
  (`:889`, security, must-keep), `::test_dev_team_token_ignored_when_pg_dsn_set` (`:875`, security,
  must-keep), `::test_no_disarm_warning_when_the_env_pair_is_already_legal` (`:665`, behaviour, kept),
  `test_bootstrap_edges.py::test_endpoint_set_does_not_warn` (`:340`, behaviour, kept). **No deleted id
  is an owner**, so no row drops. No deleted test has a negative check of its own.
- `name_pinned.json` cites two ids in the four files, both in `test_bootstrap.py` and both must-keep:
  `test_the_sanitization_probe_round_trips_through_the_live_guardrail` and
  `::test_the_sanitization_probe_is_healthy_with_the_oracle_off` (citing sites
  `tests/litellm_hook/test_acceptance_matrix.py`, `name_pinned.json:322-330`). Neither is deleted or
  renamed. No doc under the citation sources cites a deleted id (`docs/testing/` is not a source;
  only `task5a-config-audit.md` names them, in its rows and appendix).
- `must_keep.py` gate gap: none. No universe id runs a security negative-log check or matches a
  step-1 / step-2 rule that `must_keep/` lacks (`must_keep --check` 0 before and after the dry run).

## Predicted phase-2 manifest diff

| manifest | change |
|---|---|
| `expected_outcomes.minimal.json`, `expected_outcomes.full.json` | per env −5 ids, all `passed` (5,937 → 5,932): `test_bootstrap.py` 113 → 110, `test_config.py` 42 → 41, `test_settings.py` 229 → 228; one line per file changes; `test_bootstrap_edges.py` unchanged |
| `baseline_checks/_root__test_bootstrap.json` | −6 lines (3 `tests` + 3 `cases` entries) |
| `baseline_checks/_root__test_config.json` | −2 lines (1 + 1) |
| `baseline_checks/_root__test_settings.json` | −2 lines (1 + 1) |
| `baseline_checks/_root__test_bootstrap_edges.json` | unchanged |
| `negative_log_checks.json` | 3 `site` drifts, owners unchanged, no row lost: `tests/test_bootstrap.py:665 → :641` (−5 for `:61`, −19 for `:299`), `:875 → :843` and `:889 → :857` (also −8 for `:815`); `test_bootstrap_edges.py:340` unchanged |
| `must_keep/` | byte-identical (no `must_keep --write`) |
| `moves.json`, `name_pinned.json`, `coverage.*.json`, `not_applicable.json`, `external_deps.json` | unchanged (dry `inventory --write` and `name_pinned --write` changed neither `external_deps.json` nor `name_pinned.json`) |
| `docs/testing/deleted-tests.md` | +5 rows, `PR` = `Task 5b`, reviewer `auto-review (pending)`; no existing row cites the four files, so no `(now: …)` |

Lines removed per deletion (the `def` through the two blank lines after it): `test_bootstrap.py`
`:61` 5, `:299` 19, `:815` 8 (file 1,289 → 1,257 lines); `test_config.py` `:100` 7 (277 → 270);
`test_settings.py` `:470` 8 (1,442 → 1,434; `test_the_allow_key_is_refused_in_prod` `:1427 → :1419`). The two
must-keep matrix ids move up 5 (`:207 → :202`, `:235 → :230`); `name_pinned.json` holds the matrix's
citing lines, not these, so it does not change. `baseline_checks` entries carry no line, so the
shifted tests report nothing in `inventory --check` (dry run).

## Coverage

Each survivor runs the deleted test's calls on the same data, so no line or arc may drop. Dry run,
the four files only, `--cov=corp_llm_gateway --cov-branch`, compared with
`tests/_gates/coverage_gate.py`'s `from_report` / `drops`:

| env | module | before (lines / arcs) | after | whole-suite baseline |
|---|---|---|---|---|
| minimal | `bootstrap.py` | 207 / 38 | 207 / 38 | 207 / 38 |
| minimal | `settings.py` | 389 / 112 | 389 / 112 | 391 / 114 |
| minimal | `config.py` | 67 / 19 | 67 / 19 | 67 / 19 |
| full | `bootstrap.py` | 207 / 38 | 207 / 38 | 244 / 44 |
| full | `settings.py` | 389 / 112 | 389 / 112 | 390 / 113 |
| full | `config.py` | 67 / 19 | 67 / 19 | 67 / 19 |

`drops(before, after)` is empty in both environments, and no line or arc is lost or gained in any
`src/` file. The four-file numbers are below the whole-suite baselines because other test files
reach more of `settings.py` (and, in full, `bootstrap.py`); phase 2's whole-suite `coverage_gate
check` compares against those baselines. The fault injections show every deleted line is also
reached by its survivor.

## Dry run (scratch clone; nothing in the checkout's `tests/` or `src/` changed)

A `git clone --shared` of the checkout at `f066b10` in the session scratch directory, `PYTHONPATH=src:.`:

- **Fault injections**, before the deletions, one at a time, each reverted with `git checkout -- <file>`
  (`git diff --stat -- src/` empty after each), in both venvs: all five mutations made both the
  deleted test and its survivor fail (see the rows; the `:650` mutation failed all five survivor
  cases).
- The five functions deleted by an AST script (the `def` and the blank lines after it). `ruff
  check` and `ruff format --check` on the three changed files: clean, already formatted; no import
  left unused.
- `inventory --check`: exit 1 with exactly 5 `missing test` lines (the five ids) and nothing else.
  `must_keep --check`, `moves --check`, `name_pinned --check`, `negative_logs --check`: 0.
- Preview of the regeneration (scratch only, then reverted): `inventory --write` → the
  `baseline_checks` diff in the table above, deletions only; `negative_logs --write` → the three
  `site` drifts above, nothing else; `name_pinned --write` → no change; then all five `--check`
  exit 0.
- `tests/_gates` + the four files, manifests not regenerated: minimal 433 passed / 3 skipped / 1
  failed, full 436 passed / 1 failed; the one failure is `tests/_gates/test_suite_gates.py::test_the_check_inventory_matches_the_baseline`
  (the five `missing test` lines), which phase 2's regeneration clears. The ledgers-cover gate test
  passes: a deletion adds no id. The 3 minimal skips are the known asyncpg ones.
- The four files: before 406 passed / 3 skipped (minimal), 409 passed (full); after 401 passed / 3
  skipped, 404 passed.
- Fingerprints: `python -m tests._gates.fingerprint {minimal,full} --check` → 0; Postgres `pg-test` up
  on 55432.

## Ledger rows (phase 2)

The `semantic note` and `fault injection` columns are the rows above. The other columns:

| deleted node id | baseline outcome (minimal / full) | the check it made |
|---|---|---|
| `tests/test_settings.py::test_noop_provider_needs_no_credentials` | passed / passed | asserts 1, raises 0, fail 0; delegated: `_no_logger_state_left_behind` fail 1 |
| `tests/test_config.py::test_corp_llm_verify_ssl_verify_false` | passed / passed | asserts 1, raises 0, fail 0; delegated: `_no_logger_state_left_behind` fail 1 |
| `tests/test_bootstrap.py::test_module_level_guardrail_is_importable_instance` | passed / passed | asserts 1, raises 0, fail 0; delegated: `_no_logger_state_left_behind` fail 1 |
| `tests/test_bootstrap.py::test_oracle_disabled_build_guardrail_skips_client_build` | passed / passed | asserts 2, raises 0, fail 0; delegated: `_no_logger_state_left_behind` fail 1 |
| `tests/test_bootstrap.py::test_team_config_store_selects_postgres_when_dsn_set` | passed / passed | asserts 1, raises 0, fail 0; delegated: `_no_logger_state_left_behind` fail 1 |

## Noted for Task 7

Not deletions here:

- `test_settings.py::test_all_keys_is_nonempty_and_unique`: `len(keys) > 30`, a bound check.
- `test_bootstrap.py::test_build_guardrail_carries_metrics_exporter_noop_by_default`: the first
  assert (`isinstance(…, MetricsExporter)`) is implied by the second (`NoopExporter`).
- `test_bootstrap.py::test_master_key_is_checked_against_the_resolved_flag_not_the_env_var`:
  `assert bootstrap.build_guardrail(…) is not None`, true for any build that returns.
- `test_bootstrap.py::test_guardrail_attribute_builds_once_and_caches`: `assert calls == 0` right
  after patching cannot fail.
- `test_settings.py::test_validate_uses_pydantic_when_present`: nothing checks that pydantic ran;
  the name promises more than the asserts.

## Open questions / decisions by rule

Decided by the rules:

- The universe is 154, not 148 (the brief's must-keep counts held the shards' `#` header lines).
- Rule A deletes nothing: every default test reads a key of a sensitive class or, for the two
  `none` proposals, has no survivor on the same path.
- The five deletions are rule B twins; four survivors are in the same file and run the same
  resolver / builder (rev 10 waiver), one is in another file (`test_bootstrap_edges.py`), so its
  injection is required. All five injections were dry-run and failed both sides.
- No case-level deletion (`test_validate_lenient_falsy_forms_disable_oracle[0]`); no fold (rev 13:
  N1 / N2 / N3 out of scope); no move.
- The Task 5a fold tests are kept: no survivor per case.

For the reviewer:

- **Q1 — sensitivity classes.** `REDIS_URL` and `CORP_LLM_PG_DSN` are proposed `credential` because
  `KEYS` registers both `secret=True`; the default itself only picks the in-memory store, so a
  `none` reading is defensible and would delete `test_bootstrap.py::test_mapping_store_in_memory_when_url_unset`
  and `::test_team_config_store_in_memory_when_dsn_unset` (survivors named in the default-key
  table). `CORP_AUDIT_SINK` and `CORP_METRICS_EXPORTER` are proposed `none` (the audit pipeline and
  metric labels are leak surfaces in CLAUDE.md, but a default sink / exporter is not a security
  control); the decision is keep either way. `CORP_LLM_ENDPOINT`'s placeholder warning is proposed
  `policy`; keep either way.
- **Q2 — `test_settings.py::test_noop_provider_needs_no_credentials`.** Read as a rule-B twin: no
  assert reads `CORP_LLM_AUTH_PROVIDER`. If the reviewer reads it as a credential default test, it
  is kept by rule A.
- **Q3 — `test_config.py::test_corp_llm_verify_ssl_verify_false`.** A TLS-area twin. Rule A's
  sensitivity guard covers default tests only, so rule B deletes it; the survivor pins more of the
  input (no file, no `CORP_ENV`). Keep it if TLS twins are out of bounds.
- **Q4 — `test_bootstrap.py::test_forward_chatgpt_auth_unset_defaults_off`.** A strict rule-B twin of
  `::test_forward_anthropic_auth_unset_defaults_off`, kept because rule A says a sensitive default is
  kept "whatever the survivor". If rule B wins for twins, it is a sixth deletion (same file, same
  builder; injection `bootstrap.py:600` `_flag("CORP_LLM_FORWARD_CHATGPT_AUTH", "0")` → `"1"`).

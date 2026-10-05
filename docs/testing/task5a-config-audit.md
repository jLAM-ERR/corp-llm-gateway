# Task 5a config audit (phase 1)

The audit of plan `docs/plans/20260926-test-suite-refactor-and-prune.md` (rev 12, local only),
Task 5a: composition-root and config restructuring, a move PR inside `tests/test_bootstrap.py`,
`tests/test_bootstrap_edges.py`, `tests/test_settings.py` and `tests/test_config.py`. Phase 1
decides fold / keep for every member of the two families the plan names (backend selection;
fail-fast at build) and changes no test. Phase 2 (after review) does the folds, the
[deleted-tests.md](deleted-tests.md) rows and the manifest regeneration. The gates are in
[must-keep.md](must-keep.md). Tree: `release/1.0.x` at `e3aec88`.

**Decision: 2 fold groups, 7 fold members (E1 3, E2 4) → 2 new parametrised tests with 11 param
ids; 54 keep; the backend-selection family has no fold.** After phase 2: `test_bootstrap_edges.py`
18 functions (20 today), `test_settings.py` 110 (113), `test_bootstrap.py` and `test_config.py`
unchanged. Collected ids per file do not change (25 / 229 / 113 / 42, both environments).

Function counts at `e3aec88`: 80 / 20 / 113 / **28** (the brief said 27 for `test_config.py`;
`baseline_checks/_root__test_config.json` also holds 28, and the file has not changed since
`4504502`, before the baseline). So the completeness sum is 80 + 20 + 113 + 28 = **241**. All four
files are byte-identical to `807831a` (`git diff --stat 807831a e3aec88` is empty), so every
current id and line is its baseline id and line.

## Rules applied

- **fold** (plan rev 9): ≥ 3 functions in one file whose assert / raises / `pytest.fail`
  statements are textually identical modulo data literals (config keys and values, `match=`
  strings, expected types), with the same fixtures, the same builder call and the same delegated
  helpers. One parametrised test in place of the first member; each old function is a
  prune-style ledger row ("N functions → 1 parametrised test, checks per id unchanged"); new
  param ids; no `moves.json` entry. An exception type is a data literal only where every member's
  check is `pytest.raises(<type>, match=…)` and the type is the parameter. A member with an
  extra check (another assert, a `caplog` check, an `as exc` assert, a `pytest.fail` patch, a
  negative `not in`) is not a member. A different builder (`build_mapping_store()` vs
  `build_team_config_store()`), a builder call with a kwarg the others lack, or another setup
  statement is an operation, not a data literal (the Task 4 rule for `flush()` vs `feed(…)`).
- **keep** otherwise, with the reason: must-keep id; extra check; < 3 identical members;
  different builder or builder call; raises statement differs.
- Never a fold member: a must-keep id (consequence 1 below), the four negative-log owners
  (`negative_log_checks.json`, owners `test_bootstrap.py::test_dev_team_token_ignored_when_corp_env_prod`,
  `::test_dev_team_token_ignored_when_pg_dsn_set`, `::test_no_disarm_warning_when_the_env_pair_is_already_legal`,
  `test_bootstrap_edges.py::test_endpoint_set_does_not_warn`), the two acceptance-matrix ids.
  None of the six is a family member.
- Nothing moves between files and nothing is deleted (plan rev 3 / rev 12). A criterion-(3)
  twin is kept and recorded as a ready-to-copy row for Task 5b.
- Candidates were found by reading the bodies. The fold literals and the gate effects were
  checked by a dry run on a scratch clone (below).

Columns: `checks` is the test's line in `tests/_manifests/baseline_checks/_root__<file>.json`
(asserts / raises / fail; parametrize cases and loops where present). Every test in the four
files also carries the delegated `_no_logger_state_left_behind` fail 1 (conftest autouse), so
it is not repeated per row. `fixtures + builder` names the test's own arguments; the autouse
fixtures are per file: `test_bootstrap.py` `_clean_config` (`hermetic_gateway_config` + the
shared token / team stores reset to `None`), `test_bootstrap_edges.py` `_clean_config`
(`hermetic_gateway_config` only), `test_settings.py` the explicit `hermetic` fixture where
listed. `(:N)` is the line of the `def` at `e3aec88`. Line numbers marked "at HEAD" are the
folded tree's (phase 2), measured, not predicted.

## Family 1 — backend selection

| node id | checks | fixtures + builder | decision | fold group | semantic note |
|---|---|---|---|---|---|
| `tests/test_bootstrap.py::test_build_guardrail_returns_guardrail_with_in_memory_backends` (:52) | a3 r0 f0 | — ; `bootstrap.build_guardrail()` | keep (extra check) | — | No env. Asserts three objects: the guardrail type, `InMemoryTokenStore`, the core's `InMemoryMappingStore`. No other test makes this default trio from one build. |
| `tests/test_bootstrap.py::test_the_shared_token_store_selects_postgres_when_dsn_set` (:183) | a3 r0 f0; `importorskip("asyncpg")` | `monkeypatch`; `bootstrap.get_token_store()` | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept as is. Skipped in minimal, passed in full. |
| `tests/test_bootstrap.py::test_mapping_store_selects_redis_when_url_set` (:804) | a1 r0 f0 | `monkeypatch`; `bootstrap.build_mapping_store()` | keep (< 3 members) | — | `REDIS_URL=redis://cache.corp.lan:6379/0` → `RedisMappingStore`. Same statement shape as `:815`, but another builder; the only other `build_mapping_store()` single-assert test here (`:811`) sets no env, so its setup differs. Near: edges `:133` sets the same `REDIS_URL` but checks through `build_guardrail()`. |
| `tests/test_bootstrap.py::test_mapping_store_in_memory_when_url_unset` (:811) | a1 r0 f0 | — ; `bootstrap.build_mapping_store()` | keep (< 3 members) | — | No env → `InMemoryMappingStore`. Its shape partner `:823` uses another builder. |
| `tests/test_bootstrap.py::test_team_config_store_selects_postgres_when_dsn_set` (:815) | a1 r0 f0 | `monkeypatch`; `bootstrap.build_team_config_store()` | keep (< 3 members); Task 5b candidate | — | `CORP_LLM_PG_DSN=postgresql://gw:gw@pg:5432/gw` → `PostgresTeamConfigStore`. Criterion-(3) twin of edges `:147` ("Task 5b candidates"). |
| `tests/test_bootstrap.py::test_team_config_store_in_memory_when_dsn_unset` (:823) | a1 r0 f0 | — ; `bootstrap.build_team_config_store()` | keep (< 3 members) | — | No env → `InMemoryTeamConfigStore`. |
| `tests/test_bootstrap.py::test_build_guardrail_selects_postgres_token_store` (:827) | a1 r0 f0; `importorskip("asyncpg")` | `monkeypatch`; `bootstrap.build_guardrail()` | keep (< 3 members) | — | Skipped in minimal, passed in full. No identical partner. Near: edges `:82` (its asyncpg-present branch, DSN host `10.255.255.1`), must-keep `:861` (adds `CORP_LLM_DEV_TEAM_TOKEN` and a caplog check). |
| `tests/test_bootstrap.py::test_backends_resolve_from_config_file_without_env` (:925) | a2 r0 f0 | `tmp_path`, `monkeypatch`; TOML file + `config.reset_cache()`; `build_mapping_store()`, `build_team_config_store()` | keep (extra check) | — | Keys only in the TOML file; two builders, two asserts. |
| `tests/test_bootstrap_edges.py::test_pg_dsn_token_store_selected_or_fastfails_without_asyncpg` (:82) | a1 r1 f0 | `monkeypatch`; `config.reset_cache()`; `bootstrap.build_guardrail()` | keep (extra check) | — | `try: import asyncpg` picks the branch: minimal `pytest.raises(RuntimeError, match="asyncpg")`, full `isinstance(…, PostgresTokenStore)`. Passes in both environments. A table shape would drop one branch. |
| `tests/test_bootstrap_edges.py::test_team_config_store_postgres_constructs_without_io` (:105) | a1 r0 f0 | `monkeypatch`; `config.reset_cache()`; `bootstrap.build_team_config_store()` | keep (< 3 members) | — | DSN host `10.255.255.1` (unreachable). Same statements as `:117` modulo key, value and type, but another builder: 2 functions. |
| `tests/test_bootstrap_edges.py::test_mapping_store_redis_constructs_without_connect` (:117) | a1 r0 f0 | `monkeypatch`; `config.reset_cache()`; `bootstrap.build_mapping_store()` | keep (< 3 members) | — | `REDIS_URL=redis://10.255.255.1:6379/0`. See the previous row. |
| `tests/test_bootstrap_edges.py::test_redis_set_postgres_absent_selects_mixed_backends` (:133) | a3 r0 f0 | `monkeypatch`; `config.reset_cache()`; `build_guardrail()`, `build_team_config_store()` | keep (extra check) | — | Three stores asserted. |
| `tests/test_bootstrap_edges.py::test_postgres_set_redis_absent_selects_mixed_backends` (:147) | a2 r0 f0 | `monkeypatch`; `config.reset_cache()`; `build_team_config_store()`, `build_mapping_store()` | keep (extra check) | — | Two stores asserted. The survivor of the Task 5b candidate (`test_bootstrap.py:815`). |
| `tests/test_bootstrap_edges.py::test_empty_string_backend_config_falls_back_to_in_memory_and_defaults` (:161) | a4 r0 f0 | `monkeypatch`; `config.reset_cache()`; `build_guardrail()`, `build_team_config_store()`, `build_corp_llm_client()` | keep (extra check) | — | Four empty strings, four asserts. |
| `tests/test_bootstrap_edges.py::test_demo_backends_ignore_prod_env` (:253) | a2 r0 f0 | `monkeypatch`, `_restore_pkg_logger`; `_demo_guardrail._build_demo_guardrail()` | keep (different builder) | — | The demo shim's builder; the prod env is set after the import. |

## Family 2 — fail-fast at build

| node id | checks | fixtures + builder | decision | fold group | semantic note |
|---|---|---|---|---|---|
| `tests/test_bootstrap_edges.py::test_bearer_provider_without_token_fails_fast_at_build` (:49) | a0 r1 f0 | `monkeypatch`; `config.reset_cache()`; `bootstrap.build_guardrail()` | **fold** | E1 `[bearer_provider_without_token]` | `CORP_LLM_AUTH_PROVIDER=bearer` → `RuntimeError`, `match="CORP_LLM_BEARER_TOKEN"`. Its two-line comment moves above the case. |
| `tests/test_bootstrap_edges.py::test_unknown_auth_provider_fails_fast_at_build` (:61) | a0 r1 f0 | same | **fold** | E1 `[unknown_auth_provider]` | `totally-made-up` → `ValueError`, `match="CORP_LLM_AUTH_PROVIDER"`. |
| `tests/test_bootstrap_edges.py::test_oidc_provider_missing_subkeys_fails_fast_at_build` (:69) | a0 r1 f0 | same | **fold** | E1 `[oidc_provider_missing_subkeys]` | `oidc` → `RuntimeError`, `match="CORP_LLM_OIDC_ISSUER"`. |
| `tests/test_bootstrap_edges.py::test_malformed_canary_regex_fails_fast_at_build` (:306) | a0 r1 f0 | `monkeypatch`; `config.reset_cache()`; `bootstrap.build_guardrail()` | keep (raises statement differs) | — | `pytest.raises(re.error)` has no `match=`, and the key is `CORP_LLM_DLP_CANARIES`. Joining E1 would need a key column and `match=None`, an added literal. |
| `tests/test_bootstrap.py::test_both_forward_auth_flags_raise_from_build_guardrail` (:597) | a1 r1 f0 | `monkeypatch`; `bootstrap.build_guardrail()` | keep (extra check) | — | `as exc_info` + `FORWARD_AUTH_EXCLUSIVE_MESSAGE in exc_info.value.problems`. |
| `tests/test_bootstrap.py::test_both_forward_auth_flags_lenient_spellings_raise_from_build_guardrail` (:610) | a0 r1 f0; parametrize `truthy` ×3 | `monkeypatch`; `bootstrap.build_guardrail()` | keep (< 3 members) | — | Two setenvs + `pytest.raises(ConfigError, match="mutually")`. No other function in the file has this shape (`:620` sets one var and passes a kwarg). |
| `tests/test_bootstrap.py::test_both_forward_auth_flags_raise_when_one_comes_from_a_kwarg` (:620) | a0 r1 f0 | `monkeypatch`; `bootstrap.build_guardrail(forward_anthropic_auth=True)` | keep (different builder call) | — | The kwarg is what the test checks. |
| `tests/test_bootstrap.py::test_explicit_kwarg_off_takes_precedence_over_a_conflicting_env_pair` (:631) | a5 r0 f0; loop 1 | `monkeypatch`, `caplog`; `build_guardrail(forward_chatgpt_auth=False)` | keep (extra check) | — | Builds, then checks the warning text. Not a raise. |
| `tests/test_bootstrap.py::test_master_key_next_to_a_bridge_raises_from_build_guardrail` (:672) | a2 r1 f0 | `monkeypatch`; `bootstrap.build_guardrail()` | keep (extra check) | — | Message in `.problems` + the negative `"master-key-fixture" not in str(exc_info.value)`. |
| `tests/test_bootstrap.py::test_master_key_is_checked_against_the_resolved_flag_not_the_env_var` (:685) | a1 r1 f0 | `monkeypatch`; `build_guardrail(forward_anthropic_auth=True)`, `(…=False)` | keep (extra check) | — | A second build and `is not None` after the raise. |
| `tests/test_bootstrap.py::test_master_key_alone_still_builds` (:700) | a2 r0 f0 | `monkeypatch`; `bootstrap.build_guardrail()` | keep (< 3 members) | — | Builds; both flags off. Not a raise. |
| `tests/test_bootstrap.py::test_master_key_refusal_precedes_any_component_construction` (:709) | a0 r1 f2 | `monkeypatch`; `bootstrap.build_guardrail()` | keep (extra check) | — | Two `pytest.fail` patches (`build_corp_llm_client`, `build_mapping_store`). |
| `tests/test_bootstrap.py::test_oracle_off_and_local_first_off_raises_config_error_naming_both_keys` (:753) | a2 r1 f0; parametrize `local_first_off` ×2 | `monkeypatch`; `bootstrap.build_guardrail()` | keep (extra check) | — | `as exc_info` + both key names in the message. |
| `tests/test_bootstrap.py::test_oracle_off_and_local_first_default_on_builds_fine` (:767) | a1 r0 f0 | `monkeypatch`; `bootstrap.build_guardrail()` | keep (< 3 members) | — | Builds; `isinstance(guardrail, CorpLlmGuardrail)`. Identical to `:790` modulo the env key: 2 functions. |
| `tests/test_bootstrap.py::test_oracle_off_and_local_first_off_raises_even_with_corp_llm_override` (:778) | a0 r1 f0 | `monkeypatch`; `bootstrap.build_guardrail(corp_llm=object())` | keep (different builder call) | — | Bare `pytest.raises(ConfigError)`; the DI override is what the test checks. |
| `tests/test_bootstrap.py::test_oracle_default_on_and_local_first_off_builds_fine` (:790) | a1 r0 f0 | `monkeypatch`; `bootstrap.build_guardrail()` | keep (< 3 members) | — | See `:767`. |
| `tests/test_settings.py::test_validate_rejects_both_forward_auth_flags_with_the_shared_message` (:220) | a2 r1 f0 | `hermetic`, `monkeypatch`; `config.validate()` | keep (extra check) | — | `as exc` + two message asserts. |
| `tests/test_settings.py::test_validate_rejects_lenient_truthy_spellings_of_both_flags` (:234) | a0 r1 f0; parametrize `truthy` ×4 | `hermetic`, `monkeypatch`; `config.validate()` | keep (< 3 members) | — | Three setenvs (endpoint + both flags) + `match="mutually"`. The only other three-setenv raise is `:274`: 2 functions. |
| `tests/test_settings.py::test_validate_rejects_a_master_key_next_to_the_anthropic_bridge` (:257) | a3 r1 f0 | `hermetic`, `monkeypatch`; `config.validate()` | keep (extra check) | — | Two message asserts + the negative `not in str(exc.value)`. |
| `tests/test_settings.py::test_validate_rejects_a_master_key_next_to_the_chatgpt_bridge` (:274) | a0 r1 f0 | `hermetic`, `monkeypatch`; `config.validate()` | keep (< 3 members) | — | Three setenvs + `match="LITELLM_MASTER_KEY"`; see `:234`. |
| `tests/test_settings.py::test_validate_rejects_a_blank_master_key_next_to_a_bridge` (:286) | a1 r1 f0; parametrize `master_key` ×2 | `hermetic`, `monkeypatch`; `config.validate()` | keep (extra check) | — | `as exc` + message in `.problems`. |
| `tests/test_settings.py::test_validate_rejects_unknown_oversize_policy` (:351) | a0 r1 f0 | `hermetic`, `monkeypatch`; `config.validate()` | **fold** | E2 `[oversize_policy]` | `CORP_LLM_OVERSIZE_POLICY=nope`, `match="CORP_LLM_OVERSIZE_POLICY"`. |
| `tests/test_settings.py::test_validate_rejects_a_malformed_route_gate_extra` (:364) | a0 r1 f0; parametrize `raw` ×5 | `hermetic`, `monkeypatch`, `raw`; `config.validate()` | **fold** | E2 `[route_gate_extra-<raw>]` ×5 | Each of its five cases becomes one E2 id with `value=<raw>` and `match="CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH"`, in the same order. |
| `tests/test_settings.py::test_validate_rejects_a_route_gate_extra_naming_a_refused_row` (:374) | a0 r1 f0; parametrize `raw` ×3 | `hermetic`, `monkeypatch`, `raw`; `config.validate()` | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. Its checks have E2's shape (`match="refused"`), so it stays as a standalone test right after E2. |
| `tests/test_settings.py::test_validate_rejects_unknown_auth_provider` (:405) | a0 r1 f0 | `hermetic`, `monkeypatch`; `config.validate()` | **fold** | E2 `[auth_provider]` | `CORP_LLM_AUTH_PROVIDER=kerberos`, `match="CORP_LLM_AUTH_PROVIDER"`. |
| `tests/test_settings.py::test_validate_rejects_unknown_audit_sink` (:414) | a0 r1 f0 | `hermetic`, `monkeypatch`; `config.validate()` | **fold** | E2 `[audit_sink]` | `CORP_AUDIT_SINK=kafka`, `match="CORP_AUDIT_SINK"`. |
| `tests/test_settings.py::test_issuance_refuses_a_partial_config` (:690) | a3 r1 f0 | `hermetic`, `monkeypatch`; `settings.issuance()` | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. |
| `tests/test_settings.py::test_issuance_refuses_each_missing_required_key` (:707) | a1 r1 f0; parametrize `missing` ×2; loop 1 | same + `_issuance_env`, `_write` | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. |
| `tests/test_settings.py::test_issuance_refuses_a_missing_team_map` (:718) | a0 r1 f0 | same | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. With `:726` / `:735` a fold-shaped trio (`match="CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP"`); must-keep, so never folded. |
| `tests/test_settings.py::test_issuance_refuses_a_team_map_given_as_a_scalar` (:726) | a0 r1 f0 | same | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. |
| `tests/test_settings.py::test_issuance_refuses_an_empty_team_map` (:735) | a0 r1 f0 | same | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. |
| `tests/test_settings.py::test_issuance_refuses_a_team_map_entry_without_a_team_string` (:745) | a0 r1 f0; parametrize `value` ×3 | same | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. |
| `tests/test_settings.py::test_issuance_refuses_the_operator_audience` (:754) | a2 r1 f0 | same | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. |
| `tests/test_settings.py::test_issuance_refuses_a_non_https_issuer_in_prod` (:768) | a0 r1 f0; parametrize `env` ×3 | same | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. |
| `tests/test_settings.py::test_issuance_refuses_a_non_https_jwks_url_in_prod` (:780) | a0 r1 f0 | same | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. |
| `tests/test_settings.py::test_issuance_refuses_a_malformed_jwks_url_everywhere` (:829) | a0 r1 f0; parametrize `url` ×3 | same | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. |
| `tests/test_settings.py::test_issuance_refuses_non_positive_bounds` (:850) | a0 r1 f0; parametrize `key` ×5 × `value` ×4 | same | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. |
| `tests/test_settings.py::test_validate_refuses_issuance_without_postgres` (:892) | a0 r1 f0 | same; `config.validate()` | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. |
| `tests/test_settings.py::test_issuance_refuses_an_operator_audience_that_differs_only_by_whitespace` (:947) | a0 r1 f0; parametrize `operator` ×2 | same | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. |
| `tests/test_settings.py::test_validate_refuses_an_issuance_bound_past_its_ceiling` (:1046) | a0 r1 f0 | same; `config.validate()` | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. |
| `tests/test_settings.py::test_capacity_refuses_a_bad_byte_budget` (:1184) | a1 r1 f0; parametrize `value` ×9 | `hermetic`, `monkeypatch`; `settings.capacity()` | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. |
| `tests/test_settings.py::test_capacity_refuses_a_bad_draining_cap` (:1219) | a1 r1 f0; parametrize `value` ×6 | same | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. |
| `tests/test_settings.py::test_capacity_refuses_a_bad_body_deadline` (:1241) | a1 r1 f0; parametrize `value` ×6 | same | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. |
| `tests/test_settings.py::test_capacity_refuses_zero_in_prod` (:1270) | a2 r1 f0; parametrize `env` ×4 | same | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. |
| `tests/test_settings.py::test_capacity_refuses_a_bad_cap_everywhere` (:1285) | a1 r1 f0; parametrize `value` ×6 × `env` ×2 | same | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. |
| `tests/test_settings.py::test_capacity_refuses_a_bad_cancel_grace` (:1298) | a1 r1 f0; parametrize `value` ×6 | same | keep (must-keep id: step1:touched) | — | Family member, must-keep, kept. |

The `test_issuance_refuses_*` (12), `test_validate_refuses_*` (2) and `test_capacity_refuses_*` (6)
rows are the PR #16 keys; all 20 are must-keep and are listed as family members only.

## Totals

| family | members | fold | keep |
|---|---|---|---|
| backend selection | 15 (`test_bootstrap.py` 8, `test_bootstrap_edges.py` 7) | 0 | 15 |
| fail-fast at build | 46 (`test_bootstrap_edges.py` 4, `test_bootstrap.py` 12, `test_settings.py` 30) | 7 (E1 3, E2 4) | 39 |
| **total** | **61** | **7** → 2 tests, 11 param ids | **54** |

Keep by reason: must-keep id 22 (1 backend, 21 fail-fast); extra check 15 (6 backend, 9
fail-fast); < 3 identical members 13 (7 backend, 6 fail-fast); different builder or builder call
3 (`edges:253`, `test_bootstrap.py:620`, `:778`); raises statement differs 1 (`edges:306`).
22 + 15 + 13 + 3 + 1 = 54. Appendix (not in a family): 180. 61 + 180 = 241.

Ledger rows in phase 2: **7** (one per fold member function; the `:364` row lists its five
cases, in the Task 1b "— cases `[…]`" form), `PR` = `Task 5a`, reviewer `auto-review (pending)`.
No existing row of `deleted-tests.md` cites a test of the four files
(`grep -n 'test_bootstrap\|test_settings\|test_config' docs/testing/deleted-tests.md` is empty),
so no `(now: …)` annotation.

## Fold groups

**E1** — 3 functions of `tests/test_bootstrap_edges.py` (`:49`, `:61`, `:69`), each a0 r1 f0,
fixture `monkeypatch` (autouse `_clean_config` → `hermetic_gateway_config`), builder
`bootstrap.build_guardrail()`. The statements, identical modulo the provider, the exception type
and the `match=` string:

```python
monkeypatch.setenv("CORP_LLM_AUTH_PROVIDER", <provider>)
config.reset_cache()

with pytest.raises(<error>, match=<match>):
    bootstrap.build_guardrail()
```

Every member's check is `pytest.raises(<type>, match=…)`, so the type is a parameter
(`pytest.raises(error, match=match)`), and each case carries its member's type and `match=`
literal verbatim:

| old id | param id | provider | error | match |
|---|---|---|---|---|
| `test_bearer_provider_without_token_fails_fast_at_build` | `bearer_provider_without_token` | `"bearer"` | `RuntimeError` | `"CORP_LLM_BEARER_TOKEN"` |
| `test_unknown_auth_provider_fails_fast_at_build` | `unknown_auth_provider` | `"totally-made-up"` | `ValueError` | `"CORP_LLM_AUTH_PROVIDER"` |
| `test_oidc_provider_missing_subkeys_fails_fast_at_build` | `oidc_provider_missing_subkeys` | `"oidc"` | `RuntimeError` | `"CORP_LLM_OIDC_ISSUER"` |

New test `tests/test_bootstrap_edges.py::test_auth_provider_misconfig_fails_fast_at_build` at
`:49` (at HEAD: decorator `:49`, `def` `:65`, lines 49-72; 28 lines → 24):

```python
@pytest.mark.parametrize(
    ("provider", "error", "match"),
    [
        # bearer auth needs CORP_LLM_BEARER_TOKEN; missing it must abort at
        # construction, not on the first upstream request.
        pytest.param(
            "bearer", RuntimeError, "CORP_LLM_BEARER_TOKEN", id="bearer_provider_without_token"
        ),
        pytest.param(
            "totally-made-up", ValueError, "CORP_LLM_AUTH_PROVIDER", id="unknown_auth_provider"
        ),
        pytest.param(
            "oidc", RuntimeError, "CORP_LLM_OIDC_ISSUER", id="oidc_provider_missing_subkeys"
        ),
    ],
)
def test_auth_provider_misconfig_fails_fast_at_build(
    monkeypatch: pytest.MonkeyPatch, provider: str, error: type[Exception], match: str
) -> None:
    monkeypatch.setenv("CORP_LLM_AUTH_PROVIDER", provider)
    config.reset_cache()

    with pytest.raises(error, match=match):
        bootstrap.build_guardrail()
```

Ledger semantic note, ready to copy: *"Fold E1: 3 functions → 1 parametrised test, checks per
id unchanged. `tests/test_bootstrap_edges.py::test_auth_provider_misconfig_fails_fast_at_build[<param id>]`
runs this test's statements with its literals as parameters: `monkeypatch.setenv("CORP_LLM_AUTH_PROVIDER", <provider>)`,
`config.reset_cache()`, `pytest.raises(<error>, match=<match>)` around `bootstrap.build_guardrail()`;
the exception type is the parameter, each case keeps its own (`RuntimeError` / `ValueError`).
Same fixtures (`monkeypatch`, autouse `hermetic_gateway_config`), same builder. Not must-keep
(plan rev 11: `tests/test_bootstrap_edges.py` is a step-1 touched file whose rule reads `807831a`,
where the new test does not exist; no step-2 glob). Its comment, if any, is above the case."*

**E2** — 4 functions of `tests/test_settings.py` (`:351`, `:364` with 5 cases, `:405`, `:414`),
each a0 r1 f0, fixtures `hermetic` + `monkeypatch`, builder `config.validate()`. The statements,
identical modulo the key, the value and the `match=` string:

```python
monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
monkeypatch.setenv(<key>, <value>)
with pytest.raises(ConfigError, match=<match>):
    config.validate()
```

The exception type is `ConfigError` in every member, so it stays a constant (not parametrised).

| old id | param id | key | value | match |
|---|---|---|---|---|
| `test_validate_rejects_unknown_oversize_policy` | `oversize_policy` | `CORP_LLM_OVERSIZE_POLICY` | `"nope"` | `"CORP_LLM_OVERSIZE_POLICY"` |
| `test_validate_rejects_a_malformed_route_gate_extra[/internal/ops]` | `route_gate_extra-/internal/ops` | `CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH` | `"/internal/ops"` | `"CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH"` |
| `…[POST]` | `route_gate_extra-POST` | same | `"POST"` | same |
| `…[TRACE /internal/ops]` | `route_gate_extra-TRACE /internal/ops` | same | `"TRACE /internal/ops"` | same |
| `…[GET internal/ops]` | `route_gate_extra-GET internal/ops` | same | `"GET internal/ops"` | same |
| `…[GET /internal/../key]` | `route_gate_extra-GET /internal/../key` | same | `"GET /internal/../key"` | same |
| `test_validate_rejects_unknown_auth_provider` | `auth_provider` | `CORP_LLM_AUTH_PROVIDER` | `"kerberos"` | `"CORP_LLM_AUTH_PROVIDER"` |
| `test_validate_rejects_unknown_audit_sink` | `audit_sink` | `CORP_AUDIT_SINK` | `"kafka"` | `"CORP_AUDIT_SINK"` |

New test `tests/test_settings.py::test_validate_rejects_an_unknown_or_malformed_choice` at
`:351`, in place of the two adjacent members `:351` and `:364` (at HEAD: decorator `:351`, `def`
`:393`, lines 351-399); `:405` and `:414` go. The must-keep `:374` follows it, at HEAD `:403`
(decorator `:402`). Parametrised over
`("key", "value", "match")`, one `pytest.param(<key>, <value>, <match>, id=<param id>)` per row
above, in that order. The body:

```python
def test_validate_rejects_an_unknown_or_malformed_choice(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: str, match: str
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError, match=match):
        config.validate()
```

`match` equals `key` in all 8 rows; it is its own column so that each member's `match=` literal
is carried verbatim. `:364` was already parametrised: its 5 cases become 5 E2 ids in the same
order, and the function is one ledger row that lists the 5 cases.

Ledger semantic note, ready to copy: *"Fold E2: 4 functions → 1 parametrised test, checks per
id unchanged. `tests/test_settings.py::test_validate_rejects_an_unknown_or_malformed_choice[<param id>]`
runs this test's statements with its literals as parameters: `monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")`,
`monkeypatch.setenv(<key>, <value>)`, `pytest.raises(ConfigError, match=<match>)` around
`config.validate()`. Same fixtures (`hermetic`, `monkeypatch`), same builder. Not must-keep (plan
rev 11: `tests/test_settings.py` is a step-1 touched file whose rule reads `807831a`, where the
new test does not exist; no step-2 glob)."* For `:364` add: *"— cases `[/internal/ops]`, `[POST]`,
`[TRACE /internal/ops]`, `[GET internal/ops]`, `[GET /internal/../key]` → `[route_gate_extra-<raw>]`,
same order."*

Considered and not folded:

- `test_bootstrap.py` `:804` / `:811` / `:815` / `:823`: two builders, and the "unset" pair has no
  setenv. Two functions per builder.
- `test_bootstrap_edges.py` `:105` / `:117`: two builders, two functions.
- `test_bootstrap.py` `:767` / `:790`: identical modulo the env key, two functions.
- `test_settings.py` `:234` / `:274`: three setenvs + `pytest.raises(ConfigError, match=…)`, two functions.
- `test_bootstrap.py` `:610` with `:620` / `:1235`: the builder call (kwarg) or the setup differs;
  `:1235` (corp-NER section) is outside the family.
- `test_bootstrap_edges.py:306` with E1: no `match=`, other key.

## Must-keep (rev 11, read in the gate)

`tests/_gates/must_keep.py`: none of the four files is in `STEP1_ADDED` (`:40-100`) or
`STEP1_MODIFIED_IN_FULL` (`:102-116`), and none matches a `STEP2_GLOBS` entry (`:136-163`).
`test_bootstrap.py`, `test_bootstrap_edges.py` and `test_settings.py` are in
`STEP1_MODIFIED_TOUCHED` (`:119-130`); their ids come from `_step1_touched()` (`:229-247`), which
parses `git show 807831a:<path>` and keeps the tests whose span the `e9e877f..807831a` diff
touched. `test_config.py` contributes only the `POLICY_DEFAULTS` id (`:190`). Per file
(`_rule_ids()`): `test_bootstrap.py` 9 (step1:touched 6, negative-log 2, name-pinned 1),
`test_bootstrap_edges.py` 0, `test_settings.py` 72 (step1:touched 70, policy-default 2),
`test_config.py` 1 (policy-default). 82 in all, equal to `must_keep/_root__test_{bootstrap,settings,config}.txt`.

1. **A must-keep id is never a fold member.** A fold removes its members' function ids. For a
   must-keep id, `--check` (`problems()`, `:345-392`) reports "must-keep test is gone"
   (`:378-381`), and `--write` / the strict `ledger write` stop in `function_ids()` (`:292-299`,
   "must-keep rule names tests that do not exist"), because `_step1_touched` still selects the
   id from `807831a`. So every fold member above is non-must-keep, and the family's must-keep
   members stay as standalone tests (`test_bootstrap.py:183`, `test_settings.py:374`, the 20
   PR #16 refusals).
2. **A new fold test in these files is not must-keep.** It did not exist at `807831a`, so
   `_step1_touched` cannot select it; no step-2 glob, named id, policy default,
   `security_node_ids` entry (neither has a negative-log check) or name-pinned id names it. Dry
   run: `must_keep.function_ids()` on the folded tree selects neither new test, and
   `must_keep --check` exits 0. So `must_keep/` stays **byte-identical** in phase 2 and no
   `must_keep --write` is run.

The four negative-log owners: the two `security`-class ones (`test_bootstrap.py:875` / `:889`
owners) are must-keep through `security_node_ids`; the two `behaviour`-class ones
(`test_bootstrap.py:665`, `test_bootstrap_edges.py:344`) are **not** in `security_node_ids` and so
not must-keep (the brief said "must-keep via security_node_ids anyway"). Neither is a family
member; both are kept.

## Line drift (phase 2)

- `test_bootstrap.py` and `test_config.py` get no change, so nothing in them moves.
- `test_bootstrap_edges.py`: E1 replaces `:49-76` (28 lines) with 24 lines; every test below
  moves up 4 (at HEAD `test_malformed_canary_regex_fails_fast_at_build` `:306` → `:302`,
  `test_endpoint_set_does_not_warn` `:335` → `:331`; file 344 → 340 lines). Negative-log site
  `test_bootstrap_edges.py:344` → **`:340`** at HEAD (owner `::test_endpoint_set_does_not_warn`
  unchanged).
- `test_settings.py`: E2 replaces `:351-370` with 49 lines (`:351-399`) and removes `:405-420`.
  Tests between (`:374`, `:383`, `:398`) move down 29, at HEAD `:403`, `:412`, `:427`; tests
  after `:420` move down 11 (at HEAD `test_validate_accepts_valid_choices` `:423` → `:434`,
  `test_the_allow_key_is_refused_in_prod` `:1416` → `:1427`; file 1,431 → 1,442 lines). No
  negative-log site and no name-pinned id is in this file.
- `negative_log_checks.json`: `test_bootstrap.py:665`, `:875`, `:889` unchanged; one site drift
  `test_bootstrap_edges.py:344 → :340`. `negative_logs --write` keys a reviewed row by
  `owner|check` (`negative_logs.py:116-117`), `owner` is the baseline id
  (`moves.to_baseline`, `:109`), and `--write` copies `class` / `note` from the reviewed row onto
  the current `site` (`:162-175`). So only `site` changes. `--check` compares keys only, so it
  is 0 even before the rewrite (dry run). `security_node_ids` unchanged (dry run).
- `name_pinned.json:322-330`: its lines are the **citing** sites
  (`tests/litellm_hook/test_acceptance_matrix.py:217` / `:218`), not test lines, and neither
  file changes. Unchanged. Both probe ids keep their names (`test_bootstrap.py` is not touched).
- `baseline_checks/*.json`: an entry has no line column (`asserts`, `body_hash`, `case_data`,
  `constants`, `delegated`, `fail`, `fixtures`, `helpers`, `raises`), and `diff_checks`
  (`inventory.py:1338-1350`) compares those columns by node id. A line shift alone is no
  finding. Dry run: `inventory --check` reports only the 7 `missing test` + 2 `new test`
  lines; the 17 shifted edges tests and the 82 shifted settings tests report nothing.

## Manifest predictions (phase 2)

| manifest | change |
|---|---|
| `expected_outcomes.minimal.json`, `expected_outcomes.full.json` | per env −11 ids / +11 ids, all `passed` both sides: `test_bootstrap_edges.py` −3 / +3 (25 → 25), `test_settings.py` −8 / +8 (229 → 229). No other id changes. No fold member is skipped anywhere: all 7 pass in both envs (the asyncpg-skipping tests `:183`, `:827`, `:861` are not members). |
| `baseline_checks/_root__test_bootstrap_edges.json` | −6 lines (3 `tests` + 3 `cases`), +2 (the new test + its `cases` `{full: 3, minimal: 3}`) |
| `baseline_checks/_root__test_settings.json` | −8 lines (4 + 4), +2 (the new test + `cases` `{full: 8, minimal: 8}`) |
| `baseline_checks/_root__test_bootstrap.json`, `_root__test_config.json` | unchanged |
| `negative_log_checks.json` | one `site`: `test_bootstrap_edges.py:344 → :340` |
| `moves.json` | unchanged (nothing moves between files; fold tests have no entry) |
| `must_keep/` | byte-identical (see above) |
| `name_pinned.json` | unchanged |
| `external_deps.json` | unchanged: no fold member has an entry (the four files' entries are the three demo-shim `module:` programs and `test_example_toml_documents_every_key` → `config.example.toml`); dry-run `diff_external` empty; `docs/testing/` is not hashed |
| `coverage.minimal.json`, `coverage.full.json` | unchanged (below) |
| `not_applicable.json` | unchanged (no entry for the four files) |

Coverage: each fold case runs the same calls on the same data as its old function, so no line
or arc may drop. E1 reaches the same `bootstrap.build_guardrail()` → auth-provider factory
branches (bearer token missing, unknown provider, OIDC sub-keys) with the same env; E2 the same
`settings.validate()` choice / route-gate-extra checks. Dry run, the four files only, `--cov-branch`:
`bootstrap.py` 207 lines / 38 arcs, `settings.py` 389 / 112, `config.py` 67 / 19 before and after
in both envs, and no line or arc lost or gained in any `src/` file.

## Task 5b candidates (criterion (3); kept here)

| twin id | survivor | baseline outcome (minimal / full) | why | fault injection |
|---|---|---|---|---|
| `tests/test_bootstrap.py::test_team_config_store_selects_postgres_when_dsn_set` | `tests/test_bootstrap_edges.py::test_postgres_set_redis_absent_selects_mixed_backends` | passed / passed | criterion (3): same env value `CORP_LLM_PG_DSN=postgresql://gw:gw@pg:5432/gw`, same builder `bootstrap.build_team_config_store()`, the same assert `isinstance(…, PostgresTeamConfigStore)`; the survivor adds `isinstance(bootstrap.build_mapping_store(), InMemoryMappingStore)` | required (other file): `src/corp_llm_gateway/bootstrap.py:144` `return PostgresTeamConfigStore(dsn)` → `return InMemoryTeamConfigStore()`; both should fail. Not run (phase 1 touches no `src/`). |

Ready-to-copy ledger row (Task 5b): | Task 5b | `tests/test_bootstrap.py::test_team_config_store_selects_postgres_when_dsn_set` | passed / passed | asserts 1, raises 0, fail 0; delegated: `_no_logger_state_left_behind` fail 1 | `tests/test_bootstrap_edges.py::test_postgres_set_redis_absent_selects_mixed_backends` | Criterion (3), intra-layer twin in another file (both are composition-root tests at the tests root). Same env value `CORP_LLM_PG_DSN=postgresql://gw:gw@pg:5432/gw`, same builder `bootstrap.build_team_config_store()` and the same assert `isinstance(…, PostgresTeamConfigStore)`; the survivor also asserts `build_mapping_store()` is in-memory. The builder is stateless (`bootstrap.py:140-145`: `config.get("CORP_LLM_PG_DSN")`, a new store), so the fixture difference does not reach it: `test_bootstrap.py`'s autouse `_clean_config` also resets `bootstrap._token_store` / `_team_config_store`, which `build_team_config_store()` never reads; both use `hermetic_gateway_config`. The survivor's extra `config.reset_cache()` after the setenv changes no resolved value. | `src/corp_llm_gateway/bootstrap.py:144` `return PostgresTeamConfigStore(dsn)` → `return InMemoryTeamConfigStore()`: run in Task 5b (other file, so required); the result goes here | auto-review (pending) |

Near-twins checked and not criterion (3) (other input or other builder):
`test_bootstrap.py:804` vs edges `:133` (same `REDIS_URL`, survivor builds through
`build_guardrail()`); `:827` vs edges `:82` (DSN host `10.255.255.1`, branch-dependent) and
must-keep `:861` (extra env + caplog); `:811` / `:823` vs `:52` / edges `:161` (other builder or
other config); `test_settings.py:414` vs `:478` (the same `kafka` but in env over a file);
`:351` vs `:83` (`banana`, no endpoint).

## Noted, out of scope

Fold-shaped groups outside the two named families. Not classified as fold; the orchestrator
decides whether a later rev adds them.

- **N1** — `test_bootstrap.py::test_forward_chatgpt_auth_truthy_spellings_enable_the_flag` (`:407`),
  `::test_strip_inbound_headers_truthy_spellings_enable_the_flag` (`:452`),
  `::test_forward_anthropic_auth_truthy_spellings_enable_the_flag` (`:550`): each parametrised
  `truthy` ×4 (`1`, `true`, `yes`, `on`), statements `setenv(<KEY>, truthy)`; `build_guardrail()`;
  `assert guardrail.<attr> is True`, identical modulo the key and the attribute (12 ids).
- **N2** — the falsy trio `:418`, `:463`, `:561` (`…_falsy_spellings_disable_the_flag`), each ×5
  (`0`, `off`, `no`, `false`, `OFF`), `is False` (15 ids).
- **N3** — `test_settings.py::test_validate_accepts_an_absent_master_key_next_to_a_bridge` (`:303`),
  `::test_validate_allows_a_blank_master_key_when_no_bridge_is_on` (`:313`, ×2),
  `::test_validate_allows_a_master_key_when_no_bridge_is_on` (`:322`): endpoint + one setenv +
  `assert isinstance(config.validate(), Settings)`, identical modulo key and value (4 ids).

The brief already puts N1 / N2 (the truthy / falsy quartets) out of scope. Pairs that are not
≥ 3: `test_bootstrap.py` `:428` / `:473` (`…_unset_defaults_*`), `:434` / `:534`
(`…_explicit_argument_overrides_config`; `:580` has two blocks), `:916` / `:920`;
`test_config.py` `:159` / `:166` (`is_prod`), `:179` / `:185` (`require_ner`).

## Policy-default tests (plan item 3)

All **keep**, existing groupings kept, none in a family: the three must-keep policy defaults
`test_config.py::test_corp_llm_verify_defaults_to_true` (1 assert),
`test_settings.py::test_forward_anthropic_auth_defaults_off` (2),
`::test_route_gate_extras_default_to_empty` (1); and the default tests with ≥ 2 asserts:
`test_bootstrap.py::test_build_guardrail_carries_metrics_exporter_noop_by_default` (2),
`::test_forward_anthropic_auth_unset_defaults_off` (2), `::test_corp_ner_disabled_by_default_changes_nothing` (2),
`test_settings.py::test_corp_ner_defaults_leave_existing_deploys_untouched` (5),
`::test_issuance_resolves_with_defaults` (14, must-keep), `::test_issuance_store_timeout_defaults_to_ten_seconds` (2, must-keep),
`::test_capacity_defaults` (5, must-keep), `::test_the_draining_default_follows_the_inflight_cap` (2, must-keep),
`::test_the_allow_key_is_off_by_default` (2, must-keep), `test_config.py::test_default_used_when_neither_source_provides_value` (2).
Whether the non-policy ones are deletable is Task 5b's question, not audited here.

## Dry runs (scratch clone; no tracked file in the checkout changed)

A `git clone --shared` of the checkout at `e3aec88` in the session scratch directory, the two
folds applied by script, `ruff format` (one `pytest.param` line wrapped), then, with
`PYTHONPATH=src:.`:

- `ruff check` and `ruff format --check` on both files: clean.
- `inventory --check`: exactly 7 `missing test` (the members, at their baseline ids) + 2
  `new test` (the fold tests); no column diff on any other test. `diff_external`: empty.
- New tests' inventory entries: a0 r1 f0, delegated `_no_logger_state_left_behind` fail 1, and
  `fixtures`, `helpers`, `constants` equal to their members' (E1 `_clean_config`,
  `hermetic_gateway_config`, `ext:monkeypatch` …; E2 `hermetic`, `ext:monkeypatch` …);
  parametrize `('provider', 'error', 'match')` 3 cases, `('key', 'value', 'match')` 8 cases.
- Fold literals: an AST script printed each old member's `setenv` arguments and its
  `pytest.raises` type and `match=` and each new row; equal row for row.
- `must_keep --check`, `negative_logs --check`, `moves --check`, `name_pinned --check`: 0.
  `negative_logs.sites()`: one `site` drift (edges `:344 → :340`), no key lost or added.
- Collected ids: `test_auth_provider_misconfig_fails_fast_at_build[bearer_provider_without_token|unknown_auth_provider|oidc_provider_missing_subkeys]`,
  `test_validate_rejects_an_unknown_or_malformed_choice[oversize_policy|route_gate_extra-/internal/ops|route_gate_extra-POST|route_gate_extra-TRACE /internal/ops|route_gate_extra-GET internal/ops|route_gate_extra-GET /internal/../key|auth_provider|audit_sink]`.
- The four files: minimal 406 passed / 3 skipped, full 409 passed (as on the tree).
- Coverage of the four files, both envs: equal before and after (see "Manifest predictions").

## Sanity run (2026-10-05)

`pytest tests/test_bootstrap.py tests/test_bootstrap_edges.py tests/test_settings.py tests/test_config.py -q -rs`
on the tree at `e3aec88`:

| env | collected | passed | skipped |
|---|---|---|---|
| minimal (`.venv-test-minimal`, no `CI`, `CORP_REQUIRE_PROXY_CAPTURE=1`) | 409 | 406 | 3 (`test_bootstrap.py:186`, `:828`, `:864`: "PostgresTokenStore requires the 'postgres' extra") |
| full (`.venv-test-full`, `CI=true`, `CORP_REQUIRE_PROXY_CAPTURE=1`, `NO_PROXY`, `CORP_TEST_PG_DSN`; `pg-test` up on 55432) | 409 | 409 | 0 |

Both fingerprints match (`python -m tests._gates.fingerprint <env> --check` → 0). Expected after
phase 2: the same counts.

## Open questions / decisions by rule

Decided by the rules:

- **Backend selection: no fold.** Every same-shape group has two members per builder
  (`build_mapping_store()` / `build_team_config_store()`), and the "set" and "unset" tests differ
  in their setup statements. A builder is an operation, not a data literal (the Task 4 rule).
  So plan item 1 ("table-driven backend-selection test") produces no fold under the rev 9
  rule. A table over `(builder, env, expected type)` for `test_bootstrap.py:804/811/815/823`
  would need `getattr(bootstrap, builder)()` plus an `if env:` or an env-dict loop (a new
  `loops` column): a changed body shape, not proposed.
- **E1, E2 folded**; `:364` is a member though it is already parametrised: its per-id checks are
  identical and no keep reason applies.
- **E1's exception type is parametrised**; every member's check is
  `pytest.raises(<type>, match=…)`. E2's type is constant.
- **`edges:306` kept**: no `match=`.
- **The two new tests are not must-keep** (rev 11); `must_keep/` byte-identical.
- **No deletion** (rev 3 / rev 12); the one criterion-(3) twin waits for Task 5b with its row.
- **Plan items 3 and 4** hold with no edit: the policy-default tests are untouched, and
  `test_bootstrap.py` (the two acceptance-matrix ids) is untouched.

Against the brief (findings, not choices):

- `test_config.py` has 28 functions, not 27; the sum is 241.
- The two `behaviour` negative-log owners are not must-keep (not in `security_node_ids`).
- A must-keep fold member turns `--check` red through `problems()` "must-keep test is gone";
  `function_ids()`'s `SystemExit` is the `--write` / strict-ledger path. Same outcome.
- `name_pinned.json:322-330` holds citing lines in the acceptance matrix, not test lines.
- `test_bootstrap.py` gets no change in phase 2, so its three negative-log sites cannot drift.

Left for review:

- Fold names and param ids are proposals. E2's five route-gate rows could also be written as
  `*[pytest.param(…, raw, …, id=f"route_gate_extra-{raw}") for raw in [<the old list>]]`, which
  keeps `:364`'s list literal verbatim; the dry run used explicit rows.
- E2's `match` column duplicates `key`; `match=key` in the body would hold the same strings.
- Whether plan item 1 is closed as "audited, no fold qualifies", or a later rev allows the
  builder as a parameter.
- N1-N3 above.

## Appendix — tests not in a family (completeness)

`(MK)` = must-keep. Family members are the 61 rows above.

**`tests/test_bootstrap.py`** — 80 = 20 in the families + 60 here:
`test_module_level_guardrail_is_importable_instance`, `test_importing_module_does_not_build_guardrail` (MK), `test_guardrail_attribute_builds_once_and_caches`, `test_getattr_raises_for_unknown_attribute`, `test_gateway_version_is_metadata_not_demo_string`, `test_audit_sink_is_stdout`, `test_build_guardrail_carries_metrics_exporter_noop_by_default`, `test_build_guardrail_takes_an_explicit_metrics_exporter`, `test_build_guardrail_defaults_to_the_shared_exporter`, `test_build_health_router_wires_the_four_checks_and_no_issuer` (MK), `test_the_lazy_guardrail_uses_the_shared_stores` (MK), `test_the_lazy_guardrail_still_seeds_the_dev_team_token` (MK), `test_the_health_router_is_ready_without_redis_or_postgres`, `test_the_sanitization_probe_round_trips_through_the_live_guardrail` (MK), `test_the_sanitization_probe_reports_a_failing_round_trip`, `test_the_sanitization_probe_is_healthy_with_the_oracle_off` (MK), `test_build_guardrail_wraps_orchestrator_in_profile_aware`, `test_no_profile_team_passes_through_to_core_unchanged`, `test_team_with_sealed_default_profile_resolves_and_applies`, `test_oracle_disabled_build_guardrail_skips_client_build`, `test_oracle_disabled_logs_one_info_at_build_time`, `test_oracle_disabled_profiled_team_inner_orchestrator_has_no_client`, `test_oracle_falsy_spellings_disable_oracle_without_endpoint`, `test_oracle_truthy_spellings_enable_oracle_and_build_client`, `test_forward_chatgpt_auth_truthy_spellings_enable_the_flag`, `test_forward_chatgpt_auth_falsy_spellings_disable_the_flag`, `test_forward_chatgpt_auth_unset_defaults_off`, `test_forward_chatgpt_auth_explicit_argument_overrides_config`, `test_strip_inbound_headers_truthy_spellings_enable_the_flag`, `test_strip_inbound_headers_falsy_spellings_disable_the_flag`, `test_strip_inbound_headers_unset_defaults_on`, `test_a_grown_sanitized_body_goes_upstream_without_the_inbound_length`, `test_strip_inbound_headers_explicit_argument_overrides_config`, `test_forward_anthropic_auth_truthy_spellings_enable_the_flag`, `test_forward_anthropic_auth_falsy_spellings_disable_the_flag`, `test_forward_anthropic_auth_unset_defaults_off`, `test_forward_anthropic_auth_explicit_argument_overrides_config`, `test_no_disarm_warning_when_the_env_pair_is_already_legal`, `test_one_forward_auth_flag_at_a_time_builds_fine`, `test_dev_team_token_seeds_local_dev_team`, `test_dev_team_token_unset_store_stays_empty`, `test_dev_team_token_ignored_when_pg_dsn_set` (MK), `test_dev_team_token_ignored_when_corp_env_prod` (MK), `test_bootstrap_does_not_read_process_environment`, `test_demo_guardrail_does_not_read_process_environment`, `test_demo_shim_yields_in_memory_deps_and_working_guardrail`, `test_demo_shim_resolves_forward_anthropic_auth_from_config`, `test_demo_boot_path_raises_when_both_forward_auth_flags_set`, `test_importing_demo_guardrail_with_pg_dsn_and_no_asyncpg_does_not_raise`, `test_corp_ner_disabled_by_default_changes_nothing`, `test_only_network_backed_detectors_are_kept_off_code_segments`, `test_corp_ner_enabled_appends_detector_off_the_code_path`, `test_corp_ner_never_receives_code_segment_text`, `test_a_corp_ner_outage_fires_gateway_failure_exactly_once`, `test_corp_ner_registers_a_detector_extension`, `test_corp_ner_client_reads_its_limits_from_config`, `test_corp_ner_enabled_without_endpoint_fails_closed`, `test_corp_ner_reads_the_extensions_detector_table`, `test_env_wins_over_the_extensions_detector_table`, `test_table_disabled_corp_ner_stays_off`.

**`tests/test_bootstrap_edges.py`** — 20 = 11 in the families + 9 here:
`test_local_first_flag_toggles_local_detectors`, `test_gazetteer_flag_off_disables_gazetteer`, `test_decoy_env_under_wrong_keys_is_ignored`, `test_config_file_values_win_over_decoy_env_aliases`, `test_demo_seeded_token_absent_from_prod_guardrail`, `test_dlp_canaries_parsed_from_config`, `test_dlp_secret_rescan_stays_on_without_canaries`, `test_endpoint_placeholder_default_warns`, `test_endpoint_set_does_not_warn`.

**`tests/test_settings.py`** — 113 = 30 in the families + 83 here:
`test_all_keys_is_nonempty_and_unique`, `test_all_keys_contains_core_and_new_knobs`, `test_secret_flag_marks_credentials`, `test_validate_ok_when_endpoint_set`, `test_validate_hard_fails_on_missing_endpoint`, `test_validate_reports_every_problem_at_once`, `test_corp_ner_keys_are_registered`, `test_corp_ner_defaults_leave_existing_deploys_untouched`, `test_validate_requires_corp_ner_endpoint_when_enabled`, `test_validate_ignores_corp_ner_endpoint_when_disabled`, `test_validate_ok_with_oracle_disabled_and_no_endpoint`, `test_validate_fails_when_oracle_and_local_first_both_disabled`, `test_validate_still_requires_endpoint_when_oracle_enabled`, `test_validate_lenient_falsy_forms_disable_oracle`, `test_forward_anthropic_auth_defaults_off` (MK), `test_validate_ok_unless_both_forward_auth_flags_are_on`, `test_forward_auth_conflict_is_the_single_shared_rule`, `test_validate_accepts_an_absent_master_key_next_to_a_bridge`, `test_validate_allows_a_blank_master_key_when_no_bridge_is_on`, `test_validate_allows_a_master_key_when_no_bridge_is_on`, `test_master_key_is_registered_as_a_secret_so_config_check_redacts_it`, `test_master_key_conflict_is_the_single_shared_rule`, `test_validate_accepts_route_gate_extras_and_resolves_them`, `test_route_gate_extras_default_to_empty` (MK), `test_validate_accepts_valid_choices`, `test_bearer_provider_requires_token`, `test_langfuse_sink_requires_keys`, `test_noop_provider_needs_no_credentials`, `test_validate_resolves_endpoint_from_file_with_env_cleared`, `test_env_overrides_file_through_the_chain`, `test_example_toml_documents_every_key`, `test_existing_accessors_unchanged`, `test_settings_flag_helper`, `test_validate_uses_pydantic_when_present`, `test_issuance_keys_are_registered` (MK), `test_issuance_is_disabled_when_issuer_unset` (MK), `test_issuance_is_disabled_when_issuer_blank` (MK), `test_issuance_resolves_with_defaults` (MK), `test_issuance_team_map_keeps_config_file_order` (MK), `test_issuance_accepts_an_inline_team_map_table` (MK), `test_issuance_strips_a_trailing_slash_from_the_issuer_and_default_jwks_url` (MK), `test_issuance_explicit_overrides` (MK), `test_issuance_settings_are_frozen` (MK), `test_issuance_derived_jwks_url_inherits_an_http_issuer_refusal_in_prod` (MK), `test_issuance_allows_http_outside_prod` (MK), `test_issuance_allows_insecure_http_exactly_outside_prod` (MK), `test_issuance_bounds_resolve_from_the_config_file` (MK), `test_validate_reports_issuance_problems` (MK), `test_validate_accepts_a_complete_issuance_config` (MK), `test_serving_issuance_requires_postgres` (MK), `test_serving_issuance_resolves_with_postgres` (MK), `test_serving_issuance_is_none_when_disabled` (MK), `test_serving_issuance_reports_a_partial_config_and_the_missing_dsn_together` (MK), `test_validate_ignores_issuance_keys_when_issuer_unset` (MK), `test_issuance_bounds_of_one_resolve_and_are_usable` (MK), `test_the_store_timeout_key_is_registered_with_its_default` (MK), `test_issuance_store_timeout_defaults_to_ten_seconds` (MK), `test_issuance_bounds_accept_their_ceiling_and_refuse_one_past_it` (MK), `test_the_store_timeout_is_never_below_the_stores_lock_wait` (MK), `test_runtime_problems_are_empty_when_issuance_is_off` (MK), `test_runtime_problems_are_empty_when_the_config_is_refused` (MK), `test_runtime_problems_are_empty_when_everything_is_there` (MK), `test_runtime_problems_name_the_missing_extra` (MK), `test_runtime_problems_refuse_a_ca_bundle_that_cannot_be_used` (MK), `test_runtime_problems_accept_a_loadable_ca_bundle` (MK), `test_capacity_keys_are_registered` (MK), `test_capacity_defaults` (MK), `test_the_byte_budget_bounds_match_the_limiter` (MK), `test_capacity_accepts_a_byte_budget_in_range` (MK), `test_the_draining_default_follows_the_inflight_cap` (MK), `test_capacity_accepts_a_draining_cap_in_range` (MK), `test_capacity_accepts_a_body_deadline_in_range` (MK), `test_capacity_resolves_through_the_config_file` (MK), `test_capacity_accepts_a_cap_in_range_outside_prod` (MK), `test_validate_reports_the_capacity_problems` (MK), `test_validate_passes_the_default_capacity` (MK), `test_the_allow_key_is_off_by_default` (MK), `test_config_check_reports_a_litellm_debug_env_var` (MK), `test_config_check_accepts_litellm_below_debug` (MK), `test_config_check_reports_set_verbose_in_litellms_config` (MK), `test_config_check_accepts_set_verbose_false` (MK), `test_the_allow_key_silences_the_debug_problems_outside_prod` (MK), `test_the_allow_key_is_refused_in_prod` (MK).

**`tests/test_config.py`** — 28 = 0 in the families + 28 here:
`test_env_wins_over_file`, `test_file_used_when_env_missing`, `test_default_used_when_neither_source_provides_value`, `test_missing_config_file_is_silent`, `test_get_required_raises_when_missing`, `test_get_required_uses_file_when_env_missing`, `test_non_string_values_are_stringified`, `test_corp_llm_verify_ca_bundle_set`, `test_corp_llm_verify_ca_bundle_takes_precedence_over_ssl_verify`, `test_corp_llm_verify_ssl_verify_false`, `test_corp_llm_verify_defaults_to_true` (MK), `test_corp_llm_verify_prod_refuses_ssl_verify_false`, `test_corp_llm_verify_prod_allows_ca_bundle`, `test_corp_llm_verify_demo_allows_ssl_verify_false`, `test_corp_llm_verify_prod_true_when_verify_on`, `test_is_prod_true`, `test_is_prod_false`, `test_require_ner_defaults_to_false`, `test_require_ner_truthy_values`, `test_require_ner_falsey_values`, `test_require_ner_reads_from_file`, `test_get_table_reads_nested_table`, `test_get_table_dotted_prefix_descends`, `test_get_table_missing_returns_empty_dict`, `test_get_table_no_file_returns_empty_dict`, `test_get_table_on_scalar_path_returns_empty_dict`, `test_get_table_does_not_break_scalar_env_override`, `test_auth_factory_reads_from_config_file`.

Check: 61 family + (60 + 9 + 83 + 28 = 180) appendix = 241 functions.

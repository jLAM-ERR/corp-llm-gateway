# Task 6b store-contract prune audit (phase 1)

The audit of plan `docs/plans/20260926-test-suite-refactor-and-prune.md` (rev 15, local only), Task 6b:
store-contract dedupe, a prune PR over `tests/tokens/test_in_memory.py`, `tests/tokens/test_postgres_store.py`,
`tests/tokens/test_issuance.py`, `tests/tokens/test_issuance_policy.py`, `tests/team_config/test_store.py` and
`tests/team_config/test_postgres_store.py`. Phase 1 decides keep / delete for every non-must-keep test function
of the six files and changes no test. Phase 2 (after review) makes the deletions, the
[deleted-tests.md](deleted-tests.md) rows, the fault injections on the checkout and the manifest regeneration.
The gates are in [must-keep.md](must-keep.md). Tree: `release/1.0.x` at `6b8e9bb`.

**Decision: 14 delete, 23 keep.** All 14 deletions repeat a contract case on the same backend
(`tests/tokens/test_token_store_contract.py` or `tests/team_config/test_postgres_store.py`): 3 in
`tokens/test_in_memory.py`, 5 in `tokens/test_postgres_store.py`, 6 in `team_config/test_store.py`. Nothing in
`tokens/test_issuance.py`, `tokens/test_issuance_policy.py` or `team_config/test_postgres_store.py` is deleted, and
no edit is expected in the last two. After phase 2 the three edited files hold 1 / 8 / 3 test functions
(`test_in_memory.py` / `tokens/test_postgres_store.py` / `team_config/test_store.py`; 4 / 13 / 9 today), and each
environment's ledger holds 5,918 ids (5,932 today).

## The universe: 37 functions

| file | functions at `6b8e9bb` | must-keep functions | universe |
|---|---|---|---|
| `tests/tokens/test_in_memory.py` | 4 | 0 (no id in `must_keep/tokens.txt`) | 4 |
| `tests/tokens/test_postgres_store.py` | 13 | 7 | 6 |
| `tests/tokens/test_issuance.py` | 8 | 0 | 8 |
| `tests/tokens/test_issuance_policy.py` | 16 | 16 (step-2 glob: the whole file) | 0 |
| `tests/team_config/test_store.py` | 9 | 1 (`test_default_fail_policy_matches_matrix`, policy default) | 8 |
| `tests/team_config/test_postgres_store.py` | 13 | 2 | 11 |
| **total** | **63** | **26** | **37** |

Functions: module-level `def test*` / `async def test*` by `ast`. Must-keep: `tests/_gates/must_keep.py`'s
`function_ids()` gives 0 / 7 / 0 / 16 / 1 / 2. This equals `must_keep/{tokens,team_config}.txt` once the `[param]`
suffix is stripped, duplicates are dropped and each shard's two `#` header lines are left out (checked as sets for
the two directories). `moves.json` has no entry for these files, so every current id is its baseline id. The 26
must-keep functions are survivors and never candidates. `must_keep --check` exits 0 at `6b8e9bb` and on the pruned
clone, so `must_keep/` stays byte-identical. The survivors outside the universe are
`tests/tokens/test_token_store_contract.py`: 35 functions, 29 must-keep. Its six basic CRUD cases are not
must-keep, but they are not in the Files block, so they are survivors and never candidates.

Every id's checks come from `tests/_manifests/baseline_checks/{tokens,team_config}.json`. Its outcomes come from
`expected_outcomes.{minimal,full}.json`. Every universe id passed in both environments, with two exceptions. The
six `tokens/test_postgres_store.py` ids are `skipped:asyncpg not installed` in minimal and `passed` in full. The
`[postgres]` case of each `team_config/test_postgres_store.py` contract test is skipped in minimal the same way.
Every contract `[postgres]` survivor has the same pair of outcomes, so no survivor skips where its candidate passed.

## Rules applied

- **(A) Contract-case repeat** (plan Task 6b, item 1). A candidate repeats a contract case when that case, on the
  same backend (`[in_memory]` for the in-memory files, `[postgres]` for `tokens/test_postgres_store.py`), makes
  the same checks on the same inputs: the same store class and construction, the same method calls in the same
  order, equal or stronger predicates, and the same negative checks. Differences in opaque values (token strings,
  user ids, a scopes tuple the candidate never asserts) are recorded in the note. A check the contract case does
  not make is a backend-specific check, and the id stays, unless another surviving test makes it on the same
  backend. For a whole-object equality the test applied is: does any survivor assert every field after the same
  write method? If one field is missing, the id stays.
- **(B) Criterion (3), intra-layer twin** (rev 10) for the rest: `tokens/test_issuance.py`'s 8 and the non-store
  test of `team_config/test_store.py`. The candidate's checks must be a strict subset of a survivor's on the same
  construction, production path, predicates and negative checks.
- **(C) The second checklist item** is decided by the code path, not the name ([below](#second-checklist-item)).
- **Fault injection.** Required when the survivor runs another production path. For a same-class, same-method
  survivor in another file, rev 10's waiver applies, and the row still names the `src/` line. All 14 deletions
  were **dry-run on the scratch clone**: both venvs for in-memory, full for Postgres (where the candidate and
  survivor both pass). Six more injections show why six kept ids have no survivor (the `K` rows below).

**Fixture comparison** (the differences every Postgres row cites):

| | contract `[postgres]` (`test_token_store_contract.py:45-72`) | `tokens/test_postgres_store.py` `pg_store` (`:52-75`) |
|---|---|---|
| store | `PostgresTokenStore(pg_dsn(), pool_max_size=12)`, 10 connections acquired and released up front | `PostgresTokenStore(_dsn())`, default pool (5) |
| DSN | `pg_dsn()` = `CORP_TEST_PG_DSN` → demo default | `_dsn()` = `CORP_TEST_PG_DSN` → `CORP_LLM_PG_DSN` → `pg_dsn()` |
| clean slate | `init_schema()`, `TRUNCATE corp_tokens` before | `init_schema()`, `DELETE … WHERE user_id LIKE 'pg-test-%'` before and after |
| unreachable | `skip_or_fail` | `skip_or_fail` |
| `TokenInfo` | `_info`: user `alice`, team `t1`, `scopes=("read",)` | `_info`: user `pg-test-alice`, team `t1`, `scopes=("read", "write")` |
| upsert | `_upsert` (calls `store.upsert`, awaits the coroutine) | `await pg_store.upsert(...)` |
| tokens | fixed (`ct-contract-1`, `ct-c-tok1`, …) | random (`_tok()` = `pg-itest-<8 hex>`) |

In the full environment and on CI, `CORP_TEST_PG_DSN` is set (CI pins it:
`test_token_store_contract.py::test_ci_test_job_runs_the_postgres_the_contract_tests_need`), so both fixtures
reach the same database. The `CORP_LLM_PG_DSN` fallback, `pg_store`, `skip_or_fail` and the `DELETE` clean-up
stay in the file after phase 2, because the seven must-keep races use them. The plan's "backend-specific checks
(asyncpg errors, DSN handling, `skip_or_fail`) stay" is met by the residual file, and no deleted id asserts any of
them. On the in-memory side, the contract's `[in_memory]` factory is `InMemoryTokenStore()` /
`InMemoryTeamConfigStore()`, as each candidate constructs it. `_upsert` on the sync in-memory store is a plain
`store.upsert(info)` call (its result is `None`, so nothing is awaited). The team_config `_team` helpers are
identical in both files.

Columns: `checks` is the shard line (`a` asserts / `r` raises / `f` fail). Every test also carries the delegated
`_no_logger_state_left_behind` fail 1 (conftest autouse), not repeated. The Postgres-backed ids also carry
delegated `skip_or_fail` fail 1. `def line` is the `def` statement at `6b8e9bb`. Fault-injection failing lines are
`6b8e9bb`'s (the contract files do not change in phase 2).

## Per-file tables

### `tests/tokens/test_in_memory.py` — 4 ids

| node id | def line at 6b8e9bb | checks | class | decision | survivor node id(s) | semantic note | fault injection |
|---|---|---|---|---|---|---|---|
| `test_lookup_unknown_returns_none` | :21 | a1 r0 f0 | contract repeat: `test_lookup_unknown_returns_none` | delete | `tests/tokens/test_token_store_contract.py::test_lookup_unknown_returns_none[in_memory]` | Contract repeat, same backend. Same `InMemoryTokenStore()` (the contract's `_make_in_memory` factory), same single `lookup` of an absent token, same assert `is None`. The missing key is `"missing"` here and `"ct-contract-unknown"` in the survivor, an opaque string on an empty store. | n/a (same path: `InMemoryTokenStore.lookup`, rev 10 waiver), run anyway: `src/corp_llm_gateway/tokens/in_memory.py:21` `return self._tokens.get(corp_token)` → `….get(corp_token, TokenInfo(corp_token, '', '', (), datetime.now(UTC), datetime.now(UTC)))`. The candidate failed `test_in_memory.py:23` and the survivor failed `test_token_store_contract.py:99` (`assert TokenInfo(…) is None`), both venvs. Reverted, `git diff --stat -- src/` empty |
| `test_upsert_and_lookup` | :27 | a1 r0 f0 | contract repeat (weaker survivor) | keep | — | No survivor as strong. `lookup("tok-1") == info` compares all 7 fields. The contract's `test_upsert_and_lookup[in_memory]` asserts `user_id`, `team_id`, `scopes` and `revoked_at` only. No surviving test asserts `corp_token`, `issued_at` or `expires_at` after an `upsert` on the in-memory store (the contract asserts them only after `issue_for_subject`, another write method). `expires_at` is read after an upsert only through `AuthMiddleware` (`tokens/test_middleware.py::test_expired_token_raises`, another path). Keep evidence K1. | — (K1) |
| `test_revoke_user_marks_all_their_tokens` | :35 | a4 r0 f0 | contract repeat: `test_revoke_user_marks_all_their_tokens` | delete | `tests/tokens/test_token_store_contract.py::test_revoke_user_marks_all_their_tokens[in_memory]` | Contract repeat, same backend. Same three rows (alice ×2, bob ×1) through `upsert`. Here it is a direct sync `store.upsert(...)`; in the survivor it is `_upsert`, which makes the same call and awaits nothing for the sync store. Same `revoke_user("alice")`, the same four asserts in the same order (`n == 2`, both alice rows revoked, bob's not). The token names differ (`tok-1..3` vs `ct-c-tok1..3`). | n/a (same path: `InMemoryTokenStore.revoke_user`), run anyway: `src/corp_llm_gateway/tokens/in_memory.py:27` `if info.user_id == user_id and info.revoked_at is None:` → `if info.revoked_at is None:`. The candidate failed `test_in_memory.py:42` and the survivor failed `test_token_store_contract.py:140` (`assert 3 == 2`), both venvs. Reverted, empty |
| `test_revoke_user_idempotent` | :53 | a2 r0 f0 | contract repeat: `test_revoke_user_idempotent` | delete | `tests/tokens/test_token_store_contract.py::test_revoke_user_idempotent[in_memory]` | Contract repeat, same backend. One alice row through `upsert` (direct sync call vs `_upsert`), `revoke_user("alice")` twice, the same asserts `n1 == 1`, `n2 == 0`. The token name differs (`tok-1` vs `ct-c-idem1`). | n/a (same path), run anyway: `src/corp_llm_gateway/tokens/in_memory.py:27` → `if info.user_id == user_id:` (drops the "not yet revoked" filter). The candidate failed `test_in_memory.py:59` and the survivor failed `test_token_store_contract.py:156` (`assert 1 == 0`), both venvs. Reverted, empty |

### `tests/tokens/test_postgres_store.py` — 6 ids

| node id | def line at 6b8e9bb | checks | class | decision | survivor node id(s) | semantic note | fault injection |
|---|---|---|---|---|---|---|---|
| `test_pg_lookup_unknown_returns_none` | :79 | a2 r0 f0; fixture `pg_store` | contract repeat: `test_lookup_unknown_returns_none` | delete | `tests/tokens/test_token_store_contract.py::test_lookup_unknown_returns_none[postgres]` | Contract repeat, same backend (`PostgresTokenStore`, same `lookup`, same `is None`). Fixture: `pg_store` (`_dsn()`, default pool, `DELETE … LIKE 'pg-test-%'`) vs the contract's `_try_make_postgres` (`pg_dsn()`, 12-connection warmed pool, `TRUNCATE`). Both resolve `CORP_TEST_PG_DSN` first. The missing key is random `pg-missing-<hex>` vs `ct-contract-unknown`. The extra `isinstance(pg_store, PostgresTokenStore)` only narrows the fixture's `object` type: the fixture builds that class, and no production change can fail it. The must-keep `test_pg_a_pool_held_past_the_acquire_timeout_raises_timeout_not_waits` (assert at `:460`) also asserts a Postgres miss is `None`. | n/a (same path: `PostgresTokenStore.lookup`), run anyway in full (both skip in minimal): `src/corp_llm_gateway/tokens/postgres_store.py:189` `return None` → `return TokenInfo(corp_token, '', '', (), datetime.now(UTC), datetime.now(UTC))`. The candidate failed `test_postgres_store.py:83` and the survivor failed `test_token_store_contract.py:99`. Reverted, empty |
| `test_pg_upsert_and_lookup` | :87 | a8 r0 f0; fixture `pg_store` | backend-specific | keep | — | Backend-specific check the contract lacks: `got.scopes == ("read", "write")`, a 2-element `TEXT[]` round trip in order. The contract stores and asserts `("read",)` only, and no other Postgres test asserts a multi-scope row. `got.corp_token == tok` and the `issued_at` / `expires_at` `tzinfo` asserts do have implicit survivors on the same read function `_row_to_token_info`: `[postgres]` `test_parallel_distinct_jtis_at_full_capacity` (`corp_token`) and `test_issue_for_subject_stores_an_active_row` (`stored.issued_at == _T0`, an aware compare). Both write through `issue_for_subject`, not `upsert`. The scopes assert alone keeps it. Keep evidence K2. | — (K2) |
| `test_pg_revoke_reflects_in_lookup` | :104 | a5 r0 f0; fixture `pg_store` | contract repeat: `test_revoke_user_idempotent` + `test_revoke_user_marks_all_their_tokens`; tz: `test_a_subject_at_cap_with_only_expired_and_revoked_rows_revokes_nothing` | delete | `tests/tokens/test_token_store_contract.py::test_revoke_user_idempotent[postgres]`, `::test_revoke_user_marks_all_their_tokens[postgres]`, `::test_a_subject_at_cap_with_only_expired_and_revoked_rows_revokes_nothing[postgres]` | Contract repeat across three cases, same backend. `n == 1` on one row is the first assert of `test_revoke_user_idempotent[postgres]`. "revoked after `revoke_user`, read by `lookup`" is `test_revoke_user_marks_all_their_tokens[postgres]` (`a1`/`a2 .revoked_at is not None`). `revoked_at.tzinfo is not None` is the third case's `kept.revoked_at == revoked_at` (`:513`, `:521`): an aware `revoked_at` compared after `lookup`, which fails if the read returns a naive value. The tz assert reads `revoke_user`'s value, the survivor reads an `upsert`-written one. The tz of a read row is set only by `_row_to_token_info` / `_ensure_utc_opt` (`postgres_store.py:77-89`) on a `TIMESTAMPTZ` column, so no write-side change reaches it. Fixture, DSN, scopes, token naming and the `isinstance` assert: as `test_pg_lookup_unknown_returns_none`. | Required (the tz survivor writes through `upsert`, another method), full: (a) `src/corp_llm_gateway/tokens/postgres_store.py:78` `return None if dt is None else _ensure_utc(dt)` → `… else dt.replace(tzinfo=None)`. The candidate failed `test_postgres_store.py:115` (`revoked_at.tzinfo is not None`) and the survivor failed `test_token_store_contract.py:513` (`kept.revoked_at == revoked_at`). (b) `:206` `return int(status.split()[-1])` → `… + 1`. The candidate failed `:111` (`2 == 1`); `test_revoke_user_idempotent[postgres]` failed `:155` (`2 == 1`) and `test_revoke_user_marks_all_their_tokens[postgres]` failed `:140` (`3 == 2`). (c) `:201` `… AND revoked_at IS NULL` → `… AND revoked_at IS NULL AND FALSE`. The candidate failed `:111` (`0 == 1`) and `marks_all[postgres]` failed `:140` (`0 == 2`). Each reverted, empty |
| `test_pg_revoke_idempotent` | :119 | a3 r0 f0; fixture `pg_store` | contract repeat: `test_revoke_user_idempotent` | delete | `tests/tokens/test_token_store_contract.py::test_revoke_user_idempotent[postgres]` | Contract repeat, same backend. One row for the user, `revoke_user` twice, `n1 == 1`, `n2 == 0`. Here: user `pg-test-alice`, scopes `("read", "write")`, a random token and direct `await pg_store.upsert`. In the survivor: user `alice`, `("read",)`, a fixed token and `_upsert`. Fixture and `isinstance`: as above. | n/a (same path: `PostgresTokenStore.revoke_user`), run anyway in full: `src/corp_llm_gateway/tokens/postgres_store.py:201` `WHERE user_id = $2 AND revoked_at IS NULL` → `WHERE user_id = $2`. The candidate failed `test_postgres_store.py:128` and the survivor failed `test_token_store_contract.py:156` (`assert 1 == 0`). Reverted, empty |
| `test_pg_upsert_overwrite` | :132 | a3 r0 f0; fixture `pg_store` | contract repeat: `test_upsert_overwrite` | delete | `tests/tokens/test_token_store_contract.py::test_upsert_overwrite[postgres]` | Contract repeat, same backend. Two `upsert`s of one token, the second with `revoked_at=now`, then `lookup`; same asserts (`got is not None`, `revoked_at is not None`). The candidate's second `_info(tok, revoked_at=now)` also rewrites `issued_at` / `expires_at` with a fresh `now`, while the survivor keeps them. Neither test asserts them. Scopes, user, token naming, dispatch, fixture and `isinstance`: as above. | n/a (same path: `PostgresTokenStore.upsert` `ON CONFLICT`), run anyway in full: `src/corp_llm_gateway/tokens/postgres_store.py:160` `revoked_at = EXCLUDED.revoked_at` → `revoked_at = corp_tokens.revoked_at`. The candidate failed `test_postgres_store.py:142` and the survivor failed `test_token_store_contract.py:130` (`revoked_at is not None`). Reverted, empty |
| `test_pg_revoke_only_affects_target_user` | :146 | a4 r0 f0; fixture `pg_store` | contract repeat: `test_revoke_user_marks_all_their_tokens` (+ `test_revoke_user_idempotent` for the count) | delete | `tests/tokens/test_token_store_contract.py::test_revoke_user_marks_all_their_tokens[postgres]`, `::test_revoke_user_idempotent[postgres]` | Subset by the asserts, same backend and method. Candidate: target ×1 + bystander ×1, `n == 1`, target revoked, bystander not. Survivor: the same shape with one more target row (alice ×2 + bob ×1), `n == 2`, both target rows revoked, the bystander not. The bystander check is the same assert on the same situation. "The count is exactly the target's unrevoked rows, the bystander not counted" is `n == 2` (not 3) there. `n == 1` on a single target row is `test_revoke_user_idempotent[postgres]`'s first assert. Users `pg-test-alice` / `pg-test-bob` vs `alice` / `bob`; scopes, token naming, fixture and `isinstance`: as above. | n/a (same path: `PostgresTokenStore.revoke_user`), run anyway in full: `src/corp_llm_gateway/tokens/postgres_store.py:201` → `WHERE (user_id = $2 OR TRUE) AND revoked_at IS NULL`. The candidate failed `test_postgres_store.py:155` (`2 == 1`) and the survivor failed `test_token_store_contract.py:140` (`3 == 2`). Reverted, empty |

### `tests/tokens/test_issuance.py` — 8 ids

Every test here builds `TokenIssuer(InMemoryTokenStore(), verifier)` **without** a policy. That is the path
`issuance.py:95-105` mints on and `_store_upsert`s itself, and the path `gateway-admin token issue` uses
(`cli/admin.py:661`, break-glass). `tokens/test_issuance_policy.py` (must-keep) builds the same class with
`policy=IssuancePolicy(...)`, which returns at `:93-94` before any of those lines.

| node id | def line at 6b8e9bb | checks | class | decision | survivor node id(s) | semantic note | fault injection |
|---|---|---|---|---|---|---|---|
| `test_default_ttl_is_30_days` | :25 | a1 r0 f0 | other (TTL default constant) | keep | — | Not a contract case, no twin. `DEFAULT_TOKEN_TTL_DAYS == 30` is the policy-less TTL and `gateway-admin token issue --ttl-days`'s default (`cli/admin.py:796`). The nearest test, `::test_issues_token_with_default_ttl`, bounds `expires_at` to `[29 d, 30 d + 1 min]`, which a value of 29 also passes. Noted for Task 7. | — |
| `test_issues_token_with_default_ttl` | :30 | a3 r0 f0 | other | keep | — | Not a contract case, no twin. The strongest neighbours: `healthz/test_server.py::test_issue_token_via_bearer_header_returns_valid_token` (same policy-less issuer; `ct_` prefix and an `expires_at` key, no TTL window) and must-keep `test_issuance_policy.py::test_issue_stores_a_token_with_the_configured_ttl` (exact TTL, but from `IssuanceSettings` on the policy setup). | — |
| `test_issued_token_lookups_in_store` | :41 | a4 r0 f0 | other | keep | — | Not a contract case, no twin. It ties the returned `corp_token` to the stored row's user / team / scopes on the policy-less path. Nearest: `cli/test_admin.py::test_token_issue_persists` (same `issue` lines through the CLI, but it finds the row by `list_tokens("alice")`, so it never ties the returned token to the stored key) and `healthz/test_issue_token_route.py::test_a_bodiless_post_with_a_bearer_issues_a_stored_token` (`lookup(...) is not None` only). | — |
| `test_issued_token_authenticates_via_middleware` | :55 | a1 r0 f0 | distinct setup (second checklist item) | keep | — | Distinct setup: the only test that authenticates a token minted by `TokenIssuer` without a policy. The must-keep namesake (`test_issuance_policy.py:103`) mints through `IssuancePolicy.issue`. See [below](#second-checklist-item). Keep evidence K6. | — (K6) |
| `test_missing_oidc_token_rejected` | :65 | a0 r1 f0 | distinct setup | keep | — | Distinct setup. The must-keep `test_issuance_policy.py::test_token_issuer_verifies_before_the_policy_runs` makes the same `issue("")` → `OidcVerificationError` check, but only with a policy. Today both run `issuance.py:90-91` before the policy branch, but a change that guards that check on the policy escapes the survivor. Keep evidence K4. | — (K4) |
| `test_invalid_oidc_token_rejected` | :72 | a0 r1 f0 | distinct setup | keep | — | Distinct setup, as `test_missing_oidc_token_rejected`: same check as the must-keep survivor's `issue("invalid")`, which runs only with a policy. Keep evidence K5. | — (K5) |
| `test_custom_ttl_respected` | :79 | a1 r0 f0 | other | keep | — | Not a contract case, no twin. No other test passes `ttl=` to the issuer and checks expiry (`cli/test_admin.py` passes `--ttl-days` and asserts no expiry). Noted for Task 7 (upper bound only). | — |
| `test_each_issue_returns_unique_token` | :91 | a1 r0 f0 | other | keep | — | Not a contract case, no twin. Two mints on the policy-less path return different tokens. `compose/test_nginx_profile.py` checks only the shape of one `default_token_factory()` value. | — |

### `tests/tokens/test_issuance_policy.py` — 0 ids

All 16 functions are must-keep (step-2 glob `tokens/test_{oidc_verifier,issuance_policy,middleware*}.py`). They
are survivors, with no row and no edit in phase 2.

### `tests/team_config/test_store.py` — 8 ids

| node id | def line at 6b8e9bb | checks | class | decision | survivor node id(s) | semantic note | fault injection |
|---|---|---|---|---|---|---|---|
| `test_team_config_defaults` | :22 | a5 r0 f0 | other (dataclass defaults) | keep | — | Not a store case, no twin: a `TeamConfig` constructed with no store. Its asserts are policy defaults (retention 90 days / 7 years, `fail_policy == FailPolicyOverrides()`), which Task 7's trivial-test rule keeps. The must-keep `test_default_fail_policy_matches_matrix` asserts the three fields of `FailPolicyOverrides()` but not `TeamConfig`'s defaults. | — |
| `test_get_unknown_team_raises` | :42 | a0 r1 f0 | contract repeat: `test_get_unknown_raises` | delete | `tests/team_config/test_postgres_store.py::test_get_unknown_raises[in_memory]` | Contract repeat, same backend. Same `InMemoryTeamConfigStore()` (the contract's `_make_in_memory`), same `pytest.raises(TeamNotFoundError)` around one `get` on an empty store. The team id differs (`"missing"` vs `"nobody-has-this-id"`). | n/a (same path: `InMemoryTeamConfigStore.get`), run anyway: `src/corp_llm_gateway/team_config/in_memory.py:12` `raise TeamNotFoundError(team_id)` → `return TeamConfig(team_id, '')`. The candidate failed `test_store.py:44` and the survivor failed `test_postgres_store.py:71` (`DID NOT RAISE`), both venvs. Reverted, empty |
| `test_upsert_and_get` | :49 | a1 r0 f0 | contract repeat (weaker survivor set) | keep | — | No survivor as strong. `get("t1") == cfg` compares all 7 fields of `_team("t1")` on the default input. `InMemoryTeamConfigStore.get` returns the stored object itself (`in_memory.py:9-16`), so the equality can fail only on lines the contract also runs. But the `[in_memory]` survivor set asserts `team_id` and `name` (`test_upsert_and_get`, name `"One"`), `profile_ids == ()` (`test_profile_ids_default_round_trip`) and `fail_policy` (`test_fail_policy_defaults_round_trip`, `test_fail_policy_overrides_round_trip`) on a default input. It asserts `replace_md_path` and the retention fields only with non-default values (`test_replace_md_path_round_trip`, `test_retention_overrides_round_trip`). No survivor asserts `replace_md_path is None` or retention 90 / 7 after an `upsert`. Keep evidence K3. Reviewer question Q2. | — (K3) |
| `test_upsert_overwrites` | :57 | a1 r0 f0 | contract repeat: `test_upsert_overwrites` | delete | `tests/team_config/test_postgres_store.py::test_upsert_overwrites[in_memory]` | Contract repeat, same backend: the same two `upsert`s (`Original`, `Updated`), `get`, and assert `name == "Updated"`. The body is the same except the local name (`cfg` vs `got`). | n/a (same path: `InMemoryTeamConfigStore.upsert`), run anyway: `src/corp_llm_gateway/team_config/in_memory.py:16` `self._configs[config.team_id] = config` → `self._configs.setdefault(config.team_id, config)`. The candidate failed `test_store.py:62` and the survivor failed `test_postgres_store.py:88` (`'Original' == 'Updated'`), both venvs. Reverted, empty |
| `test_list_all_empty` | :66 | a1 r0 f0 | contract repeat: `test_list_all_empty` | delete | `tests/team_config/test_postgres_store.py::test_list_all_empty[in_memory]` | Contract repeat, same backend, identical body: `list_all() == ()` on an empty store. | n/a (same path: `InMemoryTeamConfigStore.list_all`), run anyway: `src/corp_llm_gateway/team_config/in_memory.py:19` → `return tuple(self._configs.values()) or (TeamConfig('x', 'x'),)`. The candidate failed `test_store.py:68` and the survivor failed `test_postgres_store.py:93`, both venvs. Reverted, empty |
| `test_list_all_returns_all` | :72 | a1 r0 f0; loops `teams` | contract repeat: `test_list_all_returns_all` | delete | `tests/team_config/test_postgres_store.py::test_list_all_returns_all[in_memory]` | Contract repeat, same backend, identical body: two `upsert`s (`t1`, `t2`), and `{t.team_id for t in teams} == {"t1", "t2"}`. | n/a (same path), run anyway: `src/corp_llm_gateway/team_config/in_memory.py:19` → `return tuple(self._configs.values())[:1]`. The candidate failed `test_store.py:77` and the survivor failed `test_postgres_store.py:101` (`{'t1'} == {'t1', 't2'}`), both venvs. Reverted, empty |
| `test_per_team_retention_overrides_persist` | :81 | a2 r0 f0 | contract repeat: `test_retention_overrides_round_trip` | delete | `tests/team_config/test_postgres_store.py::test_retention_overrides_round_trip[in_memory]` | Contract repeat, same backend: `upsert(_team("t1", retention_hot_days=30, retention_cold_years=1))`, `get`, the same two asserts. The body is the same except the local name. | n/a (same path: `InMemoryTeamConfigStore.get`), run anyway: `src/corp_llm_gateway/team_config/in_memory.py:13` `return config` → `return dataclasses.replace(config, retention_cold_years=7)`. The candidate failed `test_store.py:86` and the survivor failed `test_postgres_store.py:109` (`7 == 1`), both venvs. Reverted, empty |
| `test_per_team_fail_policy_overrides_persist` | :90 | a1 r0 f0 | contract repeat: `test_fail_policy_overrides_round_trip` | delete | `tests/team_config/test_postgres_store.py::test_fail_policy_overrides_round_trip[in_memory]` | Subset by the asserts, same backend and methods. The candidate overrides `audit_buffer_full="continue"` only and asserts that one field. The survivor overrides all three fields (`audit_buffer_full="continue"`, the other two `"fail-closed"`) and asserts `got.fail_policy == overrides`. That includes the candidate's field with the same value, and two more. The candidate's two unset fields are covered at their defaults by `test_fail_policy_defaults_round_trip[in_memory]`. Reviewer question Q3. | n/a (same path), run anyway: `src/corp_llm_gateway/team_config/in_memory.py:13` → `return dataclasses.replace(config, fail_policy=dataclasses.replace(config.fail_policy, audit_buffer_full='fail-closed'))`. The candidate failed `test_store.py:95` (`'fail-closed' == 'continue'`) and the survivor failed `test_postgres_store.py:149`, both venvs. Reverted, empty |

The two `dataclasses.replace` mutations were written as `__import__('dataclasses').replace(…)`, to keep each
mutation on one line (`in_memory.py` does not import `dataclasses`).

### `tests/team_config/test_postgres_store.py` — 11 ids

These 11 are the team_config contract: survivors, and no edit is expected in phase 2. The plan lists the file
because the plan's author expected edits there.

| node id | def line at 6b8e9bb | checks | class | decision | survivor node id(s) | semantic note | fault injection |
|---|---|---|---|---|---|---|---|
| `test_get_unknown_raises` | :70 | a0 r1 f0 | contract case | keep | — | Survivor (contract case) for `test_store.py::test_get_unknown_team_raises`. | — |
| `test_upsert_and_get` | :76 | a2 r0 f0 | contract case | keep | — | Survivor (contract case), part of the set for `test_store.py::test_upsert_and_get` (kept, Q2). | — |
| `test_upsert_overwrites` | :84 | a1 r0 f0 | contract case | keep | — | Survivor (contract case) for `test_store.py::test_upsert_overwrites`. | — |
| `test_list_all_empty` | :92 | a1 r0 f0 | contract case | keep | — | Survivor (contract case) for `test_store.py::test_list_all_empty`. | — |
| `test_list_all_returns_all` | :97 | a1 r0 f0; loops `teams` | contract case | keep | — | Survivor (contract case) for `test_store.py::test_list_all_returns_all`. | — |
| `test_retention_overrides_round_trip` | :105 | a2 r0 f0 | contract case | keep | — | Survivor (contract case) for `test_store.py::test_per_team_retention_overrides_persist`. | — |
| `test_replace_md_path_round_trip` | :113 | a1 r0 f0 | contract case | keep | — | Survivor (contract case), no in-memory repeat in the universe. | — |
| `test_profile_ids_round_trip` | :120 | a1 r0 f0 | contract case | keep | — | Survivor (contract case), no repeat in the universe. | — |
| `test_profile_ids_default_round_trip` | :127 | a1 r0 f0 | contract case | keep | — | Survivor (contract case), no repeat in the universe. | — |
| `test_fail_policy_defaults_round_trip` | :134 | a1 r0 f0 | contract case | keep | — | Survivor (contract case), with `:141` the cover for `test_store.py::test_per_team_fail_policy_overrides_persist`. | — |
| `test_fail_policy_overrides_round_trip` | :141 | a1 r0 f0 | contract case | keep | — | Survivor (contract case) for `test_store.py::test_per_team_fail_policy_overrides_persist`. | — |

### Keep evidence (scratch clone; each reverted, `git diff --stat -- src/` empty after each)

| # | mutation | candidate | the survivors examined |
|---|---|---|---|
| K1 | `tokens/in_memory.py:18` `self._tokens[info.corp_token] = info` → `= dataclasses.replace(info, expires_at=info.expires_at + timedelta(seconds=1))` | `test_in_memory.py::test_upsert_and_lookup` failed `:31` (minimal, full) | every `test_token_store_contract.py` `[in_memory]` case passed (34) |
| K2 | `tokens/postgres_store.py:86` `scopes=tuple(row["scopes"])` → `tuple(row["scopes"][:1])` | `test_pg_upsert_and_lookup` failed `:97` (`('read',) == ('read', 'write')`, full) | every `test_token_store_contract.py` `[postgres]` case passed (34, plus the CI-wiring test that `-k postgres` also matched) |
| K3 | `team_config/in_memory.py:13` `return config` → `return dataclasses.replace(config, replace_md_path=config.replace_md_path or '')` | `test_store.py::test_upsert_and_get` failed `:53` (minimal, full) | every `team_config/test_postgres_store.py` `[in_memory]` case passed (11) |
| K4 | `tokens/issuance.py:90` `if not oidc_token:` → `if not oidc_token and self._policy is not None:` | `test_missing_oidc_token_rejected` failed `:67` (`DID NOT RAISE`) | `test_issuance_policy.py::test_token_issuer_verifies_before_the_policy_runs` passed |
| K5 | `tokens/issuance.py:92` `claims = await self._verifier(oidc_token)` → `… if self._policy is not None else OidcClaims('x', 'x')` | `test_invalid_oidc_token_rejected` failed `:74` | the same must-keep test passed |
| K6 | `tokens/issuance.py:98` `corp_token=corp_token,` → `corp_token=corp_token + 'x',` (stored under another key than returned) | `test_issuance.py::test_issued_token_authenticates_via_middleware` failed (`InvalidTokenError: unknown token`, `middleware.py:70`) | `test_issuance_policy.py::test_issued_token_authenticates_via_middleware` and `::test_token_issuer_delegates_to_the_policy` passed |

## Totals

| file | universe | keep | delete |
|---|---|---|---|
| `tests/tokens/test_in_memory.py` | 4 | 1 | 3 |
| `tests/tokens/test_postgres_store.py` | 6 | 1 | 5 |
| `tests/tokens/test_issuance.py` | 8 | 8 | 0 |
| `tests/tokens/test_issuance_policy.py` | 0 | 0 | 0 |
| `tests/team_config/test_store.py` | 8 | 2 | 6 |
| `tests/team_config/test_postgres_store.py` | 11 | 11 | 0 |
| **total** | **37** | **23** | **14** |

Keep by reason:

| reason | in_memory | tokens pg | issuance | team store | team pg | total |
|---|---|---|---|---|---|---|
| survivor (contract case) | 0 | 0 | 0 | 0 | 11 | 11 |
| backend-specific check the contract lacks | 0 | 1 | 0 | 0 | 0 | 1 |
| no survivor as strong (whole-object equality) | 1 | 0 | 0 | 1 | 0 | 2 |
| distinct setup (`TokenIssuer` without a policy) | 0 | 0 | 3 | 0 | 0 | 3 |
| not a contract case, no twin | 0 | 0 | 5 | 1 | 0 | 6 |
| **keep** | **1** | **1** | **8** | **2** | **11** | **23** |

1 + 1 + 8 + 2 + 11 = 23; + 14 deletes = 37.

## Second checklist item

> `test_issued_token_authenticates_via_middleware` and `test_same_jti_twice_is_a_replay` (issuance /
> issuance_policy / contract): one survives per distinct setup, reviewer's note

**`test_issued_token_authenticates_via_middleware`: both survive; they are two setups.**

- `tokens/test_issuance.py:55` (not must-keep). `TokenIssuer(store, verifier)` with **no policy**: `issue()`
  passes `:90-92` and skips `:93-94`. At `:95-105` it mints `ct_…` itself, builds `TokenInfo` with
  `issued_at=now`, `expires_at=now + ttl`, and calls `_store_upsert` (sync or async `upsert`). Then
  `AuthMiddleware(store).authenticate(token)`. This is the path `gateway-admin token issue` uses
  (`cli/admin.py:661`).
- `tokens/test_issuance_policy.py:103` (must-keep, step-2 glob). `IssuancePolicy.issue(claims)`: the policy's
  TTL, token factory and per-subject `issue_for_subject` (cap, interval, jti) at a fixed clock, then
  `authenticate(token, now=clock.now)`. The `TokenIssuer` with a policy returns at `issuance.py:93-94` and never
  reaches `:95-105`.
- Does anything else authenticate a token minted on the policy-less path? No. Every `.authenticate(` in `tests/`
  was checked. `test_issue_token_composition.py:168` authenticates a router-issued token, but
  `bootstrap._build_token_issuer` (`bootstrap.py:477-481`) always passes `policy=`. `test_bootstrap*.py`
  authenticates `solo-dev-token` / `demo-team-token`, which bootstrap upserts and no issuer mints.
  `invariants/test_no_originals_leak.py:237` uses an unknown token. `test_middleware*.py` upserts its own rows.
  `test_issuance.py::test_issued_token_lookups_in_store` reads the row but never authenticates it, and
  `cli/test_admin.py::test_token_issue_persists` neither looks the token up nor authenticates it. K6 shows the
  policy-path survivors miss a policy-less mint that stores under another key.
- Reviewer's note (for phase 2's tick): *"Two setups: the policy-less `TokenIssuer` mint (`issuance.py:95-105`,
  the `gateway-admin token issue` path) and `IssuancePolicy.issue` (per-subject issuance). The plan keeps one per
  distinct setup, so both stay; nothing else authenticates a policy-less mint."*

**`test_same_jti_twice_is_a_replay`: both survive by must-keep; no decision is needed.** It exists only in
`tokens/test_issuance_policy.py:162` (must-keep, step-2 glob; the policy layer at a fixed clock) and
`tokens/test_token_store_contract.py:343` (must-keep, 3 ids: the function, `[in_memory]` and `[postgres]`; the
store's `issue_for_subject`). Neither is in the universe and neither can be deleted. Note for phase 2's tick:
*"Both must-keep (step-2 glob; step-1 touched); two layers, policy and store; not candidates."*

## Residual files — what phase 2 leaves

- **`tests/tokens/test_in_memory.py`**: `test_upsert_and_lookup` stays (Q1), so the module is **not**
  removed. Phase 2 leaves the imports, `_info` and that test, 59 → 25 lines. No import becomes unused (`pytest`,
  `datetime` / `UTC` / `timedelta` and `InMemoryTokenStore` / `TokenInfo` are still used), and ruff is clean. The
  deletions leave two trailing blank lines at EOF, which phase 2 strips (the dry run did).
  **If Q1 is ruled delete, the module goes with `git rm`**. The gate evidence for that, dry-run on the clone
  (all 15 deletions, module removed):
  - `inventory --check`: exit 1 with exactly 15 `missing test` lines (the 11 others + the module's 4) and nothing
    else. A missing file trips no other inventory line: the module has no `external_deps.json` entry, no
    negative-log site, no `name_pinned.json` entry and no `must_keep/tokens.txt` id.
  - `ledger check minimal <run> --scope tests/tokens/ tests/team_config/`: 15 `missing id` lines, 4 of them the
    module's.
  - After removing the file's line from both `expected_outcomes.*.json` and `inventory --write`: the ledgers hold
    5,917 ids. `baseline_checks/tokens.json` has 9 fewer `tests` and 9 fewer `cases` entries. `inventory`,
    `must_keep`, `moves`, `name_pinned` and `negative_logs --check` all exit 0. `tests/_gates` passes 33 in both
    venvs. `name_pinned.json`, `negative_log_checks.json`, `external_deps.json`, `must_keep/` and `moves.json`
    do not change.
  - The Files block says Modify, so removing the module is a rev decision. Option A: `git rm` the module (no
    empty module left behind). Option B: keep a module holding `_info` only, with no test (pytest collects
    nothing, and ruff F401 would then flag `pytest` and `InMemoryTokenStore`, so the imports would be edited
    too). A is proposed if Q1 is ruled delete.
- **`tests/team_config/test_store.py`**: 99 → 49 lines. It keeps `_team`, `test_team_config_defaults`, the
  must-keep `test_default_fail_policy_matches_matrix` and `test_upsert_and_get` (Q2). Exactly one import line is
  removed: `    TeamNotFoundError,` (`:9`; it was used only by `test_get_unknown_team_raises`). Without that edit,
  ruff F401 fails pre-commit. `pytest` and `InMemoryTeamConfigStore` stay in use by `test_upsert_and_get`.
  `DEFAULT_RETENTION_*`, `FailPolicyOverrides` and `TeamConfig` stay in use. `_team` is used by
  `test_team_config_defaults` and `test_upsert_and_get`. The `# In-memory CRUD ---` banner stays (one test under
  it). The trailing pointer comment (`:98-99`, "PostgresTeamConfigStore is contract-tested against the in-memory
  store in tests/team_config/test_postgres_store.py …") stays true, unchanged. An optional reword, if the
  reviewer wants the comment to say where the deleted cases went: `# The TeamConfigStore CRUD contract (in-memory
  and Postgres) is tests/team_config/test_postgres_store.py (Postgres cases skip without asyncpg).` (One comment
  line; it changes no test's body hash.) Gate self-tests L1 / L3 use this module whole. On the pruned clone
  `selftest ledger` still rejects both (L3 on `test_default_fail_policy_matches_matrix`).
- **`tests/tokens/test_postgres_store.py`**: 489 → 422 lines. `pg_store`, `_dsn`, `_tok`, `_info`, `_issue_for`,
  `_wait_for_lock_waiter` and `_issue_many` stay in use by the seven must-keep tests (`pg_store` in the four
  fixture-using races and in `test_pg_upsert_and_lookup`; `_dsn()` in the three that build their own store; `_tok`
  / `_info` in all). No helper is orphaned. No import goes unused (ruff clean). `_info`'s `user_id=` and
  `revoked_at=` keyword parameters lose their last callers (`test_pg_revoke_only_affects_target_user`,
  `test_pg_upsert_overwrite`). The helper is **not** edited, because a change would move the `body_hash` of every
  must-keep test that reaches it. Noted for Task 7. The module docstring (`:1-7`) stays true. An optional sentence
  for phase 2, if the reviewer wants it: `Basic CRUD (lookup, upsert, revoke) runs against both stores in
  tests/tokens/test_token_store_contract.py; this file keeps the Postgres-only checks.` A module docstring is in
  no test's body hash. The dry run did not add it.
- **`tests/tokens/test_issuance.py`**: nothing is deleted, and nothing structural changes.
- **`tests/tokens/test_issuance_policy.py`**, **`tests/team_config/test_postgres_store.py`**: no edit.

## Negative-log, name-pinned, external-deps, gate-gap findings

- `negative_log_checks.json`: 45 lines mention `tokens/` or `team_config/`, all
  `tests/tokens/test_oidc_verifier.py` (4 `sites` rows × site + owner = 8 lines; 37 `security_node_ids`), none in
  `test_middleware*.py`. **No row sits in the six files**: no deleted id is an owner and no site drifts.
  `negative_logs --check` and `--write` change nothing (dry run).
- `name_pinned.json`: no id or file of the six files (or of the contract file) is indexed. `CLAUDE.md:301` cites
  `tests/storage/test_mapping_store.py` as the contract pattern; it is indexed (`name_pinned.json:623`) and
  untouched. `team_config/test_store.py:31-35` is cited only in the plan's Context bullet, which is gitignored and
  not a citation source. After phase 2 that function is at `:30-34` (−1, the import line). No doc under the
  citation sources cites a deleted id. `name_pinned --write` prints only the known
  `CLAUDE.md:177` placeholder line.
- `external_deps.json`: no entry for any of the six files. The contract file's one entry
  (`::test_ci_test_job_runs_the_postgres_the_contract_tests_need` → `.github/workflows/ci.yml`) belongs to a
  survivor and does not change.
- `docs/testing/deleted-tests.md`: no existing row cites a line of the three edited files, so no `(now: …)`
  annotation is needed.
- `must_keep.py` gate gap: none. No universe id runs a security negative-log check, is name-pinned, is in
  `POLICY_DEFAULTS`, or is a touched test of a `STEP1_MODIFIED_TOUCHED` file. `must_keep --check` is 0 on
  `6b8e9bb` and on the pruned clone.

## Predicted phase-2 manifest diff

Previewed on the scratch clone (ledgers spliced by hand with `ledger._dump`, then `inventory --write`,
`name_pinned --write`, `negative_logs --write`):

| manifest | change |
|---|---|
| `expected_outcomes.minimal.json` | 5,932 → 5,918 ids (−14: 9 `passed`, 5 `skipped:asyncpg not installed`), 0 added, 0 changed; 3 file lines change |
| `expected_outcomes.full.json` | 5,932 → 5,918 ids (−14, all `passed`), 0 added, 0 changed; 3 file lines change |
| `baseline_checks/tokens.json` | `tests` 191 → 183, `cases` 191 → 183 (−8 / −8, 0 changed as JSON); git −16 lines |
| `baseline_checks/team_config.json` | `tests` 22 → 16, `cases` 22 → 16 (−6 / −6, 0 changed as JSON); git +2 / −14 (`test_upsert_and_get`'s two entries become the last ones and lose their trailing comma) |
| `negative_log_checks.json` | unchanged (no site in the three files) |
| `must_keep/` | byte-identical (no `must_keep --write`) |
| `moves.json`, `name_pinned.json`, `coverage.*.json`, `not_applicable.json`, `external_deps.json` | unchanged |
| `docs/testing/deleted-tests.md` | +14 rows, `PR` = `Task 6b`, reviewer `auto-review (pending)` |

Predicted record runs: minimal collects 5,117 → 5,103 (−9 passed, −5 skipped), full 5,932 → 5,918 (−14 passed).
The one expected failure before regeneration is `tests/_gates/test_suite_gates.py::test_the_check_inventory_matches_the_baseline`
(the 14 `missing test` lines).

Lines removed per deletion (the decorator through the blank lines after it), and the drift of what stays:

- `tests/tokens/test_in_memory.py` 59 → 25: `:20-25` 6, `:34-59` 26 (the last two functions, with the blank lines
  between them), 2 trailing blank lines stripped; `test_upsert_and_lookup` `:27 → :21`.
- `tests/tokens/test_postgres_store.py` 489 → 422: `:78-85` 8, `:103-117` 15, `:118-130` 13, `:131-144` 14,
  `:145-161` 17; `test_pg_upsert_and_lookup` `:87 → :79`. Every must-keep test after `:161` moves −67:
  `test_pg_jti_unique_violation_is_a_replay_without_driver_detail` `:193 → :126`,
  `test_pg_subject_lock_wait_times_out_as_busy` `:239 → :172`, `test_init_schema_adds_oidc_columns_to_preexisting_table`
  `:281 → :214`, `test_pg_colliding_subject_locks_serialise_but_never_mix_rows` `:357 → :290`,
  `test_pg_issuance_waits_for_a_pool_connection_instead_of_failing` `:393 → :326`,
  `test_pg_a_pool_held_past_the_acquire_timeout_raises_timeout_not_waits` `:432 → :365`,
  `test_pg_a_statement_past_its_timeout_is_busy` `:466 → :399`. `must-keep.md` and the ledger cite none of these
  lines.
- `tests/team_config/test_store.py` 99 → 49: `:9` 1 (the import), `:41-47` 7, `:56-64` 9, `:65-70` 6, `:71-79` 9,
  `:80-88` 9, `:89-97` 9; `_team` `:13 → :12`, `test_team_config_defaults` `:22 → :21`,
  `test_default_fail_policy_matches_matrix` `:31 → :30`, `test_upsert_and_get` `:49 → :41`.
- `baseline_checks` entries carry no line, so no surviving test reports anything in `inventory --check`
  (dry run: only the 14 `missing test` lines).

## Coverage

The six files plus the contract file, `--cov=corp_llm_gateway --cov-branch`, before and after the deletions,
compared with `tests/_gates/coverage_gate.py`'s `from_report` / `drops`:

| env | module | before (lines / arcs) | after | whole-suite baseline |
|---|---|---|---|---|
| minimal | `tokens/in_memory.py` | 47 / 12 | 47 / 12 | 47 / 12 |
| minimal | `tokens/postgres_store.py` | 40 / 0 | 40 / 0 | 50 / 3 |
| minimal | `tokens/issuance.py` | 53 / 8 | 53 / 8 | 53 / 8 |
| minimal | `tokens/middleware.py` | 61 / 10 | 61 / 10 | 89 / 27 |
| minimal | `team_config/in_memory.py` | 14 / 2 | 14 / 2 | 14 / 2 |
| minimal | `team_config/postgres_store.py` | 30 / 0 | 30 / 0 | 33 / 0 |
| full | `tokens/in_memory.py` | 47 / 12 | 47 / 12 | 47 / 12 |
| full | `tokens/postgres_store.py` | 115 / 19 | 115 / 19 | 117 / 20 |
| full | `tokens/issuance.py` | 53 / 8 | 53 / 8 | 54 / 9 |
| full | `tokens/middleware.py` | 61 / 10 | 61 / 10 | 92 / 29 |
| full | `team_config/in_memory.py` | 14 / 2 | 14 / 2 | 14 / 2 |
| full | `team_config/postgres_store.py` | 75 / 11 | 75 / 11 | 76 / 12 |

`drops(before, after)` is empty in both environments, and no line or arc is lost or gained in any of the 122
`src/` files the run reports. The seven-file numbers are below the whole-suite baselines because other test files
reach more of these modules. Phase 2's whole-suite `coverage_gate check` compares against those baselines. The
Postgres-only `DELETE`-vs-`TRUNCATE` fixture difference touches no `src/` line.

## Dry run (scratch clone; nothing in the checkout's `tests/` or `src/` changed)

A `git clone --shared` of the checkout at `6b8e9bb` in the session scratch directory, `PYTHONPATH=src:.`, the two
venvs with the env from [must-keep.md](must-keep.md). Postgres `pg-test` was up on 55432. `fingerprint
{minimal,full} --check` → 0, and `must_keep --check` → 0 in both, before anything changed.

- **Before**: the seven files collected 147 ids in each venv (minimal 87 passed / 60 skipped, full 147 passed).
- **Fault injections** (before the deletions, one at a time, each reverted with `git checkout -- <file>`, `git
  diff --stat -- src/` empty after each). D1-D3 = `test_in_memory.py`'s three deletions in table order. D4 =
  `test_pg_lookup_unknown_returns_none`, D5 = `test_pg_revoke_idempotent`, D6 = `test_pg_revoke_only_affects_target_user`,
  D7 = `test_pg_upsert_overwrite`, and D8a-c = the three injections of `test_pg_revoke_reflects_in_lookup`.
  D9-D14 = `team_config/test_store.py`'s six deletions in table order. The in-memory ones (D1-D3, D9-D14) ran in
  both venvs and the Postgres ones (D4-D8) in full. Every deleted candidate and its survivor failed (the rows
  above), and K1-K6 failed only the kept candidate: 0 unexpected outcomes. Q1's hypothetical injection
  (`tokens/in_memory.py:18` → `dataclasses.replace(info, user_id='x')`) failed both
  `test_token_store_contract.py:108` (`'x' == 'alice'`) and `test_in_memory.py:31`, in both venvs.
- **Deletions**: the 14 functions removed by an AST script (decorator through the blank lines after it), trailing
  EOF blank lines stripped, and `TeamNotFoundError,` removed from `team_config/test_store.py`'s import. Diff:
  3 files, 151 deletions, 0 insertions. `ruff check` and `ruff format --check` on the three files: clean, already
  formatted.
- **Gates, manifests not regenerated**: `inventory --check` exit 1 with exactly 14 `missing test` lines (the 14
  ids) and nothing else, the same in both venvs. `must_keep`, `moves`, `name_pinned`, `negative_logs --check`: 0.
- **After**: the seven files minimal 78 passed / 55 skipped (133), full 133 passed. `tests/_gates`: 32 passed / 1
  failed in each venv. The failure is `test_the_check_inventory_matches_the_baseline` (the 14 lines), which phase
  2's regeneration clears. The ledgers-cover test passes, because a deletion adds no id.
- **Preview of the regeneration** (scratch only): the 14 ids spliced out of both ledgers, then the three writers.
  The diff is in the table above. Then `inventory`, `must_keep`, `moves`, `name_pinned`, `negative_logs --check`
  all exit 0 and `tests/_gates` passes 33 in both venvs. `ledger check <env> <run> --scope <the seven files>` on a
  plugin run of the seven files exits 0 in both. `selftest ledger` (minimal) rejects all four (L1-L4).
  `selftest inventory` (full) rejects 19 of 19.
- **Module-removal scenario** (Q1, a second scratch branch): see [Residual files](#residual-files--what-phase-2-leaves).

## Ledger rows (phase 2)

One row per deleted id, columns as in [deleted-tests.md](deleted-tests.md): `PR` = `Task 6b`; baseline outcome
`passed / passed` for the in-memory ids and `skipped:asyncpg not installed / passed` for the five Postgres ids;
"the check it made" = the `checks` column above plus the delegated `_no_logger_state_left_behind` fail 1 (and
`skip_or_fail` fail 1, fixture `pg_store`, for the Postgres ids); survivor, semantic note and fault injection =
the columns above, the injection re-run on the checkout and cited at HEAD; reviewer `auto-review (pending)`.

## Noted for Task 7

Not deletions here:

- `tokens/test_issuance.py::test_default_ttl_is_30_days`: a constant equals a literal. It is the policy-less TTL
  default (and the CLI's `--ttl-days` default), so Task 7's policy-default rule probably keeps it.
- `tokens/test_issuance.py::test_issues_token_with_default_ttl`: its lower bound `>= 29 days` also passes for a
  29-day default, so only the upper bound pins the value.
- `tokens/test_issuance.py::test_custom_ttl_respected`: an upper bound only (`<= 1 h + 5 s`); a TTL of zero passes.
- `assert isinstance(pg_store, PostgresTokenStore)` in `tokens/test_postgres_store.py` (the kept
  `test_pg_upsert_and_lookup` and four must-keep races): a type narrowing of the fixture's `object` that no
  production change can fail.
- `tokens/test_postgres_store.py::_info`: after phase 2, no caller passes `user_id=` or `revoked_at=`. It is left
  as is, because editing it would change must-keep body hashes.
- `team_config/test_store.py::test_team_config_defaults`: dataclass defaults, but policy defaults (retention,
  fail policy), so Task 7's rule keeps it.

## Open questions / decisions by rule

Decided by the rules:

- The universe recomputes to 37 (4 / 6 / 8 / 0 / 8 / 11), as the brief said.
- 14 deletions, all contract repeats on the same backend. 13 survivors run the same store class and method (rev
  10 waiver, injection run anyway). One (`test_pg_revoke_reflects_in_lookup`) needs a survivor that writes
  through another method, so its injections were required and were run (D8a-c).
- `test_pg_upsert_and_lookup` keeps: the 2-element `TEXT[]` scopes round trip is a backend-specific check no
  survivor makes (K2).
- The policy-less `TokenIssuer` is a distinct setup: `test_issued_token_authenticates_via_middleware`,
  `test_missing_oidc_token_rejected` and `test_invalid_oidc_token_rejected` stay (K4-K6). The other five
  `test_issuance.py` tests have no twin.
- `test_same_jti_twice_is_a_replay`: both ids are must-keep; recorded for phase 2's tick.
- No case-level deletion, no fold, no move. `test_issuance_policy.py` and `team_config/test_postgres_store.py`
  are not edited.

For the reviewer:

- **Q1 — `tokens/test_in_memory.py::test_upsert_and_lookup` (kept).** The whole-object equality pins `corp_token`
  / `issued_at` / `expires_at` after an in-memory `upsert`, which no survivor asserts (K1: a one-second
  `expires_at` shift in `upsert` fails only this test). If the reviewer reads the in-memory store's `upsert`
  (`self._tokens[info.corp_token] = info`) as trivially field-preserving and rules delete, then the survivor would
  be `test_token_store_contract.py::test_upsert_and_lookup[in_memory]`, plus the injection
  `tokens/in_memory.py:18` → `= dataclasses.replace(info, user_id='x')` (the survivor fails `:108`), and the
  module goes (option A above; the gate evidence is in [Residual files](#residual-files--what-phase-2-leaves)).
- **Q2 — `team_config/test_store.py::test_upsert_and_get` (kept).** Every field is asserted after an in-memory
  `upsert` → `get` by some contract case, but `replace_md_path is None` and retention 90 / 7 only with non-default
  values (K3: a `None → ''` change in `get` fails only this test). A delete would cite the six `[in_memory]`
  contract cases in the note and run the K3-style injection on a field the survivors cover.
- **Q3 — `team_config/test_store.py::test_per_team_fail_policy_overrides_persist` (deleted).** The survivor's input
  sets two more fields (`pre_pass_down`, `audit_sink_down` = `fail-closed`) that the candidate leaves at their
  defaults. Rev 14's `:91` vs `:130` keep used "same env values" literally for a config builder. Here the store
  is a dict that keeps the dataclass by reference and asserts the candidate's field with the same value, so the
  audit reads it as a subset. If the reviewer applies rev 14's literal reading to store data too, it is kept, and
  so is `test_pg_revoke_only_affects_target_user` (Q4).
- **Q4 — `tokens/test_postgres_store.py::test_pg_revoke_only_affects_target_user` (deleted).** The survivor has one
  more target row (alice ×2 vs ×1). Each of the candidate's asserts has a counterpart on the same method:
  bystander untouched and target revoked (`marks_all[postgres]`), and `n == 1` on one row (`idempotent[postgres]`).
  D6 fails both. Kept if Q3 is ruled literal.
- **Q5 — `tokens/test_postgres_store.py::test_pg_revoke_reflects_in_lookup` (deleted).** The `revoked_at.tzinfo`
  assert is covered by an implicit aware-equality assert (`kept.revoked_at == revoked_at`) on an `upsert`-written
  row. The brief named the tz asserts as backend-specific checks that stay "unless another surviving test asserts
  it on the same backend". This one does, through the same read function (D8a fails both). If the reviewer wants
  an explicit `tzinfo` assert on a `revoke_user`-written row, it is kept.
- **Q6 — optional wording.** Should phase 2 add the `tokens/test_postgres_store.py` docstring sentence and reword
  the `team_config/test_store.py` pointer comment? Neither is needed for correctness.

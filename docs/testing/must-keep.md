# Test-suite gates and the must-keep manifest

The suite is being restructured by behaviour (plan `docs/plans/20260926-test-suite-refactor-and-prune.md`,
local only). Every PR of that work runs against the gates below, recorded on the
baseline commit **`807831a`** (release/1.0.x). Task 0 built them and changed no test.

| Gate | What it compares | Where |
|---|---|---|
| 1. Check inventory | per test function: asserts, `pytest.raises` / `pytest.fail` sites, helpers it reaches that check something, parametrize tables and loop iterables, fixtures, helpers, constants, and one hash over the normalised body of the test plus everything it reaches | `tests/_manifests/baseline_checks/<dir>.json`, one per module for `tests/*.py` (`_root__<module>.json`) |
| 1. External dependencies | every repo file a test reaches outside the Python call graph (scripts run by `subprocess`, templates, configs it reads), hashed whole; a launch the walker cannot resolve fails until reviewed | `tests/_manifests/external_deps.json`, `external_deps_overrides.json` |
| 2. Must-keep | node ids no PR may delete, re-split or reduce | `tests/_manifests/must_keep/<dir>.txt`, one per module for `tests/*.py` (`_root__<module>.txt`) |
| 3. Coverage | per source file: executed lines and branch arcs; any drop fails. The baseline is what every recorded clean whole-suite run covered (the file's `runs` field counts them), less one arc taken out by hand ([below](#coverage-baseline)) | `tests/_manifests/coverage.{minimal,full}.json` |
| 4. Expected outcomes | per node id and environment: `passed`, `skipped:<reason>`, `collection-skipped:<reason>`, `not-applicable:<note>` | `tests/_manifests/expected_outcomes.{minimal,full}.json`, `not_applicable.json` |
| 4. Environments | the two venv recipes and their fingerprints; full's constraints must install the `litellm==` `pyproject.toml` pins | `scripts/test-env.sh`, `scripts/test-env.{minimal,full}.txt`, `tests/_manifests/env_fingerprint.{minimal,full}.json` |
| Name-pinned index | every test the acceptance matrix or a doc (`CLAUDE.md`, `README.md`, `docs/*.md`, `docs/ops/*.md`, `compose/*.md`, `compose/nginx/*.md`) cites by name, with its citing sites | `tests/_manifests/name_pinned.json` |
| Negative-log review | every assertion that a log line does *not* hold something, reviewed by hand | `tests/_manifests/negative_log_checks.json` |

The static half runs in every pytest run (`tests/_gates/test_suite_gates.py`). The
dynamic half — a whole run per environment against its ledger and coverage — is
`scripts/test-gates.sh`. Gate 5 (fault injection for a disputed replacement) and the
semantic notes of gate 1 are review work, recorded in [deleted-tests.md](deleted-tests.md).

## Running the gates

```
scripts/test-env.sh minimal && scripts/test-gates.sh minimal
docker run --rm -d --name pg-test -e POSTGRES_USER=gateway -e POSTGRES_PASSWORD=gateway \
  -e POSTGRES_DB=gateway -p 55432:5432 postgres:16
scripts/test-env.sh full && \
  CORP_TEST_PG_DSN=postgresql://gateway:gateway@localhost:55432/gateway scripts/test-gates.sh full
```

CI runs both as two more steps of the `test` job, after the unchanged
`PYTHONPATH=src pytest tests/ -q` step.

A PR that changes tests on purpose regenerates the manifests and the diff is what review
reads (one line per test in `baseline_checks/`, one line per file in the ledgers):

```
export PYTHONPATH=src:.
scripts/test-gates.sh minimal --record --out .test-gates/minimal
scripts/test-gates.sh full --record --out .test-gates/full
python -m tests._gates.ledger write --minimal .test-gates/minimal/ledger.json --full .test-gates/full/ledger.json
python -m tests._gates.inventory --write
python -m tests._gates.name_pinned --write
python -m tests._gates.negative_logs --write   # then review every UNREVIEWED site
python -m tests._gates.coverage_gate check minimal .test-gates/minimal/coverage.json
python -m tests._gates.coverage_gate check full .test-gates/full/coverage.json
```

**Any PR that touches a file a test reads, not only a test PR.** `external_deps.json` hashes
each listed file whole, and the check runs in every plain `pytest tests/` run
(`tests/_gates/test_suite_gates.py`). The list has files outside `tests/` too: `CLAUDE.md`,
`pyproject.toml`, `.gitignore`, the workflows, `Dockerfile.gateway`, `docs/security*.md`,
`docs/ops/*.md`, `compose/`, `scripts/`, four files under `src/`. So a typo fix in one of them
fails CI. List them with:

```
python -c 'import json; d = json.load(open("tests/_manifests/external_deps.json")); print(*sorted({p for t in d["tests"].values() for p in t["files"]}), sep="\n")'
```

A PR that changes any listed path runs `PYTHONPATH=src:. python -m tests._gates.inventory --write`
in the same PR. Only hashes should change in `external_deps.json`, and the reviewer checks
exactly that: no added or removed file, no change to `unresolved` or `programs`, nothing in
`baseline_checks/` unless the PR also changes tests. This is a refactor-time gate: the plan's
Task 10 removes it with the rest of the inventory.

`must_keep/` and `coverage.*.json` are not regenerated by a refactor PR: a must-keep id
never goes away, and coverage may only grow. Both change only on a re-baseline (a
production change under `litellm_hook.py`, `route_gate/` or `sanitizer/streaming.py`, or a
dependency bump), with the manifest diff reviewed.

A move PR that renames a module-level helper, fixture, class or constant records it in
`tests/_manifests/renames.json` as `{"names": {"<new name>": "<old name>"}}`. The inventory
applies it to the current tree, new → old, so the moved code hashes as the baseline did.
Only module-level names are rewritten (the `def` / `class` statement and a name the module
defines or imports), never attributes, and never a name the enclosing function, lambda,
comprehension or class body binds itself (an argument, an assignment, a loop / `with` /
`except` target, an import, a nested `def`) unless that scope declares it `global`.
The walker resolves a function-local import of a `tests.` module like a module-level one, so
the helper it binds joins the test's closure; by the rule above the rename is not applied to
that name, so renaming a helper reached only that way changes the test's `body_hash`.
An unaliased `import tests[.x]` binds only `tests`, so the inventory refuses it; write
`from tests.x import name` or `import tests.x as alias`. It also refuses a scope that imports
a `tests` name it declares `global` or `nonlocal`; import it in the scope that uses it.
`delegated` is keyed by the helper's bare name, so a move keeps it; two reached helpers that
share a name in different modules are both kept, as a list of their counts.

## The two environments

| | minimal | full |
|---|---|---|
| recipe | `pip install -e . --no-deps` + pinned runners (`pytest`, `pytest-asyncio`, `pytest-cov`, `fakeredis`, `PyYAML`, `redis`, `httpx`, `pydantic`, `structlog`, `PyJWT`) | `pip install -e ".[dev,ner,postgres,oidc,asgi,metrics]"` + `en_core_web_md` 3.8.0 |
| constraints | `scripts/test-env.minimal.txt` | `scripts/test-env.full.txt` (`.venv-bench`'s versions) |
| litellm, prometheus_client, asyncpg, natasha, spacy, cryptography | all absent | all present, litellm 1.101.0 |
| invocation | `CORP_REQUIRE_PROXY_CAPTURE=1`, no `CI`, no `CORP_TEST_PG_DSN` | `CI=true`, `CORP_REQUIRE_PROXY_CAPTURE=1`, `CORP_TEST_PG_DSN`, Postgres reachable |
| baseline run | 4,693 passed, 394 skipped, 20 modules collection-skipped (834 ids), 11 not-applicable | 5,916 passed, 6 skipped, 10 not-applicable |

The fingerprint (the six markers + a sorted `name==version` list, the editable checkout
normalised, `pip`/`setuptools`/`wheel` left out) is asserted before collection. Both
recipes were also installed on `linux/amd64` (`python:3.14-slim`) and produced the same
fingerprint as on macOS.

Why minimal has `PyJWT`: `tests/auth/test_rbac.py`, `tests/cli/test_admin_rbac.py` and
`tests/tokens/test_oidc_verifier.py` import `jwt` at module level, so without it the run
stops with three collection errors. `.venv` has it too, without `cryptography`, which
keeps RS256 unavailable as it is there.

Why minimal has no `CI`: under `CI=true`, `tests/postgres_support.py` turns "asyncpg not
installed" into a failure, where the minimal ledger expects the skip. GitHub sets `CI` on
every step, so `scripts/test-gates.sh` unsets it for minimal.

The union of the two ledgers is the baseline collection: 5,932 ids. Every test function the
inventory finds has at least one of them.

### Not-applicable cases (`not_applicable.json`)

- 10 `tests/e2e/` cases skip in both environments: they need the e2e compose stack
  (`LANGFUSE_URL`, `REDIS_URL` + `CORP_LLM_ENDPOINT`, `RUN_PROXY_E2E`), which no CI job
  runs. None of the ten is a security check.
- `tests/route_gate/test_inflight.py::test_a_finished_downstream_frees_the_slot_without_extra_loop_hops[uvloop]`
  exists only where `uvloop` is importable (full).
- The plan expected the service-less overlay cases of
  `test_litellm_config_guards.py::test_compose_gateway_env_turns_no_litellm_debug_on` here.
  On `807831a` every compose file defines the gateway service, so they all pass in both
  environments and nothing is recorded for them.

### ⚠️ Open finding: four must-keep security checks skip in both environments

`tests/e2e/test_langfuse_pipeline.py::test_no_originals_in_batch_payload`,
`tests/e2e/test_proxy_pipeline.py::test_proxy_forwards_authorization_untouched`,
`::test_proxy_injects_x_corp_auth` and `::test_proxy_401_when_token_missing` need the same
e2e stack. They are security checks, so they are **not** in the not-applicable class: both
ledgers record them as plain `skipped:<reason>`, and they are must-keep, so no prune can
touch them while the DRI decides whether a CI job runs them (the ⚠️ in the plan's Task 0).

Apart from these four (`SKIPPED_IN_BOTH_OPEN` in `tests/_gates/must_keep.py`), no
must-keep id is skipped or collection-skipped in both environments, and the must-keep gate
fails if one becomes so.

## Coverage baseline

`coverage.<env>.json` is the intersection of clean whole-suite runs of `807831a`'s `src/`
(`python -m tests._gates.coverage_gate intersect <env> <report>` per run). `write` starts the
file at `"runs": 1` and each `intersect` adds one, so the file says how many runs it holds.
Some runs were side by side under load; the last batch dropped no line or arc in either
environment.

Taken out by hand, because it is timing-dependent though every whole-suite run took it:

| env | file | arc | why |
|---|---|---|---|
| minimal | `src/corp_llm_gateway/pg_session.py` | `266>-265` | `_retrieve`'s cancelled-future exit. Only `test_pg_session.py::test_a_release_past_its_budget_drops_the_connection_and_keeps_the_result` (and loop teardown) reaches it, and only when the release future is cancelled before it completes: in 8 runs of `tests/test_pg_session*.py` alone it was taken 6 times. The other exit, `266>267`, stays. Full never recorded it. |

## Must-keep (`must_keep/`)

4,401 node ids (1,923 test functions, every parametrised case listed) in 110 files.
`python -m tests._gates.must_keep --write` rebuilds the list from the rules in
`tests/_gates/must_keep.py`. `--check` (and the guard test) rebuilds it too and fails when
the rules select an id the committed list lacks, so dropping a must-keep id means editing
the rules, in review. One file per test directory (per module for `tests/*.py`), as in
`baseline_checks/`: as one file the list was 457 KB against the 500 KB commit limit. The
step-1 rule for modified files reads `git diff e9e877f..807831a` against `807831a`'s own
copy of each file, so later moves inside a file do not shift it. A clone without those two
commits falls back to the ids already committed for those files: with `CI` set that is a
gate failure, elsewhere a Python warning (pytest shows it), and `--write` refuses. CI's
`test` job checks out with `fetch-depth: 0` so it never happens there
(`tests/_gates/test_suite_gates.py` pins that).

**Step 1 — the whole test diff of PR #16-#18** (`git diff --name-status e9e877f..807831a -- tests/`:
75 added, 25 modified).

- The 59 added test modules: in full.
- The 16 added helpers — `compose/nginx_{allowlist,container,support}.py`,
  `compose/nginx_fixtures/{stub_upstream.py,test-only-proxy-locations.inc.template}`,
  `desanitize_served_script.py`, `inflight_served_script.py`, `docs/__init__.py`,
  `integration/{gateway_image,nginx_route_dump}.py`, `litellm_hook/_dispatch_fixtures.py`,
  `logger_state.py`, `loop_errors.py`, `postgres_support.py`, `response_restore.py`,
  `stalling_proxy.py` — and the two modified `conftest.py`: not node ids; they are held by
  gate 1 (their bodies are in every consuming test's hash, scripts and templates in
  `external_deps.json`) and may move only in a move PR.
- 13 modified modules outside the plan's prunable universe, in full: `cli/test_admin.py`,
  `compose/test_oauth_overlay.py`, `healthz/test_server.py`, `helm/test_chart_render.py`,
  `integration/test_route_gate_container.py`, `invariants/test_no_originals_leak.py`,
  `metrics/test_metrics.py`, `route_gate/test_{classify,middleware,table}.py`,
  `sanitizer/test_profile_orchestrator.py`, `test_asgi_entrypoint.py`, `tokens/test_middleware.py`.
- Reviewer notes, per file, for the modified modules inside the prunable universe — only
  the tests the diff added or changed are must-keep; the rest predates PR #16-#18 and is
  in only where step 2 or the lists below put it:

  | file | must-keep tests | note |
  |---|---|---|
  | `test_litellm_hook.py` | 85 | the same rule as the other files: the 80 tests the diff added or changed, which include the ticket / terminal-audit section the plan names (`:5975` to the end), the token-store bound tests (`:5829-5914`), the held-tail and tool-call stream tests (`:2489`, `:2504`, `:3500`) and PR #18's swap of the response tests onto `tests/response_restore.py`; + 5 security negative-log checks |
  | `test_settings.py` | 72 | the capacity and issuance keys PR #16 added (70 tests) + two policy defaults |
  | `test_bootstrap.py` | 9 | 6 changed by the diff + negative-log checks |
  | `test_bootstrap_edges.py` | 0 | the diff changed a helper, no test |
  | `test_litellm_config.py` | 3 | |
  | `test_litellm_hook_adversarial.py` | 1 | the harness swap touched one test |
  | `deploy/test_deploy_script.py` | 44 | the deploy flow PR #17 added (43) + the `$ENV_FILE` never-read check (step 2) |
  | `tokens/test_token_store_contract.py` | 29 | the issuance races and the jti contract |
  | `tokens/test_postgres_store.py` | 7 | the Postgres races |
  | `team_config/test_postgres_store.py` | 2 | |

**Step 2 — CLAUDE.md invariants → the files that assert them**, in full:
`tests/invariants/**`, `tests/route_gate/**`, the two served-stack suites,
`test_launch_command.py`, `test_litellm_pin.py`, `test_litellm_config_guards.py`,
`test_ci_workflow.py`, `test_asgi_entrypoint.py`, `audit/test_{invariants,block_reason,guardrail_information_gate}.py`,
`audit/test_logger.py:135-163` (the NEVER-field tests), `sanitizer/test_placeholder_allocator*.py`,
`sanitizer/test_dlp_guard.py`, `sanitizer/test_oauth_system_preamble.py`, every
`tests/litellm_hook/` module, `healthz/test_issue_token_*.py` + `test_issuance_schema_gate.py`,
`tokens/test_{oidc_verifier,issuance_policy,middleware*}.py`, `tests/compose/**`,
`tests/integration/**`, `docs/test_docs_pins.py`, `auth/test_rbac.py` (operator JWT: RS256
claims accepted; HS256, forged, expired, wrong audience / issuer, missing role rejected),
`test_serve.py` (the served target is the gated app, one worker); and by id
`test_litellm_hook.py::test_the_guardrail_defines_no_response_side_hook`,
`::test_a_ticketed_pre_call_hands_the_mapping_and_the_record_to_the_ticket`,
`sanitizer/test_allowlist.py::test_allowlisted_secret_label_not_dropped` (an allowlist
never drops a secret's label), `deploy/test_bootstrap_server_script.py::test_env_file_contents_are_never_read_or_printed`
and its `deploy/test_deploy_script.py` twin, and the four e2e security checks of the open
finding above.

**Security-policy defaults** that look trivial: `test_config.py::test_corp_llm_verify_defaults_to_true`,
`test_settings.py::test_forward_anthropic_auth_defaults_off`, `::test_route_gate_extras_default_to_empty`,
`team_config/test_store.py::test_default_fail_policy_matches_matrix`.

**Negative-log checks** (`negative_log_checks.json`): discovery
(`python -m tests._gates.negative_logs`) found 148 assertions that check a log for an
absence — `not in caplog.text`, `getMessage()` loops, aliases of either, `… is None`
over a log haystack, helpers taking `log_text` — out of 296 lines that mention
`caplog.text` / `.records` / `.messages` / `getMessage(` / `log_text`; the rest assert
presence or build the haystack. Each of the 148 was reviewed: 139 are security (an
original, a credential, a backend or exception detail, or a trace kept out of a log line —
the M1-14 shapes), 5 are behaviour (a warning that should not fire), 4 are not log checks.
The 151 tests that run a security one, directly or through `_assert_gate_surfaces_are_clean`,
`_assert_clean` or `_assert_no_leak`, are must-keep. A new or changed negative log check
fails the guard until it is reviewed.

**Name-pinned tests** (`name_pinned.json`): the acceptance matrix's `_ALL` (105 ids in 21
files) and every test a citation source (`CLAUDE.md`, `README.md`, `docs/*.md`,
`docs/ops/*.md`, `compose/*.md`, `compose/nginx/*.md`, each `*` one path segment; this
directory is not a source) cites — 47 distinct names in `docs/security.md`,
`docs/security.ru.md`, `docs/ops/capacity{,.ru}.md` (bare `::name`, `file.py::name`, a
served `name`, `{a,b}` expansions), 116 ids in all, plus 41 cited test paths (the compose
READMEs cite paths only: `compose/README.md:38,894`, `compose/README.ru.md:39,913`,
`compose/nginx/README.md:19-20`).
`CLAUDE.md`'s `tests/sanitizer/test_engine.py::test_name` is the allow-listed placeholder.
Renaming or moving one updates the matrix and every citing site in the same PR. Only
`CLAUDE.md`, `README.md`, `docs/*.md`, `docs/ops/*.md`, `compose/*.md` and
`compose/nginx/*.md` are gated; other tracked files that cite a test path
(`.claude/skills/security-audit/SKILL.md:15`, `.revmux/profile.md:56`, both
`tests/invariants/test_no_originals_leak.py`) are not sources and are updated by hand.

## Gate self-tests

Each mutation weakens a check and leaves every assert in place; the tree is restored after
each. An inventory self-test passes only when the gate reports the named column on every
named test, so a column the gate stops filling fails it. `python -m tests._gates.selftest inventory` (any venv) and
`python -m tests._gates.selftest ledger` (minimal venv):

| # | mutation | rejected by |
|---|---|---|
| a | drop the `_assert_gate_surfaces_are_clean(...)` call in `invariants/test_no_originals_leak.py::test_a_refused_route_leaks_no_original_on_any_of_the_six_surfaces` | inventory: that test's `delegated` and `helpers` |
| b | replace the `try / pytest.fail` in `sanitizer/test_streaming_adversarial.py::test_framing_integrity_every_data_line_is_valid_json` with a bare `json.loads` | inventory: that test's `fail 1 -> 0` |
| c | build `test_oauth_system_preamble.py`'s guardrail with `RegexChecksumDetector()` only | inventory: `body_hash` of every test whose recorded `helpers` holds `_guardrail` (27) |
| d | select lines by `".env"` instead of `"$ENV_FILE"` in `deploy/test_bootstrap_server_script.py::test_env_file_contents_are_never_read_or_printed` | inventory: that test's `body_hash` |
| e | make `holding_after` in `tests/desanitize_served_script.py` return `[]` | external dependencies: `external tests/desanitize_served_script.py` of every test that records it (20) |
| f | drop the `pyproject-range` case from `test_litellm_pin.py::test_pin_extraction_refuses_a_file_without_a_pin`'s parametrize table | inventory: that test's `case_data` |
| g | re-parse one synthetic module 200 times with two alternating bodies | inventory caches: each body always hashes the same and the two differ (a cache keyed by a reused `id()` returned a stale hash) |
| L1 | rename `team_config/test_store.py` so it is not collected | expected outcomes: missing ids |
| L2 | drop the `redis` param of `storage/test_mapping_store.py`'s `store` fixture | expected outcomes: missing `[redis]` ids |
| L3 | an autouse fixture that skips at setup in `team_config/test_store.py` | expected outcomes: `passed` → `skipped:…` |
| L4 | give `detectors/test_ner_en.py`'s `importorskip("spacy")` another reason | expected outcomes: collection-skip reason changed |

All eleven were rejected on the baseline tree (2026-10-04); the same checks on the
unmutated tree pass (the control).

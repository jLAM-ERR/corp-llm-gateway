# Running the tests

```
scripts/test-env.sh full && scripts/test-gates.sh full             # what CI runs (Postgres needed)
scripts/test-env.sh minimal && scripts/test-gates.sh minimal       # the package without extras or litellm
PYTHONPATH=src .venv/bin/pytest tests/ -q -m "not slow"            # skip the listed slow tests
scripts/test-gates.sh minimal --shuffle-seed 20261006              # a seeded random order
```

There are two reproducible environments. `scripts/test-env.sh minimal|full` builds
`.venv-test-<env>` from the pinned constraints in `scripts/test-env.<env>.txt`: minimal is
the package with no extras and no litellm, full is every extra CI installs, litellm and the
`en_core_web_md` wheel. `scripts/test-gates.sh minimal|full` sets the environment's
variables (full also sets `CI=true` and needs Postgres at `CORP_TEST_PG_DSN`, which it checks
first) and runs `pytest tests/ -q -rs` once; its exit code is pytest's, and `-rs` prints
every skip reason. CI's `test` job runs the full environment; minimal is a local check.

- Markers (`pyproject.toml`, `--strict-markers`): `requires_litellm`, `requires_ner`,
  `requires_helm`, `requires_shellcheck` and `not_root` become setup-time skips when the
  dependency is missing (`tests/conftest.py`). Under `CI=true` a Postgres-backed
  test fails instead of skipping.
- `slow` marks every test listed in `tests/slow_tests.txt`: over 2 s (setup + call +
  teardown) in either test environment when measured. A module-scoped fixture's setup counts
  against the first test that uses it, so some entries are slow only for that reason, and
  `-m "not slow"` moves that setup to the next unmarked test in the module rather than
  skipping it. CI runs everything; `-m "not slow"` is for local loops. The file's header says
  how it was measured and how to refresh it.
- `--shuffle-seed N` runs the modules in a random order, then the classes and tests inside
  each, from seed N; the seed is in the header and the summary. It is off by default and
  not in CI. The same seed gives the same order only in the same environment with the same
  path arguments, so a test that fails only in some order is reproduced by rerunning that
  command with that seed.
- After every test, `tests/conftest.py` fails the test at teardown when it leaves a
  `corp_llm_gateway` package attribute on a stale module: either a module other than the one
  `sys.modules` holds under that name, or one `sys.modules` no longer holds at all
  (`tests/package_state.py`). It then rebinds the attribute to the module `sys.modules`
  holds, or drops it when there is none. It never edits `sys.modules`: a `None` entry the
  test left there stays, so later imports of that module keep failing after the culprit is
  flagged.

## Which directory owns what

A behaviour is asserted in the layer that owns it. A test that only repeats a lower layer's
case through a higher layer is a prune candidate; a test that adds what its layer adds is not.
The layers: **unit** (one component's own contract, no hook; the plan's algorithm layer),
**integration** (what the litellm hook adds), **boundary** (the gate, the limiter, the
reversal, startup, the served stack, the edge, the leak surfaces).

| `tests/…` | layer | owns |
|---|---|---|
| `sanitizer/` | unit | the engine, segmenter, placeholders and the request allocator, the orchestrators and Cache A, the DLP guard, the identity preamble, and the streaming reversal (`test_streaming*.py`) |
| `detectors/`, `rules/`, `corp_ner/`, `corp_llm/` | unit | regex + checksum, dual NER, the shadow detector, `replace.md` and the gazetteer; the corp NER and corp-LLM HTTP clients |
| `payload/` | unit | the Stage 0 classifier, size threshold, compression, per-team quota |
| `storage/`, `team_config/`, `tokens/` | unit | the store contracts (mapping, team config, tokens; Postgres where `CORP_TEST_PG_DSN` is set), auth middleware single-flight, issuance policy, the OIDC verifier |
| `audit/`, `metrics/` | unit | audit records, the NEVER-fields gate, sinks, retention; the metrics exporter |
| `auth/`, `extensions/`, `profiles/`, `providers/` | unit | auth providers and RBAC; the extension registry; profile bundles, manifest and signing; the provider registry |
| `litellm_hook/` | integration | what `CorpLlmGuardrail` adds: request shapes per call type, headers and auth bridges, Stage 0 and Stage 5 at the hook, fail policy, audit facts on the ticket, litellm dispatch, the acceptance matrix |
| `route_gate/` | boundary | the route table and classifier, the gate middleware, the in-flight limiter, the response reversal (`test_desanitize_*.py` and the other `tests/response_restore.py` users, except `invariants/test_no_originals_leak.py` and the litellm_hook tests that reach it through `hook_fixtures.restore_stream`), the terminal audit record, arm checks |
| `invariants/` | boundary | the M1-14 leak surfaces and the issuance error / leak contracts |
| `healthz/` | boundary | health checks and the gateway-owned issuance route |
| `integration/`, `compose/` | boundary | the real image; the compose stack and its nginx front door |
| `e2e/` | boundary | the e2e stack: Redis and the two mocks (skips without it; CI's `e2e` job runs it, where a skip fails) |
| `cli/`, `deploy/`, `helm/` | — | the operator and laptop CLIs; the deploy and install scripts; the Helm chart |
| `tests/*.py` (root) | boundary and composition | the served-stack suites, the ASGI entrypoint and `serve`, litellm's config checks; `bootstrap`, `config`, `settings` and `pg_session` |

Shared helpers are flat modules at the root (`hook_fixtures.py`, `response_restore.py`,
`postgres_support.py`, …), imported by name.

## What a test here checks

A test runs production code and asserts on what it does. A check that only reads a
document, reads source or config as text, or tests the test harness itself does not belong
in `tests/`. What those checks used to guard is a review step now: `docs/security.md` §11
lists each gap and the review that replaces it.

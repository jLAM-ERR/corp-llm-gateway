# Running the tests

```
PYTHONPATH=src .venv/bin/pytest tests/ -q                          # everything; what CI runs
PYTHONPATH=src .venv/bin/pytest tests/ -q -m "not slow"            # skip the listed slow tests
PYTHONPATH=src .venv/bin/pytest tests/ -q --shuffle-seed=20261006  # a seeded random order
scripts/test-gates.sh minimal --shuffle-seed 20261006              # the gates in a seeded order
```

The CI-matching route is the two reproducible environments, `scripts/test-env.sh` and
`scripts/test-gates.sh`; [must-keep.md](must-keep.md) has the commands and the gates they run.

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
reversal, startup, the served stack, the edge, the leak surfaces). Boundary tests may be
moved, never pruned.

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
| `cli/`, `deploy/`, `helm/`, `docs/` | — | the operator and laptop CLIs; the deploy and install scripts; the Helm chart; doc pins (counts, keys and codes the docs name) |
| `tests/*.py` (root) | boundary and composition | the served-stack suites, the ASGI entrypoint and `serve`, launch-command and litellm pins, CI workflow pins; `bootstrap`, `config`, `settings`, `pg_session` and the conftest hooks |
| `_gates/` | — | the test-suite gates ([must-keep.md](must-keep.md)) |

Shared helpers are flat modules at the root (`hook_fixtures.py`, `response_restore.py`,
`postgres_support.py`, …), imported by name.

## Move PRs and prune PRs

A PR either moves or renames tests, or prunes them; never both.

- A **move** (a test to another module, a renamed module or test) changes no test body. It
  records each move in `tests/_manifests/moves.json`, current id → baseline id, so every
  gate keeps its key. Its manifest diff is only `moves.json`, `name_pinned.json` and the
  `site` of moved negative-log checks.
- A **prune** deletes or folds tests that are not must-keep. Every deleted or changed node id
  gets a row in [deleted-tests.md](deleted-tests.md) in the same PR: the survivor, a semantic
  note, and a fault injection where the replacement is disputed. A deleted test is in no
  ledger, so its manifest changes are its own negative-log sites and, if it was moved
  earlier, its `moves.json` entry (the moves gate refuses a key no longer in the tree); a
  fold that creates a test under a must-keep path adds its ids to `must_keep/`, additions
  only.
- Both must leave `scripts/test-gates.sh minimal` and `full` green. The mechanics, and the
  order to regenerate the manifests in, are in [must-keep.md](must-keep.md).

## Name-pinned tests

A test the acceptance matrix lists, or that `CLAUDE.md`, `README.md`, `docs/*.md`,
`docs/ops/*.md`, `compose/*.md` or `compose/nginx/*.md` cites by name, is indexed in
`tests/_manifests/name_pinned.json` with the `file:line` of every citation, and is
must-keep; a cited test module path is indexed the same way. Renaming or moving one means
updating every citation in the same PR and running
`python -m tests._gates.name_pinned --write`. Every `pytest tests/` run fails until the
index holds exactly the cited ids and paths. It does not compare line numbers, so a doc
edit that only moves a citing line still passes; regenerate anyway to keep the sites
current (the diff is line numbers only).

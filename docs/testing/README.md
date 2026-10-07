# Running the tests

```
PYTHONPATH=src .venv/bin/pytest tests/ -q                          # everything; what CI runs
PYTHONPATH=src .venv/bin/pytest tests/ -q -m "not slow"            # skip the listed slow tests
PYTHONPATH=src .venv/bin/pytest tests/ -q --shuffle-seed=20261006  # a seeded random order
scripts/test-gates.sh minimal --shuffle-seed 20261006              # the gates in a seeded order
```

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
- After every test, `tests/conftest.py` fails the test at teardown, and puts things back,
  when it leaves a `corp_llm_gateway` package attribute on a stale module: either a module
  other than the one `sys.modules` holds under that name, or one `sys.modules` no longer
  holds at all (`tests/package_state.py`).
- The gates and their manifests: [must-keep.md](must-keep.md).

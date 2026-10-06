# Running the tests

```
PYTHONPATH=src .venv/bin/pytest tests/ -q                          # everything; what CI runs
PYTHONPATH=src .venv/bin/pytest tests/ -q -m "not slow"            # skip the tests over 2 s
PYTHONPATH=src .venv/bin/pytest tests/ -q --shuffle-seed=20261006  # a seeded random order
scripts/test-gates.sh minimal --shuffle-seed 20261006              # the gates, same order
```

- `slow` marks every test listed in `tests/slow_tests.txt`: over 2 s (setup + call +
  teardown) in either test environment. CI runs everything; `-m "not slow"` is for local
  loops. The file's header says how it was measured and how to refresh it.
- `--shuffle-seed N` runs the modules in a random order, then the classes and tests inside
  each, from seed N; the seed is in the header. It is off by default and not in CI. A test
  that fails only in some order is a real order dependency: rerun with the same seed to
  reproduce it.
- After every test, `tests/conftest.py` fails the test at teardown when it leaves a
  `corp_llm_gateway` package attribute on a module other than the one `sys.modules` holds
  (`tests/package_state.py`), and puts it back.
- The gates and their manifests: [must-keep.md](must-keep.md).

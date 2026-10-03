# Deleted-tests ledger

Every test node id a prune PR removes, or a helper-consolidation PR changes, gets one
row here, in the same PR. The gates are in [must-keep.md](must-keep.md); a row is the
semantic-equivalence note gate 1 asks for.

A row is complete only when all of these hold:

- the id is not in `tests/_manifests/must_keep.txt` (must-keep ids are never deleted,
  re-split or reduced to fewer parameter cases);
- the surviving test(s) have the same inputs, select the same production path in setup,
  apply the same predicates and selectors, expect the same outcomes, and carry the same
  negative checks and parameter cases;
- for a disputed replacement, the production line the deleted test targeted was
  hand-mutated and the survivor failed (gate 5);
- the manifests regenerated in the PR (`tests/_manifests/`) show the deletion and nothing
  else for that id, and `scripts/test-gates.sh minimal|full` is green.

| PR | deleted node id | baseline outcome (minimal / full) | the check it made | survivor node id(s) | semantic note | fault injection | reviewer |
|---|---|---|---|---|---|---|---|
| | | | | | | | |

## How to fill a row

- **baseline outcome**: from `tests/_manifests/expected_outcomes.{minimal,full}.json`.
- **the check it made**: its line in `tests/_manifests/baseline_checks/<dir>.json`
  (asserts, raises / fail sites, delegated helper checks, case data, fixtures).
- **semantic note**: one sentence per difference that is not a rename; "none" is a
  valid note only when the normalised body hashes match.
- **fault injection**: `file:line` mutated, the survivor's failing assertion, and the
  revert.

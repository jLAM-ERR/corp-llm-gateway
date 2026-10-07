#!/usr/bin/env bash
# Runs the suite in one of the two test environments and checks it against the
# committed baseline (plan 20260926, docs/testing/must-keep.md):
#
#   scripts/test-gates.sh minimal|full [--record] [--venv DIR] [--out DIR] [--shuffle-seed N]
#
# 1. the environment's fingerprint (tests/_manifests/env_fingerprint.<env>.json);
#    full also needs Postgres at CORP_TEST_PG_DSN;
# 2. the static gates: check inventory + external dependencies, name-pinned index,
#    negative-log review, must-keep ids, the moves map;
# 3. the whole suite once, with the outcome-ledger plugin and branch coverage;
# 4. the run's ledger against expected_outcomes.<env>.json and its coverage against
#    coverage.<env>.json — any new skip, lost id or case, changed reason, module that
#    stops collecting, or dropped line / branch arc fails.
#
# --record runs 1 and 3 only and leaves the ledger and coverage in --out, for a
# re-baseline. --shuffle-seed N runs step 3 in the suite's own seeded order (the option
# tests/conftest.py adds); the ledger and coverage are keyed by node id and file, so the
# checks do not depend on it.
# Create the venv first with scripts/test-env.sh <env>.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

usage() {
    echo "usage: scripts/test-gates.sh minimal|full [--record] [--venv DIR] [--out DIR] [--shuffle-seed N]" >&2
    exit 64
}

[[ $# -ge 1 ]] || usage
ENV_NAME="$1"
shift
case "${ENV_NAME}" in
    minimal | full) ;;
    *) usage ;;
esac

RECORD=0
VENV=".venv-test-${ENV_NAME}"
OUT=".test-gates/${ENV_NAME}"
SHUFFLE=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --record) RECORD=1 ;;
        --venv) VENV="$2"; shift ;;
        --out) OUT="$2"; shift ;;
        --shuffle-seed) SHUFFLE=(--shuffle-seed "$2"); shift ;;
        *) usage ;;
    esac
    shift
done

PY="${VENV}/bin/python"
if [[ ! -x "${PY}" ]]; then
    echo "FATAL: no venv at ${VENV}; run scripts/test-env.sh ${ENV_NAME} first" >&2
    exit 66
fi
mkdir -p "${OUT}"

# The invocation environment is part of the recipe. minimal must not see CI: under
# CI=true tests/postgres_support.py turns "asyncpg not installed" into a failure,
# where the minimal ledger expects the skip.
export CORP_REQUIRE_PROXY_CAPTURE=1
export CORP_TEST_ENV="${ENV_NAME}"
export PYTHONPATH=src
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,${NO_PROXY}}"
export no_proxy="${NO_PROXY}"
if [[ "${ENV_NAME}" == minimal ]]; then
    unset CI CORP_TEST_PG_DSN
else
    export CI=true
    export CORP_TEST_PG_DSN="${CORP_TEST_PG_DSN:-postgresql://gateway:gateway@localhost:5432/gateway}"
fi

status=0
fail() {
    echo "GATE FAILED: $*" >&2
    status=1
}

"${PY}" -m tests._gates.fingerprint "${ENV_NAME}" --check || fail "fingerprint"
if [[ "${ENV_NAME}" == full ]]; then
    "${PY}" - <<'EOF' || fail "Postgres unreachable at CORP_TEST_PG_DSN"
import os, socket, urllib.parse
url = urllib.parse.urlsplit(os.environ["CORP_TEST_PG_DSN"])
socket.create_connection((url.hostname, url.port or 5432), timeout=10).close()
EOF
fi
[[ ${status} -eq 0 ]] || exit 1

if [[ ${RECORD} -eq 0 ]]; then
    "${PY}" -m tests._gates.inventory --check || fail "check inventory / external dependencies"
    "${PY}" -m tests._gates.name_pinned --check || fail "name-pinned index"
    "${PY}" -m tests._gates.negative_logs --check || fail "negative-log review"
    "${PY}" -m tests._gates.must_keep --check || fail "must-keep ids"
    "${PY}" -m tests._gates.moves --check || fail "moves map"
fi

set +e
"${PY}" -m pytest tests/ -q -p tests._gates.outcome_ledger \
    --outcome-ledger="${OUT}/ledger.json" \
    --cov=corp_llm_gateway --cov-branch --cov-report="json:${OUT}/coverage.json" \
    ${SHUFFLE[@]+"${SHUFFLE[@]}"}
pytest_status=$?
set -e
echo "pytest exited ${pytest_status}; ledger and coverage in ${OUT}"

if [[ ${RECORD} -eq 1 ]]; then
    exit 0
fi
[[ ${pytest_status} -eq 0 ]] || fail "pytest exited ${pytest_status}"
"${PY}" -m tests._gates.ledger check "${ENV_NAME}" "${OUT}/ledger.json" || fail "expected outcomes"
"${PY}" -m tests._gates.coverage_gate check "${ENV_NAME}" "${OUT}/coverage.json" || fail "coverage"

if [[ ${status} -eq 0 ]]; then
    echo "OK: every gate holds in the ${ENV_NAME} environment"
fi
exit ${status}

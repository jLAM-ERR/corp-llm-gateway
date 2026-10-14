#!/usr/bin/env bash
# Runs the whole suite once in one of the two test environments:
#
#   scripts/test-gates.sh minimal|full [--venv DIR] [--shuffle-seed N]
#
# Sets the environment's invocation variables; full also needs Postgres at
# CORP_TEST_PG_DSN and checks it is reachable first. Then `pytest tests/ -q -rs`;
# the exit code is pytest's. --shuffle-seed N runs the suite in its own seeded
# order (the option tests/conftest.py adds).
# Create the venv first with scripts/test-env.sh <env>.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

usage() {
    echo "usage: scripts/test-gates.sh minimal|full [--venv DIR] [--shuffle-seed N]" >&2
    exit 64
}

[[ $# -ge 1 ]] || usage
ENV_NAME="$1"
shift
case "${ENV_NAME}" in
    minimal | full) ;;
    *) usage ;;
esac

VENV=".venv-test-${ENV_NAME}"
SHUFFLE=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --venv) VENV="$2"; shift ;;
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

# The invocation environment is part of the recipe. minimal must not see CI: under
# CI=true tests/postgres_support.py turns "asyncpg not installed" into a failure,
# where the minimal environment expects the skip.
export CORP_REQUIRE_PROXY_CAPTURE=1
export PYTHONPATH=src
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,${NO_PROXY}}"
export no_proxy="${NO_PROXY}"
if [[ "${ENV_NAME}" == minimal ]]; then
    unset CI CORP_TEST_PG_DSN
else
    export CI=true
    export CORP_TEST_PG_DSN="${CORP_TEST_PG_DSN:-postgresql://gateway:gateway@localhost:5432/gateway}"
    if ! "${PY}" - <<'EOF'; then
import os, socket, urllib.parse
url = urllib.parse.urlsplit(os.environ["CORP_TEST_PG_DSN"])
socket.create_connection((url.hostname, url.port or 5432), timeout=10).close()
EOF
        echo "FATAL: Postgres unreachable at CORP_TEST_PG_DSN" >&2
        exit 1
    fi
fi

exec "${PY}" -m pytest tests/ -q -rs ${SHUFFLE[@]+"${SHUFFLE[@]}"}

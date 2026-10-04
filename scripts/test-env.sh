#!/usr/bin/env bash
# Creates one of the two reproducible test environments the suite gates run in.
#
#   scripts/test-env.sh minimal [VENV]   default VENV: .venv-test-minimal
#   scripts/test-env.sh full    [VENV]   default VENV: .venv-test-full
#
# minimal: the package without its dependencies + the pinned runner set; no litellm,
#          no extras (graceful NER degradation, inverse absent-dependency tests pass).
#          PyJWT comes without its crypto extra: three modules import jwt at module
#          level, and RS256 must stay unavailable here as it is in .venv.
# full:    every extra CI installs + the en_core_web_md wheel, at .venv-bench's versions.
#
# Both install against a committed constraints file, so the same recipe gives the
# same `name==version` set on a laptop and on the CI runner; scripts/test-gates.sh
# asserts that set (tests/_manifests/env_fingerprint.<env>.json) before collection.
# PYTHON overrides the interpreter; it must be 3.14.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

EN_MODEL_WHEEL="https://github.com/explosion/spacy-models/releases/download/en_core_web_md-3.8.0/en_core_web_md-3.8.0-py3-none-any.whl"
MINIMAL_RUNNERS=(pytest pytest-asyncio pytest-cov fakeredis PyYAML redis httpx pydantic structlog PyJWT)

usage() {
    echo "usage: scripts/test-env.sh minimal|full [VENV]" >&2
    exit 64
}

[[ $# -ge 1 && $# -le 2 ]] || usage
ENV_NAME="$1"
case "${ENV_NAME}" in
    minimal | full) ;;
    *) usage ;;
esac
VENV="${2:-.venv-test-${ENV_NAME}}"
CONSTRAINTS="scripts/test-env.${ENV_NAME}.txt"

pick_python() {
    if [[ -n "${PYTHON:-}" ]]; then
        echo "${PYTHON}"
    elif command -v python3.14 >/dev/null 2>&1; then
        echo python3.14
    else
        echo python3
    fi
}

PY="$(pick_python)"
version="$("${PY}" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if [[ "${version}" != "3.14" ]]; then
    echo "FATAL: ${PY} is Python ${version}; the test environments are 3.14" >&2
    exit 65
fi

if [[ -e "${VENV}" ]]; then
    echo "FATAL: ${VENV} already exists; move it aside first, the recipe starts from an empty venv" >&2
    exit 66
fi

"${PY}" -m venv "${VENV}"
PIP=("${VENV}/bin/python" -m pip --disable-pip-version-check --no-input)

if [[ "${ENV_NAME}" == minimal ]]; then
    "${PIP[@]}" install --no-deps -e .
    "${PIP[@]}" install -c "${CONSTRAINTS}" "${MINIMAL_RUNNERS[@]}"
else
    "${PIP[@]}" install -c "${CONSTRAINTS}" -e ".[dev,ner,postgres,oidc,asgi,metrics]"
    # Model wheel version mirrors .github/workflows/ci.yml and Dockerfile.gateway.
    "${PIP[@]}" install --no-deps "${EN_MODEL_WHEEL}"
fi

PYTHONPATH="${ROOT}" "${VENV}/bin/python" -m tests._gates.fingerprint "${ENV_NAME}" --print
echo "OK: ${ENV_NAME} environment at ${VENV}"

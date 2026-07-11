#!/usr/bin/env bash
set -euo pipefail

# Pre-push gate battery for corp-llm-gateway release images.
cd "$(git rev-parse --show-toplevel)"

# .github/workflows/build-image.yml is the source of truth for this pin — keep in sync.
LITELLM_VERSION="v1.85.0"

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; exit 1; }

echo "== gate: workflow YAML parses =="
if python3 -c "import yaml; yaml.safe_load(open('.github/workflows/build-image.yml'))"; then
  pass "workflow YAML parses"
else
  fail "workflow YAML parses"
fi

fallback_check_py() {
  local profile="$1"
  if [[ "${profile}" == "ru-en" ]]; then
    echo "import spacy; spacy.load('en_core_web_md'); import natasha; import corp_llm_gateway.bootstrap; print('OK')"
  else
    echo "import spacy; spacy.load('en_core_web_md'); import corp_llm_gateway.bootstrap; print('OK')"
  fi
}

build_and_check() {
  local profile="$1"
  local needle="$2"
  local img="gw-test:${profile}"
  local log
  log=$(mktemp)

  echo "== gate: build ${img} =="
  if ! docker build -f Dockerfile.gateway \
      --build-arg LITELLM_VERSION="${LITELLM_VERSION}" \
      --build-arg NER_PROFILE="${profile}" \
      -t "${img}" . 2>&1 | tee "${log}"; then
    rm -f "${log}"
    fail "docker build ${profile}"
  fi

  if grep -q "${needle}" "${log}" && grep -q "gateway bootstrap import OK" "${log}"; then
    echo "NOTE: self-check lines seen in build output"
  else
    echo "NOTE: self-check lines not seen in build output (warm cache)"
  fi
  rm -f "${log}"

  # Buildx echoes the full RUN command text as the step title even on a
  # CACHED hit, so the grep above can't tell a fresh execution from a cache
  # reuse — it's diagnostic only. The runtime check below always executes
  # against the built image, so it's the one genuine proof the NER stack
  # actually loads (not just that the package imports).
  local py out
  py="$(fallback_check_py "${profile}")"
  if out=$(docker run --rm --entrypoint /app/.venv/bin/python "${img}" -c "${py}" 2>&1); then
    if [[ "${out}" == "OK" ]]; then
      pass "${profile} runtime check OK (NER model + bootstrap load in built image)"
    else
      fail "${profile} runtime check returned unexpected output: ${out}"
    fi
  else
    fail "${profile} runtime check failed: ${out}"
  fi
}

build_and_check en "NER en OK"
build_and_check ru-en "NER ru-en OK"

echo "== gate: entrypoint smoke =="
if docker run --rm gw-test:en --help >/dev/null; then
  pass "entrypoint --help (en)"
else
  fail "entrypoint --help (en)"
fi

echo "ALL GATES PASSED"

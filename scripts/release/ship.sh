#!/usr/bin/env bash
set -euo pipefail

# Push a topic branch and open a PR against the single release/* branch.
cd "$(git rev-parse --show-toplevel)"

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; exit 1; }

if [[ $# -ne 1 ]]; then
  fail "usage: bash scripts/release/ship.sh <topic-branch>"
fi

branch="$1"

if [[ ! "${branch}" =~ ^(fix|feature|chore)/ ]]; then
  fail "branch '${branch}' does not match ^(fix|feature|chore)/ — refusing to ship"
fi
pass "branch name '${branch}' matches fix|feature|chore prefix"

echo "== discovering release branch =="
# Capture ls-remote output first so a network/auth failure is caught by set -e
# (a failing command inside `< <(...)` process substitution would be silently
# swallowed by the while-loop instead).
release_ls=$(git ls-remote --heads origin 'release/*')
release_refs=()
while IFS= read -r line; do
  [[ -n "${line}" ]] && release_refs+=("${line}")
done <<< "$(printf '%s\n' "${release_ls}" | awk '{print $2}' | sed 's#refs/heads/##')"

if [[ "${#release_refs[@]}" -eq 0 ]]; then
  fail "no release/* branch found on origin"
elif [[ "${#release_refs[@]}" -gt 1 ]]; then
  fail "multiple release/* branches found on origin: ${release_refs[*]} — refusing to guess"
fi
release_branch="${release_refs[0]}"
pass "discovered release branch: ${release_branch}"

echo "== push ${branch} =="
git push -u origin "${branch}"
pass "pushed ${branch}"

echo "== create PR ${branch} -> ${release_branch} =="
pr_url=$(gh pr create --base "${release_branch}" --head "${branch}" --fill)
pass "PR created: ${pr_url}"

echo "== waiting for checks to register =="
registered=""
for attempt in $(seq 1 12); do
  check_count=$(gh pr checks "${pr_url}" --json name --jq 'length' 2>/dev/null || echo 0)
  if [[ "${check_count}" -gt 0 ]]; then
    registered="yes"
    break
  fi
  echo "  no checks registered yet, waiting... (${attempt}/12)"
  sleep 10
done

if [[ -z "${registered}" ]]; then
  fail "no checks registered on ${pr_url} after ~2 minutes"
fi
pass "checks registered (${check_count} checks)"

echo "== watch checks =="
# macOS ships no GNU `timeout`/`gtimeout` by default; implement a portable
# 900s watchdog with a background sleep+kill instead of relying on it.
gh pr checks "${pr_url}" --watch &
watch_pid=$!
( sleep 900; kill "${watch_pid}" 2>/dev/null ) &
timer_pid=$!

watch_status=0
wait "${watch_pid}" || watch_status=$?
kill "${timer_pid}" 2>/dev/null || true
wait "${timer_pid}" 2>/dev/null || true

if [[ "${watch_status}" -eq 0 ]]; then
  pass "checks green"
else
  echo "FAIL or timeout waiting on checks — reporting final states"
  gh pr checks "${pr_url}" || true
  fail "PR checks did not pass for ${pr_url}"
fi

echo "== final check states =="
gh pr checks "${pr_url}"

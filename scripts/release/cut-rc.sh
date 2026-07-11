#!/usr/bin/env bash
set -euo pipefail

# Validate the current topic branch's PR against the release branch, rebase-merge it
# by number (linear-history-compliant; resumable if the PR was already merged by hand),
# tag the landed commit, and verify the resulting publish run.
cd "$(git rev-parse --show-toplevel)"

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; exit 1; }

# pyproject.toml is the source of truth for the target version; rcs are tag-only
# (no file bump per rc), so the next-rc number comes from existing tags, not the file.
resolve_pyproject_version() {
  local v
  v=$(grep -m1 '^version[[:space:]]*=' pyproject.toml | sed -E 's/^version[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/')
  if [[ -z "${v}" ]]; then
    fail "could not parse 'version = \"X.Y.Z\"' from pyproject.toml"
  fi
  echo "${v}"
}

suggest_next_rc_tag() {
  local version="$1"
  local final_tag="v${version}"
  if git rev-parse -q --verify "refs/tags/${final_tag}" >/dev/null; then
    fail "tag ${final_tag} already exists — version ${version} is already released; bump pyproject.toml before cutting another rc"
  fi
  local max_n
  max_n=$(git tag -l "v${version}-rc.*" | sed -E "s/^v${version}-rc\.//" | sort -n | tail -1)
  if [[ -z "${max_n}" ]]; then
    max_n=0
  fi
  echo "v${version}-rc.$((max_n + 1))"
}

if [[ $# -gt 1 ]]; then
  fail "usage: bash scripts/release/cut-rc.sh [<vX.Y.Z[-rc.N]> | next]"
fi

if [[ $# -eq 0 ]]; then
  suggested=$(suggest_next_rc_tag "$(resolve_pyproject_version)")
  echo "next rc tag: ${suggested} — run: bash scripts/release/cut-rc.sh next  (or pass the tag explicitly)"
  exit 2
fi

tag="$1"

if [[ "${tag}" == "next" ]]; then
  tag=$(suggest_next_rc_tag "$(resolve_pyproject_version)")
  pass "resolved 'next' -> ${tag}"
fi

if [[ ! "${tag}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?$ ]]; then
  fail "tag '${tag}' does not match vX.Y.Z[-rc.N]"
fi
pass "tag format valid: ${tag}"

echo "== checking tag ${tag} is unused =="
if git rev-parse -q --verify "refs/tags/${tag}" >/dev/null; then
  fail "tag ${tag} already exists locally — delete it or pick a new tag before re-running"
fi
remote_tag_out=$(git ls-remote --tags origin "refs/tags/${tag}")
if [[ -n "${remote_tag_out}" ]]; then
  fail "tag ${tag} already exists on origin — delete it or pick a new tag before re-running"
fi
pass "tag ${tag} is unused locally and on origin"

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

echo "== resolving PR for current branch =="
pr_json=$(gh pr view --json number,state,baseRefName,headRefName,statusCheckRollup)
pr_number=$(echo "${pr_json}" | jq -r '.number')
pr_state=$(echo "${pr_json}" | jq -r '.state')
pr_base=$(echo "${pr_json}" | jq -r '.baseRefName')
pr_head=$(echo "${pr_json}" | jq -r '.headRefName')
pass "found PR #${pr_number} (${pr_head} -> ${pr_base}, state=${pr_state})"

if [[ "${pr_state}" != "OPEN" && "${pr_state}" != "MERGED" ]]; then
  fail "PR #${pr_number} is not OPEN or MERGED (state=${pr_state})"
fi

if [[ "${pr_base}" != "${release_branch}" ]]; then
  fail "PR #${pr_number} targets '${pr_base}', not the discovered release branch '${release_branch}'"
fi
pass "PR #${pr_number} targets ${release_branch}"

if [[ "${pr_state}" == "OPEN" ]]; then
  echo "== validating PR checks are green =="
  check_count=$(echo "${pr_json}" | jq '.statusCheckRollup | length')
  if [[ "${check_count}" -eq 0 ]]; then
    fail "PR #${pr_number} has no checks reported — cannot confirm green"
  fi
  non_green=$(echo "${pr_json}" | jq -r '
    [.statusCheckRollup[]
      | {name: (.name // .context // "unnamed"),
         ok: (if has("conclusion") then
                (.status == "COMPLETED" and (.conclusion == "SUCCESS" or .conclusion == "NEUTRAL" or .conclusion == "SKIPPED"))
              else
                (.state == "SUCCESS")
              end)}
      | select(.ok | not)
      | .name
    ] | .[]
  ')
  if [[ -n "${non_green}" ]]; then
    echo "FAIL: non-green checks on PR #${pr_number}:"
    echo "${non_green}"
    fail "PR #${pr_number} is not green"
  fi
  pass "all ${check_count} checks on PR #${pr_number} are green"

  echo "== merging PR #${pr_number} =="
  if ! gh pr merge "${pr_number}" --rebase; then
    fail "merge blocked for PR #${pr_number} — likely the release branch ruleset (requires an approving review + linear history; merge commits are prohibited). Merge it yourself with: gh pr merge ${pr_number} --admin --rebase — then re-run 'bash scripts/release/cut-rc.sh ${tag}' to resume: it will detect the already-merged PR, skip the merge step, and continue with tag + publish-run verification."
  fi
  pass "PR #${pr_number} merged (rebase, signed commits preserved)"
else
  echo "NOTE: PR #${pr_number} is already MERGED — skipping the merge step, resuming from here"
fi

echo "== resolving landed commit =="
merge_oid=$(gh pr view "${pr_number}" --json mergeCommit --jq '.mergeCommit.oid')
if [[ -z "${merge_oid}" || "${merge_oid}" == "null" ]]; then
  echo "  mergeCommit oid not available yet — falling back to release branch head"
  git fetch origin
  merge_oid=$(git rev-parse "origin/${release_branch}")
fi
if [[ -z "${merge_oid}" ]]; then
  fail "could not resolve the landed commit for PR #${pr_number}"
fi
pass "landed commit: ${merge_oid}"

echo "== fetch + tag landed commit =="
git fetch origin
git tag -a "${tag}" "${merge_oid}" -m "${tag}"
git push origin "${tag}"
pass "tagged and pushed ${tag} (annotated, at landed commit ${merge_oid})"

echo "== locating build-gateway-image run for ${tag} =="
run_id=""
for attempt in $(seq 1 30); do
  run_id=$(gh run list --workflow=build-image.yml --limit 50 --json databaseId,headBranch \
    --jq ".[] | select(.headBranch==\"${tag}\") | .databaseId" | head -n1)
  if [[ -n "${run_id}" ]]; then
    break
  fi
  echo "  waiting for run to register (${attempt}/30)..."
  sleep 10
done

if [[ -z "${run_id}" ]]; then
  fail "no build-gateway-image run found for tag ${tag}"
fi
pass "found run ${run_id} for tag ${tag}"

echo "== watching run ${run_id} =="
if gh run watch "${run_id}" --exit-status; then
  pass "run ${run_id} succeeded"
else
  fail "run ${run_id} failed"
fi

echo "== published images =="
manifests=$(gh run view "${run_id}" --log | grep -oE "pushing manifest for ghcr.io/[^@ ]+" | sort -u || true)
if [[ -z "${manifests}" ]]; then
  echo "WARN: no 'pushing manifest' lines found in run log — image may still have published, check manually"
else
  echo "${manifests}"
fi

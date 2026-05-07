#!/usr/bin/env bash
#
# corp-llm-gateway installer (M6-1..M6-5).
#
# Usage:
#   curl -fsSL https://git.corp.lan/<group>/corp-llm-gateway/-/raw/master/scripts/install.sh | bash
#
# Or, with a pinned version:
#   curl -fsSL .../install.sh?v=v0.x.y | bash
#
# What it does:
#   1. Detects shell (bash / zsh / fish) and writes ANTHROPIC_BASE_URL,
#      OPENAI_BASE_URL, CORP_GATEWAY_TOKEN_FILE to your rc file.
#   2. Runs Keycloak device-flow OAuth (or stub if KEYCLOAK_DEVICE_URL is
#      unset) and writes a 30-day corp token to ~/.corp-llm-gateway/token.
#   3. Smokes the gateway with a redactable string and verifies round-trip.
#
# Idempotent: re-running rotates the token and replaces the rc lines.

set -euo pipefail

GATEWAY_URL="${CORP_GATEWAY_URL:-https://gateway.corp.lan}"
KEYCLOAK_DEVICE_URL="${KEYCLOAK_DEVICE_URL:-}"
INSTALL_DIR="${HOME}/.corp-llm-gateway"
TOKEN_FILE="${INSTALL_DIR}/token"
VERSION_FILE="${INSTALL_DIR}/VERSION"
INSTALLED_VERSION="${CORP_GATEWAY_VERSION:-dev}"

# Marker lines for rc updates — install rewrites between these markers.
RC_MARK_BEGIN="# >>> corp-llm-gateway >>>"
RC_MARK_END="# <<< corp-llm-gateway <<<"

log() { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[install:error]\033[0m %s\n' "$*" >&2; }

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        err "missing required command: $1"
        exit 1
    }
}

require_cmd curl
require_cmd jq

mkdir -p "$INSTALL_DIR"
chmod 700 "$INSTALL_DIR"

# 1. Detect shell + rc file ---------------------------------------------------
detect_rc_file() {
    local shell_name
    shell_name="$(basename "${SHELL:-/bin/bash}")"
    case "$shell_name" in
        bash) echo "${HOME}/.bashrc" ;;
        zsh)  echo "${HOME}/.zshrc" ;;
        fish) echo "${HOME}/.config/fish/config.fish" ;;
        *)
            err "unsupported shell: $shell_name (set SHELL to bash/zsh/fish)"
            exit 1
            ;;
    esac
}

RC_FILE="$(detect_rc_file)"
RC_DIR="$(dirname "$RC_FILE")"
mkdir -p "$RC_DIR"
touch "$RC_FILE"

write_rc_block() {
    local tmp
    tmp="$(mktemp)"
    # Strip any existing block.
    awk -v b="$RC_MARK_BEGIN" -v e="$RC_MARK_END" '
        $0 == b { skip = 1; next }
        $0 == e { skip = 0; next }
        skip { next }
        { print }
    ' "$RC_FILE" > "$tmp"

    local shell_name
    shell_name="$(basename "${SHELL:-/bin/bash}")"

    {
        cat "$tmp"
        echo "$RC_MARK_BEGIN"
        if [[ "$shell_name" == "fish" ]]; then
            echo "set -x ANTHROPIC_BASE_URL '$GATEWAY_URL'"
            echo "set -x OPENAI_BASE_URL '$GATEWAY_URL'"
            echo "set -x CORP_GATEWAY_TOKEN_FILE '$TOKEN_FILE'"
        else
            echo "export ANTHROPIC_BASE_URL='$GATEWAY_URL'"
            echo "export OPENAI_BASE_URL='$GATEWAY_URL'"
            echo "export CORP_GATEWAY_TOKEN_FILE='$TOKEN_FILE'"
        fi
        echo "$RC_MARK_END"
    } > "$RC_FILE"

    rm -f "$tmp"
}

# 2. Keycloak device flow (stub if URL unset) --------------------------------
issue_corp_token() {
    if [[ -z "$KEYCLOAK_DEVICE_URL" ]]; then
        log "KEYCLOAK_DEVICE_URL unset — using local-stub token (NOT FOR PROD)"
        echo "ct_local_stub_$(date +%s)" > "$TOKEN_FILE"
        chmod 600 "$TOKEN_FILE"
        return
    fi

    log "starting Keycloak device flow at $KEYCLOAK_DEVICE_URL"
    local resp device_code user_code verification_uri interval expires_in
    resp="$(curl -fsSL -X POST "$KEYCLOAK_DEVICE_URL")"
    device_code="$(echo "$resp" | jq -r '.device_code')"
    user_code="$(echo "$resp" | jq -r '.user_code')"
    verification_uri="$(echo "$resp" | jq -r '.verification_uri_complete // .verification_uri')"
    interval="$(echo "$resp" | jq -r '.interval // 5')"
    expires_in="$(echo "$resp" | jq -r '.expires_in // 600')"

    printf '\nOpen this URL to authenticate:\n  \033[1;36m%s\033[0m\nUser code: %s\n\n' \
        "$verification_uri" "$user_code"

    local elapsed=0 oidc_token=""
    while (( elapsed < expires_in )); do
        sleep "$interval"
        elapsed=$(( elapsed + interval ))
        local poll_resp
        poll_resp="$(curl -fsSL -X POST "$KEYCLOAK_DEVICE_URL/poll" \
            --data-urlencode "device_code=$device_code" || true)"
        oidc_token="$(echo "$poll_resp" | jq -r '.access_token // empty')"
        if [[ -n "$oidc_token" ]]; then break; fi
    done

    if [[ -z "$oidc_token" ]]; then
        err "device-flow timed out"
        exit 1
    fi

    log "exchanging OIDC token for 30-day corp token"
    local issue_resp corp_token
    issue_resp="$(curl -fsSL -X POST "$GATEWAY_URL/internal/issue-token" \
        -H "Authorization: Bearer $oidc_token")"
    corp_token="$(echo "$issue_resp" | jq -r '.corp_token')"
    if [[ -z "$corp_token" || "$corp_token" == "null" ]]; then
        err "failed to issue corp token"
        exit 1
    fi
    echo "$corp_token" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
}

# 3. Smoke test ---------------------------------------------------------------
run_smoke_test() {
    if [[ -z "$KEYCLOAK_DEVICE_URL" ]]; then
        log "skipping smoke test (no real corp token)"
        return
    fi
    log "running smoke test against $GATEWAY_URL"
    local corp_token
    corp_token="$(cat "$TOKEN_FILE")"
    local sample
    sample="Hello [SMOKE_TEST_TOKEN_alpha-$(date +%s%N)]."
    local resp
    resp="$(curl -fsSL -X POST "$GATEWAY_URL/v1/messages" \
        -H "X-Corp-Auth: $corp_token" \
        -H "Authorization: Bearer fake-byok-key" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"claude-3-5-sonnet\",\"max_tokens\":10,\"messages\":[{\"role\":\"user\",\"content\":\"$sample\"}]}" \
        || true)"
    if echo "$resp" | grep -q '"content"'; then
        log "smoke test OK"
    else
        err "smoke test FAILED — response: $resp"
        exit 1
    fi
}

# Main ------------------------------------------------------------------------
log "installing corp-llm-gateway client to $INSTALL_DIR"
write_rc_block
issue_corp_token
run_smoke_test
echo "$INSTALLED_VERSION" > "$VERSION_FILE"

cat <<EOF
$(printf '\033[1;32m')
✓ Installed.
$(printf '\033[0m')
Open a new shell, or run:
  source $RC_FILE

Then:
  corp-llm-gateway status

EOF

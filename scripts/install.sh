#!/usr/bin/env bash
#
# corp-llm-gateway installer (M6-1..M6-5).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/jLAM-ERR/corp-llm-gateway/main/scripts/install.sh | bash
#
# Or pinned to a tag/ref (GitHub serves raw by ref in the path):
#   curl -fsSL https://raw.githubusercontent.com/jLAM-ERR/corp-llm-gateway/v0.x.y/scripts/install.sh | bash
#
# What it does:
#   1. Detects shell (bash / zsh / fish) and writes ANTHROPIC_BASE_URL,
#      OPENAI_BASE_URL, CORP_GATEWAY_TOKEN_FILE to your rc file.
#   2. Signs you in to Keycloak (RFC 8628 device flow, KEYCLOAK_ISSUER +
#      KEYCLOAK_CLIENT_ID), trades the access token at the gateway's
#      /internal/issue-token for a 30-day corp token and writes it 0600 to
#      ~/.corp-llm-gateway/token. With KEYCLOAK_ISSUER unset it skips this step.
#   3. Smokes the gateway with your subscription token (ANTHROPIC_AUTH_TOKEN);
#      skipped when that is unset.
#
# Idempotent: re-running rotates the token and replaces the rc lines.

set -euo pipefail

GATEWAY_URL="${CORP_GATEWAY_URL:-https://gateway.corp.lan}"
KEYCLOAK_ISSUER="${KEYCLOAK_ISSUER:-}"
KEYCLOAK_ISSUER="${KEYCLOAK_ISSUER%/}"
KEYCLOAK_CLIENT_ID="${KEYCLOAK_CLIENT_ID:-}"
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

if [[ -n "$KEYCLOAK_ISSUER" && -z "$KEYCLOAK_CLIENT_ID" ]]; then
    err "KEYCLOAK_CLIENT_ID is required when KEYCLOAK_ISSUER is set"
    exit 1
fi

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
            echo "set -x OPENAI_BASE_URL '$GATEWAY_URL/v1'"
            echo "set -x CORP_GATEWAY_TOKEN_FILE '$TOKEN_FILE'"
            echo "# Pattern 1 (Claude Code): inject X-Corp-Auth via ANTHROPIC_CUSTOM_HEADERS."
            echo "if test -f '$TOKEN_FILE'"
            echo "    set -x ANTHROPIC_CUSTOM_HEADERS \"X-Corp-Auth: \$(cat '$TOKEN_FILE')\""
            echo "end"
        else
            echo "export ANTHROPIC_BASE_URL='$GATEWAY_URL'"
            echo "export OPENAI_BASE_URL='$GATEWAY_URL/v1'"
            echo "export CORP_GATEWAY_TOKEN_FILE='$TOKEN_FILE'"
            echo "# Pattern 1 (Claude Code): inject X-Corp-Auth via ANTHROPIC_CUSTOM_HEADERS."
            echo "# Re-evaluated per shell start so token rotation takes effect."
            echo "if [[ -f '$TOKEN_FILE' ]]; then"
            echo "  export ANTHROPIC_CUSTOM_HEADERS=\"X-Corp-Auth: \$(cat '$TOKEN_FILE')\""
            echo "fi"
        fi
        echo "$RC_MARK_END"
    } > "$RC_FILE"

    rm -f "$tmp"
}

# HTTP helpers -----------------------------------------------------------------
# Secrets travel to curl through a config file on stdin, never through argv.
curl_cfg_quote() {
    local v="$1"
    v="${v//\\/\\\\}"
    v="${v//\"/\\\"}"
    printf '"%s"' "$v"
}

# http_post <url> <curl config lines>: prints the body, then the HTTP status on
# its own last line. Non-zero only when the request never got an answer.
http_post() {
    printf 'url = %s\n%s\n' "$(curl_cfg_quote "$1")" "$2" \
        | curl -sS -X POST -w '\n%{http_code}' -K -
}

json_get() {
    printf '%s' "$1" | jq -r "$2 // empty" 2>/dev/null || true
}

# The error code of an OAuth / gateway error body, only when it looks like one.
error_code() {
    local code pattern='^[A-Za-z0-9_.:-]{1,64}$'
    code="$(json_get "$1" '.error // .code')"
    if [[ "$code" =~ $pattern ]]; then
        printf '%s' "$code"
    fi
}

positive_int_or() {
    local pattern='^[0-9]+$'
    if [[ "$1" =~ $pattern ]] && [[ "$1" -ge 1 ]]; then
        printf '%s' "$1"
    else
        printf '%s' "$2"
    fi
}

# 2. Keycloak device flow + corp-token exchange ---------------------------------
OIDC_ACCESS_TOKEN=""

keycloak_device_login() {
    local device_url="$KEYCLOAK_ISSUER/protocol/openid-connect/auth/device"
    local token_url="$KEYCLOAK_ISSUER/protocol/openid-connect/token"
    local resp status body code

    log "signing in with Keycloak at $KEYCLOAK_ISSUER"
    if ! resp="$(http_post "$device_url" \
        "data-urlencode = $(curl_cfg_quote "client_id=$KEYCLOAK_CLIENT_ID")")"; then
        err "cannot reach Keycloak at $KEYCLOAK_ISSUER"
        exit 1
    fi
    status="${resp##*$'\n'}"
    body="${resp%$'\n'*}"
    if [[ "$status" != "200" ]]; then
        code="$(error_code "$body")"
        err "Keycloak refused the device login (HTTP $status${code:+, $code})"
        exit 1
    fi

    local device_code user_code uri uri_complete interval expires_in
    device_code="$(json_get "$body" '.device_code')"
    user_code="$(json_get "$body" '.user_code')"
    uri="$(json_get "$body" '.verification_uri')"
    uri_complete="$(json_get "$body" '.verification_uri_complete')"
    interval="$(positive_int_or "$(json_get "$body" '.interval')" 5)"
    expires_in="$(positive_int_or "$(json_get "$body" '.expires_in')" 600)"
    if [[ -z "$device_code" || ( -z "$uri_complete" && ( -z "$uri" || -z "$user_code" ) ) ]]; then
        err "Keycloak's device login response is incomplete"
        exit 1
    fi

    if [[ -n "$uri_complete" ]]; then
        printf '\nOpen this URL in a browser and approve the sign-in:\n  \033[1;36m%s\033[0m\n' \
            "$uri_complete"
        if [[ -n "$user_code" ]]; then
            printf 'The page should show the code: %s\n' "$user_code"
        fi
    else
        printf '\nOpen this URL in a browser:\n  \033[1;36m%s\033[0m\nand enter the code: %s\n' \
            "$uri" "$user_code"
    fi
    printf '\nWaiting for you to approve...\n\n'

    local poll_cfg deadline=$(( SECONDS + expires_in ))
    poll_cfg="data-urlencode = $(curl_cfg_quote "grant_type=urn:ietf:params:oauth:grant-type:device_code")
data-urlencode = $(curl_cfg_quote "device_code=$device_code")
data-urlencode = $(curl_cfg_quote "client_id=$KEYCLOAK_CLIENT_ID")"

    while [[ "$SECONDS" -lt "$deadline" ]]; do
        sleep "$interval"
        if ! resp="$(http_post "$token_url" "$poll_cfg")"; then
            err "lost the connection to Keycloak while waiting for sign-in"
            exit 1
        fi
        status="${resp##*$'\n'}"
        body="${resp%$'\n'*}"
        if [[ "$status" == "200" ]]; then
            OIDC_ACCESS_TOKEN="$(json_get "$body" '.access_token')"
            if [[ -z "$OIDC_ACCESS_TOKEN" ]]; then
                err "Keycloak answered without an access token"
                exit 1
            fi
            return
        fi
        code="$(error_code "$body")"
        case "$code" in
            authorization_pending) ;;
            slow_down) interval=$(( interval + 5 )) ;;
            expired_token)
                err "the sign-in code expired before it was approved — re-run install.sh"
                exit 1
                ;;
            access_denied)
                err "the sign-in was denied in Keycloak — re-run install.sh to try again"
                exit 1
                ;;
            *)
                err "Keycloak token request failed (HTTP $status${code:+, $code})"
                exit 1
                ;;
        esac
    done
    err "the sign-in code expired before it was approved — re-run install.sh"
    exit 1
}

write_token_file() {
    local tmp="$TOKEN_FILE.tmp.$$"
    (
        umask 077
        printf '%s\n' "$1" > "$tmp"
    )
    chmod 600 "$tmp"
    mv -f "$tmp" "$TOKEN_FILE"
}

issue_corp_token() {
    if [[ -z "$KEYCLOAK_ISSUER" ]]; then
        log "KEYCLOAK_ISSUER unset — skipping sign-in; no corp token was issued"
        log "ask your gateway operator for one (gateway-admin token issue) and save it to $TOKEN_FILE (mode 0600)"
        return
    fi

    keycloak_device_login

    log "exchanging the Keycloak sign-in for a corp token at $GATEWAY_URL"
    local resp status body code corp_token expires_at pattern='^[A-Za-z0-9._~+/=-]+$'
    if ! resp="$(http_post "$GATEWAY_URL/internal/issue-token" \
        "header = $(curl_cfg_quote "Authorization: Bearer $OIDC_ACCESS_TOKEN")
header = \"Content-Length: 0\"")"; then
        OIDC_ACCESS_TOKEN=""
        err "cannot reach the gateway at $GATEWAY_URL"
        exit 1
    fi
    OIDC_ACCESS_TOKEN=""
    status="${resp##*$'\n'}"
    body="${resp%$'\n'*}"
    if [[ "$status" != "200" ]]; then
        code="$(error_code "$body")"
        err "the gateway refused to issue a corp token (HTTP $status${code:+, $code})"
        exit 1
    fi
    corp_token="$(json_get "$body" '.corp_token')"
    expires_at="$(json_get "$body" '.expires_at')"
    if ! [[ "$corp_token" =~ $pattern ]]; then
        err "the gateway answered without a usable corp token"
        exit 1
    fi
    write_token_file "$corp_token"
    log "corp token written to $TOKEN_FILE (expires ${expires_at:-in 30 days})"
}

# 3. Smoke test ---------------------------------------------------------------
run_smoke_test() {
    if [[ -z "${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
        log "skipping smoke test: ANTHROPIC_AUTH_TOKEN is unset (export your subscription token and re-run to smoke-test)"
        return
    fi
    if [[ ! -s "$TOKEN_FILE" ]]; then
        log "skipping smoke test: no corp token at $TOKEN_FILE"
        return
    fi
    log "running smoke test against $GATEWAY_URL"
    local corp_token sample payload resp status body code
    corp_token="$(cat "$TOKEN_FILE")"
    sample="Hello [SMOKE_TEST_TOKEN_alpha-$(date +%s)]."
    payload="{\"model\":\"claude-haiku-4-5\",\"max_tokens\":10,\"messages\":[{\"role\":\"user\",\"content\":\"$sample\"}]}"
    if ! resp="$(http_post "$GATEWAY_URL/v1/messages" \
        "header = $(curl_cfg_quote "X-Corp-Auth: $corp_token")
header = $(curl_cfg_quote "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN")
header = \"Content-Type: application/json\"
header = \"anthropic-version: 2023-06-01\"
data-binary = $(curl_cfg_quote "$payload")")"; then
        err "smoke test FAILED — cannot reach $GATEWAY_URL"
        exit 1
    fi
    status="${resp##*$'\n'}"
    body="${resp%$'\n'*}"
    if [[ "$status" == "200" ]] && printf '%s' "$body" | grep -q '"content"'; then
        log "smoke test OK"
    else
        code="$(error_code "$body")"
        err "smoke test FAILED (HTTP $status${code:+, $code})"
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

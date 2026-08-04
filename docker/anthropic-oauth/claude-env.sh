# Client-side env for `claude` against the anthropic-oauth overlay. Source it
# from your LAPTOP shell — never from .env.demo (see .env.demo.example for why):
#
#   export ANTHROPIC_AUTH_TOKEN='sk-ant-oat...'     # your subscription token
#   source docker/anthropic-oauth/claude-env.sh
#   claude
#
# This file is the single place the corp-identity header layout is written. If
# the header carrying corp identity ever changes, change it here only.

CORP_GATEWAY_URL="${CORP_GATEWAY_URL:-http://127.0.0.1:4000}"
CORP_TEAM_TOKEN="${DEMO_TEAM_TOKEN:-demo-team-token}"

export ANTHROPIC_BASE_URL="$CORP_GATEWAY_URL"
export ANTHROPIC_CUSTOM_HEADERS="X-Corp-Auth: $CORP_TEAM_TOKEN"

# An API key would shadow the subscription token in `claude` and defeat the
# whole point of this profile.
unset ANTHROPIC_API_KEY

if [ -z "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
    echo "claude-env: ANTHROPIC_AUTH_TOKEN is unset — the gateway will answer 401 E_PROVIDER_AUTH" >&2
elif [ "${ANTHROPIC_AUTH_TOKEN#sk-ant-oat}" = "$ANTHROPIC_AUTH_TOKEN" ]; then
    # Never print the value; only whether its shape can select litellm's OAuth branch.
    echo "claude-env: ANTHROPIC_AUTH_TOKEN is not an sk-ant-oat OAuth token — the gateway rejects it" >&2
fi

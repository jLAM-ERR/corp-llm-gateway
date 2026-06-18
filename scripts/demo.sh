#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="docker-compose.demo.yml"
ENV_FILE=".env.demo"

# Helper functions
fatal() {
    echo "FATAL: $*" >&2
    exit 1
}

warn() {
    echo "WARN: $*" >&2
}

info() {
    echo "INFO: $*" >&2
}

# Print usage
usage() {
    cat >&2 <<'EOF'
Usage: scripts/demo.sh <subcommand>

Subcommands:
  up              Pre-flight checks, bring up demo stack, seed Langfuse, print URLs
  down            Stop demo stack (preserve volumes)
  reset           Stop demo stack and nuke all volumes
  seed-langfuse   Idempotent Langfuse project setup (run after Langfuse is healthy)
  presenter-env   Print copy-pasteable export block for second shell
  --help, -h      Print this help and exit

Examples:
  scripts/demo.sh up               # Cold boot the demo stack
  scripts/demo.sh down             # Tear down, preserve state
  scripts/demo.sh presenter-env    # Get env vars for Claude Code shell
EOF
}

# Pre-flight checks (called from 'up')
preflight_checks() {
    # Check docker is on PATH
    command -v docker >/dev/null || fatal "docker not on PATH"

    # Check docker compose v2
    docker compose version >/dev/null 2>&1 || fatal "docker compose v2 required"

    # Check jq (used for healthcheck polling and Langfuse JSON parsing)
    command -v jq >/dev/null || fatal "jq not on PATH (brew install jq / apt install jq)"

    # Check curl (used for reachability + Langfuse setup API)
    command -v curl >/dev/null || fatal "curl not on PATH"

    # Check .env.demo exists, or copy from .env.demo.example
    if [[ ! -f "$ENV_FILE" ]]; then
        if [[ -f "${ENV_FILE}.example" ]]; then
            info "Copying ${ENV_FILE}.example to ${ENV_FILE}"
            cp "${ENV_FILE}.example" "$ENV_FILE"
            warn "Edit ${ENV_FILE} and set CORP_LLM_ENDPOINT before running 'up'"
            exit 1
        else
            fatal "${ENV_FILE}.example not found"
        fi
    fi

    # Source .env.demo and verify CORP_LLM_ENDPOINT is set
    # shellcheck disable=SC1090,SC1091
    source "$ENV_FILE"
    if [[ -z "${CORP_LLM_ENDPOINT:-}" ]]; then
        fatal "CORP_LLM_ENDPOINT not set in ${ENV_FILE}"
    fi

    # Non-fatal reachability check
    curl -fsS --max-time 5 "$CORP_LLM_ENDPOINT/health" >/dev/null 2>&1 || \
        warn "Corp LLM unreachable at $CORP_LLM_ENDPOINT — VPN connected?"
}

# Wait for all services to be healthy
wait_for_healthcheck() {
    local max_wait=240
    local elapsed=0
    local interval=5

    info "Waiting for all services to be healthy (max ${max_wait}s)..."

    while (( elapsed < max_wait )); do
        # Poll the compose ps output
        local all_healthy=true

        # Get JSON status and check each service
        if docker compose -f "$COMPOSE_FILE" ps --format json 2>/dev/null | grep -q . ; then
            # Check if all services are either healthy or running (for services without healthchecks)
            while IFS= read -r line; do
                local status
                status=$(echo "$line" | grep -o '"State":"[^"]*"' | cut -d'"' -f4 || true)
                local health
                health=$(echo "$line" | grep -o '"Health":"[^"]*"' | cut -d'"' -f4 || true)

                # Service is OK if: State is "running" OR Health is "healthy"
                if [[ "$status" != "running" && "$health" != "healthy" ]]; then
                    all_healthy=false
                    break
                fi
            done < <(docker compose -f "$COMPOSE_FILE" ps --format json 2>/dev/null | jq -r '.[]')
        else
            all_healthy=false
        fi

        if [[ "$all_healthy" == "true" ]]; then
            info "All services healthy"
            return 0
        fi

        sleep $interval
        (( elapsed += interval ))
    done

    fatal "Services did not reach healthy state within ${max_wait}s"
}

# Seed Langfuse with demo-team project (idempotent).
#
# Langfuse v3 has no anonymous project-bootstrap API. The project, org,
# and user are pre-seeded via LANGFUSE_INIT_* env vars in
# docker-compose.demo.yml (search for LANGFUSE_INIT_PROJECT_PUBLIC_KEY).
# This function just verifies the seeded keys actually work and writes
# them to .env.demo. The pair below MUST match the compose values.
seed_langfuse() {
    local langfuse_url="http://localhost:3000"
    local health_endpoint="$langfuse_url/api/public/health"
    local max_wait=60
    local elapsed=0
    local interval=2

    # Keep in sync with docker-compose.demo.yml `LANGFUSE_INIT_PROJECT_*_KEY`.
    local public_key="pk-lf-demo-00000000-0000-0000-0000-000000000001"
    local secret_key="sk-lf-demo-00000000-0000-0000-0000-000000000002"

    info "Waiting for Langfuse to be ready..."

    # Poll health endpoint
    while (( elapsed < max_wait )); do
        if curl -fsS --max-time 5 "$health_endpoint" >/dev/null 2>&1; then
            info "Langfuse is ready"
            break
        fi
        sleep $interval
        (( elapsed += interval ))
    done

    if (( elapsed >= max_wait )); then
        fatal "Langfuse health check timed out after ${max_wait}s"
    fi

    # Verify the pre-seeded keys actually authenticate. If not, the
    # init didn't run (usually a stale langfuse-postgres volume that
    # predates the LANGFUSE_INIT_* block).
    local auth_check
    auth_check=$(curl -sS -o /dev/null -w '%{http_code}' \
        --max-time 5 -u "${public_key}:${secret_key}" \
        "$langfuse_url/api/public/projects" 2>/dev/null) || auth_check="000"

    if [[ "$auth_check" != "200" ]]; then
        fatal "Pre-seeded Langfuse keys do not authenticate (HTTP $auth_check).
       This usually means langfuse-postgres has data from a run that
       predates the LANGFUSE_INIT_* block in docker-compose.demo.yml.
       Wipe it and retry:
         docker compose -f docker-compose.demo.yml down
         docker volume rm corp-llm-gateway_langfuse-postgres-data
         scripts/demo.sh up"
    fi

    info "Langfuse credentials verified: public_key=${public_key:0:11}..., secret_key=${secret_key:0:11}..."

    # Update .env.demo with sed (in-place replacement, no duplicates)
    info "Updating ${ENV_FILE} with Langfuse credentials..."

    # Backup for portability
    local backup_file="${ENV_FILE}.bak"
    cp "$ENV_FILE" "$backup_file"

    # Replace or add LANGFUSE_PUBLIC_KEY
    if grep -q "^LANGFUSE_PUBLIC_KEY=" "$ENV_FILE"; then
        sed -i.tmp "s|^LANGFUSE_PUBLIC_KEY=.*|LANGFUSE_PUBLIC_KEY=$public_key|" "$ENV_FILE"
        rm -f "${ENV_FILE}.tmp"
    else
        echo "LANGFUSE_PUBLIC_KEY=$public_key" >> "$ENV_FILE"
    fi

    # Replace or add LANGFUSE_SECRET_KEY
    if grep -q "^LANGFUSE_SECRET_KEY=" "$ENV_FILE"; then
        sed -i.tmp "s|^LANGFUSE_SECRET_KEY=.*|LANGFUSE_SECRET_KEY=$secret_key|" "$ENV_FILE"
        rm -f "${ENV_FILE}.tmp"
    else
        echo "LANGFUSE_SECRET_KEY=$secret_key" >> "$ENV_FILE"
    fi

    # Clean up backup
    rm -f "$backup_file"

    # Restart vector to pick up new env
    info "Restarting vector service..."
    docker compose -f "$COMPOSE_FILE" restart vector || fatal "Failed to restart vector"

    info "Langfuse seeding complete"
}

# Main subcommand dispatch
case "${1:-}" in
    up)
        preflight_checks
        info "Starting demo stack..."
        docker compose -f "$COMPOSE_FILE" up -d
        wait_for_healthcheck
        seed_langfuse
        cat >&2 <<'EOF'

Demo stack is ready!

LiteLLM proxy:   http://localhost:4000
Langfuse UI:     http://localhost:3000   (login: demo@corp.lan / demo-password-12345)
MinIO console:   http://localhost:9001   (login: minioadmin / minioadmin)

Next: 'scripts/demo.sh presenter-env' → paste into second shell for Claude Code.
EOF
        ;;
    down)
        info "Stopping demo stack (volumes preserved)..."
        docker compose -f "$COMPOSE_FILE" down
        ;;
    reset)
        info "Stopping demo stack and removing volumes..."
        docker compose -f "$COMPOSE_FILE" down -v
        ;;
    seed-langfuse)
        seed_langfuse
        ;;
    presenter-env)
        cat <<'EOF'
export ANTHROPIC_BASE_URL=http://localhost:4000
export ANTHROPIC_CUSTOM_HEADERS='X-Corp-Auth: demo-team-token'
EOF
        ;;
    --help|-h|help)
        usage
        exit 0
        ;;
    "")
        usage
        exit 1
        ;;
    *)
        echo "Unknown subcommand: $1" >&2
        usage
        exit 1
        ;;
esac

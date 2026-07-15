#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="docker-compose.pilot.yml"
ENV_FILE=".env.pilot"

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
Usage: scripts/pilot.sh <subcommand>

Subcommands:
  up              Pre-flight checks, bring up pilot stack, seed Langfuse, print URLs
  down            Stop pilot stack (preserve volumes)
  reset           Stop pilot stack and nuke all volumes
  seed-langfuse   Idempotent Langfuse project setup (run after Langfuse is healthy)
  presenter-env   Print copy-pasteable export block for second shell
  logs            Tail litellm filtered to the sanitize/desanitize flow + audit
  --help, -h      Print this help and exit

Examples:
  scripts/pilot.sh up               # Cold boot the pilot stack
  scripts/pilot.sh down             # Tear down, preserve state
  scripts/pilot.sh presenter-env    # Get env vars for Claude Code shell
  scripts/pilot.sh logs             # Watch only the sanitize/desanitize flow
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

    # Check .env.pilot exists, or copy from .env.pilot.example
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

    # Source .env.pilot and verify CORP_LLM_ENDPOINT is set.
    # Use an explicit ./ prefix: under `sh script.sh` (bash in POSIX mode) the
    # `source` builtin searches only $PATH for a slash-less name and will not
    # fall back to the cwd, reporting "file not found" for a file that exists.
    # shellcheck disable=SC1090,SC1091
    source "./$ENV_FILE"
    if [[ -z "${CORP_LLM_ENDPOINT:-}" ]]; then
        fatal "CORP_LLM_ENDPOINT not set in ${ENV_FILE}"
    fi

    # Non-fatal reachability check
    curl -kfsS --max-time 5 "$CORP_LLM_ENDPOINT/health" >/dev/null 2>&1 || \
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
# docker-compose.pilot.yml (search for LANGFUSE_INIT_PROJECT_PUBLIC_KEY).
# This function just verifies the seeded keys actually work and writes
# them to .env.pilot. The pair below MUST match the compose values.
seed_langfuse() {
    local langfuse_url="http://localhost:3000"
    local health_endpoint="$langfuse_url/api/public/health"
    local max_wait=60
    local elapsed=0
    local interval=2

    # Keep in sync with docker-compose.pilot.yml `LANGFUSE_INIT_PROJECT_*_KEY`.
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
       predates the LANGFUSE_INIT_* block in docker-compose.pilot.yml.
       Wipe it and retry:
         docker compose -f docker-compose.pilot.yml down
         docker volume rm corp-llm-gateway-pilot_langfuse-postgres-data
         scripts/pilot.sh up"
    fi

    info "Langfuse credentials verified: public_key=${public_key:0:11}..., secret_key=${secret_key:0:11}..."

    # Update .env.pilot with sed (in-place replacement, no duplicates)
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

    # Recreate (NOT restart) vector so it re-reads the just-written .env.pilot.
    # `docker compose restart` only stops+starts the EXISTING container with the
    # env it was created with — env_file changes are NOT picked up. The keys we
    # just wrote would be ignored and vector would keep sending empty basic-auth
    # to Langfuse (HTTP 403). --force-recreate rebuilds the container from the
    # updated env_file.
    info "Recreating vector service to pick up new Langfuse keys..."
    docker compose -f "$COMPOSE_FILE" up -d --force-recreate vector \
        || fatal "Failed to recreate vector"

    info "Langfuse seeding complete"
}

# Main subcommand dispatch
case "${1:-}" in
    up)
        preflight_checks
        info "Starting pilot stack..."
        docker compose -f "$COMPOSE_FILE" up -d
        wait_for_healthcheck
        seed_langfuse
        cat >&2 <<'EOF'

Pilot stack is ready!

Gateway (nginx): http://localhost:8080   (nginx → litellm API + admin UI /ui)
Langfuse UI:     http://localhost:3000   (nginx → langfuse-web; login: demo@corp.lan / demo-password-12345)
MinIO console:   http://localhost:9001   (login: minioadmin / minioadmin)

Next: 'scripts/pilot.sh presenter-env' → paste into second shell for Claude Code.
EOF
        ;;
    down)
        info "Stopping pilot stack (volumes preserved)..."
        docker compose -f "$COMPOSE_FILE" down
        ;;
    reset)
        info "Stopping pilot stack and removing volumes..."
        docker compose -f "$COMPOSE_FILE" down -v
        ;;
    seed-langfuse)
        seed_langfuse
        ;;
    logs)
        # Focused view: only the gateway sanitize/desanitize flow + audit
        # records. Drops uvicorn access logs, the every-5s healthcheck probe,
        # and litellm's own startup/info noise. For the full, unfiltered stream
        # use: docker compose -f docker-compose.pilot.yml logs -f litellm
        info "Tailing the sanitize/desanitize flow (Ctrl-C to stop)..."
        docker compose -f "$COMPOSE_FILE" logs -f --no-log-prefix litellm \
            | grep --line-buffered -E 'corp_llm_gateway|"redaction_count"'
        ;;
    presenter-env)
        cat <<'EOF'
# Use 127.0.0.1, NOT `localhost`. When traffic flows through xray's HTTP
# inbound, the destination hostname `localhost` triggers a DNS-resolution
# code path that breaks the response (xray's HTTP forward returns 503).
# An explicit 127.0.0.1 skips that path entirely and works under every
# configuration we tested (direct, SOCKS5, xray HTTP-proxy).
# Port 8080 is nginx, the single entry point (litellm's :4000 is not published).
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
export ANTHROPIC_CUSTOM_HEADERS='X-Corp-Auth: demo-team-token'
# If you have a corp HTTP_PROXY / HTTPS_PROXY set, claude-code's node SDK
# will route the gateway call through it. Exclude the local gateway from
# proxying so loopback stays direct. (Node honors both cases.)
export NO_PROXY="localhost,127.0.0.1${NO_PROXY:+,$NO_PROXY}"
export no_proxy="localhost,127.0.0.1${no_proxy:+,$no_proxy}"
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

#!/usr/bin/env bash
#
# corp-llm-gateway deploy — day N, runs FROM the operator's laptop.
#
# Usage:
#   scripts/deploy/deploy.sh --host user@server [options] <up|down|restart|logs|status>
#
# What it does:
#   1. Stages src/corp_llm_gateway/tokens/schema.sql into
#      compose/postgres/initdb/01-schema.sql, so the token-store schema on the
#      server can never drift from the source tree.
#   2. rsyncs compose/ to the deploy directory created by
#      scripts/deploy/bootstrap-server.sh (day 0). The local .env is NEVER
#      sent and the server's .env is never touched: secrets live only in the
#      server's copy. Local key material and the dev-only build overlay are
#      excluded for the same reason, and so is gateway/config.toml: the
#      server owns it, a laptop copy must never overwrite it.
#   3. Over SSH: docker compose pull && docker compose up -d, then polls the
#      compose healthchecks and prints a status summary.
#
# A remote lock directory serializes state-changing runs, so two operators
# deploying at once get a clean refusal instead of an interleaved deploy.

set -euo pipefail

REMOTE_DIR="${CORP_GATEWAY_DEPLOY_DIR:-/opt/corp-llm-gateway}"
DEFAULT_REMOTE_DIR="/opt/corp-llm-gateway"
COMPOSE_FILE="docker-compose.yml"
# The default mode is oauth (subscription, the production mode): it layers
# docker-compose.oauth.yml on top. virtual-keys (the base file alone) is a test
# posture and has to be asked for. Every remote `docker compose` call has to
# carry the SAME file list: a `logs` or `status` run with only the base file
# resolves a different config than the running stack, and `up -d` with the wrong
# list would silently recreate the containers in the other mode. Hence one
# variable, used everywhere.
DEPLOY_MODE="oauth"
OAUTH_OVERLAY_FILE="docker-compose.oauth.yml"
COMPOSE_FILE_ARGS="-f ${COMPOSE_FILE} -f ${OAUTH_OVERLAY_FILE}"
# --issuance (or DEPLOY_ISSUANCE=1) layers the developer token issuance overlay
# on top of the oauth one. It mounts gateway/config.toml, which only the server
# holds.
DEPLOY_ISSUANCE="${DEPLOY_ISSUANCE:-0}"
ISSUANCE_OVERLAY_FILE="docker-compose.issuance.yml"
ISSUANCE_CONFIG="gateway/config.toml"
# The front door's TLS certificate and key are installed on the server by hand;
# only the README in that directory is synced.
NGINX_CERTS_DIR="nginx/certs"
NGINX_CERTS_README="${NGINX_CERTS_DIR}/README.md"
# The two front-door services. COMPOSE_PROFILES in the server's .env picks at
# most one of them; this script never sets, exports or clears it, because a
# value here would override that file and diverge from what the boot-time
# unit (a bare `docker compose up -d`) starts.
NGINX_SERVICES=(nginx nginx-ports)
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
COMPOSE_DIR="${REPO_ROOT}/compose"
SCHEMA_SOURCE="${REPO_ROOT}/src/corp_llm_gateway/tokens/schema.sql"
STAGED_SCHEMA_NAME="01-schema.sql"

HEALTH_MAX_WAIT="${CORP_GATEWAY_HEALTH_MAX_WAIT:-300}"
HEALTH_INTERVAL=5

HOST=""
SUBCOMMAND=""
EXTRA_ARGS=()
TAIL_LINES=200
DRY_RUN=0
ASSUME_YES=0
FORCE_UNLOCK=0
LOCK_DIR=""
LOCK_HELD=0
# The non-default stack-selecting flags of this run, each with a leading space,
# for the hints that tell the operator what to run next.
SELF_FLAGS=""

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

usage() {
    cat >&2 <<'EOF'
Usage: scripts/deploy/deploy.sh --host user@server [options] <subcommand>

Deploys the compose/ stack to a server prepared by
scripts/deploy/bootstrap-server.sh. Run from a checkout on your laptop.

Subcommands:
  up          Stage the schema, sync compose/, pull, start, wait for health
  down        Stop the stack (volumes are kept); asks for confirmation
  restart     Restart the running services, then wait for health
  logs        Follow the remote container logs (Ctrl-C to stop)
  status      Print the remote service/state/health summary

Options:
  --host USER@SERVER  SSH destination (required)
  --dir PATH          Remote deploy directory (default /opt/corp-llm-gateway)
  --mode MODE         oauth (default) or virtual-keys. `oauth` is the
                      production subscription mode: it adds
                      docker-compose.oauth.yml, developers authenticate with
                      their own Anthropic subscription token, and the server's
                      .env must contain NO LITELLM_MASTER_KEY line at all.
                      `virtual-keys` is the base file alone, a test posture
                      only (nothing can issue a litellm virtual key). Pass the
                      SAME --mode to every later run against that host —
                      logs/status/down resolve the stack through this file list.
  --issuance          Also apply docker-compose.issuance.yml (developer token
                      issuance). oauth mode only. `up` refuses to start unless
                      gateway/config.toml already exists in the remote deploy
                      directory; start from compose/gateway/config.toml.example.
                      The sync never uploads that file. Env equivalent:
                      DEPLOY_ISSUANCE=1. Pass it to every later run, like --mode.
  --tail N            Lines of history for `logs` (default 200)
  --dry-run           Print what would change; transfers and starts nothing
  --yes               Skip the confirmation prompt (needed for `down`)
  --force-unlock      Clear a stale deploy lock left by a killed run
  --help, -h          Print this help and exit

Secrets: the server's .env is the only copy. This script never uploads,
reads or prints one. Certificates and keys are excluded from the sync too.

Privacy: `logs` and `status` stream remote output straight to your terminal.
The stack redacts user content, but its logs may contain whatever the stack
logs — treat that output as sensitive and do not paste it into tickets.

Exit codes:
  0  done
  1  refused, unreachable, or the stack did not become healthy
EOF
}

parse_args() {
    while (( $# > 0 )); do
        case "$1" in
            --host)
                (( $# >= 2 )) || fatal "--host needs a user@server argument"
                HOST="$2"
                shift 2
                ;;
            --host=*)
                HOST="${1#*=}"
                shift
                ;;
            --dir)
                (( $# >= 2 )) || fatal "--dir needs a path argument"
                REMOTE_DIR="$2"
                shift 2
                ;;
            --dir=*)
                REMOTE_DIR="${1#*=}"
                shift
                ;;
            --mode)
                (( $# >= 2 )) || fatal "--mode needs virtual-keys or oauth"
                DEPLOY_MODE="$2"
                shift 2
                ;;
            --mode=*)
                DEPLOY_MODE="${1#*=}"
                shift
                ;;
            --issuance)
                DEPLOY_ISSUANCE=1
                shift
                ;;
            --tail)
                (( $# >= 2 )) || fatal "--tail needs a line count"
                TAIL_LINES="$2"
                shift 2
                ;;
            --tail=*)
                TAIL_LINES="${1#*=}"
                shift
                ;;
            --dry-run)
                DRY_RUN=1
                shift
                ;;
            --yes|-y)
                ASSUME_YES=1
                shift
                ;;
            --force-unlock)
                FORCE_UNLOCK=1
                shift
                ;;
            --help|-h)
                usage
                exit 0
                ;;
            -*)
                echo "Unknown option: $1" >&2
                usage
                exit 1
                ;;
            *)
                if [[ -z "$SUBCOMMAND" ]]; then
                    SUBCOMMAND="$1"
                else
                    EXTRA_ARGS+=("$1")
                fi
                shift
                ;;
        esac
    done

    [[ -n "$HOST" ]] || fatal "--host user@server is required"
    # Everything below is interpolated into a remote shell command, so the
    # charset stays narrow rather than relying on quoting.
    [[ "$HOST" =~ ^[A-Za-z0-9._-]+(@[A-Za-z0-9._-]+)?$ ]] \
        || fatal "--host may only contain letters, digits and . _ - @ (got: ${HOST})"
    [[ "$REMOTE_DIR" == /* ]] || fatal "--dir must be an absolute path (got: ${REMOTE_DIR})"
    [[ "$REMOTE_DIR" =~ ^[A-Za-z0-9._/-]+$ ]] \
        || fatal "--dir may only contain letters, digits and . _ - / (got: ${REMOTE_DIR})"
    while [[ "$REMOTE_DIR" == */ ]]; do
        REMOTE_DIR="${REMOTE_DIR%/}"
    done
    [[ -n "$REMOTE_DIR" ]] || fatal "--dir must not be the filesystem root"
    [[ "$TAIL_LINES" =~ ^[0-9]+$ ]] || fatal "--tail needs a number (got: ${TAIL_LINES})"

    # Resolved here, not at parse time, so `--mode` is order-independent. A
    # typo must be a refusal: falling back to the default would silently switch
    # a host to the other mode, and the stack would then refuse to boot with a
    # message about a variable the operator never meant to touch.
    case "$DEPLOY_MODE" in
        virtual-keys)
            COMPOSE_FILE_ARGS="-f ${COMPOSE_FILE}"
            ;;
        oauth)
            COMPOSE_FILE_ARGS="-f ${COMPOSE_FILE} -f ${OAUTH_OVERLAY_FILE}"
            ;;
        *)
            fatal "--mode must be virtual-keys or oauth (got: ${DEPLOY_MODE})"
            ;;
    esac

    case "$DEPLOY_ISSUANCE" in
        0|"")
            DEPLOY_ISSUANCE=0
            ;;
        1)
            [[ "$DEPLOY_MODE" == "oauth" ]] \
                || fatal "--issuance needs --mode oauth (got: ${DEPLOY_MODE}); the issuance
       overlay mints X-Corp-Auth tokens for the subscription mode only."
            COMPOSE_FILE_ARGS="${COMPOSE_FILE_ARGS} -f ${ISSUANCE_OVERLAY_FILE}"
            ;;
        *)
            fatal "DEPLOY_ISSUANCE must be 0 or 1 (got: ${DEPLOY_ISSUANCE})"
            ;;
    esac

    local arg
    for arg in ${EXTRA_ARGS+"${EXTRA_ARGS[@]}"}; do
        [[ "$arg" =~ ^[A-Za-z0-9._-]+$ ]] \
            || fatal "service names may only contain letters, digits and . _ - (got: ${arg})"
    done

    LOCK_DIR="${REMOTE_DIR}/.deploy.lock"

    SELF_FLAGS=""
    [[ "$REMOTE_DIR" == "$DEFAULT_REMOTE_DIR" ]] || SELF_FLAGS+=" --dir ${REMOTE_DIR}"
    [[ "$DEPLOY_MODE" == "oauth" ]] || SELF_FLAGS+=" --mode ${DEPLOY_MODE}"
    (( DEPLOY_ISSUANCE == 0 )) || SELF_FLAGS+=" --issuance"
}

require_cmd() {
    local cmd
    for cmd in "$@"; do
        command -v "$cmd" >/dev/null 2>&1 \
            || fatal "${cmd} not on PATH (brew install ${cmd} / apt install ${cmd})"
    done
}

# --------------------------------------------------------------------------- #
# remote plumbing
# --------------------------------------------------------------------------- #

# State-changing remote command: skipped under --dry-run.
ssh_run() {
    if (( DRY_RUN )); then
        info "[dry-run] ssh ${HOST} '$1'"
        return 0
    fi
    # SC2029: the command string is built here on purpose; every value it
    # interpolates is charset-validated in parse_args.
    # shellcheck disable=SC2029
    ssh "$HOST" "$1"
}

# Read-only remote command; runs even under --dry-run.
ssh_capture() {
    # shellcheck disable=SC2029
    ssh "$HOST" "$1"
}

compose_remote() {
    ssh_run "cd ${REMOTE_DIR} && docker compose ${COMPOSE_FILE_ARGS} $*"
}

ensure_remote_ready() {
    local probe
    probe="$(ssh_capture "if [ ! -d ${REMOTE_DIR} ]; then echo no-dir; \
elif [ ! -f ${REMOTE_DIR}/.env ]; then echo no-env; else echo ok; fi")" \
        || fatal "cannot reach ${HOST} over ssh — check the host, your key and the VPN"

    case "$probe" in
        ok)
            ;;
        no-dir)
            fatal "${REMOTE_DIR} does not exist on ${HOST}.
       Run 'sudo scripts/deploy/bootstrap-server.sh' on the server first
       (day 0), then re-run this deploy."
            ;;
        no-env)
            fatal "${REMOTE_DIR}/.env is missing on ${HOST}, and this script never
       uploads one — the server's copy is the only copy. Run
       'sudo scripts/deploy/bootstrap-server.sh' there to seed it, fill it
       in, then re-run this deploy."
            ;;
        *)
            fatal "unexpected probe answer from ${HOST}: ${probe}"
            ;;
    esac
}

# A missing bind-mount source can come up as an empty directory, which the
# config loader skips: issuance would boot silently off. Hence -f, checked
# before the lock and the sync so a refused run changes nothing.
ensure_issuance_config() {
    (( DEPLOY_ISSUANCE )) || return 0
    local target="${REMOTE_DIR}/${ISSUANCE_CONFIG}"
    if ! ssh_capture "[ -f ${target} ]"; then
        fatal "--issuance needs ${target} on ${HOST}, and this script never
       uploads it — the server's copy is the only copy. Copy
       compose/gateway/config.toml.example from your checkout to that path,
       fill in the team map, then re-run this deploy."
    fi
}

# mkdir is atomic on POSIX filesystems, so two operators cannot both win it.
acquire_lock() {
    if (( DRY_RUN )); then
        return 0
    fi
    if (( FORCE_UNLOCK )); then
        warn "clearing the deploy lock at ${LOCK_DIR} (--force-unlock)"
        ssh_capture "rm -f ${LOCK_DIR}/owner; rmdir ${LOCK_DIR} 2>/dev/null || true"
    fi
    if ! ssh_capture "mkdir ${LOCK_DIR} 2>/dev/null"; then
        local owner
        owner="$(ssh_capture "cat ${LOCK_DIR}/owner 2>/dev/null || true")"
        fatal "another deploy is already running against ${HOST}:${REMOTE_DIR}
       (lock ${LOCK_DIR}, held by ${owner:-unknown}). Wait for it to finish,
       or re-run with --force-unlock if you know the holder died."
    fi
    LOCK_HELD=1

    # The tag ends up inside a remote command string, so local `id`/`hostname`
    # output is reduced to a safe charset before it gets there.
    local owner_tag
    owner_tag="$(id -un 2>/dev/null || echo unknown)@$(hostname 2>/dev/null || echo unknown)"
    owner_tag="${owner_tag} $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    owner_tag="$(printf '%s' "$owner_tag" | tr -cd 'A-Za-z0-9._@: -')"
    ssh_capture "printf '%s\n' '${owner_tag}' > ${LOCK_DIR}/owner" || true
}

release_lock() {
    (( LOCK_HELD )) || return 0
    LOCK_HELD=0
    ssh_capture "rm -f ${LOCK_DIR}/owner && rmdir ${LOCK_DIR}" \
        || warn "could not release ${LOCK_DIR} — clear it with --force-unlock"
}

# --------------------------------------------------------------------------- #
# local staging + sync
# --------------------------------------------------------------------------- #

stage_schema() {
    local target="${COMPOSE_DIR}/postgres/initdb/${STAGED_SCHEMA_NAME}"
    [[ -f "$SCHEMA_SOURCE" ]] \
        || fatal "token-store schema.sql not found at ${SCHEMA_SOURCE}.
       Run this script from a checkout — the staged copy must come from
       source, never from whatever the server happens to hold."
    [[ -d "${COMPOSE_DIR}/postgres/initdb" ]] \
        || fatal "${COMPOSE_DIR}/postgres/initdb is missing — is COMPOSE_DIR a checkout?"
    install -m 0644 "$SCHEMA_SOURCE" "$target"
    info "staged ${STAGED_SCHEMA_NAME} from src/corp_llm_gateway/tokens/schema.sql"
}

# Belt and braces: sync_compose must never be reachable without the exclude
# that keeps a laptop's .env away from a shared server.
assert_env_excluded() {
    local arg
    for arg in "$@"; do
        if [[ "$arg" == "--exclude=.env" ]]; then
            return 0
        fi
    done
    fatal "internal error: refusing to rsync without an --exclude=.env rule"
}

rsync_args() {
    # Order matters: rsync applies the first rule that matches, so each include
    # sits before the exclude it carves out of. nginx/certs/ is excluded by
    # name, not by extension: NGINX_TLS_CERT / NGINX_TLS_KEY may name a file
    # `privkey` or `server.cer` (mirrors .gitignore).
    printf '%s\n' \
        --archive \
        --compress \
        --human-readable \
        --itemize-changes \
        --include=.env.example \
        --exclude=.env \
        --exclude=.env.* \
        --exclude=docker-compose.build.yml \
        --exclude="${ISSUANCE_CONFIG}" \
        --include="${NGINX_CERTS_README}" \
        --exclude="${NGINX_CERTS_DIR}/*" \
        --exclude=*.pem \
        --exclude=*.crt \
        --exclude=*.key \
        --exclude=*.p12 \
        --exclude=*.pfx \
        --exclude=.DS_Store \
        --exclude=__pycache__/ \
        --exclude=*.pyc \
        --exclude=*.swp
}

# No --delete anywhere: the server's .env, certs/ and site-local files must
# survive every sync, and a stale extra file is cheaper than a lost secret.
sync_compose() {
    local args=()
    while IFS= read -r arg; do
        args+=("$arg")
    done < <(rsync_args)
    if (( DRY_RUN )); then
        args+=(--dry-run)
    fi
    assert_env_excluded "${args[@]}"

    [[ -d "$COMPOSE_DIR" ]] || fatal "no compose/ directory at ${COMPOSE_DIR}"
    info "syncing compose/ to ${HOST}:${REMOTE_DIR}/ (.env, ${ISSUANCE_CONFIG}, ${NGINX_CERTS_DIR}/, certs and keys excluded)"
    rsync "${args[@]}" "${COMPOSE_DIR}/" "${HOST}:${REMOTE_DIR}/"
}

# --------------------------------------------------------------------------- #
# health + status
# --------------------------------------------------------------------------- #

# One line per service: name, state, health, exit code, separated by FIELD_SEP.
# Not tabs: `read` collapses a run of whitespace separators, so an empty health
# would shift the exit code into its place. Compose v2 prints either a JSON
# array or one object per line depending on the version; both are handled.
FIELD_SEP=$'\x1f'
service_states() {
    ssh_capture "cd ${REMOTE_DIR} && docker compose ${COMPOSE_FILE_ARGS} ps --all --format json" \
        | jq -s -r '[.[] | if type == "array" then .[] else . end]
                    | .[]
                    | [(.Service // .Name // "?"), (.State // ""), (.Health // ""),
                       (if .ExitCode == null then "" else (.ExitCode|tostring) end)]
                    | join("\u001f")'
}

is_front_door() {
    local name
    for name in "${NGINX_SERVICES[@]}"; do
        if [[ "$1" == "$name" ]]; then
            return 0
        fi
    done
    return 1
}

# The front door's entrypoint exits 64-69 on a bad NGINX_* key or a missing
# certificate, and `restart: unless-stopped` then cycles it through
# "restarting". Neither heals by waiting, so neither waits out the timeout.
front_door_is_down() {
    local state="$1" health="$2"
    [[ "$state" == "exited" || "$state" == "restarting" || "$health" == "unhealthy" ]]
}

wait_for_healthcheck() {
    local max_wait="$HEALTH_MAX_WAIT"
    local interval="$HEALTH_INTERVAL"
    local elapsed=0
    local stuck=""

    info "Waiting for all services to be healthy (max ${max_wait}s)..."

    while (( elapsed < max_wait )); do
        local all_healthy=true
        local states service state health exit_code down_service="" failed_service=""
        stuck=""
        states="$(service_states || true)"

        if [[ -z "$states" ]]; then
            all_healthy=false
            stuck="none reported"
        else
            while IFS="$FIELD_SEP" read -r service state health exit_code; do
                if is_front_door "$service"; then
                    if front_door_is_down "$state" "$health"; then
                        down_service="$service"
                        stuck="${service} (state=${state:-?} health=${health:-none})"
                        break
                    fi
                    # nginx always declares a healthcheck. Between two restarts
                    # of a crash loop it shows `running` with no health for an
                    # instant, so "running" alone never counts here.
                    if [[ "$health" == "healthy" ]]; then
                        continue
                    fi
                elif [[ "$state" == "exited" && -n "$exit_code" && "$exit_code" != "0" ]]; then
                    # A failed one-shot never recovers, and whatever waits for it
                    # to complete stays `created` until the timeout.
                    failed_service="$service"
                    stuck="${service} exited with code ${exit_code}"
                    break
                elif [[ "$state" == "exited" && -z "$health" && "$exit_code" == "0" ]]; then
                    # A `restart: "no"` one-shot that finished: `ps --all` lists it.
                    continue
                # A service that DECLARES a healthcheck must actually report
                # "healthy": "starting" is not healthy yet and can still flip to
                # "unhealthy", so accepting it ended the wait on the first poll.
                # Only a service with NO healthcheck (empty Health) falls back to
                # "is it running".
                elif [[ -n "$health" ]]; then
                    if [[ "$health" == "healthy" ]]; then
                        continue
                    fi
                elif [[ "$state" == "running" ]]; then
                    continue
                fi
                all_healthy=false
                [[ -n "$stuck" ]] || stuck="${service} (state=${state:-?} health=${health:-none})"
            done <<< "$states"
        fi

        if [[ -n "$down_service" ]]; then
            print_status
            fatal "the nginx front door is down: ${stuck}.
       Its entrypoint names the setting it refused in one log line. Read it with:
       scripts/deploy/deploy.sh --host ${HOST}${SELF_FLAGS} logs ${down_service}"
        fi

        if [[ -n "$failed_service" ]]; then
            print_status
            fatal "${stuck}; the services that wait for it will not start.
       Read its log with:
       scripts/deploy/deploy.sh --host ${HOST}${SELF_FLAGS} logs ${failed_service}"
        fi

        if [[ "$all_healthy" == "true" ]]; then
            info "All services healthy"
            return 0
        fi

        sleep "$interval"
        (( elapsed += interval ))
    done

    print_status
    fatal "services did not reach a healthy state within ${max_wait}s — stuck: ${stuck}.
       Inspect with: scripts/deploy/deploy.sh --host ${HOST}${SELF_FLAGS} logs"
}

print_status() {
    local states service state health
    states="$(service_states || true)"

    if [[ -z "$states" ]]; then
        warn "no services reported by ${HOST}:${REMOTE_DIR}"
        return 0
    fi

    {
        printf '\n%-24s %-12s %s\n' SERVICE STATE HEALTH
        while IFS="$FIELD_SEP" read -r service state health _; do
            printf '%-24s %-12s %s\n' "$service" "${state:-?}" "${health:--}"
        done <<< "$states"
        printf '\n'
    } >&2
}

confirm() {
    local prompt="$1"
    local reply=""

    if (( ASSUME_YES )); then
        return 0
    fi
    if [[ ! -t 0 ]]; then
        fatal "${prompt}
       stdin is not a terminal, so there is nobody to ask. Re-run with --yes
       if you really mean it."
    fi
    read -r -p "${prompt} Type 'yes' to continue: " reply
    [[ "$reply" == "yes" ]] || fatal "aborted"
}

# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #

# oauth mode (the default) against a server whose .env still has a
# LITELLM_MASTER_KEY line is deliberately NOT pre-checked here: this script never
# reads the server's .env, not even to test whether a key is present. The stack already refuses
# that combination at boot with a named cause
# (settings.MASTER_KEY_VS_FORWARD_AUTH_MESSAGE), and wait_for_healthcheck below
# surfaces it as a failed deploy. See docs/ops/deployment-modes.md.
# Runs after the sync, so it checks the compose files `up` is about to start.
# `config --services` prints service names only, never a value, so the
# server's .env stays unread here too.
ensure_one_front_door() {
    local services name enabled=()
    services="$(ssh_capture "cd ${REMOTE_DIR} && docker compose ${COMPOSE_FILE_ARGS} config --services")" \
        || fatal "docker compose could not resolve the stack in ${HOST}:${REMOTE_DIR} (its
       error is above); nothing was pulled or started."
    while IFS= read -r name; do
        if is_front_door "$name"; then
            enabled+=("$name")
        fi
    done <<< "$services"
    if (( ${#enabled[@]} > 1 )); then
        fatal "COMPOSE_PROFILES in ${HOST}:${REMOTE_DIR}/.env enables both ${enabled[*]}.
       Pick ONE: nginx (host routing) or nginx-ports (no DNS). Both publish
       NGINX_PORT, so the second would fail to start. Nothing was pulled or started."
    fi
}

cmd_up() {
    stage_schema
    ensure_remote_ready
    ensure_issuance_config
    acquire_lock
    sync_compose
    if (( DRY_RUN )); then
        info "[dry-run] would pull images and start the stack in ${REMOTE_DIR}"
        return 0
    fi
    ensure_one_front_door
    ssh_run "cd ${REMOTE_DIR} && docker compose ${COMPOSE_FILE_ARGS} pull \
&& docker compose ${COMPOSE_FILE_ARGS} up -d"
    wait_for_healthcheck
    print_status
    info "deployed to ${HOST}:${REMOTE_DIR}"
}

cmd_down() {
    ensure_remote_ready
    confirm "This stops the gateway on ${HOST} — developer traffic will fail."
    acquire_lock
    # Volumes are kept: the token store and audit spool live in them.
    compose_remote down
    info "stopped ${HOST}:${REMOTE_DIR} (volumes kept)"
}

cmd_restart() {
    ensure_remote_ready
    confirm "This restarts the gateway on ${HOST} — in-flight requests will fail."
    acquire_lock
    compose_remote restart ${EXTRA_ARGS+"${EXTRA_ARGS[@]}"}
    if (( DRY_RUN )); then
        return 0
    fi
    wait_for_healthcheck
    print_status
}

cmd_logs() {
    ensure_remote_ready
    warn "remote logs stream straight to this terminal — treat them as sensitive"
    ssh_capture "cd ${REMOTE_DIR} && docker compose ${COMPOSE_FILE_ARGS} logs \
--follow --tail ${TAIL_LINES} ${EXTRA_ARGS+${EXTRA_ARGS[*]}}"
}

cmd_status() {
    ensure_remote_ready
    print_status
}

main() {
    parse_args "$@"
    [[ -n "$SUBCOMMAND" ]] || { usage; exit 1; }

    require_cmd ssh
    trap release_lock EXIT

    case "$SUBCOMMAND" in
        up)
            require_cmd rsync jq
            cmd_up
            ;;
        down)
            cmd_down
            ;;
        restart)
            require_cmd jq
            cmd_restart
            ;;
        logs)
            cmd_logs
            ;;
        status)
            require_cmd jq
            cmd_status
            ;;
        *)
            echo "Unknown subcommand: ${SUBCOMMAND}" >&2
            usage
            exit 1
            ;;
    esac
}

# Sourcing the script defines the functions without touching any host; only a
# direct run deploys anything.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi

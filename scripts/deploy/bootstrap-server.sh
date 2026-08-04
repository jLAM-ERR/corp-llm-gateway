#!/usr/bin/env bash
#
# corp-llm-gateway server bootstrap — day 0, runs ON the target host as root.
#
# Usage:
#   sudo scripts/deploy/bootstrap-server.sh [--dir PATH] [--systemd]
#                                           [--no-install-docker] [--force]
#
# What it does:
#   1. Verifies Docker Engine + the compose v2 plugin, installing them from
#      Docker's official repository on Debian/Ubuntu and RHEL-family hosts.
#      Any other distribution is refused, not guessed at.
#   2. Creates the deploy directory (default /opt/corp-llm-gateway).
#   3. Seeds .env from .env.example at mode 0600 and then EXITS 1, so the
#      operator edits it before anything starts. Re-run afterwards.
#   4. Optionally installs a systemd unit for boot-time start (--systemd),
#      once .env and the compose files are in place.
#
# Idempotent: an existing .env, deploy directory, apt/dnf repo file or systemd
# unit is kept, never silently replaced. Day-N deploys are a separate script
# (scripts/deploy/deploy.sh), run from the operator's laptop.

set -euo pipefail

TARGET_DIR="${CORP_GATEWAY_DEPLOY_DIR:-/opt/corp-llm-gateway}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_UNIT_NAME="corp-llm-gateway.service"
SYSTEMD_UNIT_SOURCE="${SCRIPT_DIR}/${SYSTEMD_UNIT_NAME}"
SYSTEMD_UNIT_PATH="/etc/systemd/system/${SYSTEMD_UNIT_NAME}"
DOCKER_PACKAGES=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)

INSTALL_SYSTEMD=0
INSTALL_DOCKER=1
FORCE=0
ENV_FILE=""
ENV_EXAMPLE=""

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
Usage: sudo scripts/deploy/bootstrap-server.sh [options]

Prepares a server to run the compose stack in compose/. Run once, on the
server itself, before the first deploy.

Options:
  --dir PATH            Deploy directory (default /opt/corp-llm-gateway)
  --systemd             Install + enable a systemd unit for boot-time start
  --no-install-docker   Verify Docker only; never install packages
  --force               Replace an existing systemd unit (never the .env)
  --help, -h            Print this help and exit

Exit codes:
  0  host is ready
  1  refused, or a fresh .env was seeded and needs editing
EOF
}

parse_args() {
    while (( $# > 0 )); do
        case "$1" in
            --dir)
                (( $# >= 2 )) || fatal "--dir needs a path argument"
                TARGET_DIR="$2"
                shift 2
                ;;
            --dir=*)
                TARGET_DIR="${1#*=}"
                shift
                ;;
            --systemd)
                INSTALL_SYSTEMD=1
                shift
                ;;
            --no-install-docker)
                INSTALL_DOCKER=0
                shift
                ;;
            --force)
                FORCE=1
                shift
                ;;
            --help|-h)
                usage
                exit 0
                ;;
            *)
                echo "Unknown option: $1" >&2
                usage
                exit 1
                ;;
        esac
    done

    [[ "$TARGET_DIR" == /* ]] || fatal "--dir must be an absolute path (got: ${TARGET_DIR})"
    # Kept conservative because the path is substituted into the systemd unit.
    [[ "$TARGET_DIR" =~ ^[A-Za-z0-9._/-]+$ ]] \
        || fatal "--dir may only contain letters, digits and . _ - / (got: ${TARGET_DIR})"

    ENV_FILE="${TARGET_DIR}/.env"
    ENV_EXAMPLE="${TARGET_DIR}/.env.example"
}

require_root() {
    (( EUID == 0 )) || fatal "must run as root (try: sudo $0 $*)"
}

os_release_field() {
    local key="$1"
    [[ -r /etc/os-release ]] || return 0
    sed -n "s/^${key}=//p" /etc/os-release | tr -d '"' | head -n 1
}

install_docker_apt() {
    local distro="$1"
    local arch codename keyring="/etc/apt/keyrings/docker.asc"
    local repo_list="/etc/apt/sources.list.d/docker.list"

    command -v curl >/dev/null 2>&1 || fatal "curl not on PATH (apt-get install curl)"
    arch="$(dpkg --print-architecture)"
    codename="$(os_release_field VERSION_CODENAME)"
    [[ -n "$codename" ]] || fatal "no VERSION_CODENAME in /etc/os-release — install Docker by hand"

    info "installing Docker Engine for ${distro} ${codename} (${arch})"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg

    if [[ -f "$keyring" ]]; then
        info "${keyring} already present — kept"
    else
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL "https://download.docker.com/linux/${distro}/gpg" -o "$keyring" \
            || fatal "cannot reach download.docker.com. On an air-gapped host, install
       docker-ce and docker-compose-plugin from your internal mirror, then
       re-run this script with --no-install-docker."
        chmod a+r "$keyring"
    fi

    if [[ -f "$repo_list" ]]; then
        info "${repo_list} already present — kept"
    else
        printf 'deb [arch=%s signed-by=%s] https://download.docker.com/linux/%s %s stable\n' \
            "$arch" "$keyring" "$distro" "$codename" > "$repo_list"
    fi

    apt-get update -qq
    apt-get install -y -qq "${DOCKER_PACKAGES[@]}"
    systemctl enable --now docker
}

install_docker_dnf() {
    local repo="$1"
    local pkg_mgr repo_url="https://download.docker.com/linux/${1}/docker-ce.repo"
    local repo_file="/etc/yum.repos.d/docker-ce.repo"

    pkg_mgr="$(command -v dnf || command -v yum || true)"
    [[ -n "$pkg_mgr" ]] || fatal "neither dnf nor yum on PATH — install Docker by hand"

    info "installing Docker Engine from the ${repo} repository"
    "$pkg_mgr" -y install dnf-plugins-core || "$pkg_mgr" -y install yum-utils

    if [[ -f "$repo_file" ]]; then
        info "${repo_file} already present — kept"
    else
        "$pkg_mgr" config-manager --add-repo "$repo_url" \
            || "$pkg_mgr" config-manager addrepo --from-repofile="$repo_url" \
            || fatal "cannot add the Docker repository. On an air-gapped host, install
       docker-ce and docker-compose-plugin from your internal mirror, then
       re-run this script with --no-install-docker."
    fi

    "$pkg_mgr" -y install "${DOCKER_PACKAGES[@]}"
    systemctl enable --now docker
}

install_docker() {
    local distro_id
    distro_id="$(os_release_field ID)"

    # ID only, never ID_LIKE: a derivative has no Docker repository of its own,
    # and guessing one on someone's server is worse than a clean refusal.
    case "$distro_id" in
        debian|ubuntu)
            install_docker_apt "$distro_id"
            ;;
        rhel)
            install_docker_dnf rhel
            ;;
        fedora)
            install_docker_dnf fedora
            ;;
        centos|rocky|almalinux)
            warn "${distro_id} has no Docker repository of its own — using the centos one"
            install_docker_dnf centos
            ;;
        *)
            fatal "unsupported distribution: ID=${distro_id:-unknown}.
       Supported: debian, ubuntu, rhel, centos, rocky, almalinux, fedora.
       Install Docker Engine and the compose v2 plugin by hand
       (https://docs.docker.com/engine/install/), then re-run this script
       with --no-install-docker."
            ;;
    esac
}

ensure_docker() {
    if command -v docker >/dev/null 2>&1; then
        info "docker present: $(docker --version)"
        return 0
    fi
    [[ "$INSTALL_DOCKER" == "1" ]] \
        || fatal "docker not on PATH and --no-install-docker was given"
    install_docker
}

ensure_compose_v2() {
    if docker compose version >/dev/null 2>&1; then
        info "compose v2 present: $(docker compose version --short 2>/dev/null || echo unknown)"
        return 0
    fi
    [[ "$INSTALL_DOCKER" == "1" ]] \
        || fatal "the compose v2 plugin is missing and --no-install-docker was given.
       Install docker-compose-plugin from your package source and re-run."
    warn "compose v2 plugin missing — installing it"
    install_docker
    docker compose version >/dev/null 2>&1 \
        || fatal "compose v2 still unavailable after install. This stack needs
       'docker compose' (v2 plugin); the standalone v1 binary is not supported."
}

wait_for_docker_daemon() {
    local max_wait=60
    local elapsed=0
    local interval=2

    if docker info >/dev/null 2>&1; then
        return 0
    fi

    info "Waiting for the Docker daemon (max ${max_wait}s)..."
    while (( elapsed < max_wait )); do
        if docker info >/dev/null 2>&1; then
            info "Docker daemon ready"
            return 0
        fi
        sleep "$interval"
        (( elapsed += interval ))
    done

    fatal "Docker daemon not ready within ${max_wait}s — check 'systemctl status docker'"
}

ensure_target_dir() {
    if [[ -d "$TARGET_DIR" ]]; then
        info "deploy directory ${TARGET_DIR} exists — left as is"
        return 0
    fi
    if [[ -e "$TARGET_DIR" ]]; then
        fatal "${TARGET_DIR} exists and is not a directory — refusing to touch it"
    fi
    install -m 0750 -d "$TARGET_DIR"
    info "created ${TARGET_DIR} (mode 0750)"
}

install_systemd_unit() {
    [[ "$INSTALL_SYSTEMD" == "1" ]] || return 0

    command -v systemctl >/dev/null 2>&1 || fatal "systemctl not found — no systemd on this host"
    [[ -f "$SYSTEMD_UNIT_SOURCE" ]] || fatal "unit template missing at ${SYSTEMD_UNIT_SOURCE}"
    [[ -f "${TARGET_DIR}/docker-compose.yml" ]] \
        || fatal "no docker-compose.yml in ${TARGET_DIR}. Sync the compose/ tree there
       first, then re-run with --systemd — a unit enabled against an empty
       directory fails at every boot."

    if [[ -f "$SYSTEMD_UNIT_PATH" ]] && [[ "$FORCE" != "1" ]]; then
        warn "${SYSTEMD_UNIT_PATH} exists — kept. Re-run with --force to replace it."
        return 0
    fi

    # WorkingDirectory has to track --dir; compose reads ./.env from there.
    # Staged then installed so a failed sed cannot leave a truncated unit.
    local staged
    staged="$(mktemp)"
    sed "s|^WorkingDirectory=.*|WorkingDirectory=${TARGET_DIR}|" "$SYSTEMD_UNIT_SOURCE" > "$staged"
    install -m 0644 "$staged" "$SYSTEMD_UNIT_PATH"
    rm -f "$staged"
    systemctl daemon-reload
    systemctl enable "$SYSTEMD_UNIT_NAME"
    info "installed ${SYSTEMD_UNIT_PATH}, enabled at boot"
}

# Seeds .env and stops: the shipped example holds placeholders, not values.
seed_env_file() {
    if [[ -f "$ENV_FILE" ]]; then
        info "${TARGET_DIR}/.env exists — kept (this script never overwrites it)"
        return 0
    fi

    local example="$ENV_EXAMPLE"
    if [[ ! -f "$example" ]]; then
        example="${SCRIPT_DIR}/../../compose/.env.example"
    fi
    if [[ ! -f "$example" ]]; then
        fatal "no .env.example found in ${TARGET_DIR}.
       Sync the compose/ tree to ${TARGET_DIR} first (scripts/deploy/deploy.sh
       from a checkout, or rsync compose/ by hand), then re-run this script."
    fi

    install -m 0600 "$example" "$ENV_FILE"
    warn "Seeded ${TARGET_DIR}/.env (mode 0600) from ${example}."
    warn "Edit it and fill in every key that has no default — see compose/README.md."
    warn "Then re-run this script to finish."
    exit 1
}

print_next_steps() {
    cat >&2 <<EOF

Host is ready. Deploy directory: ${TARGET_DIR}

Next, from a checkout on your laptop:
  scripts/deploy/deploy.sh --host <user>@<server> up

By hand on this host, in ${TARGET_DIR}:
  1. stage the token-store schema into postgres/initdb/01-schema.sql
     (see compose/postgres/initdb/README.md — skipping it makes every
     request fail at runtime while the stack still reports healthy)
  2. docker compose up -d
  3. docker compose ps
EOF
}

main() {
    parse_args "$@"
    require_root "$@"
    ensure_docker
    ensure_compose_v2
    wait_for_docker_daemon
    ensure_target_dir
    # Seed first: the unit is installed only once .env exists, so boot-time
    # start can never come up against a half-configured stack.
    seed_env_file
    install_systemd_unit
    print_next_steps
}

# Sourcing the script defines the functions without touching the host; only a
# direct run bootstraps anything.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi

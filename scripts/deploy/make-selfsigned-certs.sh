#!/usr/bin/env bash
#
# Throwaway TLS material for the nginx front door's `terminate` mode.
# NOT FOR PRODUCTION: use a corp-CA-signed or public certificate there
# (compose/nginx/certs/README.md).
#
# Usage:
#   scripts/deploy/make-selfsigned-certs.sh [--domain DOMAIN] [--out DIR]
#                                           [--days N] [--force] [SAN ...]
#
# Creates a throwaway CA and one leaf certificate signed by it:
#   <out>/gateway.crt        the leaf nginx presents   (NGINX_TLS_CERT=gateway.crt)
#   <out>/gateway.key        its private key, mode 0600 (NGINX_TLS_KEY=gateway.key)
#   <out>/selfsigned-ca.crt  the CA clients verify against (curl --cacert ...)
# The CA's private key is discarded: this CA can never sign anything else.
#
# SANs: gateway.<DOMAIN> and langfuse.<DOMAIN> with --domain, plus each SAN
# argument. An IPv4/IPv6 address becomes an IP SAN, anything else a DNS SAN.
# A client verifies the address it dials, so under the nginx-ports profile
# name the IP or local name clients use.
#
# stdout carries only the CA certificate's path; everything else is on stderr.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${SCRIPT_DIR}/../../compose/nginx/certs"
DOMAIN=""
DAYS=365
FORCE=0
SANS=()
WORK_DIR=""

LEAF_CERT="gateway.crt"
LEAF_KEY="gateway.key"
CA_CERT="selfsigned-ca.crt"

# The entrypoint's GATEWAY_DOMAIN rule: lowercase labels, at least two.
DOMAIN_ERE='^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'
# A DNS SAN may be a single-label local name.
DNS_ERE='^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$'
IPV4_ERE='^(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])){3}$'
IPV6_ERE='^[0-9a-f:.]*:[0-9a-f:.]*$'

fatal() {
    echo "FATAL: $(printf '%s' "$*" | tr -c ' -~' '?')" >&2
    exit 1
}

info() {
    echo "INFO: $*" >&2
}

usage() {
    cat >&2 <<'EOF'
Usage: scripts/deploy/make-selfsigned-certs.sh [options] [SAN ...]

Creates a throwaway CA and a leaf certificate signed by it for the nginx
front door's `terminate` mode. NOT FOR PRODUCTION.

Options:
  --domain DOMAIN  Add gateway.DOMAIN and langfuse.DOMAIN as DNS SANs
  --out DIR        Output directory (default: compose/nginx/certs/)
  --days N         Validity in days, 1-825 (default 365)
  --force          Replace existing output files
  --help, -h       Print this help and exit

Each SAN argument is an IPv4/IPv6 address (an IP SAN) or a DNS name.
At least one SAN is required, from --domain or an argument.

Output: gateway.crt, gateway.key (0600) and selfsigned-ca.crt. The path of
the CA certificate is printed on stdout.
EOF
}

lowercase() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

parse_args() {
    while (( $# > 0 )); do
        case "$1" in
            --domain)
                (( $# >= 2 )) || fatal "--domain needs a value"
                DOMAIN="$2"
                shift 2
                ;;
            --domain=*)
                DOMAIN="${1#*=}"
                shift
                ;;
            --out)
                (( $# >= 2 )) || fatal "--out needs a directory"
                OUT_DIR="$2"
                shift 2
                ;;
            --out=*)
                OUT_DIR="${1#*=}"
                shift
                ;;
            --days)
                (( $# >= 2 )) || fatal "--days needs a number"
                DAYS="$2"
                shift 2
                ;;
            --days=*)
                DAYS="${1#*=}"
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
            --)
                shift
                SANS+=("$@")
                break
                ;;
            -*)
                echo "Unknown option: $1" >&2
                usage
                exit 1
                ;;
            *)
                SANS+=("$1")
                shift
                ;;
        esac
    done
}

# Every SAN is written into an openssl config file: nothing but address or
# hostname characters may pass.
san_entry() {
    local value
    value="$(lowercase "$1")"
    if [[ "$value" =~ $IPV4_ERE ]]; then
        printf 'IP:%s' "$value"
    elif [[ "$value" =~ ^[0-9.]+$ ]]; then
        fatal "SAN '$1' is not a valid IPv4 address"
    elif [[ "$value" == *:* ]]; then
        # IPV6_ERE alone admits ':', '::::' or 'f:': a hex digit is required, and
        # a colon run is '::' at most and never a lone ':' at either end.
        if [[ "$value" != *[0-9a-f]* || "$value" == *:::* || "$value" == :[!:]* \
            || "$value" == *[!:]: || ! "$value" =~ $IPV6_ERE ]]; then
            fatal "SAN '$1' is not an IPv6 address"
        fi
        printf 'IP:%s' "$value"
    elif [[ "$value" =~ $DNS_ERE ]]; then
        (( ${#value} <= 253 )) || fatal "SAN '$1' is longer than 253 characters"
        printf 'DNS:%s' "$value"
    else
        fatal "SAN '$1' is neither an IP address nor a DNS name (a-z 0-9 . - only)"
    fi
}

build_san_list() {
    local entries=() san
    if [[ -n "$DOMAIN" ]]; then
        DOMAIN="$(lowercase "$DOMAIN")"
        [[ "$DOMAIN" =~ $DOMAIN_ERE ]] \
            || fatal "--domain must be a DNS name with at least two labels (example.corp), got '$DOMAIN'"
        entries+=("DNS:gateway.${DOMAIN}" "DNS:langfuse.${DOMAIN}")
    fi
    if (( ${#SANS[@]} > 0 )); then
        for san in "${SANS[@]}"; do
            entries+=("$(san_entry "$san")")
        done
    fi
    (( ${#entries[@]} > 0 )) || fatal "no SAN: pass --domain DOMAIN and/or at least one SAN argument"
    local IFS=,
    SAN_LIST="${entries[*]}"
}

check_inputs() {
    if ! [[ "$DAYS" =~ ^[1-9][0-9]{0,2}$ ]] || (( DAYS > 825 )); then
        fatal "--days must be a whole number from 1 to 825, got '$DAYS'"
    fi
    command -v openssl >/dev/null 2>&1 || fatal "openssl not on PATH"
    mkdir -p -- "$OUT_DIR"
    OUT_DIR="$(cd -- "$OUT_DIR" && pwd)"
    if (( FORCE == 0 )); then
        local name
        for name in "$LEAF_CERT" "$LEAF_KEY" "$CA_CERT"; do
            [[ ! -e "${OUT_DIR}/${name}" ]] \
                || fatal "${OUT_DIR}/${name} exists; pass --force to replace it"
        done
    fi
}

write_openssl_config() {
    cat >"$1" <<EOF
[req]
distinguished_name = dn
prompt = no

[dn]
CN = corp-llm-gateway

[v3_ca]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash

[v3_leaf]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
subjectAltName = ${SAN_LIST}
EOF
}

# openssl chatters on stderr even when it succeeds: show it only on failure.
run_openssl() {
    local output
    output="$(openssl "$@" 2>&1)" || fatal "openssl $1 failed: ${output}"
}

generate() {
    local work="$1" config="$1/openssl.cnf"
    write_openssl_config "$config"
    run_openssl ecparam -name prime256v1 -genkey -noout -out "${work}/ca.key"
    run_openssl req -new -x509 -sha256 -days "$DAYS" -config "$config" -extensions v3_ca \
        -subj "/CN=corp-llm-gateway throwaway CA (not for production)" \
        -key "${work}/ca.key" -out "${work}/ca.crt"
    run_openssl ecparam -name prime256v1 -genkey -noout -out "${work}/leaf.key"
    run_openssl req -new -sha256 -config "$config" -subj "/CN=corp-llm-gateway front door" \
        -key "${work}/leaf.key" -out "${work}/leaf.csr"
    run_openssl x509 -req -sha256 -days "$DAYS" -in "${work}/leaf.csr" \
        -CA "${work}/ca.crt" -CAkey "${work}/ca.key" \
        -CAcreateserial -CAserial "${work}/ca.srl" \
        -extfile "$config" -extensions v3_leaf -out "${work}/leaf.crt"
}

# Staged beside the targets, then renamed one at a time: not atomic as a set.
# The key goes last, so a failure part-way can leave a new certificate beside
# the old key (nginx refuses that pair), never a new key beside an old
# certificate.
install_outputs() {
    local work="$1"
    cp "${work}/leaf.key" "${OUT_DIR}/.${LEAF_KEY}.new"
    cp "${work}/leaf.crt" "${OUT_DIR}/.${LEAF_CERT}.new"
    cp "${work}/ca.crt" "${OUT_DIR}/.${CA_CERT}.new"
    chmod 600 "${OUT_DIR}/.${LEAF_KEY}.new"
    chmod 644 "${OUT_DIR}/.${LEAF_CERT}.new" "${OUT_DIR}/.${CA_CERT}.new"
    mv -f "${OUT_DIR}/.${CA_CERT}.new" "${OUT_DIR}/${CA_CERT}"
    mv -f "${OUT_DIR}/.${LEAF_CERT}.new" "${OUT_DIR}/${LEAF_CERT}"
    mv -f "${OUT_DIR}/.${LEAF_KEY}.new" "${OUT_DIR}/${LEAF_KEY}"
}

main() {
    parse_args "$@"
    build_san_list
    check_inputs

    umask 077
    WORK_DIR="$(mktemp -d)"
    # The CA key lives only here.
    trap 'rm -rf -- "$WORK_DIR"' EXIT

    generate "$WORK_DIR"
    install_outputs "$WORK_DIR"

    info "SANs: ${SAN_LIST}"
    info "leaf: ${OUT_DIR}/${LEAF_CERT}  key: ${OUT_DIR}/${LEAF_KEY}  (NGINX_TLS_CERT=${LEAF_CERT} NGINX_TLS_KEY=${LEAF_KEY})"
    info "clients verify with: curl --cacert ${OUT_DIR}/${CA_CERT} https://<a SAN above>/"
    cat >&2 <<'EOF'
########################################################################
##  NOT FOR PRODUCTION: a throwaway self-signed CA, trusted by nobody. ##
##  Use a corp-CA-signed or public certificate on a real deployment.   ##
########################################################################
EOF
    printf '%s\n' "${OUT_DIR}/${CA_CERT}"
}

main "$@"

#!/bin/sh
# Front-door entrypoint: validate -> render -> nginx -t -> exec nginx.
# Replaces the image's /docker-entrypoint.sh. $1 is the routing: host | port.
# Every value rendered into nginx syntax is checked against a character class
# first; a value that is not rendered for this mode is blanked, never rendered.
set -eu
set -f
LC_ALL=C
export LC_ALL

TEMPLATES=/corp/templates
RENDERED=/etc/nginx/rendered
CERTS=/etc/nginx/certs

fail() {
    code=$1
    shift
    # One line per failure, whatever bytes the offending value carried.
    message=$(printf '%s' "$*" | tr -c ' -~' '?')
    printf 'corp-nginx: %s\n' "$message" >&2
    exit "$code"
}

lowercase() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

# ---- 1. routing ---------------------------------------------------------------
routing=${1:-}
case $routing in
    host | port) ;;
    *) fail 64 "the routing argument (compose command:) must be 'host' or 'port', got '$routing'" ;;
esac

# ---- 2. TLS mode --------------------------------------------------------------
NGINX_TLS_MODE=${NGINX_TLS_MODE:-}
case $NGINX_TLS_MODE in
    terminate | behind-proxy) ;;
    *) fail 64 "NGINX_TLS_MODE must be exactly 'terminate' or 'behind-proxy', got '$NGINX_TLS_MODE'" ;;
esac

# ---- 3. certificate file names (terminate) --------------------------------------
check_cert_file() {
    key=$1
    name=$2
    case $name in
        '' | *[!A-Za-z0-9._-]*)
            fail 65 "$key must be a bare file name in compose/nginx/certs/ (A-Z a-z 0-9 . _ - only), got '$name'"
            ;;
    esac
    if [ ! -f "$CERTS/$name" ] || [ ! -s "$CERTS/$name" ]; then
        fail 65 "$key names $CERTS/$name, which is missing, not a file, or empty"
    fi
}

NGINX_TLS_CERT=${NGINX_TLS_CERT:-}
NGINX_TLS_KEY=${NGINX_TLS_KEY:-}
if [ "$NGINX_TLS_MODE" = terminate ]; then
    check_cert_file NGINX_TLS_CERT "$NGINX_TLS_CERT"
    check_cert_file NGINX_TLS_KEY "$NGINX_TLS_KEY"
else
    NGINX_TLS_CERT=
    NGINX_TLS_KEY=
fi

# ---- 4. trusted proxies -------------------------------------------------------
strip_leading_zeros() {
    digits=$1
    while :; do
        case $digits in
            0?*) digits=${digits#0} ;;
            *) break ;;
        esac
    done
    printf '%s' "$digits"
}

is_ipv4() {
    case $1 in
        '' | *[!0-9.]* | .* | *. | *..*) return 1 ;;
    esac
    saved_ifs=$IFS
    IFS=.
    # shellcheck disable=SC2086 # split on '.'; globbing is off
    set -- $1
    IFS=$saved_ifs
    [ $# -eq 4 ] || return 1
    for octet in "$@"; do
        [ ${#octet} -le 3 ] || return 1
        [ "$(strip_leading_zeros "$octet")" -le 255 ] || return 1
    done
}

is_trusted_entry() {
    entry=$1
    address=${entry%%/*}
    prefix=
    case $entry in
        */*)
            prefix=${entry#*/}
            case $prefix in
                '' | *[!0-9]*) return 1 ;;
            esac
            ;;
    esac
    case $address in
        *:*)
            case $address in
                *[!0-9A-Fa-f:.]*) return 1 ;;
            esac
            floor=16
            ceiling=128
            ;;
        *)
            is_ipv4 "$address" || return 1
            floor=8
            ceiling=32
            ;;
    esac
    [ -n "$prefix" ] || return 0
    prefix=$(strip_leading_zeros "$prefix")
    [ ${#prefix} -le 3 ] || return 1
    [ "$prefix" -ge "$floor" ] && [ "$prefix" -le "$ceiling" ]
}

is_ipv4_entry() { is_ipv4 "${1%%/*}"; }

NGINX_TRUSTED_PROXIES=${NGINX_TRUSTED_PROXIES:-}
trusted_count=0
ipv4_count=0
# shellcheck disable=SC2086 # a space-separated list; globbing is off
for entry in $NGINX_TRUSTED_PROXIES; do
    if ! is_trusted_entry "$entry"; then
        fail 66 "NGINX_TRUSTED_PROXIES entry '$entry' is not an IPv4/IPv6 address or CIDR with a prefix of at least /8 (IPv4) or /16 (IPv6)"
    fi
    trusted_count=$((trusted_count + 1))
    if is_ipv4_entry "$entry"; then
        ipv4_count=$((ipv4_count + 1))
    fi
done
if [ "$NGINX_TLS_MODE" = behind-proxy ] && [ "$trusted_count" -eq 0 ]; then
    fail 66 "NGINX_TRUSTED_PROXIES is required in behind-proxy: the space-separated addresses/CIDRs of the TLS terminator"
fi
# The listeners are IPv4 (listen 8080) and neither geo nor set_real_ip_from
# matches across address families: an IPv6-only list would refuse every peer.
# This guards the family mismatch only, not reachability: 0.0.0.0/8,
# 127.0.0.0/8 or 224.0.0.0/4 pass here and still 444 every real peer.
if [ "$NGINX_TLS_MODE" = behind-proxy ] && [ "$ipv4_count" -eq 0 ]; then
    fail 66 "NGINX_TRUSTED_PROXIES has no IPv4 entry: the behind-proxy listeners are IPv4, so at least one dotted-quad IPv4 address/CIDR is required (::ffff:a.b.c.d never matches an IPv4 peer)"
fi

# ---- 5. bind address (behind-proxy) -------------------------------------------
is_unspecified_address() {
    address=$1
    case $address in
        "["*"]")
            address=${address#"["}
            address=${address%"]"}
            ;;
    esac
    address=$(lowercase "$address")
    case $address in
        *[!0:.]*) ;;
        *) return 0 ;;
    esac
    case $address in
        *ffff:*)
            head=${address%%ffff:*}
            tail=${address#*ffff:}
            case $head in
                *[!0:]*) return 1 ;;
            esac
            case $tail in
                *[!0:.]*) return 1 ;;
            esac
            return 0
            ;;
    esac
    return 1
}

NGINX_BIND_ADDR=${NGINX_BIND_ADDR:-}
if [ "$NGINX_TLS_MODE" = behind-proxy ] && is_unspecified_address "$NGINX_BIND_ADDR"; then
    fail 68 "NGINX_BIND_ADDR='$NGINX_BIND_ADDR' is the unspecified address: behind-proxy would serve plain HTTP on every interface; set it to the NIC the TLS terminator reaches"
fi

# ---- 6. gateway domain (host routing) -----------------------------------------
# Lowercase labels of a-z 0-9 -, no hyphen at either end, at least two labels.
HOSTNAME_ERE='^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'

GATEWAY_DOMAIN=${GATEWAY_DOMAIN:-}
if [ "$routing" = host ]; then
    case $GATEWAY_DOMAIN in
        '' | *[!a-z0-9.-]*)
            fail 64 "GATEWAY_DOMAIN must be a lowercase DNS name (a-z 0-9 . -) under host routing, got '$GATEWAY_DOMAIN'"
            ;;
    esac
    if ! printf '%s\n' "$GATEWAY_DOMAIN" | grep -Eq "$HOSTNAME_ERE"; then
        fail 64 "GATEWAY_DOMAIN must be a DNS name with at least two labels (example.corp), got '$GATEWAY_DOMAIN'"
    fi
else
    GATEWAY_DOMAIN=
fi

# ---- 7. Langfuse public origin ------------------------------------------------
# Scheme and host only, for messages: userinfo, path, query and fragment can
# hold secrets. The authority ends at the first / ? or #, so an @ after it is
# never read; an authority with an @ or a host that is not a hostname is not shown.
url_origin() {
    case $1 in
        *://*) ;;
        *)
            printf '<not shown>'
            return
            ;;
    esac
    scheme=
    case $1 in
        https://*) scheme=https:// ;;
        http://*) scheme=http:// ;;
    esac
    authority=${1#*://}
    authority=${authority%%[/?#]*}
    case $authority in
        *@*)
            printf '%s<userinfo not shown>' "$scheme"
            return
            ;;
    esac
    host=${authority%%:*}
    case $host in
        '' | *[!A-Za-z0-9.-]*)
            printf '%s<not shown>' "$scheme"
            return
            ;;
    esac
    printf '%s%s' "$scheme" "$host"
}

LANGFUSE_PUBLIC_URL=${LANGFUSE_PUBLIC_URL:-}
langfuse_origin=$(url_origin "$LANGFUSE_PUBLIC_URL")
case $LANGFUSE_PUBLIC_URL in
    https://?*) ;;
    *) fail 69 "LANGFUSE_PUBLIC_URL must be Langfuse's public https:// origin whenever nginx is on, got '$langfuse_origin'" ;;
esac
authority=${LANGFUSE_PUBLIC_URL#https://}
authority=${authority%%[/?#]*}
# Under either routing: a credential in this value has no legitimate use.
case $authority in
    *@*) fail 69 "LANGFUSE_PUBLIC_URL must not carry credentials (userinfo), got '$langfuse_origin'" ;;
esac
# It is NEXTAUTH_URL: anything but an origin is one NextAuth cannot match.
origin_rule="LANGFUSE_PUBLIC_URL must be https://<host>[:port] with no path"
case $authority in
    "["*) fail 69 "$origin_rule; a bracketed IPv6 literal is refused (use a DNS name or an IPv4 address), got '$langfuse_origin'" ;;
esac
langfuse_host=${authority%%:*}
langfuse_port=
langfuse_rest=${LANGFUSE_PUBLIC_URL#"https://$authority"}
valid=1
case $authority in
    *:*)
        langfuse_port=${authority#*:}
        [ -n "$langfuse_port" ] || valid=0
        ;;
esac
# Checked before lowercase(), whose $(...) would drop a trailing newline.
case $langfuse_host in
    '' | *[!A-Za-z0-9.-]*) valid=0 ;;
esac
langfuse_host=$(lowercase "$langfuse_host")
# The hostname rule of step 6; a dotted-quad IPv4 address matches it too.
printf '%s\n' "$langfuse_host" | grep -Eq "$HOSTNAME_ERE" || valid=0
# Only digits and dots is an IPv4 address, never a hostname: 999.1.2.3 is refused.
case $langfuse_host in
    *[!0-9.]*) ;;
    *) is_ipv4 "$langfuse_host" || valid=0 ;;
esac
case $langfuse_port in
    *[!0-9]*) valid=0 ;;
    *)
        # The length check first: it keeps the comparison inside the shell's integers.
        if [ ${#langfuse_port} -gt 5 ] || [ "${langfuse_port:-0}" -gt 65535 ]; then
            valid=0
        fi
        ;;
esac
case $langfuse_rest in
    '' | /) ;;
    *) valid=0 ;;
esac
if [ "$valid" -eq 0 ]; then
    fail 69 "$origin_rule, got '$langfuse_origin'"
fi
if [ "$routing" = host ]; then
    if [ "$langfuse_host" != "langfuse.$GATEWAY_DOMAIN" ]; then
        fail 69 "LANGFUSE_PUBLIC_URL must name langfuse.$GATEWAY_DOMAIN under host routing, got '$langfuse_origin'"
    fi
fi

# ---- 7a. edge limits ------------------------------------------------------------
# Rendered into limit_req_zone / limit_req / limit_conn: a bare positive
# integer, ^[1-9][0-9]{0,5}$, and nothing else.
check_limit() {
    key=$1
    value=$2
    unit=$3
    valid=1
    case $value in
        '' | 0* | *[!0-9]*) valid=0 ;;
    esac
    [ ${#value} -le 6 ] || valid=0
    if [ "$valid" -eq 0 ]; then
        fail 64 "$key must be a whole number from 1 to 999999 ($unit), got '$value'"
    fi
}

NGINX_TOKEN_RATE=${NGINX_TOKEN_RATE:-10}
NGINX_TOKEN_BURST=${NGINX_TOKEN_BURST:-20}
NGINX_TOKEN_CONN=${NGINX_TOKEN_CONN:-8}
NGINX_ISSUE_RATE=${NGINX_ISSUE_RATE:-5}
check_limit NGINX_TOKEN_RATE "$NGINX_TOKEN_RATE" "requests per second per corp token"
check_limit NGINX_TOKEN_BURST "$NGINX_TOKEN_BURST" "requests over the rate per corp token"
check_limit NGINX_TOKEN_CONN "$NGINX_TOKEN_CONN" "concurrent requests per corp token"
check_limit NGINX_ISSUE_RATE "$NGINX_ISSUE_RATE" "issuance requests per minute per client address"

# ---- 8. expand the trusted list -------------------------------------------------
newline='
'
TRUSTED_SET_REAL_IP_LINES=
TRUSTED_GEO_LINES=
# shellcheck disable=SC2086 # validated above; globbing is off
for entry in $NGINX_TRUSTED_PROXIES; do
    TRUSTED_SET_REAL_IP_LINES="${TRUSTED_SET_REAL_IP_LINES:+$TRUSTED_SET_REAL_IP_LINES$newline}set_real_ip_from $entry;"
    TRUSTED_GEO_LINES="${TRUSTED_GEO_LINES:+$TRUSTED_GEO_LINES$newline}$entry 1;"
done

# ---- 9. render ----------------------------------------------------------------
export GATEWAY_DOMAIN NGINX_TLS_CERT NGINX_TLS_KEY TRUSTED_SET_REAL_IP_LINES TRUSTED_GEO_LINES \
    NGINX_TOKEN_RATE NGINX_TOKEN_BURST NGINX_TOKEN_CONN NGINX_ISSUE_RATE
# shellcheck disable=SC2016 # the names envsubst may substitute, not expansions
substitutions='${GATEWAY_DOMAIN} ${NGINX_TLS_CERT} ${NGINX_TLS_KEY} ${TRUSTED_SET_REAL_IP_LINES} ${TRUSTED_GEO_LINES} ${NGINX_TOKEN_RATE} ${NGINX_TOKEN_BURST} ${NGINX_TOKEN_CONN} ${NGINX_ISSUE_RATE}'

render() {
    template=$1
    target=$2
    if [ ! -f "$template" ]; then
        fail 67 "no template $template for NGINX_TLS_MODE=$NGINX_TLS_MODE with $routing routing"
    fi
    envsubst "$substitutions" <"$template" >"$target"
}

mkdir -p "$RENDERED"
find "$RENDERED" -mindepth 1 -delete
mkdir -p "$RENDERED/snippets"
render "$TEMPLATES/00-http.conf.template" "$RENDERED/00-http.conf"
render "$TEMPLATES/listeners/$NGINX_TLS_MODE.$routing.conf.template" \
    "$RENDERED/10-$NGINX_TLS_MODE.$routing.conf"
render "$TEMPLATES/snippets/gateway-locations.inc.template" \
    "$RENDERED/snippets/gateway-locations.inc"
render "$TEMPLATES/snippets/langfuse-locations.inc.template" \
    "$RENDERED/snippets/langfuse-locations.inc"

# ---- 10. nothing left unrendered ------------------------------------------------
if grep -Rq '[$][{]' "$RENDERED"; then
    leftover=$(grep -ERho '[$][{][^}]*[}]?' "$RENDERED" | sort -u | tr '\n' ' ')
    fail 67 "unrendered placeholders in $RENDERED: ${leftover}- a template names a variable the envsubst list does not"
fi

# ---- 11. check, then serve ------------------------------------------------------
nginx -t
exec nginx -g 'daemon off;'

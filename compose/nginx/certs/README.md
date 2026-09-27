# compose/nginx/certs — the certificate nginx presents

Read only when `NGINX_TLS_MODE=terminate`. In `behind-proxy` the admins' TLS
terminator holds the certificate and this directory stays empty.

This is the certificate nginx **serves** to clients of `gateway.<domain>`. It is
not the egress CA in `crt/proxy-ca.crt`, which the gateway **trusts** for its own
outbound calls. A deployment can need both; neither replaces the other.

## What goes here

| File | Content | Mode |
|---|---|---|
| the certificate, e.g. `gateway.crt` | PEM, the full chain: the leaf first, then any intermediates | `0644` |
| the private key, e.g. `gateway.key` | PEM, unencrypted (nginx cannot prompt for a passphrase) | `0600` |

The names are yours. Set them in the server's `.env` as bare file names:

```
NGINX_TLS_CERT=gateway.crt
NGINX_TLS_KEY=gateway.key
```

The entrypoint refuses to start (exit 65) when a name holds anything but
`A-Z a-z 0-9 . _ -`, or when the file is missing or empty.

## Required SANs

A client checks the certificate against the address it dials, so the
certificate must carry every one of them as a Subject Alternative Name:

- `nginx` profile (host routing): `gateway.<GATEWAY_DOMAIN>` and
  `langfuse.<GATEWAY_DOMAIN>`, both in the one certificate. A handshake for any
  other name, or for none, is rejected.
- `nginx-ports` profile (no DNS): the IP address or local name clients dial, as
  an IP SAN or a DNS SAN. A certificate for the DNS names alone does not cover
  access by IP.

Check what a certificate carries:

```
openssl x509 -in gateway.crt -noout -ext subjectAltName
```

## A corp-CA-signed or public certificate (production)

Make a key and a signing request on the server, send the request to the corp
PKI (or a public CA), and save the signed full chain beside the key:

```
cd compose/nginx/certs
umask 077
openssl req -new -newkey rsa:3072 -nodes -keyout gateway.key -out gateway.csr \
  -subj "/CN=gateway.corp.example" \
  -addext "subjectAltName=DNS:gateway.corp.example,DNS:langfuse.corp.example"
chmod 600 gateway.key
```

Clients that already trust that CA verify with no extra flag:
`curl https://gateway.corp.example/healthz/live`.

## A self-signed certificate (pilots and tests only)

`scripts/deploy/make-selfsigned-certs.sh` makes a throwaway CA and a leaf
signed by it, and prints the path of the CA certificate. It is **not for
production**. By default it writes into this directory,
for a pilot or a local run; on a real deployment, generate or install the
files on the server itself.

```
scripts/deploy/make-selfsigned-certs.sh --domain corp.example 10.1.2.3
curl --cacert compose/nginx/certs/selfsigned-ca.crt https://gateway.corp.example/healthz/live
```

Verify with `--cacert <that CA>`, never with `-k`: `-k` also skips the name
check, and a missing SAN is exactly the fault it would hide. Claude Code is a
Node.js program and reads an extra CA from `NODE_EXTRA_CA_CERTS`.

## Installed on the server, never synced

These files are put on the server by hand, in `compose/nginx/certs/` of the
deploy directory. Git ignores everything here except this README. Do not keep
them in the laptop checkout you deploy from: the key belongs on the server and
nowhere else.

After replacing a certificate, restart the service so the entrypoint checks the
new files and nginx loads them: `docker compose restart nginx` (or
`nginx-ports`). nginx does not warn before a certificate expires; track the date.

## ACME / Let's Encrypt is out of scope

Automatic issuance needs what a corporate gateway host does not have: inbound
port 80 from the internet (the HTTP-01 challenge) or API control of the public
DNS zone (DNS-01), plus one more container to run the renewals. Certificates
come from the corp PKI or are supplied by hand.

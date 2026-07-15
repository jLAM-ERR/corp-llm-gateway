# Corporate CA bundle (operator-provided)

The gateway verifies TLS to an **internal corp LLM** against a corporate CA
bundle. That bundle is site-specific PKI material and is **not committed** —
`*.pem` / `*.crt` in this directory are git-ignored.

If your corp LLM presents a certificate signed by an internal CA, drop the CA
chain here as **`corp-ca-bundle.pem`** (PEM: root + issuing), then point
`CORP_LLM_CA_BUNDLE` at it (already wired in `docker-compose.demo.yml` /
`docker-compose.pilot.yml` and resolvable via the standard config loader — see
`docs/security.md`).

The demo and pilot stacks mount this directory read-only and append the bundle
to the trust store at boot. When the file is absent the stack falls back to
**public roots only** (certifi), which is enough for the mock upstream and for
the public providers (`api.anthropic.com` / `api.openai.com`).

## Egress re-signing proxy CA (`proxy-ca.crt`)

Separate from the corp-LLM bundle above: if your network forces internet egress
through a **re-signing** (TLS-intercepting) HTTP(S) proxy, drop that proxy's root
CA chain here as **`proxy-ca.crt`** (PEM). Unlike `corp-ca-bundle.pem` (mounted at
runtime for the corp-LLM upstream), this one is **baked into the image at build
time** — `docker/pilot-litellm/Dockerfile` and `Dockerfile.gateway` append it to
`certifi` and point `pip`/`requests` at it, so build-time `pip` and the spaCy
model download verify against the proxy CA instead of disabling TLS. It is
`.crt`, so `git` ignores it (`crt/*.crt`); the corp-LLM CA is `.pem` — the two
files are kept apart by extension.

Absent `proxy-ca.crt`, images keep stock `certifi` and build directly (or through
a plain forwarding proxy). See `docs/ops/air-gapped.md`.

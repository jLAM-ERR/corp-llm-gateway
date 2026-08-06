# Operator-provided certificates — demo-stack corp CA + build-time proxy CA

Two files with two different jobs live here, and neither is the production
stack's **runtime** trust store — that is `compose/certs/`, see
[`../compose/certs/README.md`](../compose/certs/README.md).

- `corp-ca-bundle.pem` — corp-LLM CA for the repo-root **demo** stacks only.
- `proxy-ca.crt` — egress proxy CA, **baked into the image at build time**, and
  therefore shared with the production stack too.

Check which one you need before dropping a file here; the two directories have
been confused before.

## corp-LLM CA bundle (`corp-ca-bundle.pem`) — demo stacks

The gateway verifies TLS to an **internal corp LLM** against a corporate CA
bundle. That bundle is site-specific PKI material and is **not committed** —
`*.pem` / `*.crt` in this directory are git-ignored.

If your corp LLM presents a certificate signed by an internal CA, drop the CA
chain here as **`corp-ca-bundle.pem`** (PEM: root + issuing), then point
`CORP_LLM_CA_BUNDLE` at it (already wired in `docker-compose.demo.yml` and
resolvable via the standard config loader — see `docs/security.md`).

The demo stacks mount this directory read-only and append the bundle to the
trust store at boot. When the file is absent the stack falls back to **public
roots only** (certifi), which is enough for the mock upstream and for the public
providers (`api.anthropic.com` / `api.openai.com`).

## Egress re-signing proxy CA (`proxy-ca.crt`)

Separate from the corp-LLM bundle above, and the one file here that **also serves
the production stack**: if your network forces internet egress through a
**re-signing** (TLS-intercepting) HTTP(S) proxy, drop that proxy's root CA chain
here as **`proxy-ca.crt`** (PEM). Unlike `corp-ca-bundle.pem` (mounted at runtime
for the corp-LLM upstream), this one is **baked into the image at build time** —
`Dockerfile.gateway` appends it to `certifi` and points `pip`/`requests` at it, so
build-time `pip` and the spaCy model download verify against the proxy CA instead
of disabling TLS. It is `.crt`, so `git` ignores it (`crt/*.crt`); the corp-LLM CA
is `.pem` — the two files are kept apart by extension.

Absent `proxy-ca.crt`, images keep stock `certifi` and build directly (or through
a plain forwarding proxy). Published images are always in that state: the GitHub
Actions build (`.github/workflows/build-image.yml`) builds from a clean checkout
where this directory holds only `README.md`.

Build time only. It does **not** affect a running container: a proxy that
re-signs the *runtime* provider calls needs its CA in the runtime trust store
instead — `compose/certs/corp-ca-bundle.pem` for the production stack. See
[`../compose/certs/README.md`](../compose/certs/README.md).

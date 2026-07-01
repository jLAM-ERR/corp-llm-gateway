# Corporate CA bundle (operator-provided)

The gateway verifies TLS to an **internal corp LLM** against a corporate CA
bundle. That bundle is site-specific PKI material and is **not committed** —
`*.pem` / `*.crt` in this directory are git-ignored.

If your corp LLM presents a certificate signed by an internal CA, drop the CA
chain here as **`corp-ca-bundle.pem`** (PEM: root + issuing), then point
`CORP_LLM_CA_BUNDLE` at it (already wired in `docker-compose.demo.yml` and
resolvable via the standard config loader — see `docs/security.md`).

The demo stack mounts this directory read-only and appends the bundle to the
trust store at boot. When the file is absent the demo falls back to **public
roots only** (certifi), which is enough for the mock upstream and for the
public providers (`api.anthropic.com` / `api.openai.com`).

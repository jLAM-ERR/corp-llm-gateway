# Corporate CA bundle (operator-provided)

The data-plane litellm container verifies TLS to the corp vLLM against a
corporate CA bundle. This directory is mounted read-only into the container
at `/etc/corp-llm-gateway/certs`; its contents are site-specific PKI material
and are **not committed** — `*.pem` / `*.crt` in this directory are
git-ignored.

If the corp vLLM presents a certificate signed by an internal CA, drop the
full chain (root + issuing) here as **`corp-ca-bundle.pem`**, then in `.env`
uncomment and set:

```
CORP_LLM_CA_BUNDLE=/etc/corp-llm-gateway/certs/corp-ca-bundle.pem
```

Two different HTTP clients read TLS trust inside the litellm container: our
own `CorpLlmClient` (httpx) reads `CORP_LLM_CA_BUNDLE` above, scoped to calls
to the corp vLLM only. litellm's own clients (native `anthropic/`, `openai/`
AND `hosted_vllm/`, all aiohttp) read `SSL_CERT_FILE` — a **process-global**
trust store, not a per-client one. Pointing it straight at the corp CA alone
(as an earlier revision of this stack did) would silently replace the trust
store for `api.anthropic.com`/`api.openai.com` too, breaking those routes
for anyone who set it.

`SSL_CERT_FILE` is therefore **not** set from `.env`. `compose/docker-compose.yml`
fixes it at a combined-bundle path the container's entrypoint builds at
boot: certifi's public roots, plus `corp-ca-bundle.pem` appended if this
directory has one. This **replaces the base image's OS trust store** for
every aiohttp client in the container — the entrypoint runs before `exec
litellm`, so nothing reads the image's own `/etc/ssl/certs` afterward. That
build now aborts boot on failure (`set -e` in the entrypoint command) rather
than leaving `SSL_CERT_FILE` pointing at a 0-byte or corp-CA-only file — see
`compose/docker-compose.yml`. Leave `CORP_LLM_CA_BUNDLE` commented out (the
`.env.example` default) when the corp vLLM's certificate already chains to a
publicly-trusted root — `CorpLlmClient` then falls back to its own default
trust store, and the combined bundle still has the public roots either way.

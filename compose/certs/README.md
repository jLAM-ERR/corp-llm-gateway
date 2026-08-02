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
SSL_CERT_FILE=/etc/corp-llm-gateway/certs/corp-ca-bundle.pem
```

Two different HTTP clients read TLS trust from these two variables inside
the litellm container: our own `CorpLlmClient` (httpx) reads
`CORP_LLM_CA_BUNDLE`; litellm's `hosted_vllm/` upstream (aiohttp) reads
`SSL_CERT_FILE`. Leave both commented out (the `.env.example` default) when
the corp vLLM's certificate already chains to a publicly-trusted root — both
clients then fall back to their own default trust store, and the two env
vars are never set on the container (not even to an empty string).

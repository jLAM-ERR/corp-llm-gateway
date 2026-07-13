# Local-mode quickstart (docker compose)

Run corp-llm-gateway as a local sanitizing proxy in front of Anthropic/OpenAI,
with the LLM oracle (corp vLLM) switched off — no corp infrastructure
required. Good for trying the gateway solo, or for a team that wants the
sanitization + audit trail without standing up a corp vLLM.

## 5-minute path

> **Requires `ghcr.io/jlam-err/corp-llm-gateway` ≥ `v1.0.0-rc.5`** — the first
> published image containing the `CORP_LLM_ORACLE_ENABLED` switch this
> quickstart relies on. Tags published before this feature landed ignore the
> flag, boot oracle-on against the placeholder endpoint, and the quickstart
> won't work.
> If no `rc.5`+ tag is published yet, build locally instead:
> `docker build -f Dockerfile.gateway --build-arg NER_PROFILE=en -t corp-llm-gateway:local .`
> (from the repo root), then set `image: corp-llm-gateway:local` in
> `docker-compose.yml`.

```bash
cd examples/compose
cp .env.example .env
# edit .env: set CORP_LLM_DEV_TEAM_TOKEN (any random string) and
# ANTHROPIC_API_KEY and/or OPENAI_API_KEY (see "BYOK in local mode" below)
docker compose up -d
```

Point a client at it. Two ways:

**`corp-llm-gateway-proxy`** (installs from the repo — see the main
[README](../../README.md#developer-quickstart-laptop)):

```bash
corp-llm-gateway-proxy --listen 127.0.0.1:9999 --upstream http://localhost:4000
export ANTHROPIC_BASE_URL='http://127.0.0.1:9999'
```

**Raw curl** (send `X-Corp-Auth` yourself):

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "X-Corp-Auth: $CORP_LLM_DEV_TEAM_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-3-5-sonnet-20241022","messages":[{"role":"user","content":"hi"}]}'
```

Tear down: `docker compose down` (in-memory mapping/token store — state is lost).

## What local mode does / doesn't detect

`CORP_LLM_ORACLE_ENABLED=0` disables the LLM oracle only. The deterministic
local-first cascade still runs on every request, unchanged:
`replace.md` rules → regex+checksum (ИНН/КПП/ОГРН/БИК/СНИЛС, JWT, PEM keys,
`sk-`/`AKIA`/`ghp_`, …) → bilingual NER (Natasha RU + spaCy EN) → lemma
gazetteer → code-identifier splitter. Stage 0 pre-egress block and the Stage
5 DLP egress guard are also unaffected.

What you lose: the oracle's refinement pass on a gazetteer hit (the "is this
actually a name, or a false-positive product code-name" second opinion).
Everything the local cascade catches on its own, you keep. See
[`docs/security.md`](../../docs/security.md) §8 (fail-policy matrix, row
`oracleDisabled`) for the exact posture.

## BYOK in local mode (SPIKE finding)

**Short version: local mode uses one shared Anthropic/OpenAI key configured
on the gateway (`.env`), not a per-developer key.** This is a real change
from the corp-vLLM path's BYOK story, and it's worth understanding why.

In production, the developer's own `Authorization: Bearer …` header is
forwarded **untouched** to the corp vLLM (invariant #3) — that works because
litellm's `hosted_vllm/` provider does a low-level passthrough of inbound
request headers (which is also why `strip_inbound_headers_to_upstream`
exists, to scrub `Host` etc. before they reach the corp ingress).

Native `anthropic/` and `openai/` litellm routing — what this compose example
uses to reach `api.anthropic.com` / `api.openai.com` directly — does **not**
work that way. It builds its own upstream credential header
(`x-api-key` for Anthropic, `Authorization: Bearer …` for OpenAI) from the
`api_key` configured in `litellm-config.yaml` (resolved from `ANTHROPIC_API_KEY`
/ `OPENAI_API_KEY`). It never reads the inbound client's `Authorization`
header — there is no config flag that changes this for these two providers.

Verified two ways:
- **Code**: litellm's `forward_client_headers_to_llm_api` setting only
  forwards headers named `x-*` (and `anthropic-beta`) — `Authorization` is
  explicitly excluded. The Anthropic provider's own header-construction code
  (`llms/anthropic/common_utils.py`) builds `x-api-key`/`authorization`
  purely from the resolved `api_key`/`auth_token`, with no path that reads
  request headers at all. Same shape for the OpenAI provider.
- **Live**: ran the published GHCR image (litellm v1.85.0 baked in, the
  version pinned at the time of this spike) against a capture server
  standing in for `api.anthropic.com`, with
  `api_key: sk-ant-gateway-configured-key` in `litellm_params` and
  `forward_client_headers_to_llm_api: true`. Sent a request with
  `Authorization: Bearer sk-ant-CLIENT-OWN-KEY`. The capture server received
  `x-api-key: sk-ant-gateway-configured-key` — the client's key never
  arrived on the wire.

**Invariant #3 implication**: the CLAUDE.md invariant ("the developer's own
Authorization header is forwarded untouched to upstream") is exercised today
only on the corp-vLLM (`hosted_vllm/`) path, not on this compose example's
anthropic/openai routes. Sanitization, auth (`X-Corp-Auth`), and audit are
unaffected either way — only the upstream credential is shared instead of
per-developer. If you need true per-developer BYOK against Anthropic/OpenAI
directly, that's an open item — track it separately, don't silently assume
it's covered by this example.

## Why the extra `bootstrap.py` mount

litellm resolves `litellm_settings.callbacks` entries as a **file path**
relative to the mounted config's directory, not as a Python package import —
even though `corp_llm_gateway` is pip-installed in the image (confirmed
against the exact `litellm==1.85.0` baked into this tag: `get_instance_fn`
looks for `<config-dir>/corp_llm_gateway/bootstrap.py` on disk and never
falls back to `importlib.import_module`). Without a real file at that path,
the gateway container fails at startup with `ImportError: Could not import
guardrail from corp_llm_gateway.bootstrap`. `docker-compose.yml` mounts
`src/corp_llm_gateway/bootstrap.py` there — same technique
`docker-compose.demo.yml` already uses for `litellm_hook.py` /
`_demo_guardrail.py`. `bootstrap.py`'s own imports still resolve against the
real installed package, so this is a one-file mount, not a full source
checkout.

This is a litellm-proxy-wide behavior, not specific to local mode — the
production Helm chart hits the same lookup. As of this fix, the chart's
litellm ConfigMap carries a second key with the same delegating shim, and
the `litellm-config` volume uses `configMap.items` to project it to
`corp_llm_gateway/bootstrap.py` alongside `config.yaml` under `/etc/litellm`,
so a cluster deploy of the published image boots the same way this compose
example does. See `helm/corp-llm-gateway/templates/configmap-litellm.yaml`
and `deployment.yaml`, and the render asserts in `tests/helm/`.

## Enabling the oracle

Point `CORP_LLM_ENDPOINT` at a real corp vLLM, set
`CORP_LLM_ORACLE_ENABLED=1` (or drop the env var — it's the default), and
restart. See [`config.example.toml`](../../config.example.toml) for the full
oracle/auth-provider knob set.

## Durable mapping store

The commented `redis` service in `docker-compose.yml` shows the upgrade
path: uncomment it, add `REDIS_URL: redis://redis:6379/0` to the `gateway`
environment, and the per-conversation mapping store (Cache B) survives a
restart instead of being lost with the in-memory default.

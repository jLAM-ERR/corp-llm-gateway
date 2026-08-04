# Deployment modes

Which stack to run, how developers authenticate, and how to turn the two
optional network dependencies (the corp-LLM oracle, the corp NER service) on and
off. Russian mirror: `deployment-modes.ru.md`.

There are two authentication modes and they are **mutually exclusive**. Pick one
before you write `.env`.

| | Mode A — API keys | Mode B — subscription (OAuth) |
|---|---|---|
| Stack | `compose/` (production) | `docker-compose.demo.yml` + `docker-compose.anthropic-oauth.yml` |
| Upstream credential | the gateway's own `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | the developer's own OAuth bearer, forwarded |
| Developer sends | `Authorization: Bearer <litellm virtual key>` | `Authorization: Bearer <OAuth token>` |
| `LITELLM_MASTER_KEY` | **required** | **must be absent** |
| Per-developer revocation / spend | yes, via the LiteLLM Admin UI | no |

---

## Mode A — API keys (production compose stack)

The stack in `compose/`. Developers authenticate with a **LiteLLM virtual key**;
the gateway holds the provider credentials.

```
cd compose
cp .env.example .env
chmod 0600 .env
# fill in the required secrets, then:
docker compose up -d
```

Required in `.env` (compose refuses to start without them):
`POSTGRES_PASSWORD`, `LITELLM_MASTER_KEY`, `UI_USERNAME`, `UI_PASSWORD`,
`CORP_LANGFUSE_PUBLIC_KEY`, `CORP_LANGFUSE_SECRET_KEY` and the `LANGFUSE_*`
secrets. See `compose/README.md`.

**Provider keys are optional.** `docker-compose.yml` declares them as
`${ANTHROPIC_API_KEY:-}` / `${OPENAI_API_KEY:-}`, so an empty value is
accepted. Set only what you actually route to:

- `ANTHROPIC_API_KEY` — needed for the `claude-*` route;
- `OPENAI_API_KEY` — needed for the `gpt-*` route;
- neither — fine if you only use the `corp-*` route against the corp vLLM.

**BYOK is not available in this mode, on any route.** The native `anthropic/`
and `openai/` providers build the upstream credential from the configured
`api_key` and never read the inbound `Authorization` header; the `corp-*` /
`hosted_vllm/` route does the same. A developer's own key cannot reach upstream
here — that is what Mode B is for.

## Mode B — subscription (OAuth passthrough)

The developer's Anthropic subscription token is forwarded to `api.anthropic.com`
untouched. **No `ANTHROPIC_API_KEY` exists anywhere in this mode.**

```
docker compose -f docker-compose.demo.yml -f docker-compose.anthropic-oauth.yml up -d --build
```

It overlays the **demo** stack on purpose, because that stack sets no
`LITELLM_MASTER_KEY`. The bridge is enabled by
`CORP_LLM_FORWARD_ANTHROPIC_AUTH=1` (the overlay sets it).

**The master key and the bridge cannot coexist.** With a master key set, litellm
treats the inbound `Authorization` header as one of its own virtual keys and
answers 401 *before* `pre_call` ever runs, so the OAuth bearer would never reach
the bridge. `build_guardrail()` therefore refuses to boot and names the cause
(`settings.MASTER_KEY_VS_FORWARD_AUTH_MESSAGE`) rather than serving 401s.

> **Trap:** a blank `LITELLM_MASTER_KEY=` line **counts as set** — litellm keeps
> the empty value and enables proxy auth for anything that is not `None`. Delete
> the line entirely. A leftover key in a git-ignored `.env.demo` picked up via
> `env_file:` is the usual source.

Consequence to plan for: Mode B has **no per-developer virtual keys**, so no
per-developer revocation or spend accounting through the LiteLLM UI. Sanitization,
audit and the `X-Corp-Auth` team identity work exactly the same in both modes.

---

## Toggle: the corp-LLM oracle

The oracle is the **conditional fallback** at the end of the local-first cascade
(ADR-003). It is called only on a deterministic gazetteer hit, not on every
request.

**Off (default).** `compose/.env.example` ships `CORP_LLM_ORACLE_ENABLED=0`. In
this state no oracle call is ever attempted and `CORP_LLM_ENDPOINT` is not needed
for detection.

**On.**

```
CORP_LLM_ORACLE_ENABLED=1
CORP_LLM_ENDPOINT=https://<corp-vllm-host>/v1   # required once the oracle is on
```

Three things that bite:

- **Enabling without a reachable endpoint fails requests closed on *every*
  route**, not just `corp-*` — a gazetteer hit falls through to a non-routable
  placeholder host. Point `CORP_LLM_ENDPOINT` at a live vLLM first.
- **The `corp-*` route needs `CORP_LLM_ENDPOINT` regardless of this flag.**
  Without it, `corp-*` requests fail per-request rather than failing config load.
- **Do not turn off both the oracle and the local-first cascade.**
  `build_guardrail()` raises `ConfigError(NO_OP_SANITIZER_MESSAGE)` at boot if
  `CORP_LLM_ORACLE_ENABLED=0` **and** `CORP_LLM_LOCAL_FIRST=0` — the gateway
  refuses to run as a no-op sanitizer. `CORP_LLM_LOCAL_FIRST` is unset on this
  stack and defaults to `1`, so the shipped posture is safe; just don't set it
  to `0` without enabling the oracle.

**When it fires** is `CORP_LLM_ORACLE_TRIGGER`: `gazetteer_hit` (default) |
`any_local_finding` | `always` | `sampled:<pct>`. A team's profile can widen it
via `oracle_mode`; the effective value is the **broader** of the two — neither
can narrow the other.

## Toggle: the corp NER service

A remote NER detector appended to the local cascade. **Network-backed**, so it is
excluded from `CODE` segments (it would ship source code to an external service);
all local detectors keep scanning `CODE`.

**Off (default).** `CORP_NER_ENABLED=0`. The stack behaves exactly as if the
feature did not exist: no detector, no readiness probe.

**On.** Both keys are required together:

```
CORP_NER_ENABLED=1
CORP_NER_ENDPOINT=http://<ner-host>:<port>      # BASE url; the client appends /v1/analyze
```

- **Enabled without an endpoint is a boot refusal**, not a silent skip —
  `build_corp_ner()` raises `ConfigError`, and `gateway-admin config check`
  reports the same problem.
- **It needs a source build.** The published `GATEWAY_IMAGE_TAG` (`1.0.0-rc.6`,
  commit `1f170ea`) is an *ancestor* of the branch that added corp NER, so on
  that image these variables do nothing. Use
  `docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build`.
  That overlay builds the `ru-en` NER profile on purpose — the Dockerfile default
  (`base`) ships no EN model, and with `CORP_LLM_REQUIRE_NER=1` that image 503s
  on every request.
- Optional tuning, passed by **bare name** (leave the lines commented rather than
  empty — an empty env var wins over the config file):
  `CORP_NER_TIMEOUT_S` (30), `CORP_NER_MAX_TEXTS` (256),
  `CORP_NER_MAX_INPUT_CHARS` (200000), `CORP_NER_CA_BUNDLE` (unset).
- The NER call carries **raw user content**, so its TLS verification is never
  disabled. For an internal CA, point `CORP_NER_CA_BUNDLE` at the chain.

**Flipping either toggle is safe for Cache A.** The cache key folds a fingerprint
of the effective detector policy, so pods with different `CORP_NER_ENABLED`
values derive disjoint keys and cannot serve each other's entries. No cache flush
is needed — see `upgrade.md`.

Do not confuse `CORP_NER_ENABLED` (this remote service) with
`CORP_LLM_REQUIRE_NER` (the **in-process** RU/EN engines), which stays `1` in
production either way.

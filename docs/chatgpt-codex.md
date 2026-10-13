# ChatGPT Codex profile

This profile lets Codex talk to `corp-llm-gateway` over the OpenAI Responses
API, and lets the gateway authenticate using the developer's live ChatGPT
subscription OAuth instead of an OpenAI API key. No OpenAI API key is needed
for this profile.

Request flow (the demo stack below listens on 4000; on the production compose
stack the codex gateway is on 4001, see "In production"):

```text
Codex (ChatGPT OAuth)
  -> http://127.0.0.1:4000/v1/responses   (production: 4001, GATEWAY_CODEX_PORT)
  -> corp-llm-gateway: policy + sanitization
  -> https://chatgpt.com/backend-api/codex/responses
  -> reverse substitution on the Responses/SSE response
  -> Codex
```

## Requirements

- Docker Desktop with Compose.
- A working `CORP_LLM_ENDPOINT` in `.env.demo`: this is the sanitization
  helper model, not the external ChatGPT model.
- Codex already signed in to ChatGPT: `codex login status`. Run `codex login`
  and choose the ChatGPT sign-in if needed.

Codex itself supplies the OAuth access token and `ChatGPT-Account-Id`. Don't
copy `~/.codex/auth.json` into the container or add it to `.env.demo`.

## Start the gateway

```bash
cd /path/to/corp-llm-gateway

docker compose \
  -f docker-compose.demo.yml \
  -f docker-compose.chatgpt-codex.yml \
  up -d --build redis postgres litellm

curl -fsS http://127.0.0.1:4000/health/liveliness
```

The overlay sets `CORP_LLM_FORWARD_CHATGPT_AUTH=1`, routes the selected Codex
model as `openai/*` to the ChatGPT Codex backend, and leaves the base GLM
demo profile unchanged.

## Install the Codex profile

> **Against a production host, edit the copy first.**
> `docker/chatgpt-codex/chatgpt-codex.config.toml` is the demo profile: it
> hardcodes `base_url = "http://127.0.0.1:4000/v1"` and
> `X-Corp-Auth = "demo-team-token"`. On the production compose stack port 4000
> is `gateway-anthropic`; the codex gateway listens on `GATEWAY_CODEX_PORT`
> (default 4001). Set `base_url` to `http://127.0.0.1:4001/v1` (through an SSH
> tunnel to the server), or to the front door's origin + `/v1` when
> `NGINX_GATEWAY_UPSTREAM=gateway-codex`, and put the developer's own corp
> token in `X-Corp-Auth`. Pointed at 4000, Codex reaches the Anthropic gateway,
> which has no route for its model.

```bash
cp docker/chatgpt-codex/chatgpt-codex.config.toml \
  ~/.codex/chatgpt-codex.config.toml
# production: edit base_url (port 4001) and X-Corp-Auth in the copy
```

Run it:

```bash
codex --profile chatgpt-codex
```

One-shot check:

```bash
codex exec --profile chatgpt-codex \
  --skip-git-repo-check \
  "Answer with one word: working?"
```

The profile uses:

- `wire_api = "responses"`;
- `requires_openai_auth = true`, so Codex attaches the OAuth Bearer token and
  the ChatGPT account ID;
- `X-Corp-Auth = "demo-team-token"` for local demo auth;
- SSE instead of WebSocket, since the gateway performs reverse substitution
  in the stream.

## What the gateway changes

- `input`, `instructions`, `input_text`, function-call arguments, and tool
  output go through the same fail-closed pipeline as `messages`.
- Only an allowlisted set of Codex headers is forwarded upstream. The
  internal `X-Corp-Auth` header is stripped before the outbound request.
- `response.output_text.delta`, terminal Responses events, and tool
  arguments are desanitized, including a placeholder split across SSE
  chunks.
- The profile rejects the request with `401 E_PROVIDER_AUTH` if Codex didn't
  send a valid OAuth Bearer token.

## In production

On the production compose stack (`compose/`) this is the `codex` provider.
`COMPOSE_PROFILES=codex` (alone or with other providers) starts `gateway-codex`
on `127.0.0.1:${GATEWAY_CODEX_PORT:-4001}`, with
`CORP_LLM_FORWARD_CHATGPT_AUTH=1` and `compose/litellm/config.codex.yaml`: one
catch-all route to the literal `https://chatgpt.com/backend-api/codex`, so no
setting can send the token to another host. It runs the production guardrail
(Postgres token store, audit chain), so `X-Corp-Auth` carries a real corp
token, not `demo-team-token`. Point the profile's `base_url` at that port, or at
the front door when `NGINX_GATEWAY_UPSTREAM=gateway-codex`. See
[`ops/deployment-modes.md`](ops/deployment-modes.md).

`CORP_LLM_FORWARD_CHATGPT_AUTH` is a normal `settings.KEYS` flag: it is
resolved by `bootstrap.build_guardrail()` from the environment or the TOML
config file like every other gateway setting (see
[`config.example.toml`](../config.example.toml)). The bridge does not check
where litellm routes the request, so set it only where every route goes to the
ChatGPT Codex backend, as `config.codex.yaml` does. The Helm chart in this
release routes `"*"` to the corp vLLM: do not set the flag there, or the
developer's ChatGPT token goes to the corp vLLM.

Two more constraints apply wherever you set it:

- **`LITELLM_MASTER_KEY` must be unset.** Any master key refuses to boot, bridge
  or not: litellm's proxy auth (Mode A) was removed in 1.1.0. With the bridge on,
  litellm would also read the inbound `Authorization` as one of its own virtual
  keys and answer `401` before `pre_call` runs, so the bridge could never see the
  Codex token. Presence counts, not truthiness — remove the line, do not blank
  it.
- **Mutually exclusive with `CORP_LLM_FORWARD_ANTHROPIC_AUTH`.** Both bridges
  read the same inbound bearer; setting both refuses to boot and fails
  `gateway-admin config check`. That is why the compose stack runs one gateway
  per provider.

Only the bridge's own headers reach ChatGPT, by exact name and only with a
value in the header's format; no other client header goes upstream
([`security.md`](security.md) §13).

Note also that litellm retains one deployment per distinct per-request
`api_key` — raw value included — for the lifetime of the proxy process, with no
eviction. Restarting the process is the only way to clear them. See
[`security.md`](security.md) §13.

## Troubleshooting

```bash
docker compose \
  -f docker-compose.demo.yml \
  -f docker-compose.chatgpt-codex.yml \
  ps

docker compose \
  -f docker-compose.demo.yml \
  -f docker-compose.chatgpt-codex.yml \
  logs -f --tail=100 litellm
```

Common errors:

| Error | Cause | Fix |
|---|---|---|
| `401 E_PROVIDER_AUTH` | Codex didn't send ChatGPT OAuth | Re-run `codex login`, check `requires_openai_auth = true` |
| `401 E_MISSING_TOKEN` | No `X-Corp-Auth` | Check the profile's `http_headers` section |
| `503 E_CORP_LLM_DOWN` | The sanitization helper model is unreachable | Check `CORP_LLM_ENDPOINT` and VPN/DNS |
| upstream `401/403` | Subscription/account doesn't accept the model | Check the model in the base Codex profile and re-login |

Stop just this profile's stack:

```bash
docker compose \
  -f docker-compose.demo.yml \
  -f docker-compose.chatgpt-codex.yml \
  stop litellm redis postgres
```

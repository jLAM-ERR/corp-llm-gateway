# ChatGPT Codex profile

This profile lets Codex talk to `corp-llm-gateway` over the OpenAI Responses
API, and lets the gateway authenticate using the developer's live ChatGPT
subscription OAuth instead of an OpenAI API key. No OpenAI API key is needed
for this profile.

Request flow:

```text
Codex (ChatGPT OAuth)
  -> http://127.0.0.1:4000/v1/responses
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

```bash
cp docker/chatgpt-codex/chatgpt-codex.config.toml \
  ~/.codex/chatgpt-codex.config.toml
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

`CORP_LLM_FORWARD_CHATGPT_AUTH` is a normal `settings.KEYS` flag: it is
resolved by `bootstrap.build_guardrail()` from the environment or the TOML
config file like every other gateway setting (see
[`config.example.toml`](../config.example.toml)), not just from the demo
compose overlay above. Setting it via a Helm value / k8s env var enables the
same header bridge on a real cluster deployment — no code change needed.

Two constraints apply wherever you set it:

- **`LITELLM_MASTER_KEY` must be unset.** With a master key, litellm reads the
  inbound `Authorization` as one of its own virtual keys and answers `401`
  before `pre_call` runs, so the bridge could never see the Codex token. The
  gateway now **refuses to boot** on that combination rather than serving
  unexplained 401s. Presence counts, not truthiness — remove the line, do not
  blank it.
- **Mutually exclusive with `CORP_LLM_FORWARD_ANTHROPIC_AUTH`.** Both bridges
  read the same inbound bearer; setting both refuses to boot and fails
  `gateway-admin config check`.

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

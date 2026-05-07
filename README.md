# corp-llm-gateway

Corporate LLM gateway. Sanitizes traffic between developer Claude Code instances and Anthropic / OpenAI before it leaves the corp boundary.

Replaces the per-laptop `data-sanitizer` Claude Code plugin (which only covered user prompts) with a centrally-enforced, auditable, multi-provider gateway.

## Status

v1 — pre-execution. See `docs/plans/20260507-external-sanitizer-gateway-v1.md`.

## Architecture

Architecture B (assemble best-of-breed): single custom Python guardrail plugged into LiteLLM proxy; everything else (audit pipeline, auth, observability) is open-source operated.

```
Claude Code → gateway.corp.lan → LiteLLM (with custom guardrail) → api.anthropic.com / api.openai.com
                                       │
                                       ├── pre_call:  sanitize via two-stage engine
                                       ├── post_call: de-sanitize streaming response
                                       └── audit:     Vector → Langfuse + S3 + SIEM
```

Full architecture in the v1 plan.

## Repo layout

```
src/corp_llm_gateway/   Python guardrail (LiteLLM custom hooks + sanitizer engine)
tests/                  pytest suite
helm/corp-llm-gateway/  Helm chart for k8s deployment
docs/                   plan + audit schema + capacity + ops runbook
```

## Development

Requires Python 3.12+.

```bash
pip install -e ".[dev]"
pre-commit install
pytest
```

## Owner

the DRI@gmail.com

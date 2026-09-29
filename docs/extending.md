# Extending the gateway

**English** · [Русский](extending.ru.md)

How to add capability to corp-llm-gateway — a new detector, audit sink, metrics
exporter, provider, or jurisdiction/division profile.

## Philosophy — why in-tree, not runtime plugins

Extensions are **in-tree and declarative**: a data bundle (a *profile*) layered over the core, plus
a *closed set* of security-reviewed algorithms selected **by name**. The gateway never loads
third-party executable code on the egress path.

This is deliberate. The success criterion is *zero confirmed leak incidents*, the deployment is
air-gapped, and every change on the leak path must be CODEOWNERS-reviewed and auditable. Python
`entry_points` / pip plugins would put arbitrary third-party code between the user's data and the
upstream API — a supply-chain and audit hole we will not open. So "extending" here means a **small,
reviewable in-tree change**, not a runtime plugin.

## The three extension styles

Almost everything you can add falls into one of three shapes. Get the style right and the mechanics
follow.

| Style | You implement | You wire it up by | Selected by |
|---|---|---|---|
| **1. Config-factory backend** | an ABC (`Sink`, `MetricsExporter`, `CorpLlmAuthProvider`) | adding one entry to a factory dict | an env var |
| **2. In-tree name registry** | an ABC (`PIIDetector`, provider spec) | adding one registry line | **by name** (in a profile / by model) |
| **3. Generic `extensions.REGISTRY`** | — | the composition root, not you | inspection/health only |

Style 3 is *plumbing*: `extensions.REGISTRY` is a keyed inspection/health surface that
`bootstrap.build_guardrail()` populates (e.g. it adapts the active audit sink into the registry). You
rarely call it directly — it's how style-1/2 pieces become visible to `gateway-admin extensions`, not
how you add them.

Two special cases sit outside the table: **profiles** (pure declarative data — see
[docs/ops/profiles.md](ops/profiles.md)) and **storage** (a `REDIS_URL` toggle, not a factory — see
[Config-only backends](#config-only-backends)).

---

## Add a detector (style 2)

A detector finds spans to redact. The registry is `DETECTOR_REGISTRY` in
`src/corp_llm_gateway/profiles/registry.py`; the built-ins are `regex_checksum`, `dual_ner`,
`ner_ru`, `ner_en`, `corp_ner`.

`corp_ner` is the one **network-backed** built-in: selecting it requires
`CORP_NER_ENDPOINT` (it is a boot refusal without one), and it is excluded from
`CODE` segments — the local detectors are not, since local NER is what catches
PERSON/ORG inside fenced JSON, SQL values and config examples.

1. **Implement `PIIDetector`** (`detectors/base.py`) in `src/corp_llm_gateway/detectors/my_rule.py`:

   ```python
   from corp_llm_gateway.detectors.base import Finding, PIIDetector

   class MyRuleDetector(PIIDetector):
       async def detect(self, text: str) -> list[Finding]:
           # return one Finding(text, label, start, end, score) per match
           ...
   ```

2. **Re-export** it in `detectors/__init__.py` `__all__` (repo convention — ABC + impls are
   re-exported from each package's `__init__`).

3. **Register it by name** — one line in `DETECTOR_REGISTRY` (`profiles/registry.py`); values are
   factories `lambda cfg: Detector()`:

   ```python
   "my_rule": lambda cfg: MyRuleDetector(),
   ```

4. **Add a contract test** under `tests/detectors/` (see the existing detector tests for the pattern).

5. **Select it by name** in a profile's `profile.toml` — `detectors = ["regex_checksum", "my_rule"]` —
   then **reseal** the bundle (its `content_hash` changed):
   `python -m corp_llm_gateway.profiles.seal src/corp_llm_gateway/profiles/defaults`.

`build_detectors(names, cfg)` builds the selected set; an unknown name is a hard error.

6. **Implement `policy_signature()` if your class does not imply your coverage.** The Cache-A key
   folds a fingerprint of the effective policy, and a detector is identified there by import path +
   qualname alone. If two instances of your class can redact differently — optional models that may
   be absent, coverage-narrowing construction args, a remote endpoint — add the optional
   `PolicySignatureDetector` hook (`detectors/base.py`) and return that difference:

   ```python
   def policy_signature(self) -> tuple[str, ...]:
       return (f"my_rule:{self._mode}:{package_version('my-lib')}",)
   ```

   Rules: plain sorted strings, no builtin `hash()` / `id()` / object `repr()` / set iteration order
   (they differ per process, and pods share one Redis); no user content (M1-14); and the value must
   not change after construction — resolve anything lazy inside the hook so it latches, as
   `ner_ru` / `ner_en` do. Raising, or returning a non-string, disables Cache A for the whole
   orchestrator (fail closed). Skip the hook and you inherit class identity, which is correct only
   when every instance of the class redacts the same way.

## Add an audit sink (style 1)

A sink is where audit records go. Selection is config-only via `CORP_AUDIT_SINK`.

1. **Implement `Sink`** (`audit/sinks.py`): `async def write(self, record: dict[str, Any]) -> None`.
2. **Add a factory + name** in `audit/factory.py`: a `_make_<name>()` entry in `_SINK_FACTORIES` **and**
   a type→name entry in `_SINK_NAMES` (the reverse map keeps the registered extension name matching the
   live object).
3. **Select it** with `CORP_AUDIT_SINK=<name>` (default `stdout`; built-ins `stdout`/`langfuse`/`list`).

You do **not** register anything yourself: `get_sink()` builds the selected sink, and the composition
root adapts it into `extensions.REGISTRY` via `register_sink(REGISTRY, sink, name)`
(`bootstrap.py:250`) so it shows up under `gateway-admin extensions`. The NEVER-fields gate wraps every
sink regardless.

## Add a metrics exporter (style 1)

1. **Implement `MetricsExporter`** (`metrics/base.py`): `record_block(block_reason)`,
   `record_failure(component)`, `observe_request_latency(seconds, *, status)`, plus `render()` /
   `content_type()` for a scrape endpoint.
2. **Add a factory entry** in `metrics/__init__.py` `_EXPORTER_FACTORIES` (built-ins `noop`,
   `prometheus`).
3. **Select it** with `CORP_METRICS_EXPORTER=<name>` (default `noop`); built by `get_exporter()`.

## Classify a litellm route (after a litellm bump)

`src/corp_llm_gateway/route_gate/table.py` is the **single place** a litellm
route is classified. Nothing else may decide whether a request reaches a
provider: the middleware enforces the table and refuses anything it does not
know (default-deny). See [`security.md`](security.md) §14 for what the gate is
and why.

Bumping litellm means re-running the classification, because the guard test
(`tests/route_gate/test_litellm_route_guard.py`) reads litellm's installed
`proxy/` source with `ast` on every CI run and fails on:

- a `(method, path)` the tables do not know — **every new or moved route**;
- a REWRITTEN entry whose handler no longer reaches `pre_call_hook`;
- a hook-less `POST`/`PUT`/`PATCH` marked PASSTHROUGH without a written
  `justification` — and the justification is re-checked against the `ast`, so a
  handler that calls `litellm.acompletion`, `aresponses`, `token_counter`,
  `router.a*` or `pass_through_request` cannot be excused by a comment;
- a **dynamic registration site** (a path the collector cannot resolve to a
  literal) that is new or has moved line — `KNOWN_DYNAMIC_REGISTRATIONS` in
  `tests/route_gate/litellm_routes.py` pins each one by `(file, line)` with its
  policy.

The loop:

1. Bump the pin in all **seven** sites at once (`tests/test_litellm_pin.py`
   names them; `pyproject.toml` is one of them, so CI's `pip install -e .`
   reads the same proxy the image ships) and install it into `.venv-bench`.
2. Run the guard: `PYTHONPATH=src .venv-bench/bin/python -m pytest
   tests/route_gate/test_litellm_route_guard.py -q`. Its failure message names
   each unclassified route or moved dynamic site.
3. **Add a row by hand** to `table.py` for each one, applying the five ordered
   rules in that module's docstring (REWRITTEN generation spellings → stored
   responses by id → any other provider-reaching handler is REFUSE → named
   user-text trees are REFUSE → the rest is the admin surface, PASSTHROUGH with
   a `justification` when it writes). There is no generator script: the
   collector is a test helper, and a row is a security decision.
   A path with a `{parameter}` becomes a `LITELLM_REGEX_TABLE` row, ordered
   longest-static-prefix first.
4. **Fail closed when unsure.** REFUSE is one line to flip later; a wrong
   PASSTHROUGH is a leak. Record the judgement in the row's `why`.
5. Re-run the guard on both venvs plus `tests/route_gate/` and
   `tests/test_asgi_entrypoint.py`.

`GATEWAY_ROUTE_TABLE` (the gateway's own `/healthz/*` and `/metrics`) is
hand-written and exempt from the source guard — it describes routes litellm does
not register.

## Add a provider (style 2)

Providers are egress targets. They use their **own** `ProviderRegistry`
(`providers/registry.py`), not `extensions.REGISTRY`. v1 is intentionally locked down:
`V1_ALLOWED = {anthropic, openai, corp-vllm}`, and CLAUDE.md forbids a non-OpenAI/Anthropic provider in
v1 (Bedrock / Gemini / Azure are explicit v2).

- Built-ins are declared as `ProviderSpec` (adds `role`, `wire_format`, `health_url`) in
  `register_builtins()`; routing picks `anthropic` vs `openai` by model name.
- A v2 provider stays gated behind `CORP_ALLOW_V2_PROVIDERS=1` — do not remove that gate to ship one.

## The generic extensions registry (style 3 — plumbing)

`extensions.REGISTRY` (`extensions/registry.py`) is the keyed inspection/health surface, not a
contributor entry point. Its primitives:

- `register(spec, factory, *, replace=False)` — `factory: Callable[[], Extension]`. A duplicate
  `(kind, name)` **fails closed** (raises) unless `replace=True`, so a later registration can't
  silently shadow a NEVER-gated sink or an egress-path detector.
- `validate_api_version(EXTENSION_API_VERSION)` — every registered `ExtensionSpec.api_version` must
  equal core's (`EXTENSION_API_VERSION = "1"`), else load fails closed.
- `ExtensionSpec(name, kind, version, api_version, capabilities=frozenset(), fail_policy="fail-closed")`
  — note `version` is **required**; `fail_policy` defaults to fail-closed.

The 7 `ExtensionKind`s are `audit_sink, metrics, tracing, provider, detector, rules, payload_policy`.
Caveats: `detector` is served by the separate `DETECTOR_REGISTRY` (above), and
`tracing` / `rules` / `payload_policy` are declared but not yet wired (no factory/impl) — don't build
against them yet.

Inspect what's live:

```bash
gateway-admin extensions list      # registered (kind:name) pairs
gateway-admin extensions inspect   # specs
gateway-admin extensions health    # per-extension health()
```

`gateway-admin extensions enable|disable` are RBAC-gated **stubs** (they need an extension-state store —
a follow-up); they don't change state yet.

## Author a profile bundle

A profile is the declarative half — the way you vary detection/policy by **country / division /
regulatory regime** without touching core. A bundle is `profile.toml` (manifest: `extends`,
`detectors`, `[policy]`) plus optional `replace.md` / `*.txt` term files, hash-sealed and layered with a
monotone-tightening `PolicyKnobs.merge` (composition only *adds* redaction). A team selects profiles via
`TeamConfig.profile_ids`.

Full authoring guide — bundle layout, composition/precedence, sealing, selection:
**[docs/ops/profiles.md](ops/profiles.md)**.

## Config-only backends

No code change — flip an env var / Helm value:

- **Auth provider** — `get_auth_provider()` + `_PROVIDER_FACTORIES` (`auth/factory.py`), selected by
  `CORP_LLM_AUTH_PROVIDER` (`noop` default; `bearer`/`mtls`/`oidc`).
- **Storage** — the exception: `MappingStore` (`storage/mapping.py`) is an ABC with **no factory dict**.
  `bootstrap.build_mapping_store()` picks `RedisMappingStore` when `REDIS_URL` is set, else
  `InMemoryMappingStore`. A new backend edits that function — there is no name-selector to extend.

## Safety rules the code enforces

Every seam above is built to fail safe:

- **Fail-closed registration** — duplicate `(kind, name)` raises; no silent overwrite.
- **API-version gate** — a mismatched `api_version` fails load, not silently degrades.
- **Fail-closed default** — `ExtensionSpec.fail_policy` defaults to `fail-closed`; unknown detector /
  sink / provider names are hard errors, never no-ops.
- **Hash-sealed bundles** — editing a sealed profile requires re-sealing; a tampered bundle is caught
  fail-closed at load.
- **No third-party runtime code** on the egress path — algorithms are in-tree and named.

## The response boundary: what an extension sees

Originals exist in two places only: in the pre-call while it rewrites the
request, and in the ASGI desanitiser (`route_gate/desanitize_middleware.py`)
while it restores the response on its way to the client. Every seam in this
guide sits outside both. An audit sink or a metrics exporter gets content-free
records and label values; a callback registered in litellm (a logger, a
guardrail, an OTEL exporter) sees placeholders in the request and in the
response, because the reversal runs after litellm is done with it. There is no
hook for the client-side, restored view, and there will not be one: a component
that sees restored text is a new M1-14 surface. An extension that needs content
works on placeholders, or is a detector on the way in. Do not add an
`apply_guardrail` or a litellm `CustomGuardrail` for it: the first stops our
pre-call from running and refuses to arm (exit 70), the second turns litellm's
per-chunk hooks on for every callback, and the shipped configs are pinned to
carry neither (`docs/security.md` §15).

## Tasks several requests await: `inflight.spawn_shared`

The route gate's in-flight limiter (`route_gate/inflight.py`) owns every task a
request starts: when the client disconnects, it cancels the request and then every
task still pending that the request created. A task whose result **more than one
request** awaits — a single-flight lookup, a shared key or config fetch, a
background refresh — must therefore be started with `inflight.spawn_shared(coro,
name=...)`, never `asyncio.create_task` / `ensure_future` from request code.
Started from inside a request, it belongs to that request, and that request's
disconnect cancels it under every other waiter. The token-store auth lookup
(`tokens/middleware.py`) and the JWKS fetch (`tokens/oidc_verifier.py`) are the
two existing callers. litellm's logging worker is recognised by its module and
never tagged; it is also started in the lifespan (`asgi.py`) so the first
request never owns it.

## Governance

CODEOWNERS splits review by blast radius: `profiles/**` (data bundles) → compliance;
`detectors/**` + the registries (`profiles/registry.py`, `extensions/`, `providers/`) → security-eng.
A new algorithm on the leak path is a security review; a new jurisdiction bundle is a compliance review.

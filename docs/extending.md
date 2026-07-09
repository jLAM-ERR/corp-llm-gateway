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
`ner_ru`, `ner_en`.

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

## Governance

CODEOWNERS splits review by blast radius: `profiles/**` (data bundles) → compliance;
`detectors/**` + the registries (`profiles/registry.py`, `extensions/`, `providers/`) → security-eng.
A new algorithm on the leak path is a security review; a new jurisdiction bundle is a compliance review.

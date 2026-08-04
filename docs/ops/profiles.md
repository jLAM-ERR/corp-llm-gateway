# Profile bundles (country / division / regime)

How to vary detection and policy by jurisdiction, division, or regulatory regime
without forking core. A profile is a **declarative data bundle** layered over the
core, selecting in-tree detector algorithms **by name** — never third-party code
loaded at runtime.

For the design method (extension-point map, options, worked example), use the
**`plugin-design`** skill (`.claude/skills/plugin-design`). This doc is the
operator how-to.

## Bundle layout

A bundle is a directory named by its profile id. The in-tree examples live at
`src/corp_llm_gateway/profiles/defaults/<id>/`:

```
profiles/defaults/<id>/
  profile.toml      # required — manifest
  replace.md        # optional — per-profile redaction rules
  products.txt      # optional — gazetteer term files
  regulated.txt
  markings.txt
  allowlist.txt     # optional — never-redact values
```

`FileProfileLoader` reads `<root>/<id>/`. Term files and `replace.md` reuse the
same loaders as core (`rules/`), so a bundle is data only.

## `profile.toml`

```toml
# ru-152fz: RU personal-data regime, layered over core.
name = "ru-152fz"           # required
extends = ["core"]          # parent layers; resolved to [core, ..., this]
detectors = ["regex_checksum", "dual_ner"]   # names from DETECTOR_REGISTRY

[policy]
block_payloads = true
dlp_guard = true
oracle_mode = "any_local_finding"

[policy.fail_policy]
pre_pass_down = "fail-closed"
```

- **`name`** — required, non-empty.
- **`extends`** — ordered parent ids. `resolve_extends` flattens the DAG to
  `[core, …, most-specific]`, guarded against cycles (`ProfileCycleError`) and
  depth > 8 (`ProfileDepthError`).
- **`detectors`** — names resolved through `DETECTOR_REGISTRY`
  (`profiles/registry.py`): `regex_checksum`, `dual_ner`, `ner_ru`, `ner_en`,
  `corp_ner` (network-backed — needs `CORP_NER_ENDPOINT`, excluded from `CODE`
  segments). An
  unknown name is a `ValueError` at build time. Adding a new algorithm = one
  in-tree `detectors/<name>.py` + one registry line + a contract test (see the
  `safe-extension-registry` skill).
- **`[policy]`** knobs — `size_threshold_bytes`, `block_payloads`, `dlp_guard`,
  `oracle_mode`, `allowed_providers`, `canary_patterns`, `retention_*`, and
  `[policy.fail_policy]`.
- **`oracle_mode`** — validated at parse time (same canonical forms as
  `CORP_LLM_ORACLE_TRIGGER`: `gazetteer_hit` | `any_local_finding` | `always` |
  `sampled:<pct>`); an unknown value is a `ProfileParseError`. The profiled
  request runs on the **broader** of this and the global
  `CORP_LLM_ORACLE_TRIGGER` — neither knob can narrow the other.
- **`[policy.fail_policy]`** and **`retention_*`** are parsed, merged and folded
  into the bundle fingerprint, but no runtime path reads them yet: the egress
  path is unconditionally fail-closed and retention comes from `team_config`.
  Setting them changes nothing today except the cache key.
- **`content_hash`** — optional; see integrity below.
- Any other key (e.g. `data_residency`) is advisory — `parse_manifest` ignores
  it and no code path reads it yet.

## Merge / precedence

Layers merge **monotone-tightening** (`PolicyKnobs.merge`) — composition only
ever *adds* redaction, preserving the no-originals-leak invariant (M1-14):

- `size_threshold_bytes` = min; `block_payloads` / `dlp_guard` = OR
- `allowed_providers` = intersection; `canary_patterns` = union
- `fail_policy` = most-closed; `oracle_mode` = highest-coverage
- `retention_*` = last-writer (non-security)

Gazetteer term collisions resolve highest-precedence-first (the more specific
layer wins the label).

## Team selection

A team selects profiles via `TeamConfig.profile_ids` (persisted in the
`team_config.profile_ids` column — see `upgrade.md`). `ProfileResolver.resolve_team`
reads it off the `AuthContext.team_id` the orchestrator already receives. Empty
`profile_ids` → the empty bundle (today's behavior: adds nothing).

```
# worked example: division-x extends ru-152fz extends core
# team.profile_ids = ("division-x",)  →  layers [core, ru-152fz, division-x]
```

`division-x` (`profiles/defaults/division-x/profile.toml`) tightens
`size_threshold_bytes` to 65536 and restricts `allowed_providers` to
`["anthropic"]`; the merge intersects that with the parent layers.

## Integrity

- **`content_hash`** — an order-independent SHA-256 over the bundle's other data
  files (`compute_content_hash`; `profile.toml` itself excluded).
  `verify_integrity` recomputes it at load and **fails closed** on mismatch
  (`ProfileIntegrityError`) — catches a bundle tampered against its own manifest,
  no external PKI needed.
- **Detached signature** — a gated no-op. `CORP_PROFILE_REQUIRE_SIGNATURE` is
  unset by default; setting it fails load closed (no offline PKI decision yet).
  Leave it unset.

## Linting

`profiles/lint.py` (`lint_bundle` / `lint_root` / `discover_profiles`) checks a
manifest parses, named detectors exist in `DETECTOR_REGISTRY`, term files and
`replace.md` parse, and `extends` resolves without cycles. Wire it into CI when
adding bundles.

## Live activation — bundles DO drive production

`bootstrap.py` builds a `ProfileResolver` (`:301`) and wraps the core
orchestrator in a `ProfileAwareOrchestrator` (`:320`). A bundle assigned to a
team via `TeamConfig.profile_ids` therefore **takes effect at runtime** — it can
tighten the size threshold, the allowed providers, the detector set, the
allowlist, the canary patterns and the oracle trigger for that team's traffic.

Treat adding a bundle as a production change, not a parse-only exercise. Lint it
first (above), then roll it out to one team before widening.

Two knobs are exceptions — `[policy.fail_policy]` and `retention_*` parse and
merge but have no runtime reader (see the `[policy]` notes above), so setting
them changes only the cache key.

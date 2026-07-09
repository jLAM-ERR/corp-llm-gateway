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
  (`profiles/registry.py`): `regex_checksum`, `dual_ner`, `ner_ru`, `ner_en`. An
  unknown name is a `ValueError` at build time. Adding a new algorithm = one
  in-tree `detectors/<name>.py` + one registry line + a contract test (see the
  `safe-extension-registry` skill).
- **`[policy]`** knobs — `size_threshold_bytes`, `block_payloads`, `dlp_guard`,
  `oracle_mode`, `allowed_providers`, `canary_patterns`, `retention_*`, and
  `[policy.fail_policy]`.
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

## Known follow-up — live activation

The pieces exist — `ProfileAwareOrchestrator`
(`sanitizer/profile_orchestrator.py`) and the `litellm_hook` support for it — but
the production composition root **does not wire them yet**: `bootstrap.py`
builds a plain `SanitizationOrchestrator` with no `ProfileResolver`. So bundles
parse, lint, resolve, and merge correctly, but they do **not** drive a live
gateway until `bootstrap` swaps in `ProfileAwareOrchestrator`. Adding a bundle
today is inert at runtime; validate it with the lint + resolver, not by
deploying.

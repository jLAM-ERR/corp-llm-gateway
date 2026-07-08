---
name: plugin-design
description: Design how corp-llm-gateway varies detection/policy by country, division, or regulatory regime — reusing the profiles/ bundles, extensions/ registry, and DETECTOR_REGISTRY seams — without forking core or executing untrusted code. Produces an extension-point map, options, a recommended hybrid, and a worked example. Use for "plugin architecture", "add a jurisdiction/country/division/regime profile", "how do we extend for X".
---

# Plugin / profile design

Design (or evaluate a proposed) extension for varying requirements by **country/jurisdiction, division, or regulatory regime** — without forking core and without running untrusted code on the leak-critical egress path. Output is a design document, not code.

## The core tension (state it every time)
Compliance wants per-jurisdiction/division variation to ship fast and config-only; the corp posture forbids untrusted-code execution on the egress path, demands auditability (CODEOWNERS review), and is air-gapped. So "plugin" here = **a declarative data bundle (a profile) layered over the core, plus a closed set of in-tree, security-reviewed detector algorithms selected BY NAME** — never third-party executable code loaded at runtime.

## Reuse these existing seams (do NOT invent parallels)
- **`profiles/`** package — `ProfileBundle`, `PolicyKnobs.merge` (monotone-tightening: size=min, flags=OR, providers=intersection, fail_policy=most-closed → composition only ADDS redaction, preserving M1-14), `ProfileLoader`/`FileProfileLoader`/`CachedProfileLoader`, `manifest.resolve_extends` (cycle+depth guarded), `ProfileResolver.resolve_team`.
- **`DETECTOR_REGISTRY`** (`profiles/registry.py`) — name→detector factory; `build_detectors(names)`; unknown → ValueError. New algorithm = one in-tree `detectors/<name>.py` + one registry line + a contract test.
- **`extensions/` registry** + **`auth/factory.py`** — the keyed-registry pattern (ADR-001); `safe-extension-registry` skill for the safety checklist.
- **`TeamConfig.profile_ids`** — the per-team selection field; resolved off `AuthContext.team_id`.
- **Gazetteer / Rules / Allowlist** — data-becomes-a-detector (`rules/gazetteer.py::from_dir`, `rules/defaults/*.txt`).

## Method
1. **Extension-point map** — walk the request lifecycle (`litellm_hook.CorpLlmGuardrail.pre_call` → `sanitizer/orchestrator.py`) and map each stage (payload classifier, replace.md rules, local detector floor, gazetteer, oracle trigger, allowlist, DLP egress, provider, audit, fail-policy) to what varies by country/division/regime and the seam that carries it.
3. **Options** — compare (a) pure declarative bundle, (b) code-plugin via in-tree registry, (c) Python `entry_points`. **Reject (c)** (arbitrary code on the leak path — supply-chain/audit violation). Recommend the **hybrid**: data bundles + in-tree named detectors.
4. **Composition & precedence** — layer `[core, jurisdiction, division, regime]`; monotone-tightening merge; gazetteer collisions feed highest-precedence-first; cache-key MUST fold the resolved-profile fingerprint (residency/cross-jurisdiction bleed).
5. **Selection/routing** — deployment-level (one Helm overlay + Redis per country, hard residency), team-level (`profile_ids`, soft), header-level (add-restriction-only, audited).
6. **Governance** — no runtime code injection; bundle hash now / detached signature when PKI lands (fail-closed on mismatch); emit `profile_ids` as a CONDITIONAL audit field; CODEOWNERS split (`profiles/**` → compliance, `detectors/**` + registry → security-eng).

## Output
- **Framing** (the tension above, in this repo's terms).
- **Extension-point map** table: lifecycle stage | what varies | current mechanism | proposed seam.
- **Options** (a/b/c) with corp-fit; lead with the recommended hybrid + why.
- **Worked example** — one concrete case end-to-end (e.g. "add RU-152ФЗ jurisdiction + division-X policy"): which bundle files/detectors are created, how selection + composition happen at request time.
- **Risks & open questions** for the DRI (residency topology, policy-merge sign-off, signing PKI, NER model plurality for non-RU locales).

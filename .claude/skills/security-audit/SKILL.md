---
name: security-audit
description: Whole-repo leak-surface security audit of corp-llm-gateway against its zero-leak criterion and the 6 CLAUDE.md invariants — sanitizer coverage, NEVER-gate, auth/tokens, placeholder bijection, egress/DLP. Produces CONFIRMED/SUSPECTED findings with file:line + fixes. Use for "security audit", "sec-audit", "find leak paths", "do the invariants still hold". For reviewing a pending diff instead, use the built-in security-review.
---

# Security audit

The product exists to prevent leaks; success = **zero confirmed leak incidents**. This audit hunts for places the code fails to uphold that. Output is a findings report, not code changes. Read `CLAUDE.md` ("Critical invariants — never weaken these") and `docs/security.md` first — those invariants ARE the threat model.

## Depth
- **Quick** (default): one focused pass over the priority areas below.
- **Thorough**: fan out parallel agents by area, then **adversarially verify** each finding with an independent skeptic agent (prompt it to REFUTE; keep only findings that survive). Use for "thorough"/"audit"/pre-GA. Prefer a few traced findings over a long speculative list.

## Priority areas
1. **M1-14 leak surfaces** — `tests/invariants/test_no_originals_leak.py` pins six (logger emissions, error bodies, exception traces, metric labels, forwarded headers, pod stdout). Hunt code paths touching user content that could reach any of them; don't assume the invariant holds because a test exists — look for surfaces the test doesn't cover.
2. **Sanitizer coverage** — `sanitizer/`, `detectors/`, `rules/`, `sanitizer/segmenter/`. Content shapes that bypass the walker (nested blocks, `tool_use.input`, message-level `tool_calls`, `system`, `document`, streaming deltas, code-fence tag spans); the fail-policy (does a detector failure fail-OPEN?); oversize handling (default must be fail-closed) + chunk-seam containment for UNBOUNDED regex patterns; conditional-oracle blind spots.
3. **NEVER-fields gate** — `audit/invariants.py` (`assert_no_never_fields` in `audit/logger.py` before every sink write) + the Vector VRL in `helm/.../configmap.yaml`. Can any path emit mapping/original/credentials? Is the gate recursive or top-level-only?
4. **Auth & tokens** — `tokens/`, `auth/`. BYOK `Authorization` passthrough (never logged/rewritten); `X-Corp-Auth` stripped from EVERY header location (not just `data["headers"]`); token validation; `rbac.py` alg/`aud`/`iss`; parameterized SQL in `schema.sql`/stores.
5. **Placeholder / mapping integrity** — length-descending substitution, `RequestPlaceholderAllocator` bijection, `storage/` MappingStore cross-conversation bleed, TTL. Profile cache-key must fold the resolved-profile fingerprint (else cross-jurisdiction cache bleed).
6. **Egress / DLP** — Stage-5 `DlpEgressGuard` (only ~5 secret regexes — emails/names/INN pass), Stage-0 classifier, NetworkPolicy + CoreDNS sinkhole. Can either stage be bypassed?
7. **General** — subprocess/eval/SQLi/template injection, SSRF in `corp_llm/` httpx client + `cli/proxy.py` (absolute-URI target), unsafe deserialization, secrets in code/logs/git, dependency risk.

Use `rg` heavily (`logging`, `print(`, `.format(`, f-strings near content vars, `except`, `subprocess`, `os.environ`). Read the implementations.

## Output
- **Summary** (2–3 sentences on posture).
- **Findings table**: ID | Severity (Crit/High/Med/Low/Info) | Title | Invariant/threat | File:line.
- **Per-finding** (Crit/High/Med): what it is, exact code path, concrete leak/exploit scenario, fix. Mark **CONFIRMED** (traced) vs **SUSPECTED**.
- **What's solid** — invariants/surfaces you verified ARE defended (so coverage is known).
- **Fix priority** — ordered.

Every finding needs a `file:line`; a finding without a code path is noise.

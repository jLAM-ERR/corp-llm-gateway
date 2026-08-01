# `replace.md` authoring guide

Plan ref: M8-4.

`replace.md` is the per-team rulebook for sanitization. The corp LLM
applies these rules to find and replace team-specific terms before any
request leaves the corp boundary.

## File location

By default, the gateway looks at `<rules-dir>/<team-id>.md`. The path
is configured in `team_config.replace_md_path`.

## Format

One rule per line. The grammar is strict — invalid lines reject with
the line number.

```
- ORIGINAL = REPLACEMENT
```

Notes:

- The separator is `=`. The legacy em-dash `→` (U+2192) is still
  accepted, so existing files keep working. Bare ASCII `->` is not a
  separator and errors at load time.
- **Quote any value containing `=`** in backticks — inside backticks the
  `=` is content, not the separator. A value containing a colon does
  **not** need quoting; only `=` (and the legacy `→`) are special.
- ORIGINAL and REPLACEMENT may be wrapped in backticks for clarity:

  ```
  - `Project Polaris` = `[CONFIDENTIAL_PROJECT]`
  ```

- Lines starting with `#` are comments. So are lines starting with
  `<!--` (HTML-style for editors that render markdown).
- Blank lines are ignored.
- Whitespace around tokens is stripped.
- Matching is a plain case-insensitive substring test — no word-boundary or
  identifier-anchor requirement, for a single-word source or a multi-word
  phrase alike. `kdir = [X]` also matches the `kdir` inside `mkdir`, not just
  a standalone `Kdir` or an identifier like `KdirService`; `project polaris =
  [X]` also matches inside `reproject polaristation`.
  **This changes behavior for existing dictionaries**: matching used to be
  case-sensitive; it is now case-insensitive, so upgrading widens matches —
  review your rules if a short, common source (e.g. `Acme`) should not also
  match `acme` or `ACME`.
- The REPLACEMENT text is applied verbatim. Matching is case-insensitive,
  but the output is always exactly the replacement you configured —
  there is no case-preserving transform of the replacement based on the
  matched input's case.
- On overlap, the **longer span wins**, whether it's a dictionary rule or a
  detector/NER/oracle finding — a rule no longer automatically overrides a
  longer overlapping finding (e.g. rule `Alice = [EMPLOYEE_001]` loses its
  span to a longer `Alice Smith` → `[PERSON_001]` finding, so the output is
  `Contact [PERSON_001] today`, not `Contact [EMPLOYEE_001] Smith today`).
  A rule still wins when its span is IDENTICAL to a finding's span.

## Example file

```markdown
# Team X replace.md
# Owner: alice@corp.lan

- `Project Polaris` = `[CONFIDENTIAL_PROJECT]`
- `Acme-Internal-CRM` = `[INTERNAL_TOOL]`
- `dr.smith@partnerlab.com` = `[PARTNER_CONTACT]`
- `BadgeID-XYZ-12345` = `[BADGE_ID]`

# Hostnames (colons are fine unquoted)
- `db-prod-13.corp.internal` = `[INTERNAL_HOST]`
- `redis-prod:6379` = `[INTERNAL_HOST]`
```

## Live updates

Per M1-15: rule updates take effect on cache eviction (5 min default).
Live conversations holding pre-update mappings continue to use them
until the conversation expires; new occurrences in the same
conversation pick up new rules.

You can force a refresh by reducing the team's cache TTL or by
restarting one gateway pod (rolling restart picks up new rules
without traffic loss).

## Authoring tips

- **Be specific**. `- foo = [BAR]` will replace `foo` case-insensitively as a
  plain substring — anywhere it appears, including inside a longer word — in
  every request. If `foo` appears legitimately in many contexts, the
  replacement breaks them.
- **Order doesn't matter for correctness**, but the engine sorts
  patterns by descending length before replacement (M1-9 invariant)
  to prevent shadowing.
- **Empty originals or replacements are rejected** — the parser
  errors out at load time.
- **Test in staging first**. Apply rules in
  `gateway-staging.corp.lan` and run a sample request through to
  confirm behavior before promoting.

## Common mistakes

| Mistake | Effect | Fix |
|---|---|---|
| Used bare `->` | Parse error at load time | Use `=` (legacy `→` also works) |
| Bare value contains `=` | Mis-splits at the `=` | Wrap the value in backticks |
| Pattern is too short or generic | Many false positives | Add quotes + more context |
| Multiple rules with same pattern | Last one wins | Don't do it |
| Comment line missing `#` prefix | Treated as a rule, parse fails | Add `#` |

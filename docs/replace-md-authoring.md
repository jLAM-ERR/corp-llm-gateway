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
- ORIGINAL → REPLACEMENT
```

Notes:

- The separator is the em-dash `→` (U+2192), **not** ASCII `->`.
  ASCII `->` is rejected explicitly so two visually-similar formats
  cannot coexist.
- ORIGINAL and REPLACEMENT may be wrapped in backticks for clarity:

  ```
  - `Project Polaris` → `[CONFIDENTIAL_PROJECT]`
  ```

- Lines starting with `#` are comments. So are lines starting with
  `<!--` (HTML-style for editors that render markdown).
- Blank lines are ignored.
- Whitespace around tokens is stripped.

## Example file

```markdown
# Team X replace.md
# Owner: alice@corp.lan

- `Project Polaris` → `[CONFIDENTIAL_PROJECT]`
- `Acme-Internal-CRM` → `[INTERNAL_TOOL]`
- `dr.smith@partnerlab.com` → `[PARTNER_CONTACT]`
- `BadgeID-XYZ-12345` → `[BADGE_ID]`

# Hostnames
- `db-prod-13.corp.internal` → `[INTERNAL_HOST]`
- `db-prod-14.corp.internal` → `[INTERNAL_HOST]`
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

- **Be specific**. `- foo → [BAR]` will replace every `foo` in every
  request. If `foo` appears legitimately in many contexts, the
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
| Used `->` instead of `→` | Parse error at load time | Replace with em-dash |
| Pattern is too short or generic | Many false positives | Add quotes + more context |
| Multiple rules with same pattern | Last one wins | Don't do it |
| Comment line missing `#` prefix | Treated as a rule, parse fails | Add `#` |

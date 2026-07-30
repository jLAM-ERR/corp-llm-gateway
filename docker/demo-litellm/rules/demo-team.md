# Demo team replace.md — reversible deterministic substitutions shared with ru-llm-proxy.
# Matching is case-insensitive; single-word sources also match identifier prefixes.
# Separator is '=' (quote any value containing '=').
# '→' is still accepted for legacy files. See docs/replace-md-authoring.md for the full spec.
- `kdir` = `companynameabc`
- `betadirect` = `companynameabd`
- `beta direct` = `company name abe`
- `zephyr ledger` = `confidential project acn`
- `db-legacy-7` = `internalhostaco`

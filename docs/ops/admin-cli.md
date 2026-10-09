# `gateway-admin` reference

The operator CLI. Installed with the package (`pyproject` entry point
`gateway-admin`). Command groups: `db`, `team`, `token`, `extensions`, `config`,
`sanitize`.

## RBAC

Mutating verbs require the `gateway:operator` claim on an RS256 JWT (see
`upgrade.md`). Read verbs run ungated.

- Token source: `--token <JWT>`, else `CORP_GATEWAY_ADMIN_TOKEN`.
- Verification needs `CORP_GATEWAY_OIDC_KEY` + `CORP_GATEWAY_OIDC_AUDIENCE` +
  `CORP_GATEWAY_OIDC_ISSUER`; any missing → denied (fail-closed).
- `CORP_GATEWAY_RBAC=0` bypasses the check entirely (local dev only). Only the
  exact value `0` does; `false`, `no` or `off` leave RBAC on. `CORP_ENV` has no
  effect on it.

RBAC-gated verbs: `db init`, `team create` / `set-rules` / `set-retention`,
`token issue` / `revoke`, `extensions enable` / `disable`. Denial prints
`error: gateway:operator role required` and exits 2.

`db`, `team` and `token` require Postgres (`CORP_LLM_PG_DSN` + the `postgres`
extra); without a DSN they exit 2 with a clear message.

## `db`

```
gateway-admin db init
```

```
$ gateway-admin db init
db init: schema applied (corp_tokens, team_config)
```

Applies the token and team-config schemas (the `schema.sql` files the wheel
ships) to the database `CORP_LLM_PG_DSN` names. Idempotent: re-running it on an
initialised database exits 0 and keeps every row.

Errors (all exit 2):

- No `CORP_LLM_PG_DSN`, or the `postgres` extra missing or broken: a named
  message (`error: db init requires Postgres: set CORP_LLM_PG_DSN`,
  `error: db init requires asyncpg: install the 'postgres' extra`).
- A connection or SQL failure: the error type only
  (`error: db init failed: ConnectionRefusedError`). The DSN can carry a
  password, so it is never printed. A pooler that refuses the connection's
  keepalive startup parameters gives `StartupParameterRejectedError`.
- Another `db init` holds the lock past the wait (below):
  `error: db init failed: another db init holds the lock (LockNotAvailableError)`.
- A table lock not granted within `lock_timeout` (below):
  `error: db init failed: LockNotAvailableError`. Re-run it when the table is free.

Each schema file is applied in its own transaction, the token schema first.
Each transaction takes the advisory lock
`pg_advisory_xact_lock(7165071359132066409)` (`0x636F72705F646269`, "corp_dbi"),
so concurrent runs take turns instead of deadlocking. Settings and locks are
transaction-scoped (`set_config(..., true)`, no session `SET`) and no statement
uses bound parameters, so `db init` also works through a transaction-mode
pooler such as PgBouncer and leaves nothing set on the server session. Like the
gateway's pools, it sends the TCP keepalive startup parameters: PgBouncer needs
them in `ignore_startup_parameters` (`configuration.md`, "PgBouncer in front of
Postgres"; `runbook.md`), else `db init` fails with
`StartupParameterRejectedError`.

`lock_timeout` bounds each lock wait: 60 s for the advisory lock, then 1.5 s
for each table lock the schema statements need. Some of them
(`ALTER TABLE … ADD COLUMN IF NOT EXISTS`, the `team_config` trigger's drop and
create) take an `ACCESS EXCLUSIVE` lock even when nothing changes. While that
lock waits, new queries on the table queue behind it. The token schema locks
`corp_tokens` and then `team_config` in one transaction, so a token lookup can
queue behind both waits: about 3 s at most, under the gateway's 5 s lookup
timeout (the 1.5 s is derived from it). That bounds the lock waits only. Once a
lock is granted, the work done under it also blocks lookups and `lock_timeout`
does not bound it: on an upgrade from a schema without the OIDC columns, adding
them and building their indexes on a large `corp_tokens`; or a slow `COMMIT`
under synchronous replication. Run such an upgrade in a quiet window. On a busy
database `db init` fails fast with `LockNotAvailableError` (exit 2) rather than
stall the serving gateways; the failed transaction is rolled back, so re-running
it is safe.

## `team`

Manage per-team config (rules path, retention, fail-policy).

```
gateway-admin team create --team-id team-x --name "Team X" [--if-absent]
gateway-admin team set-rules --team-id team-x --from-file team-x.replace.md
gateway-admin team set-retention --team-id team-x --hot-days 90 --cold-years 7
gateway-admin team list [--json]
gateway-admin team show --team-id team-x [--json]
```

```
$ gateway-admin team create --team-id team-x --name "Team X"
team created: team-x

$ gateway-admin team list
TEAM_ID  NAME    HOT_DAYS  COLD_YEARS  REPLACE_MD
team-x   Team X  90        7           -
```

`set-rules` / `set-retention` / `show` on an unknown team exit 2
(`error: unknown team 'team-x'`); `create` on an existing team exits 2.
With `--if-absent` it exits 0 instead and changes nothing
(`team exists: team-x (unchanged)`), so a setup script can re-run it. The
create is one atomic insert, so two concurrent `create` runs never overwrite an
existing team's settings.

## `token`

Issue, revoke, and list corp tokens.

**`token issue` is break-glass only.** Developers get their corp token from
`scripts/install.sh`, which signs them in to Keycloak and calls
`POST /internal/issue-token` (`install.md`). Use `token issue` when that path is
down or for a service account. A CLI-issued token carries no Keycloak identity:
it does not count toward the per-developer cap
(`CORP_GATEWAY_ISSUE_MAX_ACTIVE`) or the issuance interval. `token revoke --user`
revokes both kinds; for an issued token the user is the Keycloak
`preferred_username` (or `sub`, if the claim is absent —
`CORP_GATEWAY_ISSUE_OIDC_USER_CLAIM`).

```
gateway-admin token issue --user alice --team team-x [--scopes a,b] [--ttl-days 30] [--value VALUE] [--json]
gateway-admin token revoke --user alice
gateway-admin token list [--user alice] [--json]
```

```
$ gateway-admin token issue --user alice --team team-x
issued corp token for user=alice team=team-x
token: ct_9f3c1a...
expires: 2026-08-07T12:00:00+00:00

$ gateway-admin token revoke --user alice
revoked 2 token(s) for user=alice

$ gateway-admin token list --user alice
TOKEN      USER   TEAM    SCOPES  EXPIRES     REVOKED
ct_9f3c1a…  alice  team-x  -       2026-08-07  no
```

`token list` masks a generated `ct_` token to its first 8 chars and a chosen
(`--value`) token fully, as `***`. Only generated tokens start with `ct_`:
`--value` refuses that prefix. Revocation is bound to the ≤60 s
`AuthMiddleware` cache (see `runbook.md`).

`--ttl-days` must be at least 1 and stay within the year 9999, on every
`token issue`; otherwise it is a usage error (exit 2).

### `token issue --value`

`--value VALUE` stores a value you choose instead of a random `ct_…` token, for
a fixed team token such as the Local setup's `LOCAL_TEAM_TOKEN`. It is not the
global `--token` (the operator JWT); RBAC works the same with or without it.

```
gateway-admin token issue --user local --team local --value "$LOCAL_TEAM_TOKEN" --ttl-days 36500 [--json]
```

```
$ gateway-admin token issue --user local --team local --value "$LOCAL_TEAM_TOKEN"
issued corp token for user=local team=local (value from --value)
expires: 2026-11-08T12:00:00+00:00
```

Pass the value through a variable (set from an env file or `read -rs`), never
typed on the command line: a literal lands in your shell history. Even through a
variable it is in the process's argv, visible to `ps` on the host, while the
command runs. A misspelled flag (say `--vlaue`) makes argparse print the
unrecognised arguments, the value among them, to stderr: check the command
before you run it.

- The command never prints or logs the value (its argv is another matter, above).
  Plain output has no `token:` line; `--json` reports `user_id`, `team_id`, `scopes` and `expires_at` only.
- Usage errors (exit 2, before any store call): a value that is not 16-256
  printable ASCII characters (0x21-0x7E: no spaces, control or non-ASCII
  characters), a value starting with `ct_` (reserved for generated tokens), or a
  bad `--ttl-days` (above). The message names the flag and the reason, never
  the value.
- Re-running with the same value, user and team replaces the row's
  `expires_at`, `issued_at` and `scopes`, so it is safe to repeat. Pass the same
  `--scopes` again: re-issuing without it resets them to the default (none).
- A value held by another user or team is refused (exit 2); the row is left as
  it is. Pick a new value.
- A revoked value is refused (exit 2): issuing it again would clear the
  revocation. Pick a new value instead.
- The revoked check and the write are two steps, not one transaction: a
  `token revoke` that runs while the same value is being re-issued can be
  overwritten by the re-issue. Check `token list` after revoking such a value.
- The owner check is two steps as well (lookup, then upsert): two `token issue`
  runs of the same value for different users or teams at the same time can both
  pass the check, and the last write wins. Issue a value from one place only.
- `token list` shows a chosen value as `***`, never a prefix of it.

## `extensions`

Inspect the registered extension set (audit sinks, providers, detectors, …).
Read verbs (`list` / `inspect` / `health`) are ungated; the registry is
populated from the provider registry plus the configured audit sink.

```
gateway-admin extensions list [--kind KIND] [--json]
gateway-admin extensions inspect KIND:NAME [--json]
gateway-admin extensions health [--json]
gateway-admin extensions enable KIND:NAME [--team T] [--rollout off|canary|on]   # RBAC
gateway-admin extensions disable KIND:NAME [--team T]                            # RBAC
```

```
$ gateway-admin extensions list
KIND        NAME       VERSION  API_VERSION  FAIL-POLICY
audit_sink  stdout     1        1            continue
provider    anthropic  1        1            fail-closed
provider    corp-vllm  1        1            fail-closed

$ gateway-admin extensions health
EXTENSION             HEALTH  FAIL-POLICY  DETAIL
audit_sink:stdout     OK      continue     -
provider:corp-vllm    OK      fail-closed  reachable
```

`extensions health` exits **nonzero** if any `fail-closed` extension is
unhealthy — CI/probe-usable. `inspect` on an unknown ref exits 2.

> `enable` / `disable` are RBAC-gated but currently raise `NotImplementedError`:
> there is no extension-state store yet (a tracked follow-up). They validate the
> ref exists and the caller's role first.

## `config check`

Validate the resolved config and probe dependencies. Nonzero exit on any
problem — use it as a pre-deploy gate or an initContainer.

```
gateway-admin config check [--no-probe] [--routes] [--json]
```

```
$ gateway-admin config check
config: OK
DEPENDENCY  STATUS  DETAIL
postgres    OK      reachable
redis       OK      reachable
corp-llm    OK      reachable (HTTP 200)
```

```
$ gateway-admin config check
config: INVALID
  - CORP_LLM_ENDPOINT: required — set the env var or add it to the config file ...
$ echo $?
1
```

`--no-probe` validates config only (skips the Postgres / Redis / corp-LLM
reachability probes). `--json` emits a machine-readable report and still sets
the exit code.

It runs the same resolvers the entrypoint's boot check runs, so it also reports:

- **issuance** — a partial or out-of-range `CORP_GATEWAY_ISSUE_*` set, an
  issuance audience equal to the operator audience, HTTP issuer/JWKS URLs under
  `CORP_ENV=prod`, issuance without `CORP_LLM_PG_DSN`, and (with issuance on)
  the missing `oidc` / `postgres` extras and an unreadable or non-PEM
  `CORP_LLM_CA_BUNDLE`;
- **the in-flight cap** — any of the five `CORP_LLM_*` capacity keys out of
  range, and `CORP_LLM_MAX_INFLIGHT=0` under `CORP_ENV=prod`;
- **route-gate extras** — see `--routes` below;
- **litellm DEBUG** — `LITELLM_LOG=DEBUG`, `DETAILED_DEBUG` or
  `litellm_settings.set_verbose` in litellm's config (litellm logs the original
  request before any pre-call hook; the boot refuses to arm, exit 70), unless
  `CORP_LLM_ALLOW_LITELLM_DEBUG=1`, which is itself a problem under
  `CORP_ENV=prod`.

It does not check the `corp_tokens` schema; the boot does, and exits 78 on it
(`upgrade.md`).

`--routes` also prints the effective route-gate table — row counts per verdict,
the REWRITTEN routes (the only ones whose body the guardrail rewrites), and every
`CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH` entry the operator added:

```
$ gateway-admin config check --no-probe --routes
config: OK

route gate: 551 exact + 362 regex litellm rows, 6 gateway rows (no off switch). The verdict counts below cover those litellm + gateway rows; operator extras are listed separately.
VERDICT      ROWS
PASSTHROUGH  32
REWRITTEN    8
REFUSE       879

REWRITTEN (the only routes whose body the guardrail rewrites):
  POST /chat/completions
  ...

CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH:
  (none)
```

A malformed extra, or one naming a route the table refuses, prints `INVALID: …`
on stderr and makes the whole check exit nonzero — the same value already fails
`validate()`, and the gateway exits 78 on it at boot. See
[`../security.md`](../security.md) §14 for what the gate refuses and why.

## `sanitize`

Show BEFORE/AFTER redaction for a prompt against the live cascade (diagnostics).

```
gateway-admin sanitize "text with a secret" [--team-id default] [--model M] [--json]
```

An oversize payload is refused (fail-closed, F1) and prints `BLOCKED: payload N
bytes exceeds the …-byte threshold`.

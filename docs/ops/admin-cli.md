# `gateway-admin` reference

The operator CLI. Installed with the package (`pyproject` entry point
`gateway-admin`). Command groups: `team`, `token`, `extensions`, `config`,
`sanitize`.

## RBAC

Mutating verbs require the `gateway:operator` claim on an RS256 JWT (see
`upgrade.md`). Read verbs run ungated.

- Token source: `--token <JWT>`, else `CORP_GATEWAY_ADMIN_TOKEN`.
- Verification needs `CORP_GATEWAY_OIDC_KEY` + `CORP_GATEWAY_OIDC_AUDIENCE` +
  `CORP_GATEWAY_OIDC_ISSUER`; any missing → denied (fail-closed).
- `CORP_GATEWAY_RBAC=0` bypasses the check entirely (local dev only).

RBAC-gated verbs: `team create` / `set-rules` / `set-retention`,
`token issue` / `revoke`, `extensions enable` / `disable`. Denial prints
`error: gateway:operator role required` and exits 2.

`team` and `token` require Postgres (`CORP_LLM_PG_DSN` + the `postgres` extra);
without a DSN they exit 2 with a clear message.

## `team`

Manage per-team config (rules path, retention, fail-policy).

```
gateway-admin team create --team-id team-x --name "Team X"
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

## `token`

Issue, revoke, and list corp tokens.

```
gateway-admin token issue --user alice --team team-x [--scopes a,b] [--ttl-days 30] [--json]
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

`token list` masks each token to its first 8 chars. Revocation is bound to the
≤60 s `AuthMiddleware` cache (see `runbook.md`).

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
gateway-admin config check [--no-probe] [--json]
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

## `sanitize`

Show BEFORE/AFTER redaction for a prompt against the live cascade (diagnostics).

```
gateway-admin sanitize "text with a secret" [--team-id default] [--model M] [--json]
```

An oversize payload is refused (fail-closed, F1) and prints `BLOCKED: payload N
bytes exceeds the …-byte threshold`.

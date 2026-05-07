# RBAC matrix

Plan ref: M3-8 (Langfuse roles), M2-5 (admin CLI), M8-5.

Three roles, mapped through Keycloak claims. Per the rev-3 plan, this
is intentionally flat — Keycloak Authorization Services (UMA) is
deferred to v2.

## Roles

| Role | Keycloak claim | Granted to |
|---|---|---|
| **Operator** | `gateway:operator` | Platform engineering on-call rotation |
| **Auditor** | `gateway:auditor` | Security, compliance, SRE leads |
| **Developer** | (no claim — default) | All authenticated corp devs |

## Permissions

| Capability | Developer | Auditor | Operator |
|---|---|---|---|
| Send requests through the gateway | ✓ | ✓ | ✓ |
| `gateway-admin team create` | – | – | ✓ |
| `gateway-admin team set-rules` | – | – | ✓ |
| `gateway-admin team set-retention` | – | – | ✓ |
| `gateway-admin token revoke` | – | – | ✓ |
| Read own audit records (Langfuse self-trace) | ✓ | ✓ | ✓ |
| Read all team's audit records (Langfuse) | – | ✓ | – |
| Read S3 audit cold storage | – | ✓ (read-only) | ✓ (read/write for ops) |
| View SIEM alerts | – | ✓ | ✓ |
| Modify Vector / Helm config | – | – | ✓ |
| Modify NetworkPolicy / CoreDNS sinkhole | – | – | ✓ |
| Mapping reveal (break-glass) | – | – | – (deferred to v2) |

## "No mapping reveal" in v1

Per the plan's deferred-to-v2 list and the risks table: even Operators
and Auditors cannot recover the original from a placeholder in v1.

Workflow when reveal is needed (rare): wait inside the 10h Cache A
TTL window — the gateway-side mapping is still in Redis and can be
inspected by an operator with direct Redis access. Past 10h, the
mapping is gone and reveal is impossible.

The break-glass / dual-control reveal workflow lands in v2 in the
`corp-llm-gateway-breakglass` repo.

## How the claims are issued

Keycloak admin grants the `gateway:operator` or `gateway:auditor`
claim to user accounts. The gateway's auth middleware reads these
claims off the OIDC token during the device-flow exchange (M2-3) and
stores them in `corp_tokens.scopes`.

The `gateway-admin` CLI (M2-5) gates each subcommand on
`scopes` containing `gateway:operator`. CLI-time auth uses the same
corp token as request-time auth — there's no separate admin token.

## Enforcement

Enforced at three layers:

1. **gateway-admin CLI** — checks scope before executing. Returns
   non-zero exit code on auth failure.
2. **Langfuse** — Keycloak claim mapping configured per M3-8.
3. **S3** — IAM policy bound to Keycloak group membership.

## Audit-of-audit logging

Every Operator action (token revoke, set-rules, set-retention) is
itself logged to the audit pipeline with `command_name` and
`actor_user_id`. Auditors can read this trail. This is structurally
the "audit-of-audits" hook; the Object-Locked separate bucket
mentioned in the deferred-to-v2 list is the next-iteration upgrade.

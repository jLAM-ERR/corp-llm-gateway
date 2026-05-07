# Operations runbook

Plan ref: M8-2.

## Daily operations

### Deploying a new version

1. Tag the release: `git tag v0.x.y && git push origin v0.x.y`.
2. CI builds the wheel and Helm artifacts on the tag.
3. Apply to staging: `helm upgrade --install gw helm/corp-llm-gateway -f values-staging.yaml --version v0.x.y`.
4. Wait for `/healthz/ready` green on all 3 pods.
5. Run the deep-check: `curl https://gateway-staging.corp.lan/healthz/sanitization`.
6. Promote to prod with the same command against `values-prod.yaml`.

### Rolling back

```
helm rollback gw <revision>
```

Revisions list: `helm history gw`. Default Helm keeps the last 10.

### Pinning the LiteLLM version

`values.yaml: litellm.versionPin`. Bump only after staging upgrade gate passes (per the plan's M0-7 task).

## Incident playbook

The fail-policy matrix in the plan (M4) is the source of truth for what "should" happen on each component failure. When reality disagrees, that's the bug.

### Corp-LLM unreachable

Symptom: `gateway_failure{component="corp_llm"}` rises; requests return 503 with `error_code="E_CORP_LLM_DOWN"`.

Behavior: fail-closed (per matrix). Gateway is healthy; the dependency isn't.

Action:
1. Confirm corp-LLM is actually down (curl its endpoint from a gateway pod).
2. If yes: page corp-LLM team. The gateway will recover automatically when corp-LLM recovers.
3. If no: investigate gateway-side connectivity (NetworkPolicy, DNS).

### Pre-pass engine crashed

Symptom: `gateway_failure{component="pre_pass"}` rises; requests succeed but slower (corp-LLM-only path).

Behavior: continue (per matrix).

Action:
1. Scale pre-pass replicas: `kubectl scale -n corp-llm-gateway deploy/pre-pass --replicas=2`.
2. Investigate underlying CPU pod (OOM? worker crash? unusually large payload exceeding the M1-11 threshold?).

### Redis cluster down

Symptom: requests return 503 with `error_code="E_REDIS_DOWN"`.

Behavior: fail-closed (per matrix). No mappings = no de-sanitization = unsafe to serve.

Action:
1. `kubectl -n redis get pods` — at least 2/3 should be up. If 1 down: cluster is fine; transient.
2. If all down or split-brain: failover via Redis sentinel.

### Vector buffer at 50% (alert)

Symptom: SIEM alert "vector_buffer_50pct".

Behavior (default): fail-closed at 100% (per matrix).

Action:
1. Check downstream sinks. Likely Langfuse or SIEM is down/slow.
2. If a single sink is down: the others continue. Pin which one via Vector metrics.
3. If buffer fills: requests start returning 503. Revisit fail-policy override at team level if business-critical.

### Token revocation didn't take effect immediately

Symptom: `gateway-admin token revoke --user alice` ran, but Alice's traffic still flows for ≤ 60 s.

Behavior: 60 s revocation cache (per `AuthMiddleware`). Documented offboarding lag.

Action: wait 60 s. If still flowing after 60 s, escalate — that's a real bug.

### Audit invariant test fails in CI

Symptom: `tests/invariants/test_no_originals_leak.py` red.

Behavior: build blocks. M1-14 is regression-grade — never bypass.

Action:
1. The file lists six leak surfaces. Find which assertion fired.
2. Trace back to the regression. Most common: someone added `logger.info("...%s", finding.text)` somewhere.
3. Fix the leak; the test pins the surface.

### Audit completeness < 100% in monthly check

Symptom: monthly S3 row count < non-failed request count for the month.

Behavior: violates the non-negotiable acceptance criterion.

Action:
1. Diff the missing records: which team_id, which time window?
2. Check Vector metrics at that window — buffer fill, sink errors.
3. If unexplained: this is incident-grade. Page security + DRI.

## Common operations

### Add a new team

```
gateway-admin team create --team-id team-x --name "Team X"
gateway-admin team set-rules --team-id team-x --from-file team-x.replace.md
gateway-admin team set-retention --team-id team-x --hot-days 90 --cold-years 7
```

### Revoke a fired employee's tokens

```
gateway-admin token revoke --user alice
```

Effect bound to ≤ 60 s by the revocation cache. Within that window, Alice's tokens remain valid.

### Check what's in a team's `replace.md`

The path is in `team_config.replace_md_path`. Read directly from the file or query the `team_config` table.

## Useful kubectl

```
kubectl -n corp-llm-gateway get pods
kubectl -n corp-llm-gateway logs deploy/gateway -c litellm
kubectl -n corp-llm-gateway logs deploy/gateway -c vector
kubectl -n corp-llm-gateway exec -it deploy/gateway -c litellm -- python -m corp_llm_gateway.cli.admin team --help
```

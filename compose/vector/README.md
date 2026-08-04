# Vector — the audit pipeline

Mounted read-only at `/etc/vector` in the `vector` service. Full rationale in
`compose/README.md` "Audit pipeline"; this file is the map.

| File | Loaded | What it is |
|---|---|---|
| `vector.yaml` | always | source + transforms + the Langfuse sink |
| `sinks-s3.yaml` | `CORP_AUDIT_S3_ENABLED=1` | durable archive of the flat audit record |
| `sinks-siem.yaml` | `CORP_AUDIT_SIEM_ENABLED=1` | SIEM forwarder; endpoint still an open item |

Vector merges every `--config` file it is given into one topology, so the two
optional files attach to `vector.yaml`'s `audit_only` transform and inherit the
NEVER-fields gate. Point a new sink at `audit_only` (or at a transform
downstream of it), never at `parse` or the source.

Two things in `vector.yaml` must not be edited casually:

- **`never_fields_gate` and `audit_only`** are copied verbatim from
  `docker/demo-vector/vector.yaml` and match
  `helm/corp-llm-gateway/templates/configmap.yaml`. They are defence-in-depth
  for CLAUDE.md invariant #2. Do not paraphrase or restructure them.
- **the Langfuse sink's disk buffer** (`when_full: block`) and its retry
  policy. `compose/README.md` tells operators that a full `langfuse-redis`
  surfaces as an ingestion 5xx "which Vector retries"; that is only true with
  those settings. An in-memory buffer or `drop_newest` turns it into silent
  audit loss.

Validate before deploying:

```
docker run --rm -v "$PWD:/etc/vector:ro" \
  -e CORP_LANGFUSE_PUBLIC_KEY=x -e CORP_LANGFUSE_SECRET_KEY=x \
  timberio/vector:0.53.0-alpine validate --no-environment /etc/vector/vector.yaml
```

Note that Vector interpolates `${VAR}` inside YAML **comments** too, so a
commented-out example naming an unset variable fails config load.

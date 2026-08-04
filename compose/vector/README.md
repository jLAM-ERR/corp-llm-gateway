# Vector — the audit pipeline

Mounted read-only at `/etc/vector` in the `vector` service. Full rationale in
`compose/README.md` "Audit pipeline"; this file is the map.

| File | Loaded | What it is |
|---|---|---|
| `vector.yaml` | always | source + transforms + the Langfuse sink |
| `sinks-s3.yaml` | `CORP_AUDIT_S3_ENABLED=1` | durable archive of the flat audit record |
| `sinks-siem.yaml` | `CORP_AUDIT_SIEM_ENABLED=1` | SIEM forwarder; endpoint still an open item |

Vector merges every `--config` file it is given into one topology, so the two
optional files attach to `vector.yaml`'s `audit_only` transform and inherit both
the container identity boundary and the NEVER-fields gate. Point a new sink at
`audit_only` (or at a transform downstream of it), never at `parse`, at
`never_fields_gate` or at the source.

The full path every record takes, in order:

```
container_logs -> gateway_container_only -> unwrap_docker_envelope
  -> merge_partial_lines -> to_message -> parse -> never_fields_gate
  -> audit_only -> <sinks>
```

The topology is linear and every sink hangs off `audit_only` or below, so no
record reaches a sink without passing `never_fields_gate`.

Three things in `vector.yaml` must not be edited casually:

- **`never_fields_gate` and `audit_only`** are copied verbatim from
  `docker/demo-vector/vector.yaml` and match
  `helm/corp-llm-gateway/templates/configmap.yaml`. They are defence-in-depth
  for CLAUDE.md invariant #2. Do not paraphrase or restructure them.
- **`gateway_container_only`** is the container identity boundary. The file
  source reads every container's log on the host; this filter is what limits
  the pipeline to the `litellm` container, by requiring the docker json-file
  `attrs` stamp that `compose/docker-compose.yml` produces from that service's
  `com.corp-llm-gateway.audit-source` label plus its
  `logging.options.labels`. The label, the log option and this filter are one
  mechanism in three files — change them together. `audit_only` is a schema
  gate and is **not** a substitute: `request_id` and `redaction_count` are two
  ordinary keys any co-located container can print. The filter is a
  **misconfiguration guard, not a security boundary** — the label is public and
  reproducible by anyone who can start a container here. See `compose/README.md`
  "Container identity boundary" and `docs/security.md` §8.2.
- **the source glob**, which covers the active log *and* its numbered rotations
  (`…-json.log.[0-9]`, `[0-9][0-9]`). Dropping the rotated patterns silently
  loses every record that rotated away while Vector was down. Widening them to
  `…-json.log.*` is worse, not better: it pulls in gzipped rotations Vector
  cannot decompress. The other half of that pair lives in
  `compose/docker-compose.yml`: the `litellm` service pins `compress: "false"`
  in its `logging.options`, because a daemon whose own default is json-file
  *with* `compress=true` would otherwise have that option merged in and every
  rotation would arrive as `.gz` — unreadable here, and silent audit loss. Glob
  and log-opt are one mechanism; do not relax either.
- **the Langfuse sink's disk buffer** (`when_full: block`) and its retry
  policy. `compose/README.md` tells operators that a full `langfuse-redis`
  surfaces as an ingestion 5xx "which Vector retries"; that is only true with
  those settings. An in-memory buffer or `drop_newest` turns it into silent
  audit loss. It holds for transient failures only — a `401` is not retriable
  in Vector 0.53 and the record is dropped, which is why
  `compose/README.md` "First login" prescribes the first-boot order it does.

Beware when editing a sink's `request:` block: Vector rejects an unknown key at
the top level of a sink but **silently ignores** one inside `request:`, so a
misspelled or invented retry option validates cleanly and does nothing.

Validate before deploying:

```
docker run --rm -v "$PWD:/etc/vector:ro" \
  -e CORP_LANGFUSE_PUBLIC_KEY=x -e CORP_LANGFUSE_SECRET_KEY=x \
  timberio/vector:0.53.0-alpine validate --no-environment /etc/vector/vector.yaml
```

Note that Vector interpolates `${VAR}` inside YAML **comments** too, so a
commented-out example naming an unset variable fails config load.

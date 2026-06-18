## Context

Fixture level: expanded

Repair intensity: high

Project profile: NHMS

Issue #384 is a production recovery bug in the manual retry path. The original
scheduler submission for a shared source-cycle download carries durable runtime
roots such as `object_store_root` and `object_store_prefix`. Manual retry
currently rebuilds a minimal manifest from `PipelineJob` identity and omits
those roots. The Slurm rendering path then falls back `OBJECT_STORE_ROOT` to
`workspace_dir`, so a retry can succeed while writing raw bundles to the wrong
tree.

## Goals / Non-Goals

Goals:
- Preserve `object_store_root` and `object_store_prefix` for
  `download_source_cycle` manual retry submissions.
- Preserve published-artifact root/prefix when present so retry manifests do not
  regress shared runtime context.
- Ensure rendered sbatch env exports the durable object-store root, not the
  workspace fallback, when the original runtime root is known.
- Fail closed with stable, actionable retry submission failure evidence if a
  shared source-cycle retry cannot resolve required roots safely.
- Record redacted runtime-root resolution evidence so operators can verify
  where retry output will land.

Non-goals:
- No database schema or enum migration.
- No automatic repair of raw files already written under the workspace root.
- No stale failed-stage supersession or candidate-readiness behavior; that is
  issue #386.
- No frontend or display-readonly behavior change.
- No Slurm gateway route shape change beyond manifest/env validation behavior
  required for this retry contract.

## Decisions

1. Resolve retry runtime roots before Slurm submission.
   - Rationale: retry submission is the last control-plane point that knows the
     logical failed job and can fail before producing an unsafe Slurm job.
   - Alternative rejected: rely on Slurm template fallback. That is the bug.

2. Prefer original job manifest/evidence runtime roots, then explicit runtime
   configuration, and only allow workspace fallback for non-shared-source jobs.
   - Rationale: shared source-cycle downloads write durable raw inputs consumed
     by later stages; the object-store root is part of the source-cycle identity.
   - Alternative rejected: always derive roots from environment without checking
     the failed job. That can submit a retry under a different runtime than the
     original scheduler job without evidence.

3. Keep runtime-root evidence redacted and bounded.
   - Rationale: roots and prefixes can include private paths or URI-like values;
     operators need path class and resolved value without leaking credentials.

4. Treat missing or unsafe required roots as a submission failure, not a queued
   retry.
   - Rationale: an unsafe retry can corrupt recovery evidence by writing to the
     wrong location while looking successful.

## Risk Packs Considered

- Public API / CLI / script entry: selected - retry endpoint/API response
  surfaces expose submission success/failure.
- Config / project setup: selected - behavior depends on production
  `WORKSPACE_ROOT`, `OBJECT_STORE_ROOT`, object prefix, and published roots.
- File IO / path safety / overwrite: selected - wrong root causes durable raw
  files to be written to the wrong local tree.
- Schema / columns / units / field names: selected - retry manifest and event
  evidence fields must remain stable.
- Auth / permissions / secrets: selected - retry errors and root evidence must
  redact credentials and private URI secrets.
- Concurrency / shared state / ordering: selected - manual retry guard must not
  create duplicate active retries or leave stale pending markers after
  fail-closed submission.
- Resource limits / large input / discovery: not selected - no directory or
  large-input discovery semantics change.
- Legacy compatibility / examples: selected - non-source-cycle retries and
  existing retry API contracts must continue working.
- Error handling / rollback / partial outputs: selected - failure before Slurm
  submission must leave stable `submission_failed` evidence and no Slurm job.
- Release / packaging / dependency compatibility: not selected - no dependency
  or packaging change.
- Documentation / migration notes: selected - operators need recovery guidance
  for legacy wrong-root retries.

Domain packs:
- Slurm production lifecycle / mock-vs-real parity: selected - rendered sbatch
  env must match production roots.
- Run manifest / QC provenance: selected - retry manifest/evidence must bind to
  the same source-cycle runtime identity.
- External hydro-met providers / snapshot reproducibility: selected - IFS raw
  bundles must land under the canonical source-cycle object-store prefix.
- Published NHMS artifacts / display identity: selected - published root/prefix
  must be preserved when present, but no display route change is in scope.
- Geospatial / CRS / basin geometry: not selected - no geometry behavior.
- Hydro-met time series / forcing windows: not selected - no forcing window
  semantics.
- SHUD numerical runtime / conservation / NaN: not selected - no runtime math.
- PostGIS / TimescaleDB domain behavior: not selected - no DB schema/query
  semantic change.

## Invariant Matrix

Governing invariant: Manual retry of a shared source-cycle download MUST submit
Slurm work with the same durable runtime-root contract as the original
production submission, or fail before Slurm submission with redacted actionable
evidence.

Source-of-truth identity/contract: `run_id`, `cycle_id`, `job_type`,
`pipeline_job_id`, `source_id`, `cycle_time`, `workspace_dir`,
`object_store_root`, `object_store_prefix`, `published_artifact_root`,
`published_artifact_uri_prefix`, retry count, Slurm submission payload, and
retry event evidence.

Surfaces:
- Producers: `RetryService._retry_submission_manifest`,
  retry runtime-root resolution helpers.
- Validators/preflight: retry submission validation and Slurm manifest/env
  validation.
- Storage/cache/query: `ops.pipeline_job` retry row and `ops.pipeline_event`
  retry/submission details.
- Public routes/entrypoints: `POST /api/v1/runs/{run_id}/retry` through the
  existing retry API.
- Frontend/downstream consumers: monitoring jobs table remains compatible; no
  frontend change.
- Failure paths/rollback/stale state: missing roots, unsafe root mismatch,
  submission failure, duplicate active retry guard.
- Evidence/audit/readiness: retry manifest, rendered sbatch env, retry events,
  runbook recovery notes.

Regression rows:
- Shared IFS download retry with split roots -> manifest contains
  `object_store_root=/scratch/.../object-store` and rendered sbatch exports
  that value, not the workspace root.
- Shared IFS download retry with object-store prefix -> retry manifest and
  events preserve the prefix and raw manifest URI remains under the configured
  prefix.
- Shared source-cycle retry with missing required object-store root -> retry is
  `submission_failed`, no Slurm job is submitted, and evidence has a stable
  error code plus redacted root-resolution details.
- Original failed job has secret-bearing root/prefix evidence -> persisted
  retry event/API error redacts credentials, tokens, and signed query values.
- Non-`download_source_cycle` manual retry -> existing retry behavior remains
  compatible and does not require new source-cycle root fields.
- Duplicate active manual retry -> existing conflict guard still prevents a
  second submission.
- Existing Slurm manifest with explicit `object_store_root` -> gateway/template
  continues exporting it unchanged.

## Boundary-Surface Checklist

- Shared helper roots: retry runtime-root resolver and redaction helper use.
- Public entrypoints: retry API and Slurm gateway submit.
- Read surfaces: failed pipeline job identity and optional prior manifest/event
  evidence.
- Write/delete/overwrite surfaces: retry pipeline job/event rows and source
  raw output root chosen by sbatch env.
- Staging/publish/rollback surfaces: source-cycle raw bundle staging under
  object-store; no publish/delete behavior change.
- Producer/consumer evidence boundaries: retry manifest -> sbatch env ->
  worker output path -> retry event evidence.
- Stale-state/idempotency boundaries: manual retry guard and submission failure
  state.
- Unchanged downstream consumers: scheduler readiness, retry/cancel API
  response contract, frontend monitoring display.

## Risks / Trade-offs

- Risk: roots cannot be reconstructed for older jobs. Mitigation: fail closed
  with a stable error and runbook remediation instead of writing to workspace.
- Risk: root evidence leaks credentials or private URI values. Mitigation:
  redact persisted event details and API errors; test secret-bearing inputs.
- Risk: stricter retry validation could block useful non-source retries.
  Mitigation: make the new required-root contract specific to
  `download_source_cycle` shared source retries and preserve legacy behavior for
  other job types.

# Design

## Fixture Level

Fixture level: expanded  
Repair intensity: high  
Project profile: NHMS

Why:

- Public operator CLI/script entrypoint.
- Production DB delete workflow over Timescale-backed data.
- Large-input/resource, rollback, resumability, and audit evidence risks.
- Must preserve flood quality truth from `flood.run_product_quality`.

## Change Surface

- New or updated ops script/CLI for return-period no-curve cleanup.
- Shared DB helper code only if needed to keep SQL/testability contained.
- Tests for dry-run manifest, apply batching, filters, guardrails, and quality
  preservation.
- CI selector mapping if a new script/test surface needs targeted coverage.

## Must Preserve

- `return_period_result` rows with non-null `return_period` or non-null
  `warning_level`.
- Explicit `flood.run_product_quality` rows and unavailable state for affected
  runs.
- QHH/q_down readiness and API behavior introduced by #489.
- Existing migration and integration-test behavior; no production DB mutation in
  tests unless using local/scratch fixtures.

## Must Add

- Default dry-run mode that never mutates the DB.
- Explicit apply mode with bounded `batch_size`, optional `sleep_interval`, and
  per-batch committed progress records.
- Filters: `run_id`, `basin_version_id`, `source_id`, `cycle_time` start/end.
- Manifest output with target totals, quality flag counts, run counts,
  max-over-window counts, chunk/time distribution where available, quality
  summary coverage, affected runs, per-batch records, DB size stats, and
  resumable cursor information.
- Guardrail that apply mode refuses to delete candidate rows for any run missing
  explicit `run_product_quality`; there is no force override for missing
  explicit quality.
- Stable batch identity: rows are ordered and resumed by the current
  `return_period_result` key tuple `(run_id, river_network_version_id,
  river_segment_id, duration, valid_time, max_over_window)`. Deletes must
  recheck the same candidate predicate plus the same operator filters against
  those identities.

## Risk Packs Considered

- Public API / CLI / script entry: selected - operator command flags, default
  dry-run, apply guardrails, exit codes.
- Config / project setup: selected - `DATABASE_URL`, batch/sleep/default mode
  handling.
- File IO / path safety / overwrite: selected - manifest output path must not
  clobber unexpectedly and should be bounded/atomic where applicable.
- Schema / columns / units / field names: selected - SQL predicates and manifest
  field names must match DB contracts.
- Auth / permissions / secrets: selected - logs/manifests must not leak DB
  credentials from URLs.
- Concurrency / shared state / ordering: selected - batch commits, resumable
  cursor, race-safe predicates.
- Resource limits / large input / discovery: selected - no full-table materialize
  in memory, bounded batch size, chunk/time aggregation, and graceful fallback
  when Timescale chunk metadata is absent.
- Legacy compatibility / examples: selected - old rows are removed only after
  explicit quality exists.
- Error handling / rollback / partial outputs: selected - partial apply remains
  auditable and resumable; failed batch does not erase previous evidence.
- Release / packaging / dependency compatibility: not selected - no dependency
  upgrade expected.
- Documentation / migration notes: selected - operator notes in command help or
  README/work summary must state DELETE does not reclaim disk immediately and
  that #491 owns index/vacuum/repack work.
- PostGIS / TimescaleDB domain behavior: selected - chunk/time distribution and
  hypertable size statistics must handle Timescale unavailable gracefully.
- Published NHMS artifacts / display identity: selected - `/runs` and q_down
  artifacts are out of deletion scope.

## Invariant Matrix

Governing invariant: only derived no-curve flood rows with preserved explicit
run-level quality may be deleted; every candidate, skipped row, and committed
batch remains auditable and resumable.

Source-of-truth identity/contract: `flood.run_product_quality.run_id` and
candidate predicate on `flood.return_period_result`; batch row identity is
`(run_id, river_network_version_id, river_segment_id, duration, valid_time,
max_over_window)`.

Surfaces:

- Producers: `workers/flood_frequency/return_period.py` already stopped new
  no-curve empty writes; unchanged except for optional helper reuse.
- Validators/preflight: cleanup command validates filters, candidate predicate,
  quality coverage with no override, batch size, manifest path, apply
  confirmation, and absence of schema/index DDL.
- Storage/cache/query: `flood.return_period_result`,
  `flood.run_product_quality`, catalog size queries, Timescale chunk metadata
  if available.
- Public routes/entrypoints: new script/CLI; API routes remain unchanged except
  tests may assert unchanged behavior.
- Frontend/downstream consumers: q_down/latest product and flood unavailable
  contracts from #489 remain unchanged.
- Failure paths/rollback/stale state: failed apply batch writes/returns a stable
  error with completed batch evidence and a resumable cursor.
- Evidence/audit/readiness: dry-run/apply manifest with before/after counts,
  per-batch records, affected runs, quality coverage, and size statistics.

Regression rows:

- Dry-run with candidate rows and complete quality -> manifest reports totals
  and deletes 0 rows.
- Apply with complete quality and small batch size -> deletes only candidate
  rows by stable key tuple, commits batches, and leaves quality unavailable
  summary intact.
- Candidate run missing explicit quality -> apply refuses before deletion and
  reports missing quality coverage.
- Rows with non-null `return_period` or `warning_level` -> never selected or
  deleted.
- Partial failure after a committed batch -> manifest/error identifies completed
  batches and next cursor.
- Filtered dry-run/apply -> counts, missing-quality guard, batch selection,
  delete predicates, and manifest affected-run lists use the identical filtered
  candidate set.
- Timescale metadata absent -> manifest still reports time-bucket distribution
  and records chunk distribution as unavailable rather than failing.
- Static destructive-scope check -> implementation contains no schema/index DDL
  and no object-store `/runs` deletion path.

## Boundary Checklist

- Shared helper roots: keep DB helper narrow; do not make API routes import ops
  scripts.
- Public entrypoints: CLI flags and defaults must be stable and documented in
  help/tests.
- Read surfaces: all summary queries are scoped by the same filters/predicate.
- Write/delete surfaces: deletes use the exact candidate predicate, the same
  filters used by dry-run summaries, and bounded row identity selection; no
  `DROP INDEX`, `REINDEX`, `VACUUM FULL`, or object-store deletion belongs here.
- Producer/consumer evidence boundaries: manifest must redact credentials and
  bind counts to filters and timestamps.
- Stale-state/idempotency boundaries: rerun after partial cleanup should report
  remaining rows, not double count deleted rows.
- Unchanged downstream consumers: q_down/API flood-unavailable behavior remains
  covered by existing #489 tests.

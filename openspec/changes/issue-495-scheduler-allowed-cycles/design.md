## Change Surface

- `services/orchestrator/scheduler.py`
  - `ProductionSchedulerConfig`
  - scheduler runtime config evidence
  - source cycle boundary flooring
  - cycle discovery, evidence, candidate, blocked candidate, and backfill gap
    accounting
- `tests/test_production_scheduler.py`
- `tests/test_scheduler_backfill.py`
- `infra/env/compute.example`

## Must Preserve

- Existing source-specific availability discovery remains compatible with GFS,
  IFS, and configured production sources.
- Disallowed cycles are observable as excluded discovery evidence, not silently
  erased from operator audit.
- Allowed `00/12` cycles keep existing dedupe, completion status, readiness,
  forcing, and submit behavior.
- Backfill gap counts continue to represent only cycles the business scheduler
  may actually process.
- Existing non-production tests that rely on `00/06/12/18` can opt into those
  hours through explicit config rather than relying on production defaults.

## Must Add / Change

- Add `allowed_cycle_hours_utc` to `ProductionSchedulerConfig`.
  - Env: `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC`.
  - Parsing: comma-separated integers, stripped whitespace, deduped and sorted.
  - Validation: each value must be in `0..23`; empty configured values or
    invalid tokens fail closed.
- Runtime config evidence includes `allowed_cycle_hours_utc`.
- `_floor_to_source_cycle_boundary()` accepts allowed hours and floors to the
  latest allowed cycle boundary at or before the input time.
- `_discover_cycles()` filters disallowed cycles before:
  - dedupe / latest-source collapse
  - completion status lookup
  - backfill gap counts
  - candidates / blocked_candidates
  - canonical readiness provider calls
  - forcing producer and orchestrator submit eligibility
- Disallowed `06/18` cycles emit evidence rows with:
  - `selection_status=excluded`
  - `selection_reason=cycle_hour_not_allowed`
  - source/model/cycle identity retained for audit
- Update `infra/env/compute.example` with
  `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC=0,12`.

## Risk Packs Considered

- Public API / CLI / script entry: not selected - no new public command.
- Config / project setup: selected - new production env config and parsing.
- File IO / path safety / overwrite: not selected - no filesystem writes beyond
  env example edits.
- Schema / columns / units / field names: not selected - no DB schema change.
- Auth / permissions / secrets: not selected - no new credential handling.
- Concurrency / shared state / ordering: selected - scheduler discovery order,
  gap accounting, and submit side effects must use the same filtered set.
- Resource limits / large input / discovery: selected - disallowed cycles should
  be filtered before expensive readiness/producers/submit paths.
- Legacy compatibility / examples: selected - tests and non-production paths may
  need explicit four-cycle config where existing behavior is intentional.
- Error handling / rollback / partial outputs: selected - invalid config must
  fail closed and disallowed cycles must not partially enter downstream state.
- Release / packaging / dependency compatibility: not selected - no dependency
  change.
- Documentation / migration notes: selected - production env example changes.
- Hydro-met time series / forcing windows: selected - cycle-hour selection is a
  forecast-window boundary.
- PostGIS / TimescaleDB domain behavior: not selected - no DB query semantics
  change.
- Slurm production lifecycle / mock-vs-real parity: selected - filtered cycles
  must not submit jobs.
- Run manifest / QC provenance: selected - filtered cycles must not create
  readiness or run evidence that looks like a candidate.
- Published NHMS artifacts / display identity: not selected - no publish/display
  changes.

## Invariant Matrix

Governing invariant: A production scheduler cycle may enter candidate,
backfill-gap, readiness, forcing, or submit paths only if its UTC hour is in
`ProductionSchedulerConfig.allowed_cycle_hours_utc`.

Source-of-truth identity/contract:
`ProductionSchedulerConfig.allowed_cycle_hours_utc`, parsed from
`NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC`, applied to every discovered cycle's
timezone-normalized `cycle_time.hour`.

Surfaces:

- Producers: source availability adapters and scheduler source-window discovery
  returning cycle candidates, including synthetic tests that return `06/18`.
- Validators/preflight: config parsing, `_floor_to_source_cycle_boundary()`, and
  `_discover_cycles()` allowed-hour filter.
- Storage/cache/query: cycle completion status and backfill gap accounting.
- Public routes/entrypoints: scheduler runtime config evidence only; no API
  route change.
- Frontend/downstream consumers: scheduler evidence readers and operator audit
  of candidates/blocked/gaps.
- Failure paths/rollback/stale state: invalid env value fails closed; filtered
  cycles emit excluded evidence and no downstream side effects.
- Evidence/audit/readiness: runtime config evidence, source cycle evidence,
  candidate lists, blocked candidate lists, readiness-provider calls, submit
  calls.

Regression rows:

- Env `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC=0,12` plus adapter/source window
  returning `00/06/12/18` -> only `00/12` cycles can become candidates.
- Disallowed `06/18` cycles -> evidence rows have
  `selection_status=excluded` and
  `selection_reason=cycle_hour_not_allowed`.
- Backfill audit containing only disallowed `06/18` gaps -> `gap_count`,
  `available_gap_count`, and `unavailable_gap_count` do not include them, and
  candidates/blocked candidates are empty for those cycles.
- Disallowed `06/18` cycles -> canonical readiness provider, forcing producer,
  and submit are not called for those cycles.
- Disallowed `06/18` cycles mixed with allowed `00/12` cycles -> disallowed
  rows do not consume dedupe keys, do not replace allowed cycles, and cannot
  change latest/oldest selected allowed-cycle collapse results.
- Backfill completion status provider present -> the provider is not called for
  disallowed `06/18` cycles and receives only allowed `00/12` cycles.
- Current time near `06/18` -> `_floor_to_source_cycle_boundary()` floors to the
  nearest prior allowed `00/12` boundary, not to `06/18`.
- Invalid/empty configured allowed-cycle env -> config construction fails with a
  stable error before scheduler work starts.
- Explicit four-cycle test config `0,6,12,18` -> existing four-cycle discovery
  behavior remains available where tests/non-production paths intentionally need
  it.

## Boundary-Surface Checklist

- Shared helper roots: cycle-hour parsing and floor helper should have one
  source of truth.
- Public entrypoints: scheduler config/env construction and runtime evidence.
- Read surfaces: source window discovery, completion status, readiness provider.
- Write/delete/overwrite surfaces: none directly; submit and producer side
  effects must not receive disallowed cycles.
- Staging/publish/rollback surfaces: no publish mutation.
- Producer/consumer evidence boundaries: excluded-cycle evidence must not be
  mixed with candidate or blocked-candidate evidence.
- Stale-state/idempotency boundaries: repeated discovery must produce stable
  filtered gap/candidate counts.
- Unchanged downstream consumers: allowed-cycle candidate behavior and existing
  scheduler tests with explicit config.

## Review Focus

- The scheduler hard gate is in scheduler discovery, not only adapter defaults.
- The filter occurs before any costly or side-effectful per-cycle path.
- Gap counts and candidate lists cannot include `06/18` under `0,12`.
- Evidence remains auditable for excluded cycles.
- Config parsing is deterministic and fails closed for invalid input.

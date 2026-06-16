## Implementation Tasks

- [x] Add `allowed_cycle_hours_utc` to `ProductionSchedulerConfig` with env
      parsing/validation for `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC`.
- [x] Include allowed cycle hours in scheduler runtime config evidence.
- [x] Update `_floor_to_source_cycle_boundary()` to use configured allowed
      hours.
- [x] Filter disallowed cycle hours in `_discover_cycles()` before dedupe,
      completion status, gap accounting, candidate/blocked selection, readiness,
      forcing, or submit paths.
- [x] Emit excluded evidence for disallowed cycles with
      `cycle_hour_not_allowed`.
- [x] Update focused scheduler/backfill tests for `00/12` filtering, evidence,
      gap counts, no downstream side effects, floor boundary behavior, invalid
      config, and explicit four-cycle compatibility.
- [x] Update `infra/env/compute.example` with
      `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC=0,12`.
- [x] Pass `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC` through
      `infra/compose.compute.yml` compute service environment.

## Required Evidence

- [x] `uv run --no-sync pytest -q tests/test_production_scheduler.py tests/test_scheduler_backfill.py`
- [x] `uv run --no-sync ruff check services/orchestrator/scheduler.py tests/test_production_scheduler.py tests/test_scheduler_backfill.py`
- [x] Config parse case: `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC=0,12` ->
      config stores `(0, 12)` or equivalent sorted immutable sequence and
      runtime config evidence emits `[0, 12]`.
- [x] Config parse case: duplicates/whitespace such as `12, 0,12` -> sorted
      deduped `[0, 12]`.
- [x] Config failure case: empty configured value, non-integer token, or value
      outside `0..23` -> stable fail-closed exception before scheduler work.
- [x] Direct config failure case: non-`int` values and `bool` are rejected
      before scheduler work.
- [x] Discovery case: source window returns `00/06/12/18` and allowed hours
      are `0,12` -> selected cycles include only `00/12`.
- [x] Dedupe-order case: disallowed `06/18` cycles mixed with allowed `00/12`
      cycles do not consume dedupe keys, do not replace allowed cycles, and do
      not affect latest/oldest allowed-cycle collapse results.
- [x] Legacy single-slot case: with `max_cycles_per_source=1`, disallowed
      `18` cannot replace the latest allowed `12` cycle.
- [x] Completion-status case: `cycle_completion_status_provider` or equivalent
      completion lookup is not called for disallowed `06/18` cycles and receives
      only allowed `00/12` cycles.
- [x] Evidence case: filtered `06/18` rows include
      `selection_status=excluded` and
      `selection_reason=cycle_hour_not_allowed`.
- [x] Evidence hour case: excluded cycle evidence reports the UTC hour derived
      from `cycle_time`, matching the hour used by the allowed-cycle gate.
- [x] Backfill case: `06/18` are not counted in `gap_count`,
      `available_gap_count`, or `unavailable_gap_count`.
- [x] Candidate case: `06/18` do not appear in `candidates` or
      `blocked_candidates`.
- [x] Side-effect guard: disallowed `06/18` does not call canonical readiness
      provider, forcing producer, or submit path.
- [x] Floor boundary case: current time near `06/18` floors to the nearest prior
      allowed `00/12` boundary.
- [x] Compatibility case: explicit allowed hours `0,6,12,18` preserves tests
      or paths that intentionally exercise four-cycle behavior.
- [x] QHH scheduler fixture compatibility: 06Z-only readiness fixtures use
      explicit `0,6,12,18` allowed-cycle config.

## Documentation Evidence

- [x] `infra/env/compute.example` contains
      `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC=0,12`.
- [x] `infra/compose.compute.yml` passes
      `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC` into compute containers.
- [x] Compose interpolation preserves an explicitly empty
      `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC` instead of replacing it with
      `0,12`, so scheduler config validation can fail closed.
- [x] Env comment explains that scheduler allowed hours are the hard gate for
      business candidate/backfill selection.

## Non-Goals / Out of Scope

- [ ] No adapter cycle-hour env support in this issue; #497 owns it.
- [ ] No strict warm-start enforcement in this issue; #496 owns it.
- [ ] No production command execution in CI.
- [ ] No DB schema/data migration.
- [ ] No frontend changes.

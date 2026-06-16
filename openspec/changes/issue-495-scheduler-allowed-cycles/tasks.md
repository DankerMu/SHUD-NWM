## Implementation Tasks

- [ ] Add `allowed_cycle_hours_utc` to `ProductionSchedulerConfig` with env
      parsing/validation for `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC`.
- [ ] Include allowed cycle hours in scheduler runtime config evidence.
- [ ] Update `_floor_to_source_cycle_boundary()` to use configured allowed
      hours.
- [ ] Filter disallowed cycle hours in `_discover_cycles()` before dedupe,
      completion status, gap accounting, candidate/blocked selection, readiness,
      forcing, or submit paths.
- [ ] Emit excluded evidence for disallowed cycles with
      `cycle_hour_not_allowed`.
- [ ] Update focused scheduler/backfill tests for `00/12` filtering, evidence,
      gap counts, no downstream side effects, floor boundary behavior, invalid
      config, and explicit four-cycle compatibility.
- [ ] Update `infra/env/compute.example` with
      `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC=0,12`.

## Required Evidence

- [ ] `uv run --no-sync pytest -q tests/test_production_scheduler.py tests/test_scheduler_backfill.py`
- [ ] `uv run --no-sync ruff check services/orchestrator/scheduler.py tests/test_production_scheduler.py tests/test_scheduler_backfill.py`
- [ ] Config parse case: `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC=0,12` ->
      config stores `(0, 12)` or equivalent sorted immutable sequence and
      runtime config evidence emits `[0, 12]`.
- [ ] Config parse case: duplicates/whitespace such as `12, 0,12` -> sorted
      deduped `[0, 12]`.
- [ ] Config failure case: empty configured value, non-integer token, or value
      outside `0..23` -> stable fail-closed exception before scheduler work.
- [ ] Discovery case: source window returns `00/06/12/18` and allowed hours
      are `0,12` -> selected cycles include only `00/12`.
- [ ] Dedupe-order case: disallowed `06/18` cycles mixed with allowed `00/12`
      cycles do not consume dedupe keys, do not replace allowed cycles, and do
      not affect latest/oldest allowed-cycle collapse results.
- [ ] Completion-status case: `cycle_completion_status_provider` or equivalent
      completion lookup is not called for disallowed `06/18` cycles and receives
      only allowed `00/12` cycles.
- [ ] Evidence case: filtered `06/18` rows include
      `selection_status=excluded` and
      `selection_reason=cycle_hour_not_allowed`.
- [ ] Backfill case: `06/18` are not counted in `gap_count`,
      `available_gap_count`, or `unavailable_gap_count`.
- [ ] Candidate case: `06/18` do not appear in `candidates` or
      `blocked_candidates`.
- [ ] Side-effect guard: disallowed `06/18` does not call canonical readiness
      provider, forcing producer, or submit path.
- [ ] Floor boundary case: current time near `06/18` floors to the nearest prior
      allowed `00/12` boundary.
- [ ] Compatibility case: explicit allowed hours `0,6,12,18` preserves tests
      or paths that intentionally exercise four-cycle behavior.

## Documentation Evidence

- [ ] `infra/env/compute.example` contains
      `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC=0,12`.
- [ ] Env comment explains that scheduler allowed hours are the hard gate for
      business candidate/backfill selection.

## Non-Goals / Out of Scope

- [ ] No adapter cycle-hour env support in this issue; #497 owns it.
- [ ] No strict warm-start enforcement in this issue; #496 owns it.
- [ ] No production command execution in CI.
- [ ] No DB schema/data migration.
- [ ] No frontend changes.

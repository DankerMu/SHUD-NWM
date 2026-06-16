## Implementation Tasks

- [ ] Add scheduler-compatible adapter cycle-hour env parsing for GFS.
- [ ] Add scheduler-compatible adapter cycle-hour env parsing for IFS.
- [ ] Keep adapter default cycle hours at `(0, 6, 12, 18)` when env is unset.
- [ ] Add/adjust tests proving GFS/IFS env `0,12` only discovers/probes `00/12`.
- [ ] Add/adjust tests for dedupe/sort and invalid env fail-fast.
- [ ] Add/adjust tests for direct `cycle_hours_utc` config dedupe/sort and
      rejection of boolean, string, and float entries.
- [ ] Preserve scheduler hard-gate test coverage for artificial `06/18` cycles.
- [ ] Update `infra/env/compute.example` with adapter hours and cycle-lag
      comments, keeping scheduler allowed hours and strict warm-start visible.
- [ ] Update production runbook documentation for object-store vs published
      artifact locations and strict warm-start handling.
- [ ] Add verification commands/SQL for forcing packages, run output, state
      snapshots, scheduler evidence, and published display artifacts.

## Required Evidence

- [ ] GFS env `GFS_CYCLE_HOURS_UTC=0,12` -> discovered cycle hours `[0, 12]`.
- [ ] IFS env `IFS_CYCLE_HOURS_UTC=0,12` -> discovered cycle hours `[0, 12]`.
- [ ] GFS/IFS env `12,0,12` -> config cycle hours `(0, 12)`.
- [ ] GFS/IFS direct config `cycle_hours_utc=(12, 0, 12)` -> config cycle hours
      `(0, 12)`.
- [ ] GFS/IFS invalid env cases reject empty tokens, non-integers, out-of-range
      hours, and blank input.
- [ ] GFS/IFS direct config `cycle_hours_utc=(True,)`, `("12",)`, or `(12.5,)`
      rejects with stable `ValueError`.
- [ ] Unset GFS/IFS env keeps `(0, 6, 12, 18)`.
- [ ] Explicit IFS four-cycle config keeps the existing `06/18` lead-time policy
      available for compatibility.
- [ ] Existing scheduler hard-gate test still proves `06/18` cannot reach
      candidates/readiness/forcing/submit when allowed hours are `0,12`.
- [ ] `infra/env/compute.example` contains
      `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC=0,12`,
      `GFS_CYCLE_HOURS_UTC=0,12`, `IFS_CYCLE_HOURS_UTC=0,12`, and
      `NHMS_REQUIRE_FORECAST_WARM_START=true`.
- [ ] Runbook states `forcing/` and `runs/` are under shared object-store and
      `published/` is display-only.
- [ ] Runbook includes strict warm-start checks and repair guidance.
- [ ] `uv run --no-sync pytest -q tests/test_gfs_adapter.py tests/test_ifs_adapter.py tests/test_production_scheduler.py`
- [ ] `uv run --no-sync ruff check workers/data_adapters/gfs_adapter.py workers/data_adapters/ifs_adapter.py tests/test_gfs_adapter.py tests/test_ifs_adapter.py tests/test_production_scheduler.py`
- [ ] `openspec validate issue-497-adapter-cycle-hours-runbook --strict --no-interactive`
- [ ] `git diff --check`

## Documentation Evidence

- [ ] The runbook provides node-22/node-27 path examples for:
      `OBJECT_STORE_ROOT`, `NHMS_OBJECT_STORE_COPYBACK_ROOT`,
      `NHMS_PUBLISHED_ARTIFACT_ROOT`, and `/ghdc/data/nwm/published`.
- [ ] The runbook includes SQL/shell snippets for:
      `met.forcing_version`, `hydro.state_snapshot`, `ops.pipeline_job`,
      shared object-store `forcing/` and `runs/`, scheduler evidence, and
      published display artifacts.
- [ ] Runbook verification snippets define the input tuple
      `<source_id, cycle_time, basin_version_id, model_id, run_id>` and expected
      outputs:
      `met.forcing_version` row has `status='ready'` and a `forcing/...`
      package URI; `hydro.state_snapshot` row has `status='ready'`,
      `stage='state_save_qc'`, and a state snapshot URI for the previous allowed
      cycle; `ops.pipeline_job` rows show scheduler/forcing/run stages with
      non-failed terminal status; shared object-store shell checks find
      `forcing/.../manifest.json` and `runs/...` outputs; scheduler evidence
      shows allowed-cycle decisions; published checks find only display tiles,
      logs, or display manifests under `published/...`.

## Non-Goals / Out of Scope

- [ ] No scheduler hard-gate semantic change.
- [ ] No strict warm-start runtime semantic change.
- [ ] No production command execution.
- [ ] No DB migration.
- [ ] No frontend change.

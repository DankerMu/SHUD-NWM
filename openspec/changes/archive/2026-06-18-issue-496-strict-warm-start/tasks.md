## Implementation Tasks

- [x] Add strict forecast warm-start config and env parsing for
      `NHMS_REQUIRE_FORECAST_WARM_START`.
- [x] Update production compute env example to set
      `NHMS_REQUIRE_FORECAST_WARM_START=true`.
- [x] Implement strict exact-successor state selection without latest-usable
      fallback.
- [x] Enforce strict blocker behavior for `state_manager is None`, exact state
      missing, unusable exact state, QC failure, lineage/source/package mismatch,
      and `lead_hours != 12`.
- [x] Ensure direct forecast entrypoints fail before manifest write,
      hydro_run create/update, or Slurm submit on strict warm-start failure.
- [x] Ensure `orchestrate_cycle -> _apply_cohort_warm_start` validates
      scheduler-prefilled `init_state_*` fields under the same strict policy.
- [x] Preserve non-strict forecast and analysis fallback/cold-start behavior
      with explicit tests.
- [x] Carry strict success evidence consistently into run context, run manifest,
      cycle-stage basin entries, and scheduler-facing warm-start fields.

## Required Evidence

- [x] Strict success: `00 -> 12` forecast uses exact `lead_hours=12` state,
      manifest `initial_state.valid_time == cycle_time`, runtime
      `init_mode=3`, and no `quality=cold_start_no_state`.
- [x] Strict success: `12 -> next-day 00` forecast uses exact `lead_hours=12`
      state, manifest `initial_state.valid_time == cycle_time`, runtime
      `init_mode=3`, and no `quality=cold_start_no_state`.
- [x] Strict missing exact state returns
      `warm_start_successor_checkpoint_missing` before writing a manifest,
      creating/updating hydro_run, or submitting Slurm.
- [x] Strict `state_manager is None` returns
      `warm_start_successor_checkpoint_missing` before mutation.
- [x] Strict unusable exact state returns
      `warm_start_successor_checkpoint_unusable` before writing a manifest,
      creating/updating hydro_run, or submitting Slurm.
- [x] Strict QC failure returns `warm_start_successor_checkpoint_unusable`
      before writing a manifest, creating/updating hydro_run, or submitting
      Slurm.
- [x] Strict source/package-version/package-checksum mismatch returns
      `warm_start_lineage_mismatch` before writing a manifest,
      creating/updating hydro_run, or submitting Slurm.
- [x] Strict checksum evidence present on the state but absent from the target
      package identity returns `warm_start_lineage_mismatch`.
- [x] Strict `lead_hours != 12` returns `warm_start_lineage_mismatch` before
      writing a manifest, creating/updating hydro_run, or submitting Slurm.
- [x] Scheduler-prefilled invalid state is rejected under strict mode with
      `warm_start_lineage_mismatch` or
      `warm_start_successor_checkpoint_unusable` as appropriate, and writes no
      cycle-stage manifest, run manifest, hydro_run create/update, or Slurm
      submit; valid prefilled exact-successor state is accepted and preserved.
- [x] Scheduler-prefilled strict invalid matrix covers unusable, QC-fail,
      source mismatch, package version mismatch, package checksum mismatch,
      lead mismatch, malformed `init_state_valid_time`, non-mapping
      `init_state_lineage`, non-integer `lead_hours`, and URI-only/id-missing
      mismatch.
- [x] Non-strict scheduler-prefilled `init_state_*` fields remain preserved
      instead of being overwritten by latest-usable selection.
- [x] Non-strict forecast no-state/latest-usable fallback behavior remains
      covered.
- [x] Analysis warm-start latest-usable behavior remains covered, including
      `tests/test_analysis_pipeline.py::test_analysis_run_creation_uses_scenario_and_latest_init_state`.
- [x] `uv run --no-sync pytest -q tests/test_warm_start.py tests/test_warm_start_chaining.py tests/test_production_scheduler.py tests/test_analysis_pipeline.py::test_analysis_run_creation_uses_scenario_and_latest_init_state`
- [x] `uv run --no-sync ruff check services/orchestrator/chain.py services/orchestrator/chain_types.py services/orchestrator/chain_manifests.py services/orchestrator/scheduler.py packages/common/state_lineage.py tests/test_warm_start.py tests/test_warm_start_chaining.py tests/test_production_scheduler.py`

## Documentation Evidence

- [x] `infra/env/compute.example` contains
      `NHMS_REQUIRE_FORECAST_WARM_START=true`.
- [x] Env comment explains strict mode forbids cold/fallback forecast warm-start
      and requires exact successor state.

## Non-Goals / Out of Scope

- [ ] No adapter cycle-hour env support in this issue; #497 owns it.
- [ ] No scheduler allowed-cycle hard-gate changes in this issue; #495 owns it.
- [ ] No DB schema/data migration unless required by implementation evidence.
- [ ] No frontend changes.
- [ ] No production command execution in CI.

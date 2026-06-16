## Implementation Tasks

- [ ] Add strict forecast warm-start config and env parsing for
      `NHMS_REQUIRE_FORECAST_WARM_START`.
- [ ] Update production compute env example to set
      `NHMS_REQUIRE_FORECAST_WARM_START=true`.
- [ ] Implement strict exact-successor state selection without latest-usable
      fallback.
- [ ] Enforce strict blocker behavior for `state_manager is None`, exact state
      missing, unusable exact state, QC failure, lineage/source/package mismatch,
      and `lead_hours != 12`.
- [ ] Ensure direct forecast entrypoints fail before manifest write,
      hydro_run create/update, or Slurm submit on strict warm-start failure.
- [ ] Ensure `orchestrate_cycle -> _apply_cohort_warm_start` validates
      scheduler-prefilled `init_state_*` fields under the same strict policy.
- [ ] Preserve non-strict forecast and analysis fallback/cold-start behavior
      with explicit tests.
- [ ] Carry strict success evidence consistently into run context, run manifest,
      cycle-stage basin entries, and scheduler-facing warm-start fields.

## Required Evidence

- [ ] Strict success: `00 -> 12` forecast uses exact `lead_hours=12` state,
      manifest `initial_state.valid_time == cycle_time`, runtime
      `init_mode=3`, and no `quality=cold_start_no_state`.
- [ ] Strict success: `12 -> next-day 00` forecast uses exact `lead_hours=12`
      state, manifest `initial_state.valid_time == cycle_time`, runtime
      `init_mode=3`, and no `quality=cold_start_no_state`.
- [ ] Strict missing exact state returns
      `warm_start_successor_checkpoint_missing` before writing a manifest,
      creating/updating hydro_run, or submitting Slurm.
- [ ] Strict `state_manager is None` returns
      `warm_start_successor_checkpoint_missing` before mutation.
- [ ] Strict unusable exact state returns
      `warm_start_successor_checkpoint_unusable` before mutation.
- [ ] Strict QC failure returns `warm_start_successor_checkpoint_unusable`
      before mutation.
- [ ] Strict source/package/checksum mismatch returns
      `warm_start_lineage_mismatch` before mutation.
- [ ] Strict `lead_hours != 12` returns `warm_start_lineage_mismatch` before
      mutation.
- [ ] Scheduler-prefilled invalid state is rejected under strict mode with
      `warm_start_lineage_mismatch` or
      `warm_start_successor_checkpoint_unusable` as appropriate, and writes no
      cycle-stage manifest, run manifest, hydro_run create/update, or Slurm
      submit; valid prefilled exact-successor state is accepted and preserved.
- [ ] Non-strict forecast no-state/latest-usable fallback behavior remains
      covered.
- [ ] Analysis warm-start latest-usable behavior remains covered.
- [ ] `uv run --no-sync pytest -q tests/test_warm_start.py tests/test_warm_start_chaining.py tests/test_production_scheduler.py`
- [ ] `uv run --no-sync ruff check services/orchestrator/chain.py services/orchestrator/chain_types.py packages/common/state_lineage.py tests/test_warm_start.py tests/test_warm_start_chaining.py tests/test_production_scheduler.py`

## Documentation Evidence

- [ ] `infra/env/compute.example` contains
      `NHMS_REQUIRE_FORECAST_WARM_START=true`.
- [ ] Env comment explains strict mode forbids cold/fallback forecast warm-start
      and requires exact successor state.

## Non-Goals / Out of Scope

- [ ] No adapter cycle-hour env support in this issue; #497 owns it.
- [ ] No scheduler allowed-cycle hard-gate changes in this issue; #495 owns it.
- [ ] No DB schema/data migration unless required by implementation evidence.
- [ ] No frontend changes.
- [ ] No production command execution in CI.

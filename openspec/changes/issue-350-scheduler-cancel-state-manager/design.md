## Context

Fixture level: compact

Project profile: SHUD/NWM backend scheduler

Issue #350 is a narrow backend regression: `ProductionScheduler.run_once()` discovers active Slurm jobs, skips candidate submission because `cancel_active_slurm=True`, and then calls the downstream cancellation hook. That cancellation hook does not need warm-start state snapshot selection. The current default orchestrator constructor path still eagerly passes `StateManager.from_env()`, which requires `DATABASE_URL` and fails before cancellation.

## Goals / Non-Goals

Goals:
- Make cancel-active-Slurm pass work without `DATABASE_URL` when no replacement submission or state-selection work is needed.
- Preserve default orchestrator dependency construction for normal submission paths.
- Preserve the existing evidence contract: no replacement submission, cancellation evidence is recorded, and `state_manager is None` for cancel-only default-path tests.

Non-Goals:
- No redesign of warm-start state selection.
- No change to Slurm preflight requirements for actual submission.
- No test workaround that injects or mocks `DATABASE_URL` and hides the cancel-only contract.

## Design

Add a cancellation-specific orchestrator construction path or parameter that reuses the existing default configuration/repository/retry wiring but passes `state_manager=None` for cancel-only active Slurm cancellation. The normal `_orchestrator_for()` behavior remains the path for submission and continues to construct `StateManager.from_env()`.

## Risk Packs Considered

- Public API / CLI / script entry: selected - scheduler one-shot CI behavior is an operator-facing command path.
- Config / project setup: selected - the regression is caused by an unintended `DATABASE_URL` requirement.
- State machine / lifecycle transitions: selected - active Slurm cancellation must happen before active-cycle skip and must not submit replacements.
- Error handling / rollback / partial outputs: selected - cancellation gap evidence must still produce blocked status.
- Schema / database: not selected for implementation - no schema changes; the fix avoids DB access on a path that does not need it.

## Verification

- `uv run pytest tests/test_production_scheduler.py::test_cancel_active_slurm_calls_gateway_contract_without_replacement_submission tests/test_production_scheduler.py::test_cancel_active_slurm_runs_before_cycle_level_active_skip tests/test_production_scheduler.py::test_cancel_active_slurm_gap_blocks_top_level_cancelled_status -q`
- `uv run pytest tests/test_production_scheduler.py -m "not e2e and not grib and not integration" -q`
- `openspec validate issue-350-scheduler-cancel-state-manager --strict --no-interactive`

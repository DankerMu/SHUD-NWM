## Why

Issue #350 tracks a master CI regression in the production scheduler unit-test gate. The cancel-active-Slurm path is intended to cancel existing Slurm work without submitting replacement work and without constructing warm-start state-selection dependencies. After warm-start scheduler changes, the default orchestrator construction eagerly calls `StateManager.from_env()`, so cancel-only tests fail before cancellation whenever `DATABASE_URL` is intentionally absent.

## What Changes

- Preserve the cancel-only contract: active Slurm cancellation may construct the downstream orchestrator with repository and retry dependencies, but it must not require a database-backed `state_manager`.
- Keep normal submission/orchestration behavior unchanged: paths that need warm-start state selection continue to construct the real `StateManager`.
- Add/keep regression coverage for the three failing cancel-active-Slurm scenarios from #350.

## Capabilities

### Modified Capabilities

- `production-scheduler-orchestration`: clarifies that cancel-only active Slurm mutation does not require `DATABASE_URL` or state-selection setup.

## Impact

- Affects `services/orchestrator/scheduler.py` and focused scheduler tests only.
- No public API, database schema, frontend, or Slurm gateway contract change.

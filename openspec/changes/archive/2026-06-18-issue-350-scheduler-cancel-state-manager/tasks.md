## 1. Scheduler Fix

- [x] 1.1 Update cancel-active-Slurm default orchestrator construction so cancel-only paths do not call `StateManager.from_env()`.
- [x] 1.2 Keep normal submission/default orchestrator construction using the real state manager.
- [x] 1.3 Preserve cancellation evidence and no-replacement-submission behavior.

## 2. Verification

- [x] 2.1 Run the three #350 regression tests.
- [x] 2.2 Run focused production scheduler unit tests.
- [x] 2.3 Run OpenSpec strict validation.

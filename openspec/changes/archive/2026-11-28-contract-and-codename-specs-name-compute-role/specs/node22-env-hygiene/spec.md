## MODIFIED Requirements

### Requirement: The live scheduler env drift SHALL be classified against the loader's effective defaults
`infra/env/README.md` SHALL contain, outside the required-key block, a table "compute.scheduler-dbfree.env live-drift classification (2026-10-16)" with one row per key the live file on the retired node-22 host lacked at classification time (`NHMS_SCHEDULER_LEASE_GUARD_MODE`, `NHMS_SCHEDULER_RECONCILE_ABSENCE_SECONDS`, `NHMS_SCHEDULER_IDENTITY_BLOCKED_STREAK_LIMIT`, `NHMS_SCHEDULER_NO_PROGRESS_CIRCUIT_PASSES`, `NHMS_SCHEDULER_TIMING_LEVEL`, `NHMS_SCHEDULER_EVIDENCE_RETENTION_DAYS`, `NHMS_SCHEDULER_EVIDENCE_MAX_MB`) and columns key / status / effective value when unset / operator action, where status is `required`, `optional (effective default: <value>)` or `optional, example differs (example: <v>, default: <v>)`, and "effective value when unset" is what the scheduler config loader applies when only that key is absent. A row whose key has since been set live SHALL carry the bare assignment of the live value as its operator action, with the date recorded in a note under the table. Since #3146, `NHMS_SCHEDULER_LEASE_GUARD_MODE` is set live to `atomic` (the example value), and the legacy `NHMS_SCHEDULER_FILE_LOCK_GUARD_MODE` is no longer honoured: a legacy-only configuration fails closed. The required-key block SHALL be unchanged. `tests/test_node22_scheduler_env_drift_table.py` SHALL assert every table key exists in `compute.scheduler-dbfree.env.example` and that each stated effective default equals the loader's value with that key removed from the environment; keys classified `required` SHALL have an operator step in `docs/runbooks/node22-control-plane-manual-recovery.md`.

#### Scenario: Effective default matches the loader
- **WHEN** the table states `NHMS_SCHEDULER_LEASE_GUARD_MODE` is `optional, example differs (example: atomic, default: flock)`
- **THEN** the loader, with that key unset and no legacy key present, yields `flock`

#### Scenario: Legacy lease key fails closed
- **WHEN** `NHMS_SCHEDULER_FILE_LOCK_GUARD_MODE` is set and `NHMS_SCHEDULER_LEASE_GUARD_MODE` is unset
- **THEN** the lease guard loader raises an error naming `NHMS_SCHEDULER_LEASE_GUARD_MODE` instead of applying the legacy value or the default

#### Scenario: Required key has an operator step
- **WHEN** a table row is `required`
- **THEN** the recovery runbook contains a step that sets it

#### Scenario: Required-key block untouched
- **WHEN** `uv run pytest -q tests/test_env_templates.py` runs
- **THEN** it passes unchanged

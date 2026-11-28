# node22-env-hygiene Specification

## Purpose
Backup copies of the compute role's env files have a declared home outside `infra/env/`, the live scheduler env drift is classified against the loader's real defaults, and scheduler work directories do not pollute `git status`.

## Requirements

### Requirement: `.nhms-work/` SHALL be ignored by git
`.gitignore` SHALL contain `.nhms-work/` alongside the existing `.nhms-runs/`, `.nhms-workspace/` and `.nhms-object-store/` entries.

#### Scenario: Scheduler work files do not appear in status
- **WHEN** `git check-ignore -q .nhms-work/scheduler/basins-file-registry-publish/basins-inventory.json` runs in a checkout
- **THEN** it exits 0

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

### Requirement: Backup copies SHALL have a declared home and a dry-run sweep
`infra/env/README.md` SHALL state that backup copies never live under `infra/env/` and belong under `~/NWM-presync-backup-<UTC date>/infra-env/`. `scripts/ops/node22_env_backup_sweep.sh` SHALL accept `--env-dir`, `--systemd-user-dir`, `--backup-root`, `--home` and `--apply`; SHALL list as candidates regular files directly under the env dir whose names match `*.bak*`, `*.backup-*` or `*.pre-*`; SHALL build a refusal set from every `EnvironmentFile=` value in top-level `nhms-*` units and one level of `nhms-*.service.d/*.conf`, normalized by stripping a leading `-`, expanding `%h` with `--home` and applying `realpath -m` to both sides; SHALL exit 2 naming the unit before any move when a candidate is in the refusal set; SHALL print one plan line per candidate and write `<backup root>/infra-env/sweep-receipt-<UTC>.json` (`schema_version: nhms.node22.env_backup_sweep.v1`); SHALL move nothing without `--apply`; with `--apply` SHALL `mv -n` each candidate into `<backup root>/infra-env/` and exit 2 if a target already exists; SHALL never delete.

#### Scenario: Dry run moves nothing
- **WHEN** the sweep runs without `--apply` over a tree with three backup files
- **THEN** three plan lines are printed, the receipt lists three candidates, all three files are still in place and the exit code is 0

#### Scenario: Referenced backup blocks the sweep
- **WHEN** a drop-in contains `EnvironmentFile=-%h/NWM/infra/env/compute.env.bak-x` and `--home` makes that resolve to a candidate
- **THEN** the sweep exits 2 before moving anything and names that drop-in

#### Scenario: Apply moves into the dated directory
- **WHEN** the sweep runs with `--apply`
- **THEN** every candidate exists under `<backup root>/infra-env/` with its original name and no longer under the env dir, live files not matching the patterns are untouched, and a receipt was written

#### Scenario: Existing target refuses
- **WHEN** `--apply` would overwrite a file already present under `<backup root>/infra-env/`
- **THEN** the sweep exits 2 without moving that file

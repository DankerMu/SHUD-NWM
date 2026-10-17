# node22-env-hygiene Specification

## Purpose
Backup copies of node-22 env files have a declared home outside `infra/env/`, the retired compose lane is marked superseded where the runbook still names it, the live scheduler env drift is classified against the loader's real defaults, and scheduler work directories do not pollute `git status`.

## Requirements

### Requirement: `.nhms-work/` SHALL be ignored by git
`.gitignore` SHALL contain `.nhms-work/` alongside the existing `.nhms-runs/`, `.nhms-workspace/` and `.nhms-object-store/` entries.

#### Scenario: Scheduler work files do not appear in status
- **WHEN** `git check-ignore -q .nhms-work/scheduler/basins-file-registry-publish/basins-inventory.json` runs in a checkout
- **THEN** it exits 0

### Requirement: The compose-lane entry for node-22 SHALL be marked superseded with a complete section marker
`docs/runbooks/two-node-deployment-overview.md` §5.2 SHALL contain, above the compose-lane list, one sentence stating that `infra/env/compute.env` is a retired docker-e2e artifact that no node-22 unit loads and that the unit-to-env authority is `infra/env/README.md`, followed by a complete DOC_STATUS section marker (`status: superseded`, `current_authority`, `superseded_by`, `status_since`, `archive_scope: section`, `retained_for`); the compose-lane list SHALL remain inside the marked section unchanged. A test SHALL parse the file with `_section_archive_status_marker_ranges` and assert exactly one complete `section` range whose span covers the compose-lane list.

#### Scenario: Marker is complete and covers the list
- **WHEN** `tests/test_node22_runbook_section_markers.py` runs
- **THEN** it finds one complete section range in §5.2 covering the line that names `infra/env/compute.env` in the list

#### Scenario: Governance docs gate still passes
- **WHEN** `uv run pytest -q tests/test_entropy_audit_report_contract.py` runs
- **THEN** it passes

### Requirement: The live scheduler env drift SHALL be classified against the loader's effective defaults
`infra/env/README.md` SHALL contain, outside the required-key block, a table "compute.scheduler-dbfree.env live-drift classification (2026-10-16)" with one row per key the live node-22 file lacks (`NHMS_SCHEDULER_LEASE_GUARD_MODE`, `NHMS_SCHEDULER_RECONCILE_ABSENCE_SECONDS`, `NHMS_SCHEDULER_IDENTITY_BLOCKED_STREAK_LIMIT`, `NHMS_SCHEDULER_NO_PROGRESS_CIRCUIT_PASSES`, `NHMS_SCHEDULER_TIMING_LEVEL`, `NHMS_SCHEDULER_EVIDENCE_RETENTION_DAYS`, `NHMS_SCHEDULER_EVIDENCE_MAX_MB`) and columns key / status / effective value when unset / operator action, where status is `required`, `optional (effective default: <value>)` or `optional, example differs (example: <v>, default: <v>)`, and "effective value when unset" is what the scheduler config loader applies when only that key is absent, legacy fallbacks included. The required-key block SHALL be unchanged. `tests/test_node22_scheduler_env_drift_table.py` SHALL assert every table key exists in `compute.scheduler-dbfree.env.example` and that each stated effective default equals the loader's value with that key removed from the environment; keys classified `required` SHALL have an operator step in `docs/runbooks/node22-control-plane-manual-recovery.md`.

#### Scenario: Effective default matches the loader
- **WHEN** the table states `NHMS_SCHEDULER_LEASE_GUARD_MODE` is `optional, example differs (example: atomic, default: flock)`
- **THEN** the loader, with both that key and its legacy fallback unset, yields `flock`

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

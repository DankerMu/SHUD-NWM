# Spec Delta

## Purpose
Backup copies of node-22 env files have a declared home outside `infra/env/`, the retired compose lane is marked superseded where the runbook still names it, the scheduler env example matches what the live file lacks, and scheduler work directories do not pollute `git status`.

## ADDED Requirements

### Requirement: `.nhms-work/` SHALL be ignored by git
`.gitignore` SHALL contain `.nhms-work/` alongside the existing `.nhms-runs/`, `.nhms-workspace/` and `.nhms-object-store/` entries.

#### Scenario: Scheduler work files do not appear in status
- **WHEN** `.nhms-work/scheduler/basins-file-registry-publish/basins-inventory.json` exists in a checkout
- **THEN** `git status --porcelain` does not list it

### Requirement: The compose-lane entry for node-22 SHALL be marked superseded
`docs/runbooks/two-node-deployment-overview.md` §5.2 SHALL carry a complete DOC_STATUS section marker (`status: superseded`, `current_authority`, `superseded_by`, `status_since`, `archive_scope: section`, `retained_for`) immediately before the compose-lane list, and SHALL state that `infra/env/compute.env` is a retired docker-e2e artifact that no node-22 unit loads and that the unit-to-env authority is `infra/env/README.md`.

#### Scenario: Governance docs gate accepts the marker
- **WHEN** `uv run pytest -q tests/test_entropy_audit_report_contract.py` runs
- **THEN** it passes and the audit report raises no new finding for §5.2

#### Scenario: The retired file is named as retired
- **WHEN** a reader searches the runbook for `compute.env`
- **THEN** the only mention inside §5.2 is the retired-artifact sentence below the marker

### Requirement: The scheduler env example SHALL classify the keys the live file lacks
For each of `NHMS_SCHEDULER_LEASE_GUARD_MODE`, `NHMS_SCHEDULER_RECONCILE_ABSENCE_SECONDS`, `NHMS_SCHEDULER_IDENTITY_BLOCKED_STREAK_LIMIT`, `NHMS_SCHEDULER_NO_PROGRESS_CIRCUIT_PASSES`, `NHMS_SCHEDULER_TIMING_LEVEL`, `NHMS_SCHEDULER_EVIDENCE_RETENTION_DAYS` and `NHMS_SCHEDULER_EVIDENCE_MAX_MB`, the required-key block in `infra/env/README.md` SHALL state `required` or `optional (code default: <value>)`, where the default is the value the consuming code applies when the key is absent. Keys classified `required` SHALL have a corresponding operator step in `docs/runbooks/node22-control-plane-manual-recovery.md`. A test SHALL assert that every key named in the block appears in `compute.scheduler-dbfree.env.example` and that every `optional` default matches the code constant.

#### Scenario: Optional default matches code
- **WHEN** the README names a key `optional (code default: X)`
- **THEN** the consuming module's default for that key equals `X`

#### Scenario: Required key has an operator step
- **WHEN** the README names a key `required`
- **THEN** the recovery runbook contains a step that sets it

### Requirement: Backup copies SHALL have a declared home and a dry-run sweep
`infra/env/README.md` SHALL state that backup copies never live under `infra/env/` and belong under `~/NWM-presync-backup-<UTC date>/infra-env/`. `scripts/ops/node22_env_backup_sweep.sh` SHALL list, as candidates, files directly under `infra/env/` whose names match `*.bak*`, `*.backup-*` or `*.pre-*`; SHALL refuse (exit 2, naming the unit) when any candidate path appears in an `EnvironmentFile=` line of any file under the user systemd directory; SHALL print one plan line per candidate and write a receipt; SHALL move nothing unless `--apply` is given; and with `--apply` SHALL `mv` each candidate into the dated directory and never delete.

#### Scenario: Dry run moves nothing
- **WHEN** the sweep runs without `--apply` over a tree with three backup files
- **THEN** three plan lines are printed, the receipt lists three candidates, all three files are still in place and the exit code is 0

#### Scenario: Referenced backup blocks the sweep
- **WHEN** a candidate path is named in an `EnvironmentFile=` line of a deployed unit
- **THEN** the sweep exits 2 before moving anything and names that unit

#### Scenario: Apply moves into the dated directory
- **WHEN** the sweep runs with `--apply`
- **THEN** every candidate exists under `<backup root>/infra-env/` with its original name and no longer under `infra/env/`, and live files not matching the patterns are untouched

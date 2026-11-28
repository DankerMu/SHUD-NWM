# node22-unit-tracking Specification

## Purpose
The live user-scope systemd units of the compute role are a tracked, manifest-listed repository artifact, and a read-only probe reports whether the deployed units, including drop-ins and the top-level oneshots, still match the tracked copies.

## Requirements

### Requirement: Live units of the compute role SHALL be tracked byte-exactly with a manifest and without secret values
The repository SHALL track under `infra/systemd/node22/` byte-exact copies of seven deployed files. Six are recorded in `docs/runbooks/receipts/2026-10-16-node22-live-units-snapshot.md`: `nhms-compute-scheduler.service`, `nhms-compute-scheduler.timer`, `nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf`, `nhms-compute-scheduler.service.d/20-runtime-path.conf`, `nhms-slurm-gateway.service` (the deployed base unit) and `nhms-slurm-gateway.service.d/10-node22-live.conf`. The seventh, `nhms-slurm-gateway.service.d/20-runtime-path.conf`, is recorded in `docs/runbooks/receipts/2026-11-26-w3-gateway-runtime-path-dropin-snapshot.md`. Two of the six, `nhms-compute-scheduler.service` and `nhms-slurm-gateway.service`, were redeployed with a new `Description=` (#3427) and are recorded again in `docs/runbooks/receipts/2026-11-28-w3-scheduler-and-gateway-units-snapshot.md`; where two receipts record a path, the later receipt holds the bytes the repository tracks. The 2026-10-16 receipt also records `nhms-compute-api.service`; that unit is retired (#3394) and SHALL NOT be a file under `infra/systemd/node22/` nor a manifest entry, while the receipt, being a record of that day, keeps its row. `infra/systemd/node22/MANIFEST.json` SHALL list every user unit file deployed on the compute control host as `{deployed_relative_path, tracked_path, sha256}`: the seven above, the fourteen top-level oneshot `.service`/`.timer` files tracked at `infra/systemd/<name>`, the alert template `nhms-node22-unit-failure-alert@.service` tracked at `infra/systemd/nhms-node22-unit-failure-alert@.service`, and the two journal-backup units of the compute control host `w3`, `nhms-w3-journal-backup.service` and `nhms-w3-journal-backup.timer`, tracked at `infra/systemd/<name>` (twenty-four entries). A test SHALL assert for every tracked file under `node22/`: its sha256 equals the manifest entry and the table of the latest receipt that records it, the only paths that more than one receipt records are the two redeployed units, and the three receipts together record exactly the seven files and the retired `nhms-compute-api.service`; no line assigns a non-empty value to a variable whose name ends in `TOKEN`, `SECRET`, `PASS` or `PASSWORD`; every non-empty `EnvironmentFile=` value, after stripping a leading `-`, is either the documented secret path `/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env` or equals `/scratch/frd_muziyao/` joined with a `NWM/infra/env/...` entry of the compute role's authority table in `infra/env/README.md`; empty `EnvironmentFile=` reset lines are allowed.

#### Scenario: Tracked copies match the receipt and carry only path references
- **WHEN** the tracked-unit test scans `infra/systemd/node22/` and `MANIFEST.json`
- **THEN** every sha256 matches, every non-empty `EnvironmentFile=` resolves to an authority-table path or the secret path, empty reset lines pass, and no secret-named variable has a value

#### Scenario: A redeployed unit follows its latest receipt
- **WHEN** two receipts record `nhms-compute-scheduler.service` or `nhms-slurm-gateway.service`
- **THEN** the tracked copy and its manifest entry equal the sha256 in the later receipt
- **AND** the test fails when the manifest, the tracked file or the later receipt holds the bytes or the sha256 of the earlier receipt

#### Scenario: Manifest covers the top-level oneshots
- **WHEN** the test reads `MANIFEST.json`
- **THEN** it contains the seven oneshot `.service` files (including `nhms-scheduler-state-index-prune.service` since #3121), their seven `.timer` files, the alert template and the two `nhms-w3-journal-backup` units with `tracked_path` under `infra/systemd/` and sha256 equal to those files

#### Scenario: The w3-only journal-backup units are listed
- **WHEN** the drift probe runs on `w3` against the deployed user units
- **THEN** `nhms-w3-journal-backup.service` and `nhms-w3-journal-backup.timer` are reported as manifest entries with status `same`, not as untracked units

#### Scenario: The gateway runtime-path drop-in is tracked
- **WHEN** the drift probe runs on `w3` against the deployed user units after this change
- **THEN** `nhms-slurm-gateway.service.d/20-runtime-path.conf` is a manifest entry with status `same`, `untracked` is empty and the exit code is 0

#### Scenario: The retired compute API unit is neither tracked nor listed
- **WHEN** the tracked-unit test reads `infra/systemd/node22/`, `MANIFEST.json` and the three receipts
- **THEN** no file under `infra/systemd/node22/` and no manifest entry is named `nhms-compute-api.service`, and the only name the receipts record that is not tracked is `nhms-compute-api.service`
- **AND** the test fails when the file returns, when the entry returns, and when the receipts record any other untracked name

#### Scenario: An installed copy of the retired unit is drift
- **WHEN** the drift probe runs against a systemd directory that still holds `nhms-compute-api.service`
- **THEN** the file is listed under `untracked` and the exit code is 1

### Requirement: A read-only, manifest-driven probe SHALL report deployed-versus-tracked drift
`scripts/node22_unit_drift_check.py` SHALL read `MANIFEST.json`, compare each entry's deployed file under `--systemd-user-dir` (default `$XDG_CONFIG_HOME/systemd/user`, else `~/.config/systemd/user`) with its tracked file by bytes and classify it `same`, `differs` (printing a unified diff) or `missing`; SHALL scan only the systemd directory's top level for `nhms-*.service` and `nhms-*.timer` and one level of `nhms-*.service.d/*.conf`, listing files absent from the manifest as `untracked`; SHALL ignore every other directory and symlink; SHALL write a receipt JSON (`schema_version: nhms.node22.unit_drift_check.v1`, per-file status, manifest sha256, timestamps); SHALL NOT invoke `systemctl` and SHALL NOT write under the systemd directory. Exit status SHALL be 0 when every entry is `same` and `untracked` is empty; 1 when any entry is `differs` or `missing` or `untracked` is non-empty; 2 when the manifest is missing, empty, not valid JSON or malformed (wrong `schema_version`, an entry with a missing or extra key, a path field that is absolute or contains a `..` segment, a `tracked_path` outside `infra/systemd/`, a duplicate `deployed_relative_path`, or a tracked file whose sha256 differs from its entry), the systemd directory does not exist or cannot be inspected, a tracked or deployed file is unreadable (any error other than the deployed file being absent, which is `missing`), the `--receipt` path sits or resolves under the systemd directory, already exists as anything but a regular file (a symlink, directory or FIFO), its parent directory does not exist or cannot be inspected, or the receipt cannot be written. Exit 2 SHALL outrank exit 1 when both apply.

#### Scenario: All manifest entries deployed identically
- **WHEN** every manifest entry has an identical deployed counterpart and no extra `nhms-*` unit or drop-in is deployed
- **THEN** the receipt lists every entry as `same` with an empty `untracked` list and the exit code is 0

#### Scenario: A top-level oneshot differs
- **WHEN** the deployed `nhms-scheduler-journal-retention.service` differs from `infra/systemd/nhms-scheduler-journal-retention.service`
- **THEN** that entry is `differs`, the diff hunk is printed and the exit code is 1

#### Scenario: An untracked drop-in is deployed
- **WHEN** `nhms-compute-scheduler.service.d/30-hotfix.conf` exists in the systemd directory and not in the manifest
- **THEN** it is listed under `untracked` and the exit code is 1

#### Scenario: A manifest entry is missing on the host
- **WHEN** a manifest entry has no deployed file
- **THEN** it is `missing` and the exit code is 1

#### Scenario: Backup directories are ignored
- **WHEN** the systemd directory contains every manifest entry byte-identical plus the real leftovers of the retired node-22 host recorded in `docs/governance/entropy-audit-2026-10-16.md` (`nhms-scheduler-file-provider-refresh.service.pre-no-private-tmp-*`, `nhms-compute-scheduler.service.d/disabled/*.conf.*`, the directories `gateway-rollout-backup/`, `gateway-rollout-backups/`, `issue-1684-deploy-backups/`, `retired-20260630/`, `runtime-path-recovery/` with unit files inside, and a `*.wants/` symlink)
- **THEN** none of their contents appear in the receipt, `untracked` is empty and the exit code is 0

#### Scenario: Probe cannot run
- **WHEN** the manifest is missing, empty, invalid JSON or malformed in any of the ways the requirement lists, or the systemd directory does not exist, or a tracked file is missing, or a deployed file exists but is unreadable, or the receipt's parent directory does not exist or cannot be inspected, or the receipt path sits or resolves under the systemd directory, or the receipt already exists as a non-regular file, or the systemd directory cannot be inspected, or drift and an unreadable file occur together
- **THEN** the probe reports `status: probe_failed` where it can (the receipt is still written when its path is writable; otherwise `status: probe_failed` goes to stderr), nothing under the systemd directory is created, modified or removed, and the exit code is 2

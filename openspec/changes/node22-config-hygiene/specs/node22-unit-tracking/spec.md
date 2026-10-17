# Spec Delta

## Purpose
The live node-22 user-scope systemd units are a tracked, manifest-listed repository artifact, and a read-only probe reports whether the deployed units, including drop-ins and the top-level oneshots, still match the tracked copies.

## ADDED Requirements

### Requirement: Live node-22 units SHALL be tracked byte-exactly with a manifest and without secret values
The repository SHALL track under `infra/systemd/node22/` byte-exact copies of the seven deployed files recorded in `docs/runbooks/receipts/2026-10-16-node22-live-units-snapshot.md`: `nhms-compute-api.service`, `nhms-compute-scheduler.service`, `nhms-compute-scheduler.timer`, `nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf`, `nhms-compute-scheduler.service.d/20-runtime-path.conf`, `nhms-slurm-gateway.service` (the deployed base unit) and `nhms-slurm-gateway.service.d/10-node22-live.conf`. `infra/systemd/node22/MANIFEST.json` SHALL list every file deployed on node-22 as `{deployed_relative_path, tracked_path, sha256}`: the seven above and the twelve top-level oneshot `.service`/`.timer` files tracked at `infra/systemd/<name>`. A test SHALL assert for every tracked file under `node22/`: its sha256 equals the manifest entry and the receipt table; no line assigns a non-empty value to a variable whose name ends in `TOKEN`, `SECRET`, `PASS` or `PASSWORD`; every non-empty `EnvironmentFile=` value, after stripping a leading `-`, is either the documented secret path `/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env` or equals `/scratch/frd_muziyao/` joined with a `NWM/infra/env/...` entry of the node-22 authority table in `infra/env/README.md`; empty `EnvironmentFile=` reset lines are allowed.

#### Scenario: Tracked copies match the receipt and carry only path references
- **WHEN** the tracked-unit test scans `infra/systemd/node22/` and `MANIFEST.json`
- **THEN** every sha256 matches, every non-empty `EnvironmentFile=` resolves to an authority-table path or the secret path, empty reset lines pass, and no secret-named variable has a value

#### Scenario: Manifest covers the top-level oneshots
- **WHEN** the test reads `MANIFEST.json`
- **THEN** it contains the six oneshot `.service` files and their six `.timer` files with `tracked_path` under `infra/systemd/` and sha256 equal to those files

### Requirement: A read-only, manifest-driven probe SHALL report deployed-versus-tracked drift
`scripts/node22_unit_drift_check.py` SHALL read `MANIFEST.json`, compare each entry's deployed file under `--systemd-user-dir` (default `$XDG_CONFIG_HOME/systemd/user`, else `~/.config/systemd/user`) with its tracked file by bytes and classify it `same`, `differs` (printing a unified diff) or `missing`; SHALL scan only the systemd directory's top level for `nhms-*.service` and `nhms-*.timer` and one level of `nhms-*.service.d/*.conf`, listing files absent from the manifest as `untracked`; SHALL ignore every other directory and symlink; SHALL write a receipt JSON (`schema_version: nhms.node22.unit_drift_check.v1`, per-file status, manifest sha256, timestamps); SHALL NOT invoke `systemctl` and SHALL NOT write under the systemd directory. Exit status SHALL be 0 when every entry is `same` and `untracked` is empty; 1 when any entry is `differs` or `missing` or `untracked` is non-empty; 2 when the manifest is missing or empty, the systemd directory does not exist, a tracked or deployed file is unreadable, or the receipt cannot be written.

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
- **WHEN** the systemd directory contains `retired-20260630/` and `gateway-rollout-backups/` with unit files inside
- **THEN** none of their contents appear in the receipt

#### Scenario: Probe cannot run
- **WHEN** the manifest is empty or the systemd directory does not exist or the receipt path is unwritable
- **THEN** the probe reports `status: probe_failed` where it can and exits 2

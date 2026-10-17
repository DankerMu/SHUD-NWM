# Spec Delta

## Purpose
The live node-22 user-scope systemd units are a tracked repository artifact, and a read-only probe reports whether the deployed units still match the tracked copies.

## ADDED Requirements

### Requirement: Live node-22 units SHALL be tracked without secret values
The repository SHALL track, under `infra/systemd/node22/`, byte-exact copies of the deployed node-22 user-scope units `nhms-compute-api.service`, `nhms-compute-scheduler.service`, `nhms-compute-scheduler.timer`, the drop-ins `nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf`, `nhms-compute-scheduler.service.d/20-runtime-path.conf` and `nhms-slurm-gateway.service.d/10-node22-live.conf`. Tracked files SHALL reference secret env files by path only. A test SHALL fail when any tracked file under `infra/systemd/node22/` assigns a non-empty value to a variable whose name ends in `TOKEN`, `SECRET`, `PASS` or `PASSWORD`, or when an `EnvironmentFile=` path in a tracked unit is not listed in the node-22 authority table of `infra/env/README.md`.

#### Scenario: Tracked copies carry only path references
- **WHEN** the tracked-unit test scans `infra/systemd/node22/`
- **THEN** every `EnvironmentFile=` value is either a path under `infra/env/` named in the README authority table or the documented secret path `/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env`
- **AND** no line assigns a non-empty secret-named variable

### Requirement: A read-only probe SHALL report deployed-versus-tracked drift
`scripts/node22_unit_drift_check.py` SHALL compare every file under `infra/systemd/node22/` with the file at the same relative path under the user systemd directory (`--systemd-user-dir`, default `$XDG_CONFIG_HOME/systemd/user` or `~/.config/systemd/user`), classify each as `same`, `differs` or `missing`, list every deployed `nhms-*.service` or `nhms-*.timer` with no tracked counterpart as `untracked`, write a receipt JSON (`schema_version: nhms.node22.unit_drift_check.v1`, per-file status, timestamps), and exit 0 only when every tracked file is `same` and the `untracked` list is empty. It SHALL NOT invoke `systemctl` and SHALL NOT write under the systemd directory.

#### Scenario: All tracked units deployed identically
- **WHEN** every tracked file has an identical deployed counterpart and no extra `nhms-*` unit is deployed
- **THEN** the receipt lists every file as `same` with an empty `untracked` list and the exit code is 0

#### Scenario: A deployed unit differs
- **WHEN** one deployed unit's bytes differ from the tracked copy
- **THEN** that file is `differs` in the receipt, the diff hunk is printed on stdout, and the exit code is 1

#### Scenario: A deployed unit is not tracked
- **WHEN** a deployed `nhms-*.service` has no file under `infra/systemd/node22/` and no file of the same name in `infra/systemd/`
- **THEN** it is listed under `untracked` and the exit code is 1

#### Scenario: The systemd directory is unreadable
- **WHEN** the systemd user directory does not exist
- **THEN** the probe writes a receipt with `status: probe_failed` and exits 2

## MODIFIED Requirements

### Requirement: Live node-22 units SHALL be tracked byte-exactly with a manifest and without secret values
The repository SHALL track under `infra/systemd/node22/` byte-exact copies of the seven deployed files recorded in `docs/runbooks/receipts/2026-10-16-node22-live-units-snapshot.md`: `nhms-compute-api.service`, `nhms-compute-scheduler.service`, `nhms-compute-scheduler.timer`, `nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf`, `nhms-compute-scheduler.service.d/20-runtime-path.conf`, `nhms-slurm-gateway.service` (the deployed base unit) and `nhms-slurm-gateway.service.d/10-node22-live.conf`. `infra/systemd/node22/MANIFEST.json` SHALL list every file deployed on node-22 as `{deployed_relative_path, tracked_path, sha256}`: the seven above, the fourteen top-level oneshot `.service`/`.timer` files tracked at `infra/systemd/<name>`, the alert template `nhms-node22-unit-failure-alert@.service` tracked at `infra/systemd/nhms-node22-unit-failure-alert@.service`, and the two units that exist only on the compute role's control host `w3`, `nhms-w3-journal-backup.service` and `nhms-w3-journal-backup.timer`, tracked at `infra/systemd/<name>` (twenty-four entries). A test SHALL assert for every tracked file under `node22/`: its sha256 equals the manifest entry and the receipt table; no line assigns a non-empty value to a variable whose name ends in `TOKEN`, `SECRET`, `PASS` or `PASSWORD`; every non-empty `EnvironmentFile=` value, after stripping a leading `-`, is either the documented secret path `/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env` or equals `/scratch/frd_muziyao/` joined with a `NWM/infra/env/...` entry of the node-22 authority table in `infra/env/README.md`; empty `EnvironmentFile=` reset lines are allowed.

#### Scenario: Tracked copies match the receipt and carry only path references
- **WHEN** the tracked-unit test scans `infra/systemd/node22/` and `MANIFEST.json`
- **THEN** every sha256 matches, every non-empty `EnvironmentFile=` resolves to an authority-table path or the secret path, empty reset lines pass, and no secret-named variable has a value

#### Scenario: Manifest covers the top-level oneshots
- **WHEN** the test reads `MANIFEST.json`
- **THEN** it contains the seven oneshot `.service` files (including `nhms-scheduler-state-index-prune.service` since #3121), their seven `.timer` files, the alert template and the two `nhms-w3-journal-backup` units with `tracked_path` under `infra/systemd/` and sha256 equal to those files

#### Scenario: The w3-only journal-backup units are listed
- **WHEN** the drift probe runs on `w3` against the deployed user units
- **THEN** `nhms-w3-journal-backup.service` and `nhms-w3-journal-backup.timer` are reported as manifest entries with status `same`, not as untracked units

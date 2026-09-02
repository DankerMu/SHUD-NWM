## ADDED Requirements

### Requirement: Node-22 environment-file authority is documented

`infra/env/README.md` SHALL carry a table mapping every node-22 user systemd unit (`nhms-compute-scheduler`, `nhms-scheduler-evidence-retention`, `nhms-scheduler-file-provider-refresh`, `nhms-compute-api`, `nhms-slurm-gateway`) to the EnvironmentFile(s) it loads and to the tracked template for each, marking `compute.host.env` as untracked with no template, and SHALL state that `infra/env/compute.env` is the compose-lane instance of `compute.example` that no node-22 unit reads. The tracked `infra/env/compute.example` SHALL NOT carry a basin root that does not exist on node-22, SHALL label any root value that is a compose-lane placeholder (such as `NHMS_MODEL_ASSET_ROOT`) as a placeholder rather than asserting it exists, and SHALL name the live authority files in its header.

#### Scenario: Reader resolves the scheduler's basin configuration

- **WHEN** a reader wants the production scheduler's `NHMS_BASINS_ROOT` or `NHMS_SCHEDULER_BASIN_IDS`
- **THEN** `infra/env/README.md` directs them to `compute.scheduler-dbfree.env` (template `compute.scheduler-dbfree.env.example`)
- **AND** states that `compute.env` values are not the scheduler's configuration

#### Scenario: Tracked template carries no dead path

- **WHEN** `infra/env/compute.example` is read
- **THEN** `NHMS_BASINS_ROOT` equals the value in `compute.scheduler-dbfree.env.example`
- **AND** no `/volume/data/nwm` path remains in the file
- **AND** the `NHMS_MODEL_ASSET_ROOT` comment calls the value a placeholder and does not claim it exists on node-22

#### Scenario: Live compute.env no longer contradicts production

- **WHEN** `grep -iE 'BASIN|MODEL_IDS' /scratch/frd_muziyao/NWM/infra/env/compute.env` is run on node-22 after the ops edit
- **THEN** `NHMS_SCHEDULER_BASIN_IDS` and `NHMS_SCHEDULER_MODEL_IDS` are empty and `NHMS_BASINS_ROOT` is the dbfree value
- **AND** the file header names `compute.scheduler-dbfree.env` as the scheduler authority
- **AND** a same-mode backup of the pre-edit file exists beside it

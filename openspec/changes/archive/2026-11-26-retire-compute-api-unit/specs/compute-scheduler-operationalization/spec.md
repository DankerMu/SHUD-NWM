## MODIFIED Requirements

### Requirement: The compute units' environment-file authority is documented

`infra/env/README.md` SHALL carry a table mapping every node-22 user systemd unit (`nhms-compute-scheduler`,
`nhms-scheduler-evidence-retention`, `nhms-scheduler-file-provider-refresh`, `nhms-slurm-gateway`)
to the EnvironmentFile(s) it loads and to the tracked template for each, marking `compute.host.env` as untracked with
no template, and SHALL state that an `infra/env/compute.env` still present on the host is a leftover of the retired
docker/compose lane that no unit reads and that has no tracked template.

#### Scenario: Reader resolves the scheduler's basin configuration

- **WHEN** a reader wants the production scheduler's `NHMS_BASINS_ROOT` or `NHMS_SCHEDULER_BASIN_IDS`
- **THEN** `infra/env/README.md` directs them to `compute.scheduler-dbfree.env` (template `compute.scheduler-dbfree.env.example`)
- **AND** states that the values of a leftover `compute.env` are not the scheduler's configuration

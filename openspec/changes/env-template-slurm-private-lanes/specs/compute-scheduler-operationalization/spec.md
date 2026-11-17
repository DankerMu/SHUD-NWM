## ADDED Requirements

### Requirement: The db-free scheduler env template SHALL carry every maintained Slurm private-lane key

`infra/env/compute.scheduler-dbfree.env.example` SHALL set each Slurm private-lane key that names a maintained private
copy and whose absence makes the Slurm job env fall back to a shared key: `NHMS_SLURM_SCHEDULER_STATE_INDEX` and
`NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST`. The `nhms-required-keys: compute.scheduler-dbfree` block in
`infra/env/README.md` SHALL list both, so that the template test fails when the template loses one. A private-lane key
with no producer SHALL NOT be added to the template: `NHMS_SLURM_SCHEDULER_CANONICAL_READINESS_INDEX` stays unset, so
jobs fall back to the shared canonical readiness index that provider refresh maintains, and the template and the README
SHALL say so.

#### Scenario: An env rebuilt from the template keeps the maintained private lanes

- **WHEN** `compute.scheduler-dbfree.env` is rebuilt from the tracked template
- **THEN** `NHMS_SLURM_SCHEDULER_STATE_INDEX` and `NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST` are present
- **AND** `NHMS_SLURM_SCHEDULER_CANONICAL_READINESS_INDEX` is absent, and the template comment names the shared index
  as the value jobs receive

#### Scenario: Losing a maintained private-lane key fails the template test

- **WHEN** `NHMS_SLURM_SCHEDULER_STATE_INDEX` or `NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST` is removed from
  `compute.scheduler-dbfree.env.example`
- **THEN** `tests/test_env_templates.py` fails on the README required-keys block

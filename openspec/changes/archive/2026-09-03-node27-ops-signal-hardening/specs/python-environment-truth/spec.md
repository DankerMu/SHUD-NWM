## ADDED Requirements

### Requirement: pytest temporary directories MUST be bounded

The repository pytest configuration SHALL set `tmp_path_retention_policy = "failed"` so only failing tests retain their `tmp_path` directories, and the node-27 host discipline SHALL place pytest's temporary root on the `/home` volume via `TMPDIR`, never via a shared `--basetemp`.

#### Scenario: Passing tests leave no tmp_path residue

- **WHEN** a pytest session finishes with passing tests that used `tmp_path`
- **THEN** each of those directories is removed at that test's teardown

#### Scenario: node-27 temporary root is off the root volume

- **WHEN** pytest runs on node-27 under the documented profile
- **THEN** `pytest-of-nwm` is created under `/home/nwm/tmp`, and root-volume free space is unchanged by the run

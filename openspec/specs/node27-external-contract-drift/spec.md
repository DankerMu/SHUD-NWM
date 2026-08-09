# node27-external-contract-drift Specification

## Purpose
TBD - created by archiving change node27-external-contract-snapshot. Update Purpose after archive.
## Requirements
### Requirement: Host-contract drift MUST be detectable out-of-band, read-only, before a mutation window

The repository SHALL carry a committed snapshot of node-27's measured
external contracts and a read-only probe that compares live
observations against it, so that drift in any pinned measured constant
(`CONTAINER_PG_RESTORE_REALPATH`, `SYSTEMD_UNSET_TIMESTAMP`,
`CLIENT_BACKEND_TYPE`) or compared host-context fact surfaces as a
named, non-zero-exit diff outside any mutation window — never first
inside one. The snapshot fixture SHALL stay aligned 1:1 with the
contract module's measured constants, and no automated path may update
the fixture.

#### Scenario: A tampered compared field is named and fails the check

- **WHEN** any entry of the fixture's compared sections differs from
  the live observation (equivalently: the fixture is tampered while
  observations are stubbed to the true values)
- **THEN** `--check` exits with the dedicated drift code and the
  structured report names exactly the drifted field with expected and
  observed values

#### Scenario: Fixture and contract module cannot move alone

- **WHEN** the fixture's `contract` section and the
  `node27_container_contract` measured constants disagree
- **THEN** the hermetic alignment test fails in CI, and `--check`
  itself refuses on-node before spawning any probe, exiting with the
  dedicated misalignment code distinct from the drift code

#### Scenario: Volatile host facts never flake the check

- **WHEN** only `informational` content (backend_type distribution,
  measured_at, hostname) differs from the live state
- **THEN** `--check` exits 0

#### Scenario: A probe that cannot execute is never reported as drift

- **WHEN** any probe fails to execute, or exits zero with output that
  is empty or missing the field it measures
- **THEN** `--check` exits with the probe-execution-failure code,
  distinct from the drift code, and the report names the failing
  probe rather than a drifted field

#### Scenario: The probe cannot mutate the host

- **WHEN** the probe module's spawnable argv table and SQL strings are
  enumerated
- **THEN** every argv matches the frozen read-only whitelist and every
  SQL string is SELECT/SHOW-only, enforced by a test


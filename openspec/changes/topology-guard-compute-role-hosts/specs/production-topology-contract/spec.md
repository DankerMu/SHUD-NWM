## MODIFIED Requirements

### Requirement: Static guardrails block topology drift

The project SHALL provide automated or scripted checks that flag active
compute-role database-writer and display-env-writer drift in current operational
surfaces.

The database-writer check and the compute-host leg of the local-PostgreSQL check
SHALL recognise the compute role by each of its names: the codename and the two
host names (`w3`, `w2`).
A host name SHALL be matched on the original text as a lower-case whole token
that is not adjacent to an ASCII letter, digit or underscore and is not part of
a dotted name. An upper-case spelling, a domain name and a
longer word that contains the letters are not host names.

#### Scenario: Active node-22 DB writer assumptions are flagged

- **WHEN** an active script, runbook, or governance entry describes node-22 as
  the current NHMS active DB writer or asks operators to use node-22 local
  PostgreSQL as current production state
- **THEN** the guard reports a finding unless the reference is clearly
  historical, archived, or compatibility-only context

#### Scenario: A database-writer claim about a compute host is flagged

- **WHEN** an active script, runbook, or governance entry describes a compute
  host, by host name, as the current NHMS active database writer or as the host
  of the active primary database
- **THEN** the guard reports the same finding it reports for the codename
- **AND** a negative wording that is allowed for the codename is allowed when it
  uses a host name instead, alone or as a pair of both host names

#### Scenario: A compute host's local PostgreSQL is flagged unless denied

- **WHEN** a clause of an active surface names a compute host, by host name,
  together with a local PostgreSQL
- **THEN** the guard reports a finding unless that same clause denies that such
  a database exists or forbids its use
- **AND** the archived, stopped or rollback wording that excuses a reference to
  the retired host's own PostgreSQL does not excuse it

#### Scenario: Text that only looks like a host name is not flagged

- **WHEN** a line names a database writer and contains a host name's letters
  only in upper case, inside a domain name, or inside a longer word
- **THEN** the guard reports no compute-role finding for it

#### Scenario: Slurm's accounting database is not an NHMS database

- **WHEN** a clause says that slurmdbd or Slurm accounting on a compute host
  writes to its own accounting store, and names no PostgreSQL, primary, NHMS or
  current, active or production state
- **THEN** the guard reports no database-writer finding for it
- **AND** a clause that names slurmdbd and also claims the PostgreSQL or NHMS
  primary for the host is still reported

#### Scenario: Display env is not reused for data-plane writer jobs

- **WHEN** active scripts or runbooks configure data-plane writer or archived
  rollback mirror jobs
- **THEN** the guard reports a finding if they source `infra/env/display.env` as
  the writer/mirror authority
- **AND** display API restart and readonly object-store reads may still use
  display runtime env where appropriate

## ADDED Requirements

### Requirement: The compute-node topology check SHALL separate database mirrors from file mirrors

The `production-topology-node22-local-postgres` check SHALL report a line only
when that line carries a rollback signal or a database signal, never on the mere
co-occurrence of the compute node and the word "mirror". A non-database mirror —
the `NHMS_SLURM_SCHEDULER_STATE_INDEX` state-index file mirror on the compute
node's local `/scratch`, a worker mirror, or the ordinary verb "mirrored" —
SHALL NOT be reported by that check. An explicit assertion that a surface holds
no database SHALL NOT itself supply the database signal, and SHALL NOT suppress
a database token that appears elsewhere on the same line. Rollback wording SHALL
remain reportable regardless of any such assertion. The check's existing
detections — the archived port, local-PostgreSQL wording, and a DSN token paired
with a mirror — SHALL be unchanged.

#### Scenario: A state-index file mirror is not drift

- **WHEN** an active surface says the compute node reaches both the NFS
  canonical state index and its own local `/scratch` mirror, and states that the
  host is DB-free and takes no DB handle, with no rollback wording
- **THEN** `production-topology-node22-local-postgres` reports no finding for
  that line

#### Scenario: "mirrored" as an ordinary verb is not drift

- **WHEN** an active spec line uses "mirrored" to describe copying a value
  verbatim, separately names the compute node's private storage, and carries no
  database token and no rollback wording
- **THEN** `production-topology-node22-local-postgres` reports no finding for
  that line

#### Scenario: A real rollback database mirror is still drift

- **WHEN** an active surface names the archived port, or local-PostgreSQL
  wording, or a DSN token paired with a mirror, or rollback wording, without the
  required archived/stopped compatibility wording
- **THEN** `production-topology-node22-local-postgres` reports a finding for
  that line
- **AND** it still reports one when the same line additionally asserts that some
  surface holds no database, whether the trigger is the rollback wording or a
  database token that survives that assertion

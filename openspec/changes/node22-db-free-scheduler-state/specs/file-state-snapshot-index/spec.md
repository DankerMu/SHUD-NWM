## ADDED Requirements

### Requirement: Strict warm-start can use a file-backed state snapshot index

The system SHALL support strict forecast warm-start state lookup without
`PsycopgStateSnapshotRepository`.

#### Scenario: Exact successor checkpoint is found

- **WHEN** strict forecast warm-start requires a checkpoint for
  `model_id + source_id + valid_time + expected cycle_id + required lead_hours`
- **THEN** the file-backed state index returns only an exact matching usable
  state snapshot
- **AND** returned evidence includes state URI, checksum, source identity,
  producing cycle, lead hours, valid time, model package lineage, and index
  schema version.

#### Scenario: Missing checkpoint fails closed

- **WHEN** the exact successor checkpoint is missing, unusable, has a checksum
  mismatch, has missing or wrong expected cycle/lead lineage, or belongs to a
  different model/source/time
- **THEN** scheduler blocks the candidate with strict warm-start evidence
- **AND** it does not fall back to latest usable state in DB-free mode.

#### Scenario: Overlapping valid-time checkpoints remain distinct

- **WHEN** two state-save outputs share `model_id + source_id + valid_time` but
  were produced by different cycles or lead hours
- **THEN** their state IDs, object keys, and file-index identities remain
  distinct
- **AND** strict warm-start selects the checkpoint matching the requested lead
  and expected producer cycle.

#### Scenario: State index writes are manifest-last

- **WHEN** state snapshot index data is produced or refreshed for scheduler use
- **THEN** referenced state objects exist before the index is published
- **AND** the index manifest is written last with checksum/generation evidence.

### Requirement: Downstream state save remains compatible

The system SHALL keep downstream state-save outputs compatible with the
file-backed index without requiring compute nodes to write to PostgreSQL.

#### Scenario: State-save output can refresh the file index

- **WHEN** a downstream `state_save_qc` stage produces a usable checkpoint
- **THEN** the DB-free pipeline can update or stage a file-index record for that
  checkpoint
- **AND** subsequent scheduler passes can discover it without `DATABASE_URL`.

#### Scenario: Same-checksum state-save rerun repairs lineage metadata

- **WHEN** a DB-free state-save rerun sees an existing state object with the
  same checksum but missing or stale source/cycle/lead/package metadata
- **THEN** it updates the file-index metadata without rewriting the state object
- **AND** the repaired record must pass QC before strict warm-start can use it.

#### Scenario: State-save command runs without PostgreSQL

- **WHEN** `state_save_qc` runs under DB-free scheduler mode
- **THEN** it does not require `DATABASE_URL` or
  `PsycopgStateSnapshotRepository`
- **AND** it writes a file-index record or staged index update with checksum,
  validity, source/cycle/lead identity, package lineage, and bounded evidence.

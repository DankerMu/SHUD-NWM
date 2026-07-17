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

### Requirement: State-index history is generation-scoped

The system SHALL scope forecast state-index continuity checks to the current
model generation so that old-generation checkpoints do not count as
current-generation warm-start history, while remaining readable for audit
and rollback.

#### Scenario: Same-generation exact predecessor admits warm continuation

- **WHEN** the candidate's model generation equals the current
  registry-canonical generation AND an exact state checkpoint of the same
  generation exists at the expected predecessor cycle / lead
- **THEN** scheduler selects `warm_continue` and admits submission
- **AND** evidence records `generation`, `transition_decision=warm_continue`,
  and the `selected_predecessor` identity.

#### Scenario: Old-generation checkpoints do not block declared cold start

- **WHEN** scheduler executes a `cold_declared_cutover` at
  `effective_cycle_utc`
- **THEN** old-generation state entries for the same `model_id` remain
  readable for audit and rollback
- **AND** they do NOT satisfy exact-predecessor warm-start
- **AND** the cold-start decision succeeds without touching old-generation
  objects.

#### Scenario: Wrong-generation checkpoint never satisfies strict warm-start

- **WHEN** strict warm-start searches for a predecessor and finds a checkpoint
  whose generation differs from the candidate's
- **THEN** the checkpoint is treated as unusable
- **AND** scheduler blocks with a `block_wrong_generation` reason
- **AND** does not fall back to latest usable state across generations.

### Requirement: Cold start for truly new model

The system SHALL admit cold start exactly once for a `model_id` that has no
prior state-index history across ANY generation; subsequent cycles of the
same generation require an exact predecessor and fail closed if absent.

#### Scenario: New model cold-starts once at earliest selected cycle

- **WHEN** a `model_id` has no state-index entries in any generation
- **THEN** scheduler admits `cold_new_model` at the earliest selected source
  cycle
- **AND** evidence records `transition_decision=cold_new_model`,
  `cold_start_reason=no_prior_history`, and `selected_predecessor=null`.

#### Scenario: Subsequent cycles of a new model require exact predecessor

- **WHEN** after a `cold_new_model` at cycle T0, scheduler evaluates cycle
  T1 = T0 + source cadence for the same `model_id` in the same generation
- **THEN** it requires an exact predecessor checkpoint at T0 with the same
  generation
- **AND** blocks with `block_predecessor_pending` (typed reason
  `state_snapshot_index_prior_checkpoint_missing_after_history`) if the
  predecessor is absent, identifying the required predecessor identity in
  evidence
- **AND** does NOT cold-start again on the missing predecessor.

### Requirement: Predecessor-aware backfill selection

The system SHALL make backfill cycle selection predecessor-aware: a
successor cycle is not attempted while its required predecessor is missing
and the raw manifest for the predecessor exists.

#### Scenario: Predecessor selected before successor

- **WHEN** cycle T is blocked because the required predecessor
  (T minus source cadence — 12h for 00/12 sources) checkpoint or journal is
  missing AND the raw manifest for the predecessor exists
- **THEN** scheduler emits a predecessor-select candidate for the predecessor
  and defers T with a `block_predecessor_pending` reason
- **AND** T is neither submitted nor permanently failed while the predecessor
  is pending.

#### Scenario: Backfill respects generation identity

- **WHEN** scheduler selects a backfill predecessor for cycle T of generation
  G
- **THEN** the predecessor must be of the same generation G
- **AND** a candidate predecessor from a different generation is refused with
  a `generation_mismatch` reason.

### Requirement: Stale-lineage journal entries do not suppress backfill

The system SHALL quarantine completed / failed journal entries whose recorded
predecessor identity does not match the required predecessor of the current
generation from canonical readiness scoring, while preserving them as
immutable audit entries.

#### Scenario: Stale-lineage entries are quarantined

- **WHEN** the journal contains a completed cycle-T entry whose recorded
  predecessor identity does not match the required predecessor for T of the
  current generation
- **THEN** scheduler treats T as not-canonical-ready
- **AND** the correct backfill selection is not suppressed
- **AND** the stale entry remains readable as an immutable audit entry.

#### Scenario: Broad env override does not loosen generation semantics

- **WHEN** `NHMS_REQUIRE_FORECAST_WARM_START=false` is set
- **THEN** scheduler still requires a valid declaration for a
  `package_changed` transition, an exact predecessor within one generation,
  and refuses a wrong-generation checkpoint as usable
- **AND** the env only affects optional warm-start hints, not the transition
  contract.

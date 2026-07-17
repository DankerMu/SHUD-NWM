## ADDED Requirements

### Requirement: Scheduler model discovery can use a file-backed registry

The system SHALL support node-22 scheduler model discovery from a versioned
file/object-store registry manifest instead of `PsycopgModelRegistryStore`.

#### Scenario: File registry loads production models

- **WHEN** DB-free scheduler mode points at a registry manifest
- **THEN** scheduler model discovery loads approved production models from that
  manifest
- **AND** it preserves model ID, basin identity, model package URI, package
  checksum, resource profile, display capabilities, frequency capabilities, and
  segment counts needed for candidate construction.

#### Scenario: Invalid registry fails closed

- **WHEN** the registry manifest is missing, malformed, has duplicate model
  IDs, lacks required identity fields, or has checksum/resource-profile gaps
- **THEN** scheduler planning is blocked before submission
- **AND** evidence identifies the invalid field without leaking secrets.

#### Scenario: Registry evidence is bounded

- **WHEN** a file-backed registry is used
- **THEN** scheduler evidence records schema version, manifest path or URI,
  model count, selected model IDs, generated time, and checksum
- **AND** it does not inline unbounded model package contents.

#### Scenario: Registry publisher writes manifest last

- **WHEN** the production registry publisher refreshes the file-backed registry
- **THEN** all referenced model package manifests and checksums are verified
  before publication
- **AND** the registry manifest is written last with schema version,
  `generated_at`, checksum, and bounded publisher evidence.

### Requirement: Scheduler canonical readiness can use a file-backed index

The system SHALL support canonical product readiness evaluation from a
file/object-store index instead of `PsycopgMetStore`.

#### Scenario: File readiness feeds canonical evaluation

- **WHEN** scheduler evaluates canonical readiness for a source/cycle/model
- **THEN** it reads the configured canonical readiness index
- **AND** passes product evidence into the existing canonical readiness
  evaluator
- **AND** records product counts, forecast-hour coverage, canonical product ID,
  and readiness status.

#### Scenario: Missing canonical readiness is not treated as complete

- **WHEN** the canonical readiness index is missing or lacks the requested
  source/cycle/model identity
- **THEN** scheduler treats canonical readiness as unavailable or incomplete
  according to existing candidate rules
- **AND** it does not query PostgreSQL as a fallback in DB-free mode.

#### Scenario: Invalid canonical readiness fails closed

- **WHEN** the readiness index has an unsupported schema version, stale
  `generated_at`, source/cycle/model/basin identity mismatch, checksum
  mismatch, missing forecast-hour products, or missing referenced objects
- **THEN** scheduler treats canonical readiness as unavailable or incomplete
- **AND** evidence identifies the failing index field without secrets.

#### Scenario: Readiness publisher writes index last

- **WHEN** the canonical readiness publisher refreshes the file-backed
  readiness index
- **THEN** referenced canonical product objects and checksums are verified
  before publication
- **AND** the readiness index is written last with schema version,
  `generated_at`, checksum, source/cycle/model/basin identity, forecast-hour
  coverage, product counts, product URIs, object-existence evidence, and
  bounded publisher evidence.

#### Scenario: Raw handoff boundary remains enforced

- **WHEN** node-22 DB-free scheduler evaluates a node-27 raw source cycle
- **THEN** it still validates the node-27 NFS raw manifest source/cycle
  identity, URI suffix, entry list, referenced raw files, and compute-visible
  staging evidence
- **AND** raw-ready plus canonical-zero state starts downstream work at
  `restart_stage=convert`
- **AND** missing or invalid raw evidence blocks without submitting
  `download_source_cycle`.

### Requirement: Scheduler consumes registry declared package cutovers

The system SHALL consume the `nhms.scheduler.registry_package_cutover.v1`
declaration channel emitted by the registry publisher (schema landed by
#1080) and bind each declared cutover to the target `model_id` before
candidate submission can cold-start on its effective cycle.

#### Scenario: Declaration bound at planning time

- **WHEN** scheduler plans a candidate for a `model_id` whose registry
  `package_checksum` differs from the previous canonical (a `package_changed`
  row)
- **THEN** scheduler loads the declaration from the configured channel
- **AND** matches (`model_id`, `old_checksum`, `new_checksum`, `generation`)
  exactly
- **AND** records `effective_cycle_utc` and `transition_mode` in candidate
  evidence.

#### Scenario: Missing / stale / mismatched declaration fails closed

- **WHEN** no declaration is loaded, or the declaration `old_checksum` /
  `new_checksum` / `generation` does not match the current registry state, or
  the candidate cycle is outside the declared effective window
- **THEN** scheduler blocks the candidate with a typed reason
  (`registry_cutover_declaration_missing` /
  `registry_cutover_declaration_stale` /
  `registry_cutover_cold_start_out_of_window`) and does not submit
- **AND** evidence identifies the failing declaration field without leaking
  secrets.

### Requirement: Model generation identity threads through scheduler boundaries

The system SHALL derive a `generation` token deterministically from the
registry `package_checksum` and thread it through candidate construction,
state-index lookup, backfill selection, and evidence.

#### Scenario: Generation token derived from package checksum

- **WHEN** scheduler builds a candidate from the registry manifest
- **THEN** the candidate carries a `generation` token derived deterministically
  from `package_checksum`
- **AND** downstream state-index queries filter by generation
- **AND** backfill selection refuses a candidate predecessor whose generation
  differs from the successor's.

#### Scenario: Evidence records generation for every decision path

- **WHEN** scheduler records candidate evidence
- **THEN** `model_id`, `generation`, `transition_decision`,
  `selected_predecessor` identity (or `null`), `cold_start_reason`, and any
  typed block reason are captured
- **AND** the evidence stays bounded (no unbounded log / state inlining).

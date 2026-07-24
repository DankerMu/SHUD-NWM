## ADDED Requirements

### Requirement: DB-free retry manifests preserve runtime mode

Retry submissions for node-22 DB-free scheduler operation SHALL preserve the
same runtime repository and backend selectors as initial scheduler submissions.

#### Scenario: automatic forecast retry runs without database url

- **WHEN** a failed forecast task is automatically retried in DB-free scheduler
  mode
- **THEN** the submitted Slurm manifest MUST include
  `scheduler_db_free_required=true` and the canonical file backend selectors
  needed by the SHUD runtime
- **AND** the retry MUST be runnable without `DATABASE_URL`
- **AND** retry evidence MUST record the prior job id, new Slurm job id, stage,
  model id, source/cycle identity, retry attempt, and DB-free runtime mode.

#### Scenario: manual forecast retry runs without database url

- **WHEN** an operator manually retries a failed forecast task in DB-free
  scheduler mode
- **THEN** the submitted Slurm manifest MUST include the same DB-free runtime
  selectors as automatic retry
- **AND** the retry MUST be runnable without `DATABASE_URL`
- **AND** retry evidence MUST record the prior job id, new Slurm job id, stage,
  model id, source/cycle identity, retry attempt, and manual retry marker.

#### Scenario: missing upstream forcing blocks downstream retry

- **WHEN** retry or resume targets a downstream forecast stage whose referenced
  `forcing_package_uri` no longer exists in the configured object-store root
- **THEN** the scheduler MUST block or restart from the correct upstream stage
  before submitting forecast work
- **AND** the failure MUST use a stable artifact/copyback classifier rather than
  generic `NODE_FAILURE`.

#### Scenario: operator repairs one exact cycle from verified raw input

- **GIVEN** the default missing-forcing policy remains fail-closed
- **AND** node-22 production requires direct-grid forcing
- **AND** the node-27 NFS raw manifest and all referenced raw files are present,
  ready, and match the requested source and exact cycle
- **AND** the trusted NFS raw root comes from scheduler configuration rather
  than a redacted public journal field
- **AND** every selected candidate has a complete validated warm-state identity
  containing state id, URI, checksum, valid time, and lineage
- **WHEN** an operator runs the exact-cycle wrapper with
  `--repair-missing-forcing`
- **THEN** only a candidate blocked solely by `FORCING_PACKAGE_URI_MISSING`
  SHALL be reclassified to restart at `forcing`
- **AND** planning mode SHALL record an authorized/rejected policy decision
  without submitting work
- **AND** submit mode SHALL execute the existing `ForecastOrchestrator` chain
  beginning with `produce_forcing_array`, retaining the source/cycle cohort and
  global Slurm `%32` array bound
- **AND** the scheduler MUST NOT invoke login-node forcing, submit forecast
  directly, fall back to cold start, or replace the selected warm-state lineage.
- **AND** a terminal forcing job from a prior attempt MUST NOT suppress the new
  versioned forcing retry reservation
- **AND** public scheduler evidence MUST redact local raw roots and paths.

#### Scenario: repair pass preserves ordinary sibling warm admission

- **GIVEN** repair mode is enabled for one exact cycle
- **WHEN** another candidate in that pass has no classified state, an ordinary
  retry or other blocker, a different target cycle, or any classification other
  than the stable `FORCING_PACKAGE_URI_MISSING` blocker
- **AND** that candidate fails strict warm-start admission
- **THEN** the candidate SHALL retain the ordinary typed warm-start blocker
- **AND** it SHALL produce no forcing, forecast, local producer, orchestrator,
  or Slurm work
- **AND** initial classification and every post-Slurm-sync reclassification
  SHALL apply this same candidate-scoped admission decision.

#### Scenario: exact-cycle forcing repair preconditions fail closed

- **WHEN** the explicit repair flag is absent, the target cycle is missing or
  malformed, the candidate is outside that exact cycle, direct-grid is not the
  required and valid model contract, the forcing reference is unsafe, or the
  raw manifest is absent, stale, unreadable, or identity-mismatched, or warm
  state is missing, partial, cold/cutover, or identity-mismatched
- **THEN** the original missing-forcing blocker SHALL remain blocked
- **AND** an explicitly requested but rejected repair SHALL record a stable
  rejection reason and SHALL NOT submit Slurm or SHUD work.

#### Scenario: exact-cycle forcing repair preserves aggregate concurrency bound

- **WHEN** GFS and IFS repair cohorts are eligible in the same scheduler pass
- **THEN** their simultaneously active Slurm array throttles MUST sum to no more
  than 32
- **AND** repair configuration requesting an aggregate Slurm array concurrency
  above 32 MUST be rejected before submission.

#### Scenario: downstream canonical rejection preserves the original blocker

- **WHEN** raw and warm repair preconditions pass but current canonical input is
  not ready
- **THEN** the candidate MUST retain top-level
  `missing_forcing_package_uri` / `FORCING_PACKAGE_URI_MISSING` classification
- **AND** canonical rejection details MUST be recorded only as nested repair
  evidence with zero submission.

#### Scenario: DB-free deployment supplies trusted raw authority

- **WHEN** an operator provisions node-22 from
  `infra/env/compute.scheduler-dbfree.env.example` and the documented exact-cycle
  wrapper
- **THEN** the runtime SHALL receive the explicit trusted raw root
  `/ghdc/data/nwm/object-store` and object prefix `s3://nhms`
- **AND** the root SHALL be allow-listed and use the existing canonical shared-
  NFS copyback mount rather than a duplicate mount declaration
- **AND** missing, relative, outside-boundary, or malformed root/prefix values
  SHALL fail static/runtime preflight without exposing the raw value in public
  evidence.

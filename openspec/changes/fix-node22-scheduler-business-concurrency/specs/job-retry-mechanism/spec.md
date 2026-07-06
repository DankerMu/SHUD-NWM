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

## ADDED Requirements

### Requirement: Scheduler lease extraction SHALL preserve mutation fences

Scheduler lease extraction SHALL preserve mutation fences even when lease code
is moved to a dedicated module.

#### Scenario: scheduler pass starts
- **WHEN** the production scheduler starts a pass
- **THEN** lock-root preflight, lease acquisition, runtime-root preflight,
  startup reconcile, candidate discovery, lease-lost fence, pre-execution
  evidence reservation, and mutation occur in the same order as before

#### Scenario: lease renewal races with another holder
- **WHEN** lease renewal, CAS, live-holder detection, or cross-host TTL behavior
  is exercised
- **THEN** only the current valid holder can renew or mutate, and lease loss
  prevents orchestrator construction and submission

### Requirement: Candidate-state extraction preserves legacy compatibility

Candidate-state and identity validation helpers SHALL be extracted without
changing evidence fields, legacy aliases, retry decisions, or terminal-state
guards.

#### Scenario: legacy candidate state row is evaluated
- **WHEN** a candidate state row lacks full M23 identity fields but has legacy
  compatible source/cycle/model evidence
- **THEN** the same authoritative, legacy-non-authoritative, or filtered
  decision is produced as before extraction

#### Scenario: manual retry or active Slurm evidence is present
- **WHEN** candidate-state validation sees manual retry, active Slurm sync,
  terminal success, permanent failure, cancelled, or stale failure evidence
- **THEN** the extracted helper preserves the current decision and evidence
  payload

### Requirement: Discovery and backfill extraction preserves candidate ordering

Cycle discovery, completion checks, and backfill selection SHALL be moved only
when candidate ordering and legacy fallback behavior remain unchanged.

#### Scenario: backfill gaps are present
- **WHEN** backfill is enabled and multiple gaps exist
- **THEN** the oldest eligible gap is selected first and later gaps are deferred
  exactly as before

#### Scenario: model discovery returns empty
- **WHEN** backfill is enabled but no models are available
- **THEN** scheduler behavior falls back to the legacy newest-cycle mode

### Requirement: Candidate construction extraction preserves gating

Candidate construction SHALL preserve canonical readiness, fresh full-chain,
zero-canonical, active Slurm sync, duplicate candidate, and blocked-cycle
behavior.

#### Scenario: canonical readiness is unavailable
- **WHEN** canonical readiness provider raises or returns blocked evidence
- **THEN** the candidate is blocked/deferred with the same reason and evidence
  fields as before extraction

#### Scenario: active Slurm sync is allowed
- **WHEN** active Slurm status sync updates stale active rows
- **THEN** candidate inclusion, defer, or skip decisions remain unchanged

### Requirement: Scheduler execution extraction preserves pass ordering

Scheduler execution extraction SHALL preserve forcing, cohort grouping,
concurrent submission evidence, and scheduler pass mutation ordering.

#### Scenario: forced production run is scheduled
- **WHEN** a forced production run enters execution after candidate selection
- **THEN** the extracted execution helper preserves the current forcing
  decision, cohort grouping, submission behavior, and evidence payload

#### Scenario: concurrent candidates are submitted
- **WHEN** multiple eligible candidates are submitted in one scheduler pass
- **THEN** concurrent submit evidence, candidate ordering, and mutation fences
  remain the same as before extraction

#### Scenario: published artifact root is absent during planning
- **WHEN** runtime root preflight runs with a missing `published_artifact_root`
  and all other required roots valid
- **THEN** the preflight reports `published_artifact_root.allow_create=true`,
  does not emit `SCHEDULER_ROOT_PUBLISHED_ARTIFACT_ROOT_NOT_FOUND`, and dry-run
  planning remains non-mutating without creating the directory

#### Scenario: any other required runtime root is absent
- **WHEN** runtime root preflight runs with a missing workspace, object-store,
  runtime, temp, lock, or evidence root
- **THEN** the scheduler reports the existing root blocker before registry,
  adapter, active-repository, or submission work begins

### Requirement: Scheduler evidence extraction preserves reservation proof

Scheduler evidence extraction SHALL preserve pass evidence assembly,
pre-execution reservation proof, bounded serialization, and evidence keys.

#### Scenario: pre-execution evidence is reserved
- **WHEN** a scheduler pass reaches the pre-execution evidence reservation step
- **THEN** reservation proof is written after lease-lost fencing and before
  mutation/submission with the same evidence fields as before extraction

#### Scenario: pass evidence is serialized
- **WHEN** scheduler pass evidence is assembled or bounded for persistence
- **THEN** status, reason, schema version, evidence keys, and serialization
  limits remain unchanged

### Requirement: Chain type and catalog extraction preserves static contracts

Chain type and stage-catalog extraction SHALL preserve existing stage
definitions, stage ordering, context/result type contracts, and `chain.py`
import compatibility.

#### Scenario: stage catalog is moved
- **WHEN** static chain stage definitions move to `chain_stages.py`
- **THEN** stage IDs, order, script names, success/failure states, array flags,
  and downstream dependencies remain unchanged

#### Scenario: chain types are moved
- **WHEN** shared contexts, result dataclasses, or type aliases move to
  `chain_types.py`
- **THEN** existing imports from `chain.py` keep working through compatibility
  re-exports until callers migrate

### Requirement: Chain stage extraction preserves reservation protocol

Forecast-chain stage execution SHALL preserve reserve-before-sbatch and
bind-after-submit semantics for array and non-array stages.

#### Scenario: reservation is lost
- **WHEN** a stage reservation reports an already-inflight owner
- **THEN** no sbatch submission is made by the losing pass

#### Scenario: reservation is won
- **WHEN** a stage reservation is created or reclaimed
- **THEN** sbatch uses the same deterministic idempotency comment and the
  reservation is bound only after a real Slurm job id is returned

#### Scenario: startup reconcile finds reserved-unbound rows
- **WHEN** startup reconcile runs before planning/submission
- **THEN** dead reserved-unbound rows are handled without causing duplicate
  Slurm submissions

#### Scenario: active stage is resumed after startup
- **WHEN** an existing non-terminal stage job is found during chain execution
- **THEN** the chain polls that Slurm job to terminal, records the same
  status/event/log evidence, and does not submit a replacement job

#### Scenario: stage polling times out
- **WHEN** a submitted stage does not reach a terminal Slurm state before the
  configured timeout
- **THEN** the pipeline job and forecast cycle are marked with the stable
  `SLURM_JOB_TIMEOUT` failure evidence and no fabricated accounting is emitted

#### Scenario: terminal log publication fails
- **WHEN** terminal log persistence fails during submit, resume, or timeout
  handling
- **THEN** durable job status evidence is still recorded and the chain does not
  advertise a missing or failed published log URI

#### Scenario: manual retry targets a terminal stage
- **WHEN** manual retry metadata targets a failed, permanently failed,
  submission-failed, cancelled, or partially failed stage
- **THEN** the chain submits a new retry attempt identity instead of reusing the
  old terminal job id or idempotency key

### Requirement: Chain manifest extraction SHALL preserve published evidence

Chain manifest extraction SHALL preserve published evidence, schema versions,
and evidence keys when model-run assembly, manifest index writing, runtime
manifest safe writes, array aggregation, partial status, or publish-stage
evidence moves to dedicated modules.

#### Scenario: model run manifest is written
- **WHEN** a model run manifest or manifest index is produced
- **THEN** schema version, identity fields, quality states, residual blockers,
  and safe-write behavior remain stable

#### Scenario: array stage partially fails
- **WHEN** array accounting reports partial task failure
- **THEN** aggregate status, task outcomes, downstream manifest reduction, and
  publish behavior remain the same as before extraction

### Requirement: Compatibility shims protect downstream callers

Structural extraction SHALL keep old import or method surfaces available until
callers and tests migrate.

#### Scenario: tests import private scheduler or chain helpers
- **WHEN** existing tests or downstream code import helper names from
  `scheduler.py` or `chain.py`
- **THEN** the first extraction PR keeps compatibility shims or re-exports so
  behavior-preserving tests continue to pass

### Requirement: Object-store copyback preserves complete run products

Production copyback to `NHMS_OBJECT_STORE_COPYBACK_ROOT` SHALL validate source
and target paths without following symlinks and SHALL publish only complete
`runs/<run_id>` trees.

#### Scenario: configured copyback path contains a symlink component
- **WHEN** tile publication prepares `NHMS_OBJECT_STORE_COPYBACK_ROOT`
- **THEN** the raw configured path is checked before path resolution, symlink
  components are rejected, and no target `runs/<run_id>` tree is created

#### Scenario: copyback root equals object-store root
- **WHEN** `NHMS_OBJECT_STORE_COPYBACK_ROOT` exactly equals `OBJECT_STORE_ROOT`
- **THEN** physical copyback is skipped only after every `runs/<run_id>` source
  tree passes no-follow traversal plus manifest, output, and log completeness
  validation

#### Scenario: copyback roots overlap without equality
- **WHEN** the copyback root is inside the object-store root, or the object-store
  root is inside the copyback root
- **THEN** publication fails with a normalized object-store copyback error before
  creating target run-product trees

#### Scenario: source or target object-store operation fails
- **WHEN** source reads, target writes, unsafe run ids, or filesystem validation
  fail during copyback
- **THEN** the failure is reported as a copyback publish error with run id,
  object key, object-store root, copyback root, error type, and error details

### Requirement: Copyback replacement is rollback-safe

Replacing a canonical copyback `runs/<run_id>` tree SHALL avoid deleting or
partially corrupting the previous complete tree when promotion fails.

#### Scenario: promotion fails after staging succeeds
- **WHEN** a complete staged run tree exists but replacing the canonical
  `runs/<run_id>` directory fails
- **THEN** the previous canonical tree remains available and no partial new tree
  is exposed at the canonical path

### Requirement: q_down display publication waits for required copyback

q_down display publication SHALL not advance stable object-store, published
artifact, cycle manifest, or map-layer references until required run-product
copyback has succeeded.

#### Scenario: first publish copyback fails
- **WHEN** q_down publication requires copyback and copyback fails before any
  previous q_down cycle manifest exists
- **THEN** no new q_down per-run manifest, publish log, cycle manifest, or
  display layer is visible at the stable published locations

#### Scenario: republish copyback fails
- **WHEN** a q_down cycle has already published and a later republish fails
  during copyback
- **THEN** the previous q_down cycle manifest remains unchanged and the failed
  run's new per-run manifest/log artifacts are not exposed at stable URIs

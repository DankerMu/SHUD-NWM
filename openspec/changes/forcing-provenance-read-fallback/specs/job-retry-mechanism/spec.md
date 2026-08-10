# Delta: job-retry-mechanism

## MODIFIED Requirements

### Requirement: Missing upstream artifacts SHALL demote failure-state retries to a stable repair-eligible blocker

When a candidate carries a failure signal and the forcing package its forecast stage references does not exist in the configured object-store root, the scheduler SHALL NOT emit another forecast retry from any decision branch — including the failure fallback and the permanent-failure branch — and SHALL instead emit the stable missing-forcing blocker (reason `missing_forcing_package_uri`, stable classifier, `artifact_exists` false, forecast restart stage) that the explicit single-cycle repair authorization channel accepts. Before treating a state with no forcing package reference as missing, the scheduler SHALL attempt provenance recovery through the witnessed read tiers (journal row, journal direct file, object-store forcing-version sidecar record derived from the candidate identity); for the sidecar tier the existing artifact existence probe SHALL target the witnessed package manifest file key (never the directory-shaped package URI, which the object-path validator rejects), so that a physically present package never produces the missing-forcing blocker, and a witnessed-absent package produces exactly the unchanged `missing_forcing_package_uri` blocker. Only when no tier yields a witness SHALL the decision block with the distinct reason `forcing_version_row_absent` (error code and stable classifier `FORCING_VERSION_ROW_ABSENT`, null artifact reference, provenance source marked absent), which carries the same structural repair-eligible contract, and the single-cycle repair authorization channel — including its stable-classifier structural check — SHALL accept both blocker reason/classifier pairs. Provenance-tier failures (unreadable, malformed, oversized sidecar, unconfigured store, incomplete identity) SHALL classify as no-witness rather than as a determined-missing package, and SHALL never fail open into a retry. Failure classification produced inside the compute task SHALL survive the DB-free path to array accounting through a durable per-task outcome receipt, with generic `NODE_FAILURE` used only as the fail-safe when no receipt is readable. The effective retry attempt SHALL derive from the durable per-stage attempt record (job-identity retry suffix) on both the in-stage retry gate and the scheduler's cross-pass failure policy, so that the configured retry limit bounds retries even for misclassified failures.

#### Scenario: Failure-state candidate with missing forcing blocks instead of retrying

- **WHEN** a candidate has a failure signal (failed pipeline status or
  failed hydro run) and its referenced forcing package is absent from
  the object store
- **THEN** the decision is blocked with reason
  `missing_forcing_package_uri` and the full artifact-guard evidence,
  satisfying the structural contract required by the explicit
  missing-forcing repair policy, and no forecast work is submitted

#### Scenario: Null journal provenance with a physically present package recovers instead of blocking

- **WHEN** a candidate has a failure signal, its restart stage is the
  forecast stage, the journal records null forcing provenance, and the
  object-store forcing-version sidecar record derived from the
  candidate identity names a package whose witnessed manifest file the
  existence probe finds present
- **THEN** the decision does not emit any missing-forcing blocker for
  that package, the recovery evaluation proceeds past the upstream
  artifact guard, and the evidence of the decision ultimately emitted
  for the candidate records the provenance source as the object-store
  sidecar tier

#### Scenario: Null journal provenance with a witnessed-absent package keeps the missing blocker

- **WHEN** the journal records null forcing provenance, the sidecar
  record names a package, and the existence probe finds the witnessed
  package manifest file absent from the object store
- **THEN** the decision is the unchanged stable missing-forcing
  blocker (reason `missing_forcing_package_uri`), with the provenance
  source marked as the sidecar tier, preserving the fail-closed
  semantics for a determined-missing package

#### Scenario: No witness at any provenance tier blocks with the distinct row-absent reason

- **WHEN** a candidate has a failure signal, its restart stage is the
  forecast stage, and no forcing provenance witness exists at any read
  tier (no journal row, no journal direct file, and the sidecar record
  is absent, unreadable, malformed, oversized, the object store is
  unconfigured, or the candidate identity is incomplete)
- **THEN** the decision is blocked with reason
  `forcing_version_row_absent` (artifact reference null in the guard
  evidence, provenance source marked absent with the tier-unavailable
  detail), regardless of the recorded failure code — including
  permanently-classified codes such as policy or manifest failures,
  whose original cause remains visible in the per-job evidence — the
  blocker keeps the same structural repair-eligible contract, and a
  manual retry request, which is evaluated before the guard, remains
  the operator escape hatch

#### Scenario: Repair authorization accepts both blocker reasons

- **WHEN** an operator authorizes the explicit single-cycle
  missing-forcing repair for a candidate blocked with either
  `missing_forcing_package_uri` or `forcing_version_row_absent`
- **THEN** the repair authorization channel — including its
  stable-classifier structural check — accepts the blocker and
  proceeds identically for both reason/classifier pairs, and the
  re-blocking echo path re-emits the decision token paired with the
  underlying blocker reason

#### Scenario: Permanently-classified failure with missing forcing remains repair-eligible

- **WHEN** the recorded failure code is non-transient (for example
  `ARTIFACT_NOT_FOUND`) and the referenced forcing package is absent
- **THEN** the decision is the same stable missing-forcing blocker —
  not a generic permanent-failure guard — so the single-cycle repair
  channel remains usable

#### Scenario: Task-produced classifier survives the DB-free path

- **WHEN** a DB-free SHUD array task fails with a classified runtime
  error and its per-task outcome receipt is readable in the object
  store
- **THEN** array accounting records that classifier for the task and
  the aggregation, and falls back to `NODE_FAILURE` only when the
  receipt is absent or unreadable

#### Scenario: Retry limit binds through the durable attempt suffix

- **WHEN** a forecast cohort job's identity carries a retry suffix at
  or beyond the configured retry limit
- **THEN** the in-stage retry gate refuses further resubmission and
  the next scheduler pass computes an exhausted retry policy instead
  of re-issuing the failure retry

## ADDED Requirements

### Requirement: Forcing provenance SHALL be read through aligned witnessed tiers with a visible source

The DB-free journal read paths SHALL resolve forcing provenance for the same (source, cycle, model) identically: the candidate-state read SHALL apply the same journal-direct-file fallback the forcing-context read already applies when the journal row is absent, materializing the recovered provenance into the candidate state so downstream artifact-guard container scanning finds it, and both reads SHALL agree on the recovered forcing version identity and package URI. Every consumer-visible provenance record SHALL carry a source marker naming the tier that produced it (journal row, journal direct file, or object-store sidecar; absent when no tier yielded a witness), so an operator can distinguish a missing journal row from a missing package. When no witnessed tier yields provenance, the reads SHALL report the absence honestly (null provenance, absent source marker) and SHALL NOT fabricate a synthetic provenance record on the recovery path.

#### Scenario: Candidate state applies the direct-file fallback the context read already has

- **WHEN** the journal has no forcing-version row for a cycle but the
  journal direct file for that (source, cycle, model) exists
- **THEN** the candidate state materializes the direct-file provenance
  (marked with the direct-file source) and the forcing-context read
  and the candidate-state read return the same forcing version
  identity and package URI

#### Scenario: Both journal tiers empty yields honest null

- **WHEN** neither a journal forcing-version row nor a journal direct
  file exists for the (source, cycle, model)
- **THEN** the candidate state carries null forcing provenance with no
  fabricated record, and the decision layer's sidecar tier is the only
  remaining witness source

#### Scenario: Evidence exposes the provenance source tier

- **WHEN** a failure-state decision consumed forcing provenance (or
  determined its absence)
- **THEN** the decision evidence names the provenance source tier as
  one of journal, direct, object_store_sidecar, or absent, readable by
  an operator triaging a blocked recovery

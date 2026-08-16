## MODIFIED Requirements

### Requirement: Missing upstream artifacts SHALL demote failure-state retries to a stable repair-eligible blocker

When a candidate carries a failure signal and the forcing package its forecast stage references does not exist in the configured object-store root, the scheduler SHALL NOT emit another forecast retry from any decision branch — including the failure fallback and the permanent-failure branch — and SHALL instead emit the stable missing-forcing blocker (reason `missing_forcing_package_uri`, stable classifier, `artifact_exists` false, forecast restart stage) that the explicit single-cycle repair authorization channel accepts. Before treating a state with no forcing package reference as missing, the scheduler SHALL attempt provenance recovery through the witnessed read tiers (journal row, journal direct file, object-store forcing-version sidecar record derived from the candidate identity); a redaction placeholder standing in for a withheld URI is not a probeable package reference and SHALL take this recovery path rather than the recorded-URI probe (the public-read redaction boundary is never bypassed and the probe itself is never taught about placeholders). For the sidecar tier the existing artifact existence probe SHALL target the package manifest file key derived from the candidate-derived sidecar key (never the directory-shaped package URI, which the object-path validator rejects, and never the record's own manifest URI taken verbatim — that recorded URI serves as corroborating evidence only), so that a physically present package never produces the missing-forcing blocker, a witnessed-absent package produces exactly the unchanged `missing_forcing_package_uri` blocker, and a sidecar record pointing at a foreign manifest cannot stand in as this candidate's witness. A probe-layer store error after a successful sidecar witness SHALL be contained fail-closed (blocked, never an escaped exception aborting the scheduler pass) and SHALL classify as no-witness rather than as a determined-missing package, because an unreadable probe object is a read fault a package rebuild cannot clear. The sidecar read limit SHALL admit the provenance records the forcing producer actually writes in production, and a record exceeding that limit SHALL be reported with its own tier detail, distinct from a permission or I/O read failure. Only when no tier yields a witness SHALL the decision block with the distinct reason `forcing_version_row_absent` (error code and stable classifier `FORCING_VERSION_ROW_ABSENT`, null artifact reference, provenance source marked absent), which carries the same structural repair-eligible contract, and the single-cycle repair authorization channel — including its stable-classifier structural check — SHALL accept both blocker reason/classifier pairs. Provenance-tier failures (unreadable, malformed, oversized sidecar, unconfigured store, incomplete identity) SHALL classify as no-witness rather than as a determined-missing package, and SHALL never fail open into a retry. Failure classification produced inside the compute task SHALL survive the DB-free path to array accounting through a durable per-task outcome receipt, with generic `NODE_FAILURE` used only as the fail-safe when no receipt is readable. The effective retry attempt SHALL derive from the durable per-stage attempt record (job-identity retry suffix) on both the in-stage retry gate and the scheduler's cross-pass failure policy, so that the configured retry limit bounds retries even for misclassified failures. The copyback leg SHALL apply the same withheld-reference ruling: a redaction placeholder standing in for a copyback source reference is not a probeable reference and SHALL never reach the artifact existence probe; when copyback is required, the decision SHALL block with the distinct reason `copyback_source_withheld` (error code and stable classifier `COPYBACK_SOURCE_WITHHELD`, the placeholder itself carried as the artifact reference) rather than the determined-missing `missing_copyback_source` blocker, and a withheld copyback reference with no copyback requirement SHALL NOT block. The withheld-copyback blocker names a reference the public-read redaction boundary withheld: existence cannot be determined on that plane, and the blocker SHALL NOT enter the missing-forcing repair-authorization channel, whose forcing-rebuild remedy cannot clear a withheld reference; defining a clearing mechanism for it is deferred until a copyback write side exists to define one. The withheld ruling SHALL apply to the reference the alias resolution actually returned: when the first non-empty resolved value is a placeholder, the leg SHALL NOT continue scanning lower-priority aliases for a probeable substitute (a surviving unredacted echo is not a trustworthy stand-in for a withheld reference).

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

#### Scenario: A production-scale sidecar record still yields a witness

- **WHEN** the sidecar record carries the per-station lineage the
  forcing producer writes in production, making it multiple megabytes,
  and its package manifest file is present
- **THEN** the sidecar tier reads and parses the record, the decision
  does not emit a missing-forcing blocker, and the record size alone
  never degrades the tier into a no-witness outcome

#### Scenario: No witness at any provenance tier blocks with the distinct row-absent reason

- **WHEN** a candidate has a failure signal, its restart stage is the
  forecast stage, and no forcing provenance witness exists at any read
  tier (no journal row, no journal direct file, and the sidecar record
  is absent, unreadable, malformed, oversized beyond the read limit,
  the probe object itself is unreadable, the object store is
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

#### Scenario: Withheld copyback reference is never probed and blocks with the distinct withheld reason

- **WHEN** a failure-state candidate's state carries a copyback source reference equal to a redaction placeholder (e.g. `[object-uri]`) and copyback is required (restart stage `copyback` or the state requires a copyback source)
- **THEN** the copyback leg SHALL NOT invoke the artifact existence probe for the placeholder
- **THEN** the decision SHALL block with reason `copyback_source_withheld` and error code `COPYBACK_SOURCE_WITHHELD`, never `COPYBACK_SOURCE_MISSING`
- **THEN** the artifact guard SHALL carry `artifact_type` `copyback_source` and the withheld placeholder as its artifact reference

#### Scenario: Withheld copyback reference without a copyback requirement does not block

- **WHEN** a failure-state candidate's state carries a redaction placeholder for its copyback source but copyback is not required
- **THEN** the copyback leg SHALL NOT probe the placeholder and SHALL NOT emit any copyback blocker

#### Scenario: Withheld copyback blocker stays outside the forcing repair channel

- **WHEN** a `copyback_source_withheld` blocker is evaluated by the stable missing-forcing blocker predicate
- **THEN** it SHALL NOT classify as a stable missing-forcing blocker, because the forcing repair authorization's rebuild remedy cannot clear a withheld copyback reference

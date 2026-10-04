# forcing-submit-recovery Specification

## Purpose
TBD - created by archiving change preserve-forcing-submit-ambiguity. Update Purpose after archive.

## Requirements

### Requirement: New forcing attempts preserve unresolved acceptance
The orchestrator SHALL persist sufficient attempt/member identity before Gateway entry and SHALL distinguish unresolved acceptance from proven rejection, permanent failure and proven absence.

#### Scenario: Gateway crossed but response unverifiable
- **WHEN** a new forcing POST returns HTTP502 SLURM_PARSE_ERROR, transport failure or invalid success identity after entering the Gateway
- **THEN** durable state and stage/scheduler evidence SHALL preserve submission ambiguity, empty Slurm binding and origin error with unknown_after_attempt and no proven-absence claim
- **AND** automatic failure retry SHALL NOT submit the attempt again

#### Scenario: Explicit pre-acceptance rejection
- **WHEN** the Gateway proves a pre-acceptance policy or validation rejection
- **THEN** existing rejected semantics SHALL remain applicable

### Requirement: Unresolved forcing prevents overlapping submission across cohort keys
The scheduler SHALL atomically check and reserve forcing member authority by source/cycle/model overlap independently of stage-derived cohort run keys.

#### Scenario: Restart with renamed or overlapping cohort
- **WHEN** an unresolved new convert_cohort forcing attempt exists and a later/restarted pass derives forcing_cohort with identical, reordered, subset or overlapping members
- **THEN** no second forcing submission SHALL occur for intersecting members
- **AND** unrelated source/cycle/model work SHALL remain eligible

#### Scenario: Concurrent overlapping submissions
- **WHEN** two concurrent passes try to reserve intersecting forcing members
- **THEN** at most one SHALL be admitted to Gateway submission

### Requirement: Authoritative resolution permits normal stage continuation
The existing lifecycle SHALL resolve the new forcing attempt using trustworthy unique task identity/status/accounting and the existing complete array-accounting, aggregation and forecast-preparation path rather than leaving every ambiguity permanently fenced.

#### Scenario: Original execution is confirmed complete
- **WHEN** the accepted original attempt is uniquely bound and its existing normal forcing completion checks pass
- **THEN** the next normal orchestration SHALL continue to forecast without submitting forcing again

#### Scenario: Acceptance remains unproven
- **WHEN** existing authority cannot prove a unique execution or nonacceptance
- **THEN** the scheduler SHALL report unresolved/reconciling state without retry or skipping to forecast
- **AND** only existing trustworthy rejection/absence proof SHALL allow retry, never empty output alone

### Requirement: Existing production and forecast contracts remain unchanged
The fix SHALL preserve historical forecast identity/digest/reconciliation, #2439 phase semantics, strict forcing witnesses, successful legacy products and ordinary no-ambiguity forcing.

#### Scenario: No unresolved new attempt
- **WHEN** ordinary forcing has no conflicting unresolved authority
- **THEN** it SHALL submit and continue normally

#### Scenario: Sparse historical failed row has successful replacement
- **WHEN** a historical permanent/null-id forcing row lacks new identity fields and already has successful later execution, as49174/49309
- **THEN** the fix SHALL NOT reinterpret it into a new global or cycle-wide blocking authority or modify its history/products

#### Scenario: Forecast reconciliation
- **WHEN** existing forecast accepted-submit and strict warm-start tests run
- **THEN** their prior outcomes and historical identity bytes SHALL be preserved

### Requirement: Operator-verified absence exit for held forcing rows

The file journal SHALL provide an operator-verified absence exit for a forcing master that is `reserved`, unbound and `submit_result_ambiguous` with a complete forcing submit identity. The exit SHALL, under the journal lock, re-check stage, identity completeness, status, absence of a binding, the held state, and that the operator-supplied attempt and anchor equal the row's; any mismatch SHALL be rejected by name with zero writes. It SHALL also refuse a `checked_at` that is in the future or earlier than the attempt anchor plus the absence grace. On success it SHALL write the same retry-permitting row state as the automatic absence permit and record `checked_by`, `checked_at` and `verification_note` in an `operator_verified_absence` pipeline event, not on the row, and SHALL perform no sbatch and no scancel. `checked_by` and `verification_note` SHALL be operator-supplied and non-blank; there is no default.

#### Scenario: Held forcing row with no job in sacct
- **WHEN** the operator runs the exit with matching attempt and anchor, a verifier and verification evidence
- **THEN** the row becomes retryable, its members are no longer held-skipped, the audit fields are recorded and nothing is submitted

#### Scenario: Rejected exit
- **WHEN** the attempt or anchor does not match, the forcing row is not held, is already bound, its identity is incomplete, the verification is dated before the grace, or the verifier or evidence is blank
- **THEN** the command fails naming the reason and the journal is byte-identical

#### Scenario: Non-forcing row
- **WHEN** the same command targets a forecast master or an unknown job id
- **THEN** the existing forecast demotion contract applies unchanged and no forcing refusal name is produced

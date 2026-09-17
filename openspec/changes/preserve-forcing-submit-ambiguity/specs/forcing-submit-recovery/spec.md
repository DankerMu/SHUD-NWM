## ADDED Requirements

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

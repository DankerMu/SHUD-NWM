## MODIFIED Requirements

### Requirement: Complete per-source discharge result attribution
The prewarm CLI SHALL emit schema nhms.node27-mvt-prewarm.v4. Every source entry SHALL include integer discharge_ok and discharge_failed in addition to discharge_requests. Each issued discharge request SHALL increment the total and exactly one outcome bucket for its own source, using 2xx as success. Unissued requests SHALL NOT increment any discharge counter. Existing global failures, PNG accounting and exit status semantics SHALL remain unchanged.

#### Scenario: Mixed source-specific outcomes
- **WHEN** gfs discharge requests include success and failure and ifs has a different result distribution
- **THEN** each source exposes its exact separate successful and failed totals, discharge_requests equals their sum, and global failed_count includes all issued discharge failures.

#### Scenario: Deadline and unavailable source
- **WHEN** requests are skipped by the deadline or a source has no discovered cycle or discovery fails
- **THEN** unissued requests contribute zero to discharge_requests, discharge_ok and discharge_failed, and existing deadline/discovery failure reporting remains intact.

#### Scenario: Transport failure
- **WHEN** a discharge warmer raises an exception or returns a non-2xx result
- **THEN** that result increments only its source discharge_failed and existing global failure accounting.

#### Scenario: CLI error schema
- **WHEN** the CLI emits its existing top-level failure envelope
- **THEN** it uses schema nhms.node27-mvt-prewarm.v4 without fabricating per-source results.


## ADDED Requirements

### Requirement: Prewarm covers the full published default cycle
For each source, prewarm SHALL use all published valid times of its newest cycle in discovery order for discharge z3/z4 and apply the existing PNG horizon/error classification separately. It SHALL preserve supplied timestamp strings and multiplicity, and emit prewarm_scope=full_cycle rather than lead_hours. The global deadline, request timeout, source-interleaved ordering and nonzero incomplete/error exit status SHALL remain enforced; full-cycle scope SHALL NOT imply that deadline-skipped work was issued.

#### Scenario: Times beyond the former twelve-hour window
- **WHEN** both sources publish valid times extending beyond first-time plus twelve hours
- **THEN** every published time is scheduled for z3/z4 discharge, available and selected counts reflect that full set, and completed successful runs have issued discharge totals for the whole set.

#### Scenario: Full-cycle work exceeds deadline or PNG contract
- **WHEN** a job remains unissued at the deadline or a published time is outside the PNG horizon
- **THEN** deadline skips and PNG out-of-contract results remain explicit and nonzero-exit as before, without suppressing the other source or misclassifying issued discharge outcomes.

# prewarm-source-outcomes Specification

## Purpose
Attribute every issued discharge prewarm result to its source without losing failures or counting unissued work.
## Requirements
### Requirement: Complete per-source discharge result attribution
The prewarm CLI SHALL emit schema nhms.node27-mvt-prewarm.v3. Every source entry SHALL include integer discharge_ok and discharge_failed in addition to discharge_requests. Each issued discharge request SHALL increment the total and exactly one outcome bucket for its own source, using 2xx as success. Unissued requests SHALL NOT increment any discharge counter. Existing global failures, PNG accounting and exit status semantics SHALL remain unchanged.

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
- **THEN** it uses schema nhms.node27-mvt-prewarm.v3 without fabricating per-source results.


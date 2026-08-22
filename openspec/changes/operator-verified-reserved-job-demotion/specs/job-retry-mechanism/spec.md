## ADDED Requirements

### Requirement: Operator-verified absence is a distinct reclaimable file-journal decision

The file-journal reclaim predicate and the forecast-cycle reconcile-verified retry shortcut SHALL recognize exactly two absence decisions, `absence_retry_permitted` and `operator_verified_absence`, while retaining all existing accepted-submit identity, unbound, source, outcome, null-reason, attempt, anchor, and cohort validity checks. `operator_verified_absence` SHALL be an accepted accounting decision but SHALL NOT enter the generic versioned-transition whitelist, the manual-retry source-status set, or the identity-streak decision set. `identity_mismatch_released` and every other `reservation_lost` sub-shape SHALL remain non-reclaimable. A successful reclaim SHALL derive the new attempt from durable state, increment the attempt exactly once, and capture a fresh anchor under the lock rather than reusing the operator's old expected anchor.

#### Scenario: Operator-demoted cohort follows the existing reclaim and submit path

- **WHEN** the typed operator transition has durably produced an unbound `reservation_lost/operator_verified_absence` master with a null reason class and intact cohort identity
- **THEN** the cycle retry shortcut treats the forecast stage as retryable, reservation reclaim succeeds, the next attempt number is one greater, the new attempt anchor is lock-owned, and the existing submission path can submit the cohort once

#### Scenario: Automatic absence reclaim remains unchanged

- **WHEN** a master has the existing `reservation_lost/absence_retry_permitted` shape
- **THEN** the same shortcut and reclaim behavior continue without output or identity changes

#### Scenario: Identity release remains a spent non-reclaimable key

- **WHEN** a master has `reservation_lost/identity_mismatch_released` or any other non-absence decision
- **THEN** both the cycle retry shortcut and file-journal reclaim reject it, so the new operator token does not broaden the identity-release path

#### Scenario: Generic transition cannot forge operator authority

- **WHEN** a caller attempts to write `operator_verified_absence` through the generic versioned accepted-submit transition
- **THEN** the transition is rejected by the typed-authority gate and no journal evidence is changed

#### Scenario: Reclaim still rejects stale attempt identity

- **WHEN** an operator-demoted row is presented to reclaim with a stale attempt, stale anchor, mismatched immutable cohort identity, bound Slurm job id, non-null reason class, or matched job id
- **THEN** reclaim returns no reservation and writes nothing

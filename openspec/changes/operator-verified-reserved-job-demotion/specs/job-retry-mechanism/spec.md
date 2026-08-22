## ADDED Requirements

### Requirement: Operator-verified absence is a distinct reclaimable file-journal decision

The file-journal reclaim predicate and the forecast-cycle reconcile-verified retry shortcut SHALL widen only their accepted decision membership to exactly two absence decisions, `absence_retry_permitted` and `operator_verified_absence`. The cycle retry door SHALL retain its existing composition: the caller requires an unbound `reservation_lost` row, while the shortcut requires an accepted or ambiguous outcome, exact-comment source, null matched job id, and valid cohort identity; existing marker-free automatic-absence rows satisfying that legacy contract SHALL remain compatible. The file-journal reclaim CAS SHALL additionally retain its current-master/idempotency match, unbound, null-reason/null-match, exact expected attempt and anchor, and immutable cohort-identity predicates. `operator_verified_absence` SHALL be an accepted accounting decision but SHALL NOT enter the generic versioned-transition whitelist, the manual-retry source-status set, or the identity-streak decision set. `identity_mismatch_released` and every other non-absence `reservation_lost` sub-shape SHALL remain non-reclaimable. A successful current-master reclaim SHALL derive the new attempt solely from durable state, increment it exactly once, and capture a fresh anchor under the lock rather than accepting either value from the lock-external request.

#### Scenario: Operator-demoted cohort follows the existing reclaim and submit path

- **WHEN** the typed operator transition has durably produced an unbound `reservation_lost/operator_verified_absence` master with a null reason class and intact cohort identity
- **THEN** the cycle retry shortcut treats the forecast stage as retryable, reservation reclaim succeeds, the next attempt number is one greater, the new attempt anchor is lock-owned, and the existing submission path can submit the cohort once

#### Scenario: Automatic absence reclaim remains unchanged

- **WHEN** a current master has the existing `reservation_lost/absence_retry_permitted` shape, or the cycle shortcut receives a marker-free automatic-absence row satisfying its pre-existing status, binding, accounting, and cohort-identity checks
- **THEN** the same shortcut behavior remains compatible and current-master reclaim continues without output or identity changes

#### Scenario: Identity release remains a spent non-reclaimable key

- **WHEN** a master has `reservation_lost/identity_mismatch_released` or any other non-absence decision
- **THEN** both the cycle retry shortcut and file-journal reclaim reject it, so the new operator token does not broaden the identity-release path

#### Scenario: Generic transition cannot forge operator authority

- **WHEN** a caller attempts to write `operator_verified_absence` through the generic versioned accepted-submit transition
- **THEN** the transition is rejected by the typed-authority gate and no journal evidence is changed

#### Scenario: Reclaim still rejects stale attempt identity

- **WHEN** an operator-demoted row is presented to reclaim with a stale attempt, stale anchor, mismatched immutable cohort identity, bound Slurm job id, non-null reason class, or matched job id
- **THEN** reclaim returns no reservation and writes nothing

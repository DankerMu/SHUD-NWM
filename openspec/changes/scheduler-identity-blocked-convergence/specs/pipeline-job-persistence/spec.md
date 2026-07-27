# pipeline-job-persistence — delta for scheduler-identity-blocked-convergence

## ADDED Requirements

### Requirement: Reserved-unbound identity-mismatch outcomes SHALL converge instead of wedging the pipeline

The journal SHALL persist, on each versioned accepted-submit master row, a consecutive-outcome counter that increments each time restart reconciliation records an `identity_mismatch_blocked` outcome for that reserved-unbound row, saturates once it reaches the configured limit (and does not increment while the exit is disabled), and resets to zero whenever the row's accounting state is replaced by any other transition — including a bind, an absence-path release, or the start of a new submission attempt after a reclaim. When the counter reaches the configured limit and the row is past the accepted-submit grace period — anchored to the submission attempt start time, never to a timestamp refreshed by the counter's own writes — reconciliation SHALL migrate the row out of `reserved` into `reservation_lost` through a dedicated compare-and-swap journal transition (expected attempt, attempt anchor, expected `reserved` status, unbound required) recording the typed decision `identity_mismatch_released` and preserving the counter's final value. The released row is a deliberately non-reclaimable terminal: its idempotency key SHALL NOT be revivable through reservation reclaim; liveness is preserved because, when the retry budget still allows, new attempts mint new retry-suffixed keys. A disabled or non-positive limit SHALL preserve today's behavior (no release). The closed master-status vocabulary SHALL NOT gain new members for this exit, and the generic evidence-transition API's decision whitelist SHALL NOT be widened.

#### Scenario: Consecutive identity-mismatch outcomes release the reservation

- **WHEN** a reserved-unbound row records `identity_mismatch_blocked` on N consecutive reconcile passes, N reaches the configured limit, and the row is past the accepted-submit grace
- **THEN** the row transitions `reserved` → `reservation_lost` with reconciliation decision `identity_mismatch_released`, the counter's final value is preserved on the row, and subsequent passes no longer surface the row as reserved-unbound — unwedging cycle-level orchestration that previously failed with `PIPELINE_ALREADY_ACTIVE`

#### Scenario: A non-blocked outcome resets the streak

- **WHEN** a reserved-unbound row records `identity_mismatch_blocked` outcomes followed by any different reconcile outcome before the limit is reached
- **THEN** the counter resets to zero and the release exit does not trigger until a fresh consecutive run reaches the limit

#### Scenario: A reclaimed reservation starts a fresh streak

- **WHEN** a row accumulates blocked outcomes, exits through the absence path, is reclaimed into a new submission attempt, and then records its first `identity_mismatch_blocked` outcome
- **THEN** the counter has restarted from zero — the stale pre-reclaim streak does not make the first post-reclaim blocked outcome trigger the release

#### Scenario: Guards hold the release closed

- **WHEN** the counter reaches the limit but the row is within the accepted-submit grace, or the limit is disabled (unset, zero, or negative), or the release compare-and-swap fails because the row's attempt state moved concurrently
- **THEN** no status migration occurs and the pass records the ordinary `identity_mismatch_blocked` outcome

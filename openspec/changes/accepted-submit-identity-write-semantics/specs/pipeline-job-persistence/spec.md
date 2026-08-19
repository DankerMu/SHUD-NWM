# pipeline-job-persistence Spec Delta

## MODIFIED Requirements

### Requirement: Accepted-submit cohort forecast terminal rows SHALL record init-state identity forward-only

The accepted-submit cohort forecast path SHALL persist the init-state identity (`init_state_id`, `checksum`, `uri`, `valid_time`) **at reservation time**, where the planning context is available, as a per-model identity mapping keyed by `array_task_id`/`model_id` on the cohort master row, outside the cohort-digest input set; terminal per-model row construction SHALL read each row's identity from the master-row mapping by its own `array_task_id` rather than from cohort-member projection, and a scalar single-identity field SHALL NOT be used. The recording SHALL NOT alter the ordinary-upsert frozen-field semantics and SHALL NOT enter the cohort-digest member field set — historical rows' `forecast_cohort_digest` validation results SHALL be unchanged. The identity's value SHALL be stable from the **first** reservation onward: reclaiming a dead reservation into a new submission attempt SHALL NOT refresh the mapping, and derived per-model rows SHALL reject a divergent ordinary-upsert write to the mapping exactly as the master row does. That rejection SHALL apply only to writes that explicitly carry the mapping — an ordinary upsert that omits the field SHALL continue to keep the persisted value silently, as it does today. Invalid or partial identity payloads SHALL be rejected by accepted-submit normalization. Existing journal rows without these fields SHALL remain readable unchanged — no migration, no backfill, no rewrite of historical rows.

#### Scenario: New cohort terminal rows carry the identity

- **WHEN** a cohort forecast job reserved after this change reaches a terminal status through the accepted-submit path
- **THEN** its journal row records the init-state identity captured at reservation time

#### Scenario: Historical cohort digests are untouched

- **WHEN** normalization validates a pre-change cohort row's `forecast_cohort_digest` after this change is deployed
- **THEN** the validation result is identical to before this change

#### Scenario: Legacy rows stay untouched and readable

- **WHEN** the journal contains pre-change cohort rows without init-state fields
- **THEN** readers treat the record as absent without error and no writer mutates those rows

#### Scenario: Invalid identity is rejected by the invariant gates

- **WHEN** an upsert presents an init-state identity payload with a malformed or partial field set
- **THEN** accepted-submit normalization rejects the transition rather than persisting a partial record

#### Scenario: An upsert that omits the mapping keeps the persisted value

- **WHEN** an ordinary upsert targets a derived per-model accepted-submit row without carrying an
  init-state identity mapping at all
- **THEN** the write succeeds and the persisted mapping is kept unchanged — the freeze SHALL NOT
  fail closed on the row-constructor's default empty value.

#### Scenario: Derived per-model rows freeze the mapping like the master row

- **WHEN** an ordinary upsert targets a derived per-model accepted-submit row and carries an
  init-state identity mapping that differs from the persisted one — including a public-view
  round-trip whose object URI has been replaced by a display placeholder, an explicitly empty
  mapping, and a structurally valid mapping with different content
- **THEN** the write is rejected with an evidence-invariant error and the durable journal payload
  retains the value captured at reservation time.

#### Scenario: A reclaimed reservation keeps the first attempt's mapping

- **WHEN** a dead reservation is reclaimed into a new submission attempt and the reclaim request
  row carries a freshly recomputed init-state identity mapping that differs from the persisted one
- **THEN** the persisted mapping remains the first attempt's value, the reclaim still succeeds with
  its submission attempt incremented and its attempt anchor restamped, and terminal per-model rows
  projected afterwards carry that same first-attempt mapping.

#### Scenario: A public-view snapshot is not a valid write payload

- **WHEN** a caller replays an unmodified public-view snapshot of an accepted-submit master row
  back through the ordinary upsert path, where the public view has replaced object URIs with
  display placeholders
- **THEN** the write is rejected rather than laundering the placeholder into durable state.

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

#### Scenario: The streak and release invariants are test-anchored

- **WHEN** the invariant guards for this counter and this decision are exercised — a negative or
  non-integer streak, a pre-outcome transition carrying a non-zero streak, an
  `identity_mismatch_released` decision whose status is not `reservation_lost`, and a
  non-identity-mismatch decision carrying a non-zero streak
- **THEN** each guard rejects the transition with its typed error and leaves the journal row
  unchanged, and each guard has a negative test that fails when that guard alone is removed.

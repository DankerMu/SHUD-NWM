# job-retry-mechanism (delta)

## MODIFIED Requirements

### Requirement: Strict-warm-start terminal mismatch retries SHALL respect a stage-scoped budget

When the candidate ladder would emit `retry_strict_warm_start_terminal_init_state_mismatch` for a terminal-success candidate whose recorded init-state identity mismatches the strict warm-start resolution, the scheduler SHALL first evaluate the stage-scoped retry attempt against the configured retry limit. When the attempt has reached the limit, the scheduler SHALL emit the stable blocked decision `blocked_strict_warm_start_init_state_mismatch` carrying a retry-policy block (automatic retry not allowed, manual retry required, attempt, retry limit) instead of the retry decision, and the blocked decision SHALL NOT participate in forced terminal resubmission or replacement-retry scoping. When the attempt is below the limit, the retry decision and its evidence SHALL remain unchanged. The stage-scoped attempt SHALL bind in the production geometry where the reserved master row carries no retry count and attempts are recorded only as retry-suffixed pipeline job rows — **including the reverse geometry where the maximum-attempt retry-suffixed row is older than `job_limit` fresher rows of other stages**: on the file-journal candidate-state projection (the production path, which reads the cycle's rows unlimited before projecting), the `job_limit` truncation SHALL retain, for every canonical downstream stage whose maximum effective retry attempt in the projection input is non-zero, the pipeline-job row carrying that maximum (derived through the same authoritative-stage-field and `effective_retry_attempt` chain the budget consumers use — never job-id substring parsing and never a locally forked stage-alias table), subject to the `job_limit` hard cap below, so that stage-scoped attempt derivation over the truncated projection returns the true upper bound for every retained stage. The `job_limit` hard cap SHALL never be exceeded (when retained upper-bound rows alone would exceed it, the freshest by truth timestamp win), and geometries where the retained rows already fall inside the freshness window SHALL produce an element-for-element identical projection to the pure-freshness selection. The DB-backed candidate-state read path, which truncates in SQL upstream of the projection, is explicitly outside this guarantee.

#### Scenario: Budget exhaustion demotes the retry to a stable blocked decision

- **WHEN** a completed cycle's candidate has a strict warm-start init-state mismatch and its stage-scoped retry attempt has reached the retry limit
- **THEN** the decision is `blocked_strict_warm_start_init_state_mismatch` with the retry-policy block, no forecast work is selected for resubmission, and the candidate is not re-selected on subsequent passes while the mismatch persists

#### Scenario: Below-budget behavior is unchanged

- **WHEN** the same mismatch is observed while the stage-scoped attempt is below the retry limit
- **THEN** the emitted decision and evidence are byte-identical to today's `retry_strict_warm_start_terminal_init_state_mismatch` shape

#### Scenario: The blocked decision is excluded from force-resubmit whitelists

- **WHEN** orchestration evaluates forced terminal resubmission and replacement-retry scoping against a candidate carrying the blocked decision
- **THEN** neither whitelist matches — their member sets are unchanged by this change — and no replacement submission occurs

#### Scenario: The budget binds against retry-suffixed journal rows

- **WHEN** the candidate state derives from a journal containing a reserved master row with zero retry count and stage-matching `*_retry_N` pipeline job rows mirroring the production wedge geometry
- **THEN** the stage-scoped attempt evaluates to at least N and the demotion triggers once N reaches the retry limit

#### Scenario: The budget binds in the reverse truncation geometry

- **WHEN** the `*_forecast_retry_N` row carrying the stage's maximum attempt is older than `job_limit` fresher rows of other stages and `N` has reached the retry limit
- **THEN** the truncated file-journal projection still yields stage-scoped attempt `N` and the demotion to `blocked_strict_warm_start_init_state_mismatch` triggers instead of an unbudgeted retry

#### Scenario: Friendly geometry projection is unchanged

- **WHEN** the maximum-attempt rows already sit inside the freshness window
- **THEN** the truncated projection equals the freshness-ordered top-`job_limit` selection element for element, in the same order

#### Scenario: Hard cap survives degenerate retention

- **WHEN** the per-stage upper-bound rows alone would exceed `job_limit`
- **THEN** the projection still contains exactly `job_limit` rows, chosen among the upper-bound rows by freshest truth timestamp

#### Scenario: Zero-attempt stages claim no retention slot

- **WHEN** a canonical stage's maximum effective attempt in the input is zero
- **THEN** no row of that stage is retained beyond what the freshness window admits, and the stage-scoped derivation still returns zero

#### Scenario: Copyback is retained and download is not a retention stage

- **WHEN** the input carries an out-of-window maximum-attempt `copyback` row and out-of-window `download` rows
- **THEN** the `copyback` upper-bound row is retained (it is a canonical downstream stage) while `download` rows claim no retention slot (not a canonical downstream stage), matching the consumer-side canonical-stage table rather than any locally forked alias table

## ADDED Requirements

### Requirement: Released identity-blocked reservation rows SHALL remain outside automatic retry classification

A pipeline-job row produced by identity-blocked reservation release (`status="reservation_lost"`, `identity_mismatch_released` sub-shape) SHALL carry no `error_code` — the reservation writes that seed the row SHALL keep setting `error_code` to null, and the release transition SHALL NOT introduce one — and `should_auto_retry`/`classify_failure` SHALL therefore evaluate it as non-retriable. Tests SHALL pin both the written row shape (driving the real reserve-then-release sequence, not a hand-built row) and the `should_auto_retry` verdict, so that any future edit stamping a transient error code onto the reservation or release writes fails the pin instead of silently opening a duplicate-submission route. This requirement governs only the automatic-retry classification decision: the reservation reclaim predicate and the reconcile-verified retry shortcut (the two reclaim doors that the existing "Lost reservations are not mark sources" requirement keeps open) are explicitly unaffected.

#### Scenario: Release row shape carries no error code

- **WHEN** an identity-blocked reservation is reserved and then released through the real transition sequence
- **THEN** the resulting accounting row has `status == "reservation_lost"` and a null `error_code`

#### Scenario: Released row is not auto-retriable

- **WHEN** automatic retry classification evaluates the released reservation row
- **THEN** `should_auto_retry` is false, and the pin fails the moment any transient error code is introduced on the reservation or release writes

#### Scenario: Reclaim doors stay open

- **WHEN** the reservation reclaim predicate or the reconcile-verified retry shortcut evaluates the released row
- **THEN** their existing behavior is unchanged by this requirement

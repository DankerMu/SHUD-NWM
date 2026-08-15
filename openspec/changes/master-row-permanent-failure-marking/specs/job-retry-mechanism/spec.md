# job-retry-mechanism (delta)

## ADDED Requirements

### Requirement: Permanent-Failure Marking Covers Master-Row Geometry

The permanent-failure mark required for non-transient failures and for exhausted retry budgets SHALL land on file-journal master-row geometry through every exit that declines automatic retry, with the same observable semantics as non-master rows, without weakening the journal's master-row write restrictions.

This extends the existing "Retry Guard — Non-Transient Error Exclusion" and
"Max Retries Exhausted — Permanent Failure" requirements to master-row
geometry; it does not alter their code classification or budget semantics.

#### Scenario: Master row declined for auto-retry is marked permanently failed

- **WHEN** a file-journal `pipeline_job` row whose accepted-submit row kind
  is `master` fails and automatic retry is declined — whether because the
  error code is non-transient (e.g. `OUT_OF_MEMORY`, `INVALID_MANIFEST`),
  unknown and defaulted non-transient, or transient with the retry budget
  exhausted
- **THEN** if the row's persisted status is a markable failure source
  (`failed`, `submission_failed`), it SHALL transition
  to `status="permanently_failed"` through a typed journal authority
  transition, regardless of which decline exit ran (lost reservations and
  partially failed cohorts are carved out below)
- **THEN** the transition SHALL preserve the row's accepted-submit
  accounting evidence (reconciliation decision, submit outcome, matched
  Slurm job id) unchanged
- **THEN** a `pipeline_event` with `event_type="permanently_failed"` and the
  row's real prior status SHALL be appended exactly once per actual status
  transition
- **THEN** the decline outcome SHALL remain: no automatic retry scheduled,
  no new `pipeline_job` rows created, and the cycle's `PipelineResult`
  status remains `"failed"`
- **THEN** a journal write failure while marking SHALL NOT alter the decline
  outcome — the decline exits still return their pre-marking results, the
  failure is surfaced as an operator-visible signal rather than an exception
  escaping into the orchestration cycle, and the idempotent mark is
  re-attempted on a later pass

#### Scenario: The mark survives subsequent cohort projection passes

- **WHEN** a later orchestration pass resumes or re-projects the cohort of a
  master row already marked `permanently_failed`
- **THEN** the row SHALL remain `permanently_failed` (projection updates its
  evidence fields without reverting the terminal status) and no additional
  `permanently_failed` or reverting status-change event SHALL be appended

#### Scenario: Marking is idempotent against stale callers

- **WHEN** a decline runs again for a row already persisted as
  `permanently_failed` (including callers holding a stale job snapshot), or
  runs with a stale snapshot claiming `permanently_failed` while the
  persisted row is still `failed`
- **THEN** the idempotency decision SHALL consult the persisted row: an
  already-marked row is returned unchanged with no duplicate event, and a
  not-yet-marked row is still marked despite the stale snapshot

#### Scenario: The typed transition only accepts markable failure sources

- **WHEN** the typed permanent-failure transition is invoked on a master row
  whose persisted status is outside the markable set
  `{failed, submission_failed}` (e.g. `running`, `reserved`, `succeeded`,
  `cancelled`, `partially_failed`, `reservation_lost`)
- **THEN** the row SHALL remain unchanged, no event SHALL be appended, and
  the call SHALL report a stale/no-op outcome rather than raising

#### Scenario: Partially failed cohorts keep partial-advance semantics

- **WHEN** a mixed-outcome forecast cohort projects its master row to
  `partially_failed` and a failed member's error declines automatic retry
  through the nested partial-array-retry exit (whether non-transient or
  retry-budget exhausted)
- **THEN** the master row SHALL NOT be marked `permanently_failed` — the
  mark declines as stale with zero writes and zero events — and a subsequent
  resume pass SHALL behave exactly as before marking existed: the cohort's
  succeeded members keep advancing through downstream stages, the cycle's
  terminal status stays the partial outcome, and no failure terminal is
  written in place of the partial one

#### Scenario: Lost reservations are not mark sources

- **WHEN** a decline exit or any caller invokes the mark on a master row
  whose persisted status is `reservation_lost` (either durable sub-shape:
  `absence_retry_permitted` or `identity_mismatch_released`)
- **THEN** the mark SHALL be declined as stale with zero writes and zero
  events and SHALL NOT raise — a lost reservation is reclaim-pending, not a
  permanently failed job, and both reclaim doors (the reservation reclaim
  predicate and the reconcile-verified retry shortcut) SHALL remain open

#### Scenario: Marked master rows do not resurrect via upstream refresh

- **WHEN** a master row already marked `permanently_failed` would otherwise
  qualify for the upstream-refresh resubmission path available to `failed`
  rows
- **THEN** no resubmission SHALL occur — matching the semantics non-master
  rows already receive from their permanent-failure mark, for both the
  non-transient and the exhausted-budget domains

#### Scenario: Master row with transient code and remaining budget keeps existing retry identity behavior

- **WHEN** a master row fails with a transient error code and
  `should_auto_retry` is true
- **THEN** the existing master-row retry-identity flow SHALL proceed
  unchanged and the row SHALL NOT be marked permanently failed

#### Scenario: Marking is scoped to retry services with journal capability

- **WHEN** the decline logic runs with a retry service that lacks
  file-journal persistence capability
- **THEN** the decline SHALL keep its existing behavior — no scheduling, no
  marking, and no raising

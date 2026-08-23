## ADDED Requirements

### Requirement: Operator-verified absence is a distinct reclaimable file-journal decision

The file-journal reclaim predicate and the forecast-cycle reconcile-verified retry shortcut SHALL widen only their accepted decision membership to exactly two absence decisions, `absence_retry_permitted` and `operator_verified_absence`. The cycle retry door SHALL retain its existing composition: the caller requires an unbound `reservation_lost` row, while the shortcut requires an accepted or ambiguous outcome, exact-comment source, null matched job id, and valid cohort identity; existing marker-free automatic-absence rows satisfying that legacy contract SHALL remain compatible. The file-journal reclaim CAS SHALL additionally retain its current-master/idempotency match, unbound, null-reason/null-match, exact expected attempt and anchor, and immutable cohort-identity predicates. `operator_verified_absence` SHALL be an accepted accounting decision but SHALL NOT enter the generic versioned-transition whitelist, the manual-retry source-status set, or the identity-streak decision set. `identity_mismatch_released` and every other non-absence `reservation_lost` sub-shape SHALL remain non-reclaimable. A successful current-master reclaim SHALL derive the new attempt solely from durable state, increment it exactly once, and capture a fresh anchor under the lock rather than accepting either value from the lock-external request.

#### Scenario: Operator-demoted cohort follows the existing reclaim and submit path

- **WHEN** the typed operator transition has durably produced an unbound `reservation_lost/operator_verified_absence` master with a null reason class and intact cohort identity
- **THEN** the cycle retry shortcut treats the forecast stage as retryable, reservation reclaim succeeds, the next attempt number is one greater, the new attempt anchor is lock-owned, and the existing submission path can submit the cohort once

#### Scenario: Automatic absence reclaim remains unchanged

- **WHEN** a current master has the existing `reservation_lost/absence_retry_permitted` shape, or the cycle shortcut receives a marker-free automatic-absence row satisfying its pre-existing status, binding, accounting, and cohort-identity checks
- **THEN** lower-level automatic reclaim keeps its existing attempt, anchor, and identity derivation; automatic public cycles keep their retry-suffixed replacement identity, while only operator-demoted recovery reuses the old master's job id and idempotency key

#### Scenario: Identity release remains a spent non-reclaimable key

- **WHEN** a master has `reservation_lost/identity_mismatch_released` or any other non-absence decision
- **THEN** both the cycle retry shortcut and file-journal reclaim reject it, so the new operator token does not broaden the identity-release path

#### Scenario: Generic transition cannot forge operator authority

- **WHEN** a caller attempts to write `operator_verified_absence` through the generic versioned accepted-submit transition
- **THEN** the transition is rejected by the typed-authority gate and no journal evidence is changed

#### Scenario: Reclaim still rejects stale attempt identity

- **WHEN** an operator-demoted row is presented to reclaim with a stale attempt, stale anchor, mismatched immutable cohort identity, bound Slurm job id, non-null reason class, or matched job id
- **THEN** reclaim returns no reservation and writes nothing

## MODIFIED Requirements

### Requirement: Permanent-Failure Marking Covers Master-Row Geometry

The permanent-failure mark required for non-transient failures and for exhausted retry budgets SHALL land on file-journal master-row geometry through every exit that declines automatic retry for rows whose persisted status is a markable failure source (`failed`, `submission_failed`; carve-outs below), with the same observable semantics as non-master rows, without weakening the journal's master-row write restrictions.

This extends the existing "Retry Guard — Non-Transient Error Exclusion" and
"Max Retries Exhausted — Permanent Failure" requirements to master-row
geometry; within the markable domain it does not alter their code
classification or budget semantics. Two master-row shapes are not "failed
jobs" in the sense of those base requirements and are carved out by
scenarios below: `partially_failed` masters (the cohort retains partial
success and stays governed by the partial-advance contract) and
`reservation_lost` masters (reclaim-pending, not permanently failed).

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
  whose persisted status is `reservation_lost` (one of the three durable
  decision sub-shapes: `absence_retry_permitted`,
  `operator_verified_absence`, or `identity_mismatch_released`)
- **THEN** the mark SHALL be declined as stale with zero writes and zero
  events and SHALL NOT raise — a lost reservation is not a permanently failed
  job; the reservation reclaim predicate and reconcile-verified retry shortcut
  SHALL remain open only for the two absence decisions
  (`absence_retry_permitted` and `operator_verified_absence`) and SHALL remain
  closed for `identity_mismatch_released`

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

### Requirement: Released identity-blocked reservation rows SHALL be recoverable through an operator-gated path and SHALL announce themselves

A released identity-blocked reservation row SHALL be recoverable by an operator and SHALL NOT be a silent terminal.
The shape this requirement governs is `status == "reservation_lost"`,
`reconciliation_decision == "identity_mismatch_released"`, `slurm_job_id` null,
`matched_slurm_job_id` null, on a current-contract cohort master.

**Recovery is a marker, not a row.** The operator-gated recovery API SHALL record
a durable operator-recovery attestation **on the released row itself** and SHALL
NOT pre-materialize the successor pipeline-job row. Writing the successor eagerly
is forbidden because it occupies the very `job_id` and idempotency key that the
ordinary retry path would mint, and the ordinary path refuses to submit a row it
did not itself reserve — the recovery would consume the only submittable identity
and leave it inert. The recovery's only durable output SHALL be an input the
ordinary submission path already consumes.

**Liveness.** After the attestation is recorded, an ordinary scheduler pass SHALL
reach an actual submission for that cohort: the stage SHALL mint the next
`_retry_<n>` identity through the existing retry-identity derivation, and the
reservation for that identity SHALL be newly created rather than refused as
already in flight. The recovered attempt SHALL participate in ordinary candidate
selection exactly as any other retry attempt does — it SHALL NOT carry the
released row's member set forward, because the ordinary retry convention is to
build each attempt's reservation from the then-current cohort, and a stale member
set would silently re-run a superseded basin manifest.

**Attestation is required and is not a proof.** The attestation SHALL be settable
only by an explicit operator action, **and that action SHALL exist as a supported
operator entry point** — a human SHALL be able to discover a row in this shape and
act on it without source access or an interactive interpreter. A recovery
mechanism with no invocation surface does not satisfy this requirement: it is
indistinguishable, for the operator the requirement exists for, from the silent
terminal it replaces. The entry point SHALL cover both halves — discovery (which
rows are in this shape, and the values needed to act on them) and the action —
because an operator who cannot obtain the action's required inputs is stopped one
step earlier and no better off. A refusal SHALL name which precondition failed,
and SHALL be write-free. Repeating the action SHALL be idempotent. The entry point
SHALL be a command-line surface with **no automatic caller and no HTTP route** —
the node this runs on is db-free compute and the operator is on a shell there.
Its help text SHALL state, at the point of use, that no Slurm-side liveness or
absence check is performed and that invoking it is an attestation, not a proof:
the one place a human is guaranteed to read this is where they are about to act. The recovery API SHALL NOT perform any
Slurm-side liveness or absence check, and SHALL NOT be described as one: on a
cluster whose accounting does not store job comments, absence is not provable.
Invoking it places that judgement with the operator rather than with the machine.
Recording the attestation twice on the same row SHALL be idempotent.

**The lower-level predicates accept exactly two absence decisions; the
released-row disjunct is additive.** The reconcile-verified retry predicate SHALL
retain all of its non-decision gates — the accepted or ambiguous submit outcome,
the exact-comment source, the null matched Slurm job id, and the valid cohort
identity — and SHALL accept exactly two legitimate absence decisions:
`absence_retry_permitted` and `operator_verified_absence`. The file-journal
reservation reclaim predicate SHALL likewise retain all of its non-decision gates
— current-master/idempotency match, unbound, null reason class and null matched
job id, exact expected attempt and anchor, and immutable cohort identity — and
SHALL accept exactly the same two absence decisions. `absence_retry_permitted`
retains its existing byte-for-byte meaning and automatic behavior.
`operator_verified_absence` is the only second legitimate decision member and can
be authored only by the dedicated typed operator-demotion path. The released-row
geometry this requirement governs (`identity_mismatch_released` plus the
`operator_recovery_attested_at` attestation field) remains an additive, separate
disjunct at the consuming call site — neither `identity_mismatch_released` nor the
attestation field may enter, widen, weaken, or reorder either lower-level
predicate. No automatic path SHALL be able to set the attestation or reach the
recovery API.

**Signal.** The release write point SHALL emit, exactly once per release, a
queryable operator-visible record carrying the searchable token
`IDENTITY_RELEASED_RESERVATION_NEEDS_OPERATOR`, naming the job id, the cohort
digest, and the `identity_blocked_streak`. The record SHALL be emitted for a
released row regardless of which prior state it arrived in — a fresh reservation
or one re-seeded through reservation reclaim. Freezing the row without such a
record is not permitted, because the row is otherwise indistinguishable from an
ordinary in-flight reservation until a human happens to read the journal.

**The signal SHALL NOT be able to fail the release.** The release write is
durable and has no rollback, and the release path is never re-entered for an
already-released row, so a raising emission would leave the row permanently
released with no record — reproducing the very silent terminal this requirement
exists to end. The emission SHALL therefore be best-effort with respect to the
release: a failure SHALL NOT propagate out of the release call, and SHALL NOT
abort the enclosing reconcile pass. **The fallback SHALL NOT be silent**: when
the primary emission fails, the failure itself SHALL leave a durable, queryable
trace through whatever channel remains available, and only if that too fails may
it degrade to a log — never to nothing, and never to a raise. A refusal that
makes no release write SHALL remain write-free and signal-free.

#### Scenario: an ordinary pass submits after operator recovery

- **WHEN** an operator records the recovery attestation on a released
  identity-blocked reservation row, and an ordinary scheduler pass then runs the
  forecast stage for that cycle
- **THEN** the pass SHALL mint the next `_retry_<n>` identity for the stage
- **AND** the reservation for that identity SHALL be newly created, not refused
  as already in flight
- **AND** the pass SHALL reach the stage's submission call rather than skipping
  it as a duplicate submission

#### Scenario: recovery writes no successor row

- **WHEN** the recovery API is invoked on a released identity-blocked
  reservation row
- **THEN** no pipeline-job row SHALL be created by that call
- **AND** the `_retry_<n>` identity that the ordinary path would mint SHALL
  remain unoccupied

#### Scenario: recovery refuses shapes it does not own

- **WHEN** the recovery API is invoked on a row whose
  `reconciliation_decision` is not `identity_mismatch_released`, or whose
  `slurm_job_id` or `matched_slurm_job_id` is bound, or which is not a
  current-contract cohort master
- **THEN** the call SHALL be refused and no attestation SHALL be recorded

#### Scenario: repeated attestation is idempotent

- **WHEN** the recovery API is invoked twice on the same released row
- **THEN** the second call SHALL leave the row in the same state as the first
- **AND** the cohort SHALL still yield at most one recovered attempt, because the
  ordinary reservation path's own conflict gate owns that exclusion

#### Scenario: without the attestation nothing changes

- **WHEN** an ordinary scheduler pass runs the forecast stage for a released
  identity-blocked reservation row that carries no operator attestation
- **THEN** the stage SHALL behave exactly as it does today, and no submission
  SHALL occur

#### Scenario: automatic classification is unchanged by the recovery path

- **WHEN** automatic retry classification evaluates a released identity-blocked
  reservation row, before or after the recovery API exists
- **THEN** `should_auto_retry` SHALL be false and the row's `error_code` SHALL be
  null

#### Scenario: an operator can discover and act without source access

- **WHEN** a row enters the released identity-blocked shape and an operator goes
  looking for it through supported tooling
- **THEN** the operator SHALL be able to enumerate rows in that shape together
  with the values the recovery action requires
- **AND** SHALL be able to perform the attestation through the same supported
  tooling, with no interactive interpreter and no access to the source tree
- **AND** a refused attempt SHALL name the precondition that failed and SHALL
  write nothing
- **AND** after the attestation, an ordinary scheduler pass SHALL reach a real
  submission for that cohort — the operator-facing path SHALL be pinned end to
  end, not only the method behind it

#### Scenario: a failing signal does not undo or hide the release

- **WHEN** the operator-visible record cannot be written after the release write
  has already landed durably — whether the failure is a journal error or a
  filesystem/OS error
- **THEN** the release call SHALL NOT raise and the reconcile pass SHALL NOT be
  aborted
- **AND** the row SHALL remain durably released
- **AND** a durable trace of the emission failure SHALL exist, so the released
  row is not left indistinguishable from one that signalled correctly

#### Scenario: the release announces the terminal from either prior state

- **WHEN** an identity-blocked reservation is released after a fresh
  reservation, and separately when it is released after the reservation was
  re-seeded through reclaim
- **THEN** each release SHALL emit exactly one operator-visible record carrying
  the token `IDENTITY_RELEASED_RESERVATION_NEEDS_OPERATOR` and naming the job id,
  cohort digest, and `identity_blocked_streak`

## ADDED Requirements

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
step earlier and no better off. A refusal SHALL name which precondition failed. The recovery API SHALL NOT perform any
Slurm-side liveness or absence check, and SHALL NOT be described as one: on a
cluster whose accounting does not store job comments, absence is not provable.
Invoking it places that judgement with the operator rather than with the machine.
Recording the attestation twice on the same row SHALL be idempotent.

**The door predicate is unchanged; the operator disjunct is additive.** The
reconcile-verified retry predicate SHALL keep requiring
`absence_retry_permitted` byte-for-byte, and the reservation reclaim predicate
SHALL keep requiring it too. The operator attestation SHALL be admitted as an
additional, separate disjunct at the consuming call site — never by widening,
weakening, or reordering either predicate itself. No automatic path SHALL be able
to set the attestation or reach the recovery API.

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
- **AND** a refused attempt SHALL name the precondition that failed

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

## ADDED Requirements

### Requirement: Released identity-blocked reservation rows SHALL be recoverable through an operator-gated path and SHALL announce themselves

A released identity-blocked reservation row SHALL be recoverable by an operator and SHALL NOT be a silent terminal.
The shape this requirement governs is `status == "reservation_lost"`,
`reconciliation_decision == "identity_mismatch_released"`, `slurm_job_id` null,
`matched_slurm_job_id` null, on a current-contract cohort master.

**Recovery.** A dedicated typed recovery API SHALL mint the next
`_retry_<n>` identity for such a row through the same helper that the automatic
arms use, so the suffix derivation stays single-sourced. The API SHALL preserve
the row's cohort identity (`cohort_digest` and `cohort_members`) onto the
successor unchanged — a recovered attempt is the same cohort, never a re-picked
one. It SHALL be CAS-guarded on the expected submission attempt and attempt
anchor, so a concurrently advanced attempt loses the race instead of producing a
second successor. It SHALL additionally refuse a repeat invocation on a row it
has already recovered — minting a successor does not by itself advance the source
row's attempt fields, so the CAS guard alone does not prevent one released row
from yielding two successors. It SHALL refuse every row outside the shape above.

**No absence proof.** The recovery API SHALL NOT perform any Slurm-side liveness
or absence check, and SHALL NOT be described as one. On a cluster whose
accounting does not store job comments, absence is not provable; invoking the API
is an operator attestation, and the requirement deliberately places that judgement
with the operator rather than with the machine.

**No automatic reach.** The recovery API SHALL NOT consult
`should_auto_retry`, SHALL NOT write an `error_code`, and SHALL NOT be reachable
from any automatic arm. The companion requirement "Released identity-blocked
reservation rows SHALL remain outside automatic retry classification" stays in
force and unweakened: this requirement governs only the operator-initiated path.

**Signal.** The release write point SHALL emit, exactly once per release, a
queryable operator-visible record carrying the searchable token
`IDENTITY_RELEASED_RESERVATION_NEEDS_OPERATOR`, naming the job id, the cohort
digest, and the `identity_blocked_streak`. The record SHALL be emitted for a
released row regardless of which prior state it arrived in — a fresh reservation
or one re-seeded through reservation reclaim. Freezing the row without such a
record is not permitted, because the row is otherwise indistinguishable from an
ordinary in-flight reservation until a human happens to read the journal.

#### Scenario: operator recovers a released cohort master

- **WHEN** an operator invokes the recovery API on a row in the released
  identity-blocked shape
- **THEN** exactly one successor row SHALL be created whose `job_id` and
  idempotency key carry the next `_retry_<n>` suffix
- **AND** the successor SHALL carry the same `cohort_digest` and the same
  `cohort_members` as the released row
- **AND** the released row's own `error_code` SHALL remain null

#### Scenario: recovery refuses shapes it does not own

- **WHEN** the recovery API is invoked on a row whose
  `reconciliation_decision` is not `identity_mismatch_released`, or whose
  `slurm_job_id` or `matched_slurm_job_id` is bound, or which is not a
  current-contract cohort master
- **THEN** the call SHALL be refused and no successor row SHALL be created

#### Scenario: a concurrently advanced attempt loses the race

- **WHEN** the recovery API is invoked with an expected submission attempt or
  attempt anchor that no longer matches the persisted row
- **THEN** the call SHALL make no write and SHALL report that it did not recover

#### Scenario: automatic classification is unchanged by the recovery path

- **WHEN** automatic retry classification evaluates a released identity-blocked
  reservation row, before or after the recovery API exists
- **THEN** `should_auto_retry` SHALL be false and the row's `error_code` SHALL be
  null

#### Scenario: the release announces the terminal from either prior state

- **WHEN** an identity-blocked reservation is released after a fresh
  reservation, and separately when it is released after the reservation was
  re-seeded through reclaim
- **THEN** each release SHALL emit exactly one operator-visible record carrying
  the token `IDENTITY_RELEASED_RESERVATION_NEEDS_OPERATOR` and naming the job id,
  cohort digest, and `identity_blocked_streak`

#### Scenario: a repeat recovery on an already-recovered row is refused

- **WHEN** the recovery API is invoked a second time on a released row for which
  a successor has already been minted
- **THEN** the call SHALL be refused, no second successor SHALL be created, and
  no write SHALL occur

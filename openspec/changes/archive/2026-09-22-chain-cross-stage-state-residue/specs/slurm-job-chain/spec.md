## MODIFIED Requirements

### Requirement: Cycle-stage retry attempt numbering SHALL use the last retry suffix

The cycle executor SHALL derive the next retry attempt for a stage from existing
job rows (when no explicit context retry attempt is set) as follows: each matching row
whose job id starts with the stage base id followed by `_retry_` SHALL contribute the attempt
encoded in its LAST `_retry_<n>` suffix, and the next attempt SHALL be the
maximum contribution plus one. A row whose last suffix does not parse SHALL
contribute nothing, regardless of any recorded retry count. An explicit context
retry attempt SHALL keep precedence, and the derivation SHALL keep using the
same jobs snapshot that selected the stage row. The explicit context retry
attempt is the invocation's own claim (an operator/API direct field or an
active manual-retry marker) as it stood when the cycle call started; the
attempt a stage obtains from its own reservation is scoped to that stage and
SHALL be reset to the invocation claim before the next stage is entered, so no
stage targets an attempt obtained by another stage's reservation. An explicit
invocation claim N is, by design, applied to every stage of that call; a stage
whose `_retry_N` row is already occupied remains governed by the manual-retry
claim contract. This supersedes the #1201 design D2 "inheritance retained"
wording.

#### Scenario: Stacked retry ids number past the last suffix

- **WHEN** the rows for a stage base `B` include `B_retry_1` and a selected
  terminal `B_retry_1_retry_2_retry_3`, and no context retry attempt is set
- **THEN** the minted retry id SHALL be `B_retry_4`, and the reservation key and
  gateway comment SHALL carry `retry_4`

#### Scenario: Flat suffixes and malformed tails are unchanged

- **WHEN** the rows include `B_retry_1`, `B_retry_2`, and `B_retry_garbage`
- **THEN** the next attempt SHALL be 3

#### Scenario: A downstream stage never inherits the upstream reservation attempt

- **WHEN** a markerless forced resubmission restarts at `forecast`, forecast
  reserves a fresh retry attempt, and the downstream `parse` stage already has
  a terminal `_retry_1` row
- **THEN** `parse` SHALL derive its attempt from its own rows, really submit,
  and SHALL NOT end as `skipped_duplicate_submission`

## ADDED Requirements

### Requirement: Forcing stage resume SHALL match the cohort's exact model set

A forcing row that carries complete member identity SHALL be, when the chain looks for an existing `forcing` array row to resume, a resume or terminal match
only when its member model set equals the current cohort's model set. A row
with complete identity whose members overlap the cohort and are still
unresolved SHALL keep blocking submission. A row without complete member
identity (pre-identity legacy) SHALL keep the prior stage-name match.

#### Scenario: A sibling model's terminal forcing row is not resumed

- **WHEN** an unscoped multi-model cohort runs and the cycle holds a terminal
  succeeded forcing row whose complete member identity names a different
  model set
- **THEN** the chain SHALL submit forcing for the current cohort instead of
  resuming that row

#### Scenario: An excluded sibling row holding the base id does not wedge the stage

- **WHEN** the excluded sibling forcing row occupies the shared run's bare
  forcing job id
- **THEN** the chain SHALL submit under a non-colliding `_retry_N` id and SHALL
  NOT end as `skipped_duplicate_submission`

#### Scenario: The cohort's own terminal forcing row is resumed

- **WHEN** the terminal forcing row's complete member identity equals the
  current cohort's model set
- **THEN** the chain SHALL resume it without a new submission

### Requirement: The cohort restart stage SHALL come only from the basin manifest's top-level field

The chain SHALL derive a cycle's restart stage only from each basin payload's
top-level `restart_stage`; it SHALL NOT fall back to `state_evidence`. The
candidate manifest builder SHALL write that field from the candidate's
evidence (`restart_stage`, else `restart_from_stage` when the scheduler's downstream-stage canonicalizer recognizes it), except for fresh
full-chain candidates, which carry none. The cohort start stage SHALL be the
earliest stage among members that carry one.

#### Scenario: A residual marker on a fresh full-chain member does not skip stages

- **WHEN** a `(0,"full")` cohort contains a fresh full-chain member whose
  evidence still holds `restart_stage: "forecast"`, plus markerless members
- **THEN** the chain SHALL start at `convert` and SHALL NOT skip convert or
  forcing for any member

#### Scenario: An ordinary restart candidate keeps its restart

- **WHEN** a non-fresh candidate's evidence carries only `restart_from_stage`
- **THEN** its manifest SHALL carry that value as the top-level `restart_stage`,
  and the chain SHALL canonicalize it on read and start at that stage when the
  chain recognizes it (an unrecognized value starts the chain at stage 0,
  never later)

### Requirement: The chain SHALL NOT carry a raw-manifest guard for a retired download stage

The forecast chain stage loop SHALL contain no probe keyed on a `download`
stage; raw-manifest repair belongs to scheduler admission only.

#### Scenario: A legacy download row does not trigger a download resubmission

- **WHEN** the cycle holds a legacy succeeded `download` row
- **THEN** the first submission SHALL be `convert` and no `download_retry_1`
  SHALL be minted

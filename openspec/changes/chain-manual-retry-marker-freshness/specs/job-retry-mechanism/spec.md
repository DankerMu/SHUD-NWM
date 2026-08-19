# job-retry-mechanism (delta)

## ADDED Requirements

### Requirement: Retry-attempt claims from persisted manual-retry markers SHALL be honoured only under an active manual-retry decision

A retry-attempt claim that originates in a candidate's persisted `manual_retry` marker SHALL reach the forecast chain's attempt targeting only when the same scheduler-emitted `state_evidence`'s decision face carries an ACTIVE manual-retry decision — the evidence `decision` is `manual_retry` or its `reason` is `manual_retry_requested`. This judgement SHALL be applied at the candidate-manifest minting boundary, where the scheduler projects `state_evidence.manual_retry` into the basin payload's `manual_retry_attempt`/`retry_attempt` fields (the production channel that otherwise shadows every downstream read), and again at the chain's own `state_evidence` read as defence in depth, both consuming ONE shared predicate. Without an active manual-retry decision the manifest SHALL NOT mint those fields from the marker and the chain SHALL fall through to deriving the next unused `_retry_<n>` suffix for the stage; each dropped claim SHALL leave queryable evidence (a structured record stating that no active manual-retry decision accompanies the claim — the claim is not thereby asserted to be stale, since a higher-priority decision lane may lawfully preempt a live marker — naming the basin, the claimed attempt, and the decision actually present) rather than degrade silently. When the evidence carries neither a `decision` nor a `reason` key the claim SHALL be treated the same way — degrading to the next free attempt still submits, while honouring a wedged claim against an occupied terminal `_retry_<n>` row blocks the stage forever. Operator-supplied direct fields passed into an invocation from outside the scheduler projection are that invocation's own input and SHALL keep being honoured without this judgement. A dropped claim SHALL leave the candidate exactly where a markerless candidate of the same decision stands — in particular, for a decision outside the forced terminal-resubmit set with a terminal failed row present, the stage resumes that row exactly as it would with no marker at all; the claim judged inactive changes nothing, and this rule neither widens nor narrows any decision lane's own resubmission policy. The cycle-scoping predicate that reads the same marker to widen single-basin execution scope stays outside this requirement (no attempt minting; job-id derivation remains run-id-namespaced; a recorded boundary). This rule is the chain/manifest-side complement of the cycle-granularity marker requirement's attempt-pin discipline: that rule gates EVENT-derived markers through its pin test, while the state-level `manual_retry` mapping is copied into evidence whole — this rule closes that remaining channel at its consumers.

#### Scenario: A wedged marker claim no longer blocks the stage

- **WHEN** a candidate's `state_evidence` carries a persisted `manual_retry`
  claim (`new_attempt` 1) under a non-manual-retry decision and the stage's
  `_retry_1` job id is already occupied by a terminal row bound to a
  `slurm_job_id`
- **THEN** the candidate's basin manifest carries no
  `retry_attempt`/`manual_retry_attempt` minted from the marker, the forced
  resubmission targets the next free suffix (`_retry_2`) and actually submits
  — no `skipped_duplicate_submission` — and a structured record logs the
  dropped claim with the decision actually present

#### Scenario: An active manual-retry decision keeps its precise attempt identity

- **WHEN** the evidence decision is `manual_retry` (or its reason is
  `manual_retry_requested`) with `new_attempt` N
- **THEN** the manifest mints the attempt fields and the chain targets exactly
  `_retry_<N>`, byte-identical to today's fresh-marker behaviour

#### Scenario: Unjudgeable evidence fails safe

- **WHEN** a `manual_retry` payload appears on evidence carrying neither a
  `decision` nor a `reason` key
- **THEN** the claim is dropped exactly as one without an active decision, and
  the derivation falls through to the next free attempt

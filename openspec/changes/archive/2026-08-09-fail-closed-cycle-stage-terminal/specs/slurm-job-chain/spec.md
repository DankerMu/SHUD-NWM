# slurm-job-chain (delta)

## ADDED Requirements

### Requirement: Cycle-stage terminal handling is fail-closed

The per-stage cycle executor for `orchestrate_cycle` SHALL advance to
a downstream stage only when the current stage's terminal status is in
the pipeline success set or is the partial-success status handled by
the partial-capture mechanics; every other stage terminal SHALL end the
cycle with an explicit non-success cycle terminal, and a cycle whose
work was skipped or unrecognized SHALL NOT report success on the cycle
result or on the scheduler evidence plane.

#### Scenario: Duplicate-submission skip defers the cycle

- **WHEN** a cycle stage returns `skipped_duplicate_submission` because
  the reserve gate found another pass holding the in-flight reservation
- **THEN** the executor SHALL NOT run any downstream stage of that
  cycle in the same pass
- **THEN** the cycle SHALL terminate with the dedicated non-success
  terminal `skipped_duplicate_submission` — not a success status, and
  not a failure terminal that would trigger failure-retry adjudication
  or resubmission against the reservation-holding pass's active row
- **THEN** the skipped stage's span counters SHALL record zero
  submissions and zero failures

#### Scenario: Skipped candidate is non-success on the evidence plane

- **WHEN** scheduler evidence is built for a candidate whose cohort
  cycle terminated with `skipped_duplicate_submission`
- **THEN** the candidate's evidence SHALL NOT report final candidate
  success and SHALL surface a not-successful residual signal
- **THEN** the candidate's evidence item SHALL carry retrievable
  duplicate-skip evidence derived from the cohort's cycle result
  (cohort-scoped, matching the existing stage-status fan-out
  semantics)
- **THEN** a pass that submitted other work before the skip SHALL
  surface as a partial, review-visible pass rather than a fully
  successful one
- **THEN** readiness validation SHALL recognize the skip status in
  its pass-status vocabulary as a review-visible (blocked) state and
  SHALL count the skipped candidate consistently with the producer's
  partial accounting — without manufacturing a status-vocabulary or
  partial-cardinality acceptance error, and without loosening the
  compatibility rules that infer submission from model-run statuses

#### Scenario: Unrecognized stage terminal fails closed

- **WHEN** a cycle stage returns a terminal status that is neither in
  the pipeline success set, nor the partial-success status, nor a
  status with a dedicated break branch
- **THEN** the executor SHALL terminate the cycle as failed with an
  explicit error code identifying the unrecognized status rather than
  silently advancing downstream

#### Scenario: Stage-versus-cycle consistency invariant

- **WHEN** a cycle result is returned and any stage span with a
  positive entering basin count records every entering basin as failed
- **THEN** the cycle terminal SHALL NOT be a member of the pipeline
  success set

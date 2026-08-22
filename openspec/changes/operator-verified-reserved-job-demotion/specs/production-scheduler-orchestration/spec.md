## ADDED Requirements

### Requirement: Operators can atomically demote a manually verified-dead comment-unobservable reservation

The file-journal scheduler SHALL expose a row-scoped operator CLI that converts a current accepted-submit cohort master from the exact held state (`status=reserved`, no bound or matched Slurm job id, `submit_outcome=submit_result_ambiguous`, `reconciliation_source=slurm_exact_comment`, `reconciliation_decision=accounting_unavailable`, and `reconciliation_reason_class=comment_accounting_unproven`) to `status=reservation_lost` with the distinct `operator_verified_absence` decision only when the operator supplies explicit confirmation, operator identity, a timezone-aware check time, a bounded non-empty verification note, and exact persisted submission-attempt and attempt-anchor expectations. The transition SHALL execute under the cycle lock, reject every stale or mismatched request without changing journal bytes, clear the post-state reason class, and atomically write the cohort master, eligible active member failure projections, and a durable audit event containing the operator evidence and prior accounting blocker. The command SHALL be file-journal-only and SHALL behave identically through the click and argparse entrypoints.

#### Scenario: Exact confirmed request records one audited demotion

- **WHEN** an operator has independently verified absence and invokes `demote-reserved-job` with all required confirmation, operator, attempt, and anchor values matching the exact held master
- **THEN** the command exits zero with a stable JSON receipt, the master becomes `reservation_lost/operator_verified_absence` with a null reason class, matching active member rows are projected to `failed/SLURM_RESERVATION_LOST`, and one operator audit event records the prior blocker and verification evidence in the same durable append

#### Scenario: Missing confirmation or evidence is rejected before writing

- **WHEN** `--confirm`, operator identity, timezone-aware check time, or the bounded verification note is missing or invalid
- **THEN** both CLI entrypoints exit non-zero, report the validation error, and leave the journal byte-identical

#### Scenario: Stale or wrong durable state loses the compare-and-swap

- **WHEN** the job id does not name a current master, or any current status, binding, outcome, source, decision, reason class, submission attempt, or attempt anchor differs from the supplied held-row expectation
- **THEN** the command exits non-zero and writes no master, member, event, sequence, or materialized-latest record

#### Scenario: Concurrent successor cannot be demoted

- **WHEN** another actor binds, permits, releases, demotes, or reclaims the reservation before the operator transition obtains the cycle lock
- **THEN** the locked re-read detects the changed authority state and the stale operator request writes nothing

#### Scenario: State and audit evidence fail together

- **WHEN** validation or append of any master, member, or audit-event record fails before the batch commit
- **THEN** neither the operator decision nor any partial member/event evidence becomes durable

#### Scenario: Automatic fail-closed behavior remains unchanged

- **WHEN** reconcile runs on a cluster that does not store job comments and no operator command is invoked
- **THEN** it continues to record `accounting_unavailable/comment_accounting_unproven`, keeps the row `reserved`, and never infers absence from the empty comment query

#### Scenario: PostgreSQL and manual retry surfaces do not gain this authority

- **WHEN** a caller uses the PostgreSQL repository, the HTTP/manual retry API, or a generic file-journal evidence transition
- **THEN** no `operator_verified_absence` demotion capability is exposed and `reserved` remains outside the manual-retry source statuses

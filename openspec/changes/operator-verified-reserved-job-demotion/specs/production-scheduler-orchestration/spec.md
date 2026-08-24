## ADDED Requirements

### Requirement: Operators can atomically demote a manually verified-dead comment-unobservable reservation

The file-journal scheduler SHALL expose a row-scoped operator CLI that converts a current accepted-submit cohort master from the exact held state (`status=reserved`, no bound or matched Slurm job id, `submit_outcome=submit_result_ambiguous`, `reconciliation_source=slurm_exact_comment`, `reconciliation_decision=accounting_unavailable`, and `reconciliation_reason_class=comment_accounting_unproven`) to `status=reservation_lost` with the distinct `operator_verified_absence` decision only when the operator supplies explicit confirmation, operator identity, a timezone-aware check time, a bounded non-empty verification note, and exact persisted submission-attempt and attempt-anchor expectations. The transition SHALL execute under the cycle lock, reject every stale or mismatched request without changing journal bytes, clear the post-state reason class, and atomically append the cohort master, eligible active member failure projections, and a durable audit event containing the operator evidence and prior accounting blocker. That authority append is the operation's commit point. A later direct/latest derived-projection failure SHALL NOT turn the committed demotion into a reported failure: the command SHALL return committed success with bounded non-secret projection warnings, while journal replay remains authoritative and a repeated request remains a zero-write CAS refusal. The command SHALL be file-journal-only and SHALL behave identically through the click and argparse entrypoints.

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

#### Scenario: State and audit evidence fail together before commit

- **WHEN** validation or append of any master, member, or audit-event record fails before the authority batch commit
- **THEN** neither the operator decision nor any partial member/event evidence becomes durable

#### Scenario: Derived projection failure after commit is reported as committed

- **WHEN** the authority batch commits and a later direct-job or latest materialization write fails
- **THEN** the command still reports the demotion as committed, carries a bounded non-secret warning naming each failed projection, attempts the remaining independent projections, and does not append another operator decision when the same request is retried

#### Scenario: Automatic fail-closed behavior remains unchanged

- **WHEN** reconcile runs on a cluster that does not store job comments and no operator command is invoked
- **THEN** it continues to record `accounting_unavailable/comment_accounting_unproven`, keeps the row `reserved`, and never infers absence from the empty comment query

#### Scenario: PostgreSQL and manual retry surfaces do not gain this authority

- **WHEN** a caller uses the PostgreSQL repository, the HTTP/manual retry API, or a generic file-journal evidence transition
- **THEN** no `operator_verified_absence` demotion capability is exposed and `reserved` remains outside the manual-retry source statuses

#### Scenario: Release validation does not manufacture a production incident

- **WHEN** a read-only census of the active production file journal finds no naturally occurring master in the exact held pre-state
- **THEN** release evidence SHALL use the deterministic held-to-reclaim chain, fault/refusal matrix, final-head CI, and recorded census; it SHALL NOT stop the production scheduler, force gateway unavailability, inject or rewrite journal authority, or submit a real cohort merely to create a live receipt

#### Scenario: A natural held-row incident retains an in-situ receipt

- **WHEN** a naturally occurring exact held master is independently confirmed dead with name/time/user/account `sacct` and `squeue` evidence and the guarded command is used operationally
- **THEN** the incident record SHALL retain the success receipt and durable audit event, a stale or repeated zero-write refusal, the fresh reclaim attempt and anchor, exactly one cohort resubmission, and the cleanup boundary

#### Scenario: Non-dedicated accepted-submit writers cannot persist the operator decision

- **WHEN** the submit-attempt commit writer receives an accepted transition carrying `operator_verified_absence`, the cohort defer or cohort task-projection writer receives the raw decision token, or ordinary pipeline-job upsert receives the token while creating or upgrading a current-contract row
- **THEN** each current-contract writer rejects it with the typed-authority error before row construction, lock acquisition, durable mutation, or event, the journal stays byte-identical, and existing legitimate decisions and non-token legacy upgrades still apply unchanged; legacy transition/reconciliation writer compatibility is tracked separately in #1805

#### Scenario: Committed reclaim never strands a pre-sbatch live reservation

- **WHEN** the public old-ID operator recovery path commits the reclaim authority append and a derived direct or inventory projection write then fails before any submission
- **THEN** the failure SHALL NOT be reported as an uncommitted failure that leaves a live `reserved` row: the flow either completes the single submission path or transitions the row to a non-live retryable authority state under the lock, and the next public pass does not fail with `PIPELINE_ALREADY_ACTIVE`

#### Scenario: The receipt locator uses the one safe journal-root authority

- **WHEN** `--journal-root` is a symlink loop or a literal unexpanded tilde path
- **THEN** the loop root fails through the typed operational error path before the authority append with no traceback and zero journal bytes, and the tilde root's success receipt locator equals the expanded authority root actually used by repository reads and writes

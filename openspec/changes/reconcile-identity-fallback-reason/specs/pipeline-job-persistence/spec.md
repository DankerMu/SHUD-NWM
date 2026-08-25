## MODIFIED Requirements

### Requirement: Comment-based absence proof requires proven comment accounting capability

The restart-reconciliation comment capability probe SHALL classify one querier instance as comment-storing, explicitly comment-less, or unknown. A present `AccountingStoreFlags` line containing `job_comment` is comment-storing; a present line explicitly lacking that token, including `(null)`, is explicitly comment-less; probe execution failure or an absent `AccountingStoreFlags` line is unknown. The probe SHALL log execution failure differently from an explicit missing flag. The accepted-submit contract-version check SHALL still run before capability classification.

For a comment-storing cluster, exact-comment owner/global queries, coverage proof, confirmed absence, and retry-permission behavior SHALL remain unchanged. The global-visibility gate SHALL continue to apply to every exact-comment query scope it guarded before this change, and the existing raise-priority order after the contract-version check SHALL remain unchanged.

For an unknown cluster, and for legacy or non-forecast queries on an explicitly comment-less cluster, the querier SHALL raise its transient query-unavailable error with reason class `comment_accounting_unproven` before issuing any `sacct` command. Restart reconcile SHALL emit `action=query_unavailable`, keep the row reserved and unbound, and never record an absence conclusion.

Only an explicitly comment-less cluster and a current accepted-submit forecast cohort with a strict UTC `submission_attempt_started_at` plus non-empty expected Slurm user and account MAY enter the conservative fallback. The fallback SHALL issue one name-scoped accounting query for `nhms_forecast`, exact user/account, and the interval from the immutable attempt anchor through one frozen query-end instant. It SHALL render `--starttime` and `--endtime` in host-local wall-clock strings using the same rule as the existing comment query, request `JobID,JobName,State,ExitCode,Comment,User,Account,Submit`, and use the existing shared byte, logical-row, and whole-query timeout budget. The parser SHALL interpret a timezone-less Slurm `Submit` value in the same host-local timezone before converting it to UTC, reject a missing or unparsable Submit value as transient query-unavailable evidence, reject a submit instant outside the closed attempt window, validate exact owner/account and forecast-family job name, normalize array and step rows to a bare numeric master id, and retain at most two distinct masters because only zero, unique, and ambiguous classifications are required.

Exactly one fallback master MAY bind only after all remaining durable/runtime/ownership identity gates pass. The two reserved comment gates SHALL treat an empty accounting comment as not stored on this fallback path; a present comment different from the reservation's idempotency comment remains fatal at both gates. A successful fallback bind SHALL atomically persist `reconciliation_source=slurm_name_window_unique`, `reconciliation_decision=matched_bound`, and the matched bare Slurm id. `slurm_name_window_unique` SHALL be accepted only for `matched_bound`; every other durable accounting decision remains `slurm_exact_comment` sourced.

Every unsuccessful fallback SHALL remain fail-closed and SHALL preserve the durable #1564 held authority tuple byte-for-byte: `status=reserved`, no `slurm_job_id`, `reconciliation_source=slurm_exact_comment`, `reconciliation_decision=accounting_unavailable`, and `reconciliation_reason_class=comment_accounting_unproven`. If that tuple is not yet present on the first pass, the only permitted durable write is its attempt-scoped establishment; no unsuccessful fallback may write a bind, status demotion, retry permission, identity-mismatch transition, or identity-blocked streak. Pass evidence SHALL distinguish the cases as follows:

- zero eligible masters: `action=fallback_no_match`, `match_count=0`;
- two or more distinct eligible masters: `action=ambiguous_fallback_match`, `match_count=2` (the bounded “at least two” value);
- one eligible master that fails a remaining identity gate: `action=identity_mismatch_blocked`, `match_count=1`;
- process/timeout/byte/row failure: `action=query_unavailable` with the existing bounded-query reason class;
- missing or unparsable Submit evidence: `action=query_unavailable`, `reconciliation_reason_class=fallback_submit_unparsable` in pass evidence only.

The comment-unproven and unsuccessful-fallback outcome family deliberately SHALL NOT converge automatically: it SHALL NOT increment `identity_blocked_streak`, SHALL NOT enter the identity-mismatch release ladder, and SHALL NOT create any automatic absence/release exit. On a comment-less cluster, only a uniquely proven live job binds; row-scoped confirmed-dead disposal remains the documented guarded operator action.

#### Scenario: an explicitly comment-less cluster binds one unique owned candidate

- **WHEN** `AccountingStoreFlags=(null)`, a current forecast reservation has an immutable attempt anchor and exact expected user/account, and the bounded name-window query yields one in-window master with an empty comment that passes every remaining identity gate
- **THEN** the reservation binds that master exactly once with source `slurm_name_window_unique`, decision `matched_bound`, and the matched bare Slurm id

#### Scenario: ambiguity never binds or changes disposal authority

- **WHEN** the fallback query yields at least two distinct owned in-window forecast masters
- **THEN** pass evidence reports `ambiguous_fallback_match` and `match_count=2`, the row stays reserved and unbound, and the durable comment-unproven held tuple remains byte-for-byte valid for guarded operator demotion

#### Scenario: no fallback match is not an absence proof

- **WHEN** the explicitly comment-less fallback yields zero eligible masters
- **THEN** pass evidence reports `fallback_no_match` and `match_count=0`, the row stays reserved and unbound, and no retry permission or reservation-lost transition is written

#### Scenario: present-but-different comment remains fatal at both gates

- **WHEN** the unique name-window candidate carries a non-empty comment that differs from the reservation comment
- **THEN** both the reserved identity check and final bind guard refuse it, pass evidence reports `identity_mismatch_blocked` with `match_count=1`, and no bind/status/retry/streak write occurs beyond establishing the held tuple if needed

#### Scenario: incomplete ownership cannot enter fallback

- **WHEN** the current reservation lacks expected user or account, or the candidate lacks or disagrees with either value
- **THEN** no name-window candidate binds and the row remains reserved and unbound under the durable held tuple

#### Scenario: malformed Submit evidence is transient denial

- **WHEN** an otherwise eligible name-window row has missing, unparsable, or out-of-window Submit evidence
- **THEN** it cannot bind or prove absence; unparsable evidence reports pass-only reason `fallback_submit_unparsable`, and the durable held tuple remains unchanged

#### Scenario: bounded query failure is transient denial

- **WHEN** fallback accounting exceeds the existing byte, logical-row, or whole-query timeout bound, or the subprocess fails
- **THEN** pass evidence reports `query_unavailable` with the applicable existing bounded-query reason, and no bind or absence transition occurs

#### Scenario: unknown capability remains query-free

- **WHEN** `scontrol` fails or its output omits `AccountingStoreFlags`
- **THEN** pass evidence reports `query_unavailable` / `comment_accounting_unproven` before any `sacct`, and name-window fallback is not attempted

#### Scenario: a comment-storing cluster is unchanged

- **WHEN** the probe proves `AccountingStoreFlags` includes `job_comment`
- **THEN** owner/global exact-comment queries page `sacct` exactly as before, owned matches bind with source `slurm_exact_comment`, and a coverage-complete confirmed absence past grace may still permit retry

#### Scenario: the probe runs once per querier instance

- **WHEN** one querier instance serves multiple queries in a session
- **THEN** capability classification executes at most once and its verdict is reused; rebuilding the querier on the next pass re-probes transient unknown state

### Requirement: Cohort runtime identity cross-check SHALL treat absent hydro_run identity fields as not-stored, not as mismatched

The file-journal runtime identity cross-check SHALL, for accepted-submit forecast cohorts (`forecast_cohort_runtime_identity_matches`) and for each cohort member, continue to require a per-model `hydro_run` row in the same source and cycle that strictly matches on `run_id`, `model_id`, `scenario_id`, `source_id`, and `cycle_time`. For `candidate_id` and `basin_id` the check SHALL compare strictly when the `hydro_run` row carries a value, and SHALL skip the field — without failing the check — when the row's value is absent (`None`), because some file-journal per-model writer paths do not persist these fields; a present-but-different value SHALL remain fatal for these two fields.

The check SHALL NOT compare `array_task_id` or `submission_attempt` at all. An array task id is the index a member occupied within a single array submission and is frozen on the `hydro_run` row at the submission that created it, so it is not stable across submissions when membership changes. The attempt number is likewise frozen on a successful per-model row while reclaim advances the accepted-submit master to a new attempt. Both fields SHALL remain persisted as lineage evidence for the submission that wrote the row, but neither is cross-submission equality identity.

The cohort-member side SHALL remain fully strict, and the reconcile-side gates (exact master Slurm id, ownership user/account, stage-family job name, comment-when-stored, and the task-id mapping against current `cohort_members`) SHALL be unchanged. Those gates draw their submission identity from the current durable master and live accounting rather than from frozen per-model lineage.

#### Scenario: A renumbered member set no longer fails the cohort

- **WHEN** a cohort is submitted whose member set differs from an earlier submission for the same source and cycle, so that members' array task indices are renumbered, and each member's per-model `hydro_run` row still carries the array task id written by that earlier submission, and every stable identity field agrees
- **THEN** the runtime identity cross-check SHALL pass, and restart reconcile SHALL NOT record `identity_mismatch_blocked` or `identity_mismatch_released` on the basis of the array task id

#### Scenario: A reclaimed cohort accepts frozen prior-attempt rows

- **WHEN** the current accepted-submit master is on submission attempt 2 and every otherwise matching successful per-model `hydro_run` row remains frozen at submission attempt 1
- **THEN** the runtime identity cross-check SHALL pass and SHALL NOT block the cohort on the attempt number alone

#### Scenario: Present-but-different sibling fields still block

- **WHEN** a per-model `hydro_run` row carries a non-absent `candidate_id` or `basin_id` that differs from the cohort member's value
- **THEN** the runtime identity cross-check SHALL fail and terminal restart reconcile SHALL record `identity_mismatch_blocked` with reason `runtime_identity_mismatch` and zero durable writes

#### Scenario: Strict fields stay strict when degradable fields are absent

- **WHEN** a per-model `hydro_run` row has absent `candidate_id` and `basin_id` but disagrees with the cohort member on `run_id`, `model_id`, `scenario_id`, `source_id`, or `cycle_time` — or the row is missing entirely
- **THEN** the runtime identity cross-check SHALL fail; tolerating frozen array/attempt lineage SHALL NOT weaken any stable identity field

#### Scenario: Production-shaped hydro_run rows reconcile to matched_bound

- **WHEN** an inflight forecast cohort's per-model `hydro_run` rows carry `None` for `candidate_id` and `basin_id` and `sacct` returns a terminal master record passing all reconcile-side identity gates with complete task accounting
- **THEN** restart reconcile SHALL record a `terminal` outcome with reconciliation decision `matched_bound` and project the per-task outcomes, instead of recording `identity_mismatch_blocked`

## ADDED Requirements

### Requirement: Terminal cohort identity blocks SHALL expose stable clause-level reasons

The terminal accepted-submit file-cohort identity validator SHALL return a stable reason for every failure site while preserving its match verdict. The folded cohort-valid/runtime condition SHALL be separated. The exact reason vocabulary SHALL be `cohort_identity_invalid`, `runtime_identity_mismatch`, `master_id_mismatch`, `comment_mismatch`, `stage_family_mismatch`, `ownership_unproven`, `ownership_user_mismatch`, `ownership_account_mismatch`, `cohort_members_unparsable`, `task_identity_values_mismatch`, `task_identity_values_unparsable`, `task_id_unparsable`, `task_mapping_mismatch`, `task_job_name_mismatch`, and `task_comment_mismatch`.

A terminal failure SHALL continue to emit `action=identity_mismatch_blocked`, preserve the existing status, and perform zero durable/status/event writes. `ReconcileOutcome` SHALL add optional `reconciliation_reason_class`; scheduler `restart_reconcile.inflight.outcomes[]` SHALL serialize it without changing any existing key or value. The reason is pass evidence only and SHALL NOT be written into the accepted-submit durable accounting tuple by this inflight leg.

#### Scenario: each failure site is distinguishable

- **WHEN** each terminal validator failure site is exercised independently
- **THEN** the unchanged blocking action carries the corresponding reason token, including distinct `cohort_identity_invalid` and `runtime_identity_mismatch` results for the formerly folded predicates

#### Scenario: task-accounting failures do not masquerade as runtime identity

- **WHEN** live task identity values, Slurm ids, job names, or comments fail their respective terminal gates
- **THEN** evidence names the matching task-level token rather than `runtime_identity_mismatch`

#### Scenario: existing inflight evidence remains compatible

- **WHEN** scheduler restart reconcile serializes a blocked or successful inflight outcome
- **THEN** every pre-existing action, status, and write-count value remains unchanged and the optional reason field is the only additive key

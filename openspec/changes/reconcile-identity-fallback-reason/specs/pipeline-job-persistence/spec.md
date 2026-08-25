## MODIFIED Requirements

### Requirement: Comment-based absence proof requires proven comment accounting capability

The restart-reconciliation comment capability probe SHALL classify one querier instance as comment-storing, explicitly comment-less, or unknown. A present `AccountingStoreFlags` line containing `job_comment` is comment-storing; a present line explicitly lacking that token, including `(null)`, is explicitly comment-less; probe execution failure or an absent `AccountingStoreFlags` line is unknown. The probe SHALL log execution failure differently from an explicit missing flag. The accepted-submit contract-version check SHALL still run before capability classification.

For a comment-storing cluster, exact-comment owner/global queries, coverage proof, confirmed absence, and retry-permission behavior SHALL remain unchanged. The global-visibility gate SHALL continue to apply to every exact-comment query scope it guarded before this change, and the existing raise-priority order after the contract-version check SHALL remain unchanged.

For an unknown cluster, and for legacy or non-forecast queries on an explicitly comment-less cluster, the querier SHALL raise its transient query-unavailable error with reason class `comment_accounting_unproven` before issuing any `sacct` command. Restart reconcile SHALL emit `action=query_unavailable`, keep the row reserved and unbound, and never record an absence conclusion.

Only an explicitly comment-less cluster and a current accepted-submit forecast cohort with a strict UTC `submission_attempt_started_at` plus non-empty expected Slurm user and account MAY enter the conservative fallback. The fallback SHALL issue one name-scoped accounting query for `nhms_forecast`, exact user/account, and the interval from the immutable attempt anchor through one frozen query-end instant. It SHALL render `--starttime` and `--endtime` in host-local wall-clock strings using the same rule as the existing comment query, request `JobID,JobName,State,ExitCode,Comment,User,Account,Submit`, and use the existing shared byte, logical-row, and whole-query timeout budget. The parser SHALL interpret a timezone-less Slurm `Submit` value in the same host-local timezone before converting it to UTC, reject a missing or unparsable Submit value on an otherwise eligible forecast/owner row as transient query-unavailable evidence, reject a submit instant outside the closed attempt window, validate exact owner/account and forecast-family job name before candidate classification, normalize accepted forecast array and step ids to a bare numeric master id, and retain at most two distinct masters because only zero, unique, and ambiguous classifications are required. Forcing, batch/extern, and unrelated job names SHALL be ineligible and SHALL NOT become identity-mismatch candidates or malformed-Submit evidence.

Exactly one fallback master MAY bind only after all remaining durable/runtime/ownership identity gates pass and durable claimant exclusivity is proven atomically. A bare Slurm master id already bound to any other active current accepted-submit master SHALL NOT bind. A settled sibling master with the same numeric id and the same canonical Slurm Submit instant is the same accounting incarnation and SHALL also block; the same numeric id with a different canonical Submit instant is a recycled incarnation and SHALL NOT block. Direct master projections MAY identify bounded source/cycle candidates, but canonical cycle authority SHALL decide occupancy so a stale, damaged, or missing projection cannot fabricate or hide an owner. Inventory cleanup SHALL restore a missing bounded flat locator from canonical authority before pruning a terminal anchor; first-migration backfill SHALL preserve a handoff anchor until that restore completes. Under concurrent cleanup, migration, and bind attempts, a fallback SHALL therefore observe either the canonical anchor or the bounded locator, never vacancy caused only by a derived-projection crash. A candidate whose submit instant falls inside another current reserved-unbound forecast attempt's window for the same expected Slurm user/account SHALL have more than one durable claimant and SHALL NOT bind for any claimant; every claimant stays fail-closed regardless of reconcile iteration order or concurrent source/cycle writers. Only a candidate with one current reserved claimant, no other durable owner of the same accounting incarnation, and all remaining identity gates passing MAY bind. The two reserved comment gates SHALL treat an empty accounting comment as not stored on this fallback path; a present comment different from the reservation's idempotency comment remains fatal at both gates. A successful fallback bind SHALL atomically persist `reconciliation_source=slurm_name_window_unique`, `reconciliation_decision=matched_bound`, and the matched bare Slurm id. `slurm_name_window_unique` SHALL be accepted only for `matched_bound`; every other durable accounting decision remains `slurm_exact_comment` sourced.

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

#### Scenario: overlapping reserved attempts cannot claim one master

- **WHEN** one otherwise eligible master has a submit instant inside the attempt windows of two current reserved-unbound forecast masters with the same expected Slurm user/account
- **THEN** neither claimant binds the master, both rows stay reserved and unbound under the durable held tuple with streak zero, and the result is independent of row iteration or source/cycle lock order

#### Scenario: an already-bound master cannot be claimed again

- **WHEN** an otherwise eligible fallback master id is already bound to another active current accepted-submit master in any source or cycle
- **THEN** the reserved claimant cannot bind that id and stays under the durable held tuple, even when its own accounting query contains no second master

#### Scenario: terminal history distinguishes accounting incarnations

- **WHEN** a settled sibling carries the same numeric Slurm id
- **THEN** canonical cycle authority blocks the fallback when the sibling's Submit instant equals the candidate Submit instant, but permits a recycled id whose canonical Submit instant differs

#### Scenario: derived projection failure cannot erase an accounting incarnation

- **WHEN** a terminal canonical master owns the candidate's exact `(Slurm id, Submit)` incarnation but its derived flat projection is stale, damaged, or missing during steady-state cleanup, first migration, crash-resume, or a concurrent bind
- **THEN** cleanup/migration preserves an anchor-to-flat locator handoff under journal-global serialization, canonical cycle authority still blocks the fallback, and no ordering may bind the same incarnation twice

#### Scenario: an exclusive claimant still binds

- **WHEN** one in-window master has exactly one current reserved claimant, no other current accepted-submit master owns its id, and every remaining identity gate passes
- **THEN** that claimant alone binds the master with source `slurm_name_window_unique`

#### Scenario: no fallback match is not an absence proof

- **WHEN** the explicitly comment-less fallback yields zero eligible masters, including a result set containing only forcing, batch/extern, or unrelated job names
- **THEN** pass evidence reports `fallback_no_match` and `match_count=0`, the row stays reserved and unbound, and no retry permission, identity-mismatch transition, or reservation-lost transition is written

#### Scenario: present-but-different comment remains fatal at both gates

- **WHEN** the unique name-window candidate carries a non-empty comment that differs from the reservation comment
- **THEN** both the reserved identity check and final bind guard refuse it, pass evidence reports `identity_mismatch_blocked` with `match_count=1`, and no bind/status/retry/streak write occurs beyond establishing the held tuple if needed

#### Scenario: fallback runtime identity failure stays held

- **WHEN** an explicitly comment-less query yields one exclusive candidate but the reservation's genuine runtime identity is missing or present-but-different
- **THEN** the candidate does not bind, pass evidence reports `identity_mismatch_blocked` with `match_count=1`, the durable held tuple remains valid, and repeated passes keep `identity_blocked_streak=0` without automatic release

#### Scenario: incomplete ownership cannot enter fallback

- **WHEN** the current reservation lacks expected user or account, or the candidate lacks or disagrees with either value
- **THEN** no name-window candidate binds and the row remains reserved and unbound under the durable held tuple

#### Scenario: malformed Submit evidence is transient denial

- **WHEN** an otherwise eligible name-window row has missing, unparsable, or out-of-window Submit evidence
- **THEN** it cannot bind or prove absence; unparsable evidence reports pass-only reason `fallback_submit_unparsable`, and the durable held tuple remains unchanged

#### Scenario: the attempt window is closed at both endpoints

- **WHEN** an otherwise eligible candidate's host-local Submit instant converts exactly to the immutable attempt anchor or frozen query-end
- **THEN** the instant is in-window, while an instant before the anchor or after the query-end is ineligible and cannot bind

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

### Requirement: Reserved-unbound identity-mismatch outcomes SHALL converge instead of wedging the pipeline

The journal SHALL persist, on each versioned accepted-submit master row, a consecutive-outcome counter that increments each time restart reconciliation durably records the exact-comment accounting transition `reconciliation_source=slurm_exact_comment` / `reconciliation_decision=identity_mismatch_blocked` for that reserved-unbound row, saturates once it reaches the configured limit (and does not increment while the exit is disabled), and resets to zero whenever the row's accounting state is replaced by any other durable transition — including a bind, an accounting-unavailable held tuple, an absence-path release, or the start of a new submission attempt after a reclaim. A pass-evidence action named `identity_mismatch_blocked` SHALL NOT increment the counter when the durable row instead remains `reconciliation_decision=accounting_unavailable` / `reconciliation_reason_class=comment_accounting_unproven`, as it does for an unsuccessful comment-less fallback.

When the counter reaches the configured limit and the row is past the accepted-submit grace period — anchored to the submission attempt start time, never to a timestamp refreshed by the counter's own writes — reconciliation SHALL migrate the row out of `reserved` into `reservation_lost` through a dedicated compare-and-swap journal transition (expected attempt, attempt anchor, expected `reserved` status, unbound required) recording the typed decision `identity_mismatch_released` and preserving the counter's final value. The released row is a deliberately non-reclaimable terminal: its idempotency key SHALL NOT be revivable through reservation reclaim; liveness is preserved because, when the retry budget still allows, new attempts mint new retry-suffixed keys. A disabled or non-positive limit SHALL preserve today's behavior (no release). The closed master-status vocabulary SHALL NOT gain new members for this exit, and the generic evidence-transition API's decision whitelist SHALL NOT be widened.

#### Scenario: Consecutive durable identity-mismatch transitions release the reservation

- **WHEN** a reserved-unbound row durably records the exact-comment `identity_mismatch_blocked` transition on N consecutive reconcile passes, N reaches the configured limit, and the row is past the accepted-submit grace
- **THEN** the row transitions `reserved` → `reservation_lost` with reconciliation decision `identity_mismatch_released`, the counter's final value is preserved on the row, and subsequent passes no longer surface the row as reserved-unbound

#### Scenario: Fallback pass-only identity blocks never enter the release ladder

- **WHEN** an explicitly comment-less fallback repeatedly emits pass action `identity_mismatch_blocked` while preserving durable `accounting_unavailable` / `comment_accounting_unproven`
- **THEN** `identity_blocked_streak` remains zero, no `identity_mismatch_released` transition occurs, and the guarded operator-demotion tuple stays valid

#### Scenario: A non-blocked durable outcome resets the streak

- **WHEN** a reserved-unbound row durably records exact-comment `identity_mismatch_blocked` transitions followed by any different durable reconcile transition before the limit is reached
- **THEN** the counter resets to zero and the release exit does not trigger until a fresh consecutive durable run reaches the limit

#### Scenario: A reclaimed reservation starts a fresh streak

- **WHEN** a row accumulates blocked transitions, exits through the absence path, is reclaimed into a new submission attempt, and then durably records its first exact-comment `identity_mismatch_blocked` transition
- **THEN** the counter has restarted from zero — the stale pre-reclaim streak does not make the first post-reclaim blocked transition trigger the release

#### Scenario: Guards hold the release closed

- **WHEN** the counter reaches the limit but the row is within the accepted-submit grace, or the limit is disabled (unset, zero, or negative), or the release compare-and-swap fails because the row's attempt state moved concurrently
- **THEN** no status migration occurs and the pass records the ordinary exact-comment `identity_mismatch_blocked` outcome

#### Scenario: The streak and release invariants are test-anchored

- **WHEN** the invariant guards for this counter and this decision are exercised — a negative or non-integer streak, a pre-outcome transition carrying a non-zero streak, an `identity_mismatch_released` decision whose status is not `reservation_lost`, and a non-identity-mismatch decision carrying a non-zero streak
- **THEN** each guard rejects the transition with its typed error and leaves the journal row unchanged, and each guard has a negative test that fails when that guard alone is removed

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

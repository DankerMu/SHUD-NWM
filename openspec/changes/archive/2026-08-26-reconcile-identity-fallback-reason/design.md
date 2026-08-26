## Context

Issues #1792, #1795, and #1565 meet at the accepted-submit forecast-cohort reconciliation boundary. The master `submission_attempt` advances on reclaim while successful per-model `hydro_run` rows are immutable, so comparing the two repeats the array-layout category error fixed by #1749. On node-22, `AccountingStoreFlags=(null)` makes exact-comment recovery impossible; #1116 correctly blocked false absence, but the remaining deterministic owner/name/attempt-window evidence is used only by operators. Finally, terminal inflight identity checking has fourteen `return False` sites (one site folds two independent predicates) and emits one undifferentiated action.

This is an expanded fixture with high repair intensity because a false positive can bind or re-submit the wrong Slurm job. The three issues are one PR because #1565's candidate still passes through the runtime validator fixed by #1792, and both alter requirements already carried by `pipeline-job-persistence`.

## Goals / Non-Goals

**Goals:**

- Treat only submission-stable fields as cross-submission runtime identity.
- Recover a comment-less reserved cohort only from one bounded, uniquely owned, fully validated accounting candidate.
- Keep every unsuccessful fallback outcome fail-closed and compatible with the #1564 operator-demotion compare-and-swap tuple.
- Give each terminal cohort identity failure site one stable reason token without changing the blocking action or performing durable writes.

**Non-Goals:**

- No automatic absence proof or retry permission from the name-window fallback.
- No fallback when the comment capability probe fails, omits `AccountingStoreFlags`, ownership identity is incomplete, or the accepted-submit contract is not current.
- No `squeue` integration, Slurm submission/cancellation, production-journal fixture manufacturing, database change, or change to the three out-of-scope `identity_mismatch_blocked` producers named in #1795.
- No rewrite of frozen `hydro_run.submission_attempt`; it remains lineage evidence.

## Decisions

### D1. Use a tri-state comment capability

The capability result is `True` (the config line contains `job_comment`), `False` (the line is present and explicitly lacks it, including `(null)`), or unknown (probe failure or missing line). `True` retains exact-comment behavior. Only explicit `False` may enter fallback. Unknown continues to raise `comment_accounting_unproven` before any `sacct`, preserving the safe direction of #1116.

### D2. Keep fallback inside the accepted-submit proof boundary

For a current accepted-submit forecast cohort with a strict UTC `submission_attempt_started_at` and non-empty expected user/account, the comment querier may issue one bounded `sacct --name nhms_forecast` query from that anchor to the querier's frozen `now`, requesting `JobID,JobName,State,ExitCode,Comment,User,Account,Submit`. The command renders both bounds as host-local wall-clock strings, matching the existing Slurm query rule. It reuses the existing byte, logical-row, and whole-query timeout budget rather than introducing an unbounded scan. A timezone-less Slurm Submit value is interpreted in the same host-local timezone and converted to UTC. Missing/unparsable Submit is transient denial only for an otherwise eligible forecast/owner row; an out-of-window row is ineligible. Parsing rejects forcing, batch/extern, and unrelated job names before candidate classification, deduplicates accepted forecast array/step ids by bare numeric master id, and retains at most two distinct masters, which is enough to distinguish unique from ambiguous. A candidate must have an in-window submit instant, exact user/account, and forecast-family name before it reaches existing cohort identity gates.

Zero candidates is not absence; two or more is ambiguity. Neither can demote or release the row. A unique candidate with an empty comment may pass the two existing comment gates; a present-but-different comment remains fatal. Every other identity and ownership gate remains strict. Runtime identity is evaluated for every current-contract forecast reservation, but its disposition depends on the proven accounting lane: an exact-comment/unknown-capability runtime mismatch keeps the existing durable `identity_mismatch_blocked` streak semantics, while a unique candidate reached through the explicitly comment-less fallback that fails runtime identity stays in the held-tuple family with streak zero.

### D3. Preserve held durable authority on every unsuccessful fallback

The durable row remains `reserved`, unbound, and retains `reconciliation_source=slurm_exact_comment`, `reconciliation_decision=accounting_unavailable`, and `reconciliation_reason_class=comment_accounting_unproven` for zero, ambiguous, and identity-mismatched fallback results. If the tuple is absent on the first pass, establishing that attempt-scoped tuple is the only permitted durable write. This keeps `nhms-pipeline demote-reserved-job` usable and prevents the identity-mismatch convergence ladder from releasing a candidate that was never positively identified.

Pass evidence is fixed: unknown/process failure is `query_unavailable`; zero is `fallback_no_match` with count 0; ambiguity is `ambiguous_fallback_match` with saturated count 2; a unique candidate failing a remaining gate is `identity_mismatch_blocked` with count 1 and no identity-mismatch durable transition; missing/unparsable Submit is `query_unavailable` with pass-only `fallback_submit_unparsable`. None increments the streak or proves absence.

Only a successful unique bind writes `reconciliation_source=slurm_name_window_unique`, `reconciliation_decision=matched_bound`, and the matched bare Slurm id. “Unique” means one durable claimant, not one result in one query: the candidate id is unowned by every other active current accepted-submit master in the reconcile inventory, and its Submit instant falls inside exactly one current reserved-unbound attempt window for the same expected Slurm user/account.

The journal stores two different time concepts and SHALL NOT conflate them. Existing `submitted_at` remains the gateway acceptance/commit timestamp on normal and exact-comment submit paths; it is not canonical Slurm accounting `Submit` evidence and therefore cannot prove numeric-id recycling. Each new bound attempt records an attempt-scoped immutable `slurm_binding_source`: `gateway_submit` for an ordinary successful submit commit, `slurm_exact_comment` for exact-comment recovery, or `slurm_name_window_unique` for fallback recovery. Only accounting evidence may additionally populate `slurm_accounting_submitted_at`; the current fallback bind requires it and persists the parsed host-local-to-UTC sacct `Submit` instant. Those two bind-provenance fields are independent of the mutable current reconciliation tuple, survive `accounting_unavailable` defer and every terminal projection, and clear only when reclaim begins a new attempt.

Whenever a later transition reasserts `matched_bound`, it derives the current legal reconciliation source from immutable binding provenance (`slurm_name_window_unique` for fallback, `slurm_exact_comment` for unchanged exact-comment/gateway paths) rather than using a factory default. Settled same-id history permits recycle only when the existing row carries a strict UTC `slurm_accounting_submitted_at` from a provenance-compatible accounting bind and that instant differs from the new candidate; absent, gateway-only, exact-comment-without-Submit, malformed, or otherwise mixed provenance is uncertainty and blocks fail-closed. Existing current-contract rows that predate these additive fields remain readable; missing canonical accounting Submit never becomes evidence of recycle.

The bounded flat master surface names source/cycle candidates, but canonical cycle authority alone decides occupancy: a stale, damaged, missing, or valid-but-wrong-kind derived projection cannot fabricate or hide an owner. Source/cycle discovery comes unconditionally from the safe master filename before payload decoding; decoded payload kind may assist only the no-lineage compatibility branch and may never suppress canonical cycle replay. If a terminal batch commits the canonical journal before its direct projection, inventory cleanup atomically restores a missing flat locator before pruning the anchor; first-migration backfill preserves a handoff anchor until that restore completes. Thus a concurrent fallback always sees either the canonical anchor or the bounded flat locator, including crash/resume and marker-absent migration states. The typed commit API serializes every owner/claimant-creating write under the journal-global cross-process lock, and the reconcile snapshot refuses every multi-claimant candidate before the first bind. `AcceptedSubmitTransition` accepts `slurm_name_window_unique` only for `matched_bound`; every other current accounting decision remains exact-comment sourced without erasing immutable binding provenance.

### D4. Remove `submission_attempt` from runtime identity, not from lineage

`forecast_cohort_runtime_identity_matches` continues to require the same per-model row and strict `run_id`, `model_id`, `scenario_id`, `source_id`, and `cycle_time`. It does not compare either `array_task_id` or `submission_attempt`, because both describe the submission that created an immutable row. `candidate_id` and `basin_id` keep present-but-different-is-fatal behavior. Live sacct master/task identity and the current master's `cohort_members` still prove the active submission.

### D5. Return one reason per terminal identity failure site

`_terminal_file_cohort_identity_matches` returns the match verdict with an optional reason. The folded validity/runtime predicate is split. The stable tokens are:

- `cohort_identity_invalid`, `runtime_identity_mismatch`, `master_id_mismatch`, `comment_mismatch`, `stage_family_mismatch`
- `ownership_unproven`, `ownership_user_mismatch`, `ownership_account_mismatch`, `cohort_members_unparsable`
- `task_identity_values_mismatch`, `task_identity_values_unparsable`, `task_id_unparsable`, `task_mapping_mismatch`, `task_job_name_mismatch`, `task_comment_mismatch`

The caller adds the token to `ReconcileOutcome.reconciliation_reason_class`. Scheduler evidence serializes it additively; all prior keys and values are unchanged. No reason token is written to accepted-submit durable state on this inflight leg.

### D6. Production validation is read-only plus scratch state

Node-22 validation first proves the live capability is explicit `False`. Existing historical `nhms_forecast` accounting rows may be queried over a narrow frozen interval to produce one unique result and a wider interval to produce ambiguity. Binding is exercised only against a scratch file journal; production journal, scheduler, and Slurm state are untouched. If no suitable historical rows exist, the runbook's production-safe rule permits deterministic tests to stand instead of manufacturing a held row.

## Risks / Trade-offs

- **A broad name query could bind the wrong job** → exact owner/account, submit instant, job family, current contract, runtime identity, single durable claimant, and unoccupied Slurm id are all mandatory; any uncertainty keeps every claimant held.
- **Probe failure could be mistaken for explicit no-comment capability** → missing line and process failure are a separate unknown state and issue no accounting query.
- **Ambiguity could break #1564 disposal** → fallback failures do not replace the durable held tuple.
- **Removing attempt comparison weakens proof of the current submission** → current submission proof remains on live master/task accounting and `cohort_members`; attempt stays auditable lineage, not equality identity.
- **New reason tokens could alter existing consumers** → the action string and existing evidence keys/values remain unchanged; the reason field is additive and optional.

## Migration Plan

Deploy as an additive scheduler/reconcile change. Rollback restores the prior fail-closed no-automation behavior; rows bound successfully before rollback remain ordinary accepted rows with a recognized persisted source in the same release. No data backfill is required.

## Open Questions

None. The issue acceptance line asking stale `array_task_id` itself to yield a runtime-mismatch reason is superseded by merged #1749, which requires that stale layout to pass; the diagnostic regression therefore uses a genuine remaining runtime-identity mismatch while separately preserving stale-layout compatibility.

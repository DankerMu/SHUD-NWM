## Context

`_candidate_state_decision_evaluated` (`services/orchestrator/scheduler_state_decision.py`) is the DB-free planner's per-member decision. Its "is something in flight" checks are:

- `_state_active_jobs`: rows with a real Slurm binding and status in `ACTIVE_PIPELINE_STATUSES`;
- the hydro status arm, which is `created` / `staged` / `submitted` placeholders;
- `active_truth`.

A `reserved` row whose `sbatch` result is ambiguous has no Slurm binding and a non-active status. Only the hydro placeholder stands in for it, and three supersession branches can null that placeholder. `completed_stage_retry_supersedes_hydro_placeholder` is the one that fired on 2026-09-27. When a placeholder is nulled, a held reservation reads as "nothing in flight".

The journal-level `has_active_pipeline` (`_job_is_active`: any non-terminal status, including `reserved`) is not consulted before the decision when a state provider is configured (`scheduler_candidates.py:406-425`). The second journal gate after the decision (`scheduler_candidates.py:1246-1256`) is bypassed for any decision that `candidate_state_scoped_retry_detector` (`scheduler_state_decision.py:725-752`) recognizes as a candidate-scoped retry (identity + restart_stage), and `retry_after_completed_stage` qualifies. That is exactly how 57637 got through.

The fix goes in the decision rather than in the detector. A decision-level skip stops every retry/resume branch before it can reach that bypass, and it keeps the detector's contract for the retries it legitimately exempts (a failed row's retry must pass the journal's active check, because that row itself is non-terminal).

## Goals / Non-Goals

- Goal: no candidate-state decision may resubmit, retry, or resume a candidate while a held reservation attributed to it exists.
- Goal: once the reservation is bound or released, decisions are unchanged.
- Non-goal: the one-bind-per-pass reconcile budget (#2655).
- Non-goal: a superseded duplicate attempt's partial failure outranking a later success. That shape only arises after a duplicate, which this change prevents; it is recorded on #2666 as out of scope.
- Non-goal: changing `ACTIVE_PIPELINE_STATUSES`. It is shared by the failure-signal, placeholder, and retry predicates, and widening it would change their semantics.

## Decision

Add a held-reservation predicate over the decision state's pipeline rows. A row matches when both hold:

- its status equals `persistence.RESERVED_STATUS` (the single existing constant, not a new set);
- `_job_has_real_slurm_binding` is false.

Evaluate the predicate right after the `active_slurm_job` check and before any supersession, completed-stage, terminal, or failure branch. On a match, return `CandidateStateDecision("skip", "active_duplicate_pipeline", {... "decision": "skip_active", "active_status": "reserved", "held_reservations": [...bounded row evidence...], "replacement_submitted": False})`.

- Reusing `active_duplicate_pipeline` keeps every downstream consumer on a reason they already route: stall probe, typed-reason runbook, and the evidence summaries that keep `reason` under compaction. #2655 production already showed this reason for the same held shape.
- Attribution is by presence in the provider-filtered candidate state, NOT by `cohort_member_row_is_attributed` / `COHORT_MEMBER_ATTRIBUTED_STAGES` (`scheduler_state_types.py:59,91-108`). That stage gate excludes `forecast`, so reusing it would miss exactly the #2666 row.
  - `FileOrchestrationJournal` already drops `non_member` rows (`file_orchestration_journal.py:1868-1873`, #2543/#2603).
  - Every remaining reserved, unbound row blocks, whatever its `cohort_membership` annotation: `member`, `unwitnessed` (cycle-scope, no recorded members), or `incomplete` (truncated membership record, fail-closed as in #2603 B2b).
- The predicate must see the rows even if the ordinary decision view strips the shared-cycle surface (E13b). If the reserved master row is only visible in the undecorated state, evaluate it there. The implementer confirms which view carries the forecast master row in the production shape (the #2655 rehearsal receipt shows `["forecast","reserved",null,"member"]` in `pipeline_jobs`).

### Why before the terminal and blocked branches, and what it costs

- **Why.** The three skip→forced-retry rewrites (journal-predecessor quarantine, strict warm-start mismatch, `terminal_run_manifest_missing`) only rewrite `terminal_*` skip reasons and explicitly exclude `active_duplicate_pipeline` (`scheduler_candidates.py:79,88-97`). A held candidate that reached a terminal branch could therefore be rewritten into a forced resubmission. The held check has to precede them.
- **Consumers that change:**
  - `_cycle_completion_verdict` (`scheduler_discovery.py:320-376`) counts every reason other than the two terminal ones as `gap`, so a cycle with a held row reads `gap` until reconcile binds or releases it. Successor cycles and backfill wait on it, which is the intended freeze.
  - The retention frontier (`_RETENTION_TERMINAL_SKIP_REASONS`, `scheduler_runtime.py:1920`) treats `active_duplicate_pipeline` as in-flight.
- **Cost.** While a row is held, a candidate that would otherwise be `permanent_failure` / `missing_upstream_artifact` / `cohort_membership_unprovable` reads as skip. `list-operator-actions` scans blocked literals (`production-scheduler-orchestration` spec:960), so it stops listing that candidate until the reservation resolves. This is acceptable because the operator exit for a held row is reconcile anyway.

## Risks / Trade-offs

- **Stale reserved rows freeze candidates.** This is intended and matches the #1116/#1565 fail-closed contract. Restart reconcile owns their exit, by binding, demotion (`demote-reserved-job`), or identity-blocked release. Measured footprint: one row in production.
- **Manual retry markers do not override a held reservation.** A marker on a candidate with a live-ambiguous submission would still double-submit. The runbook exit is reconcile, not the marker.

## Required evidence

| Scenario | Test |
|---|---|
| Production shape, driven through the real `FileOrchestrationJournal.candidate_state` → `build_candidates` (not a hand-built state dict), so the membership annotation, the E13b view strip, and the :1246 gate are all exercised. Input: cohort of ≥2 members; model-less forecast master with recorded `cohort_members`, `reserved` / `submit_result_ambiguous` / `slurm_job_id=null`; forcing succeeded; hydro `created`; no failure rows | every member `skip` / `active_duplicate_pipeline`, `active_status: reserved`; no candidate selected; red before (`retry_after_completed_stage`) |
| Held `state_save_qc` master (gfs 12Z first-half shape: forecast succeeded, state_save_qc `reserved` / ambiguous / unbound) | skip, not `resume_after_completed_stage` |
| Terminal hydro success + an attributed held row | skip / `active_duplicate_pipeline`; `_cycle_completion_verdict` = `gap`; retention frontier counts it as in-flight |
| Held reserved row whose `cohort_membership` is `incomplete` or `unwitnessed` | skip |
| Evidence compaction (`pre_write_size_pressure`) of a pass with held-skipped candidates | `skipped_candidates[].reason` still `active_duplicate_pipeline`; `held_reservations` bounded at `candidate_state_job_limit` rows, each row limited to the `_job_state_evidence` projection |
| Same, reason `comment_accounting_unproven`, and same, reason `ambiguous_fallback_match` | skip |
| Held row plus a failure signal elsewhere in the cycle (the 0925 00Z shape) | still skip, and now explicitly held-reservation evidence |
| Held row plus a manual-retry marker | skip (no marker override) |
| `non_member` sibling cohort reserved row | not blocking (decision unchanged) |
| After bind + terminal projection `succeeded` | resume from the next stage (gfs 12Z shape: state_save_qc) |
| After release (`reservation_lost`, identity_mismatch_released) | unchanged pre-change decision (measured: `retry` / `resume_after_completed_stage` / forecast — the #1116 release contract the wedge test's third pass relies on; the retry *service* still refuses auto-retry of the released row itself) |
| Reserved row WITH a real Slurm binding | handled by the existing active-job path (unchanged) |
| Chain-level reproduction on the file journal: gateway timeout on the forecast submit, then a planner pass | no second forecast sbatch |

## Rollout

- Merge, then deploy with the timer stopped: node-22 `git pull --ff-only`.
- Rehearse on a fresh copy of the production journal (fenced harness, `sacct` / `scontrol` only). Expected: the held IFS row binds to 57553 and becomes `partially_failed`, and no member is `selected` for a forecast resubmission.
- Then run the supported basin-18 manual retry (preview, then `--execute`), start the timer, and record the first live pass receipt.

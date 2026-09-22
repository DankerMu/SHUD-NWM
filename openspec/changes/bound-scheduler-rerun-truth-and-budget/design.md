## Context

The node-22 db-free scheduler derives every candidate decision from file-journal rows. The three issues are one failure class: a decision reads a stale truth (old success row, frozen `hydro_run`, prefix-local suffix history) and ignores a newer one that the budget or the breaker depends on.

## Decisions

Change surface:
- `scheduler_state_decision._candidate_state_decision_evaluated` (terminal legs at `:227-281`, failure leg at `:404-414`); the new helper lives in a new module (for example `scheduler_state_terminal_recency.py`) that reuses `_latest_failure_truth_timestamp` / `_first_state_datetime` from `scheduler_state_identity_filter.py` without editing that file.
- `FileOrchestrationJournal.completed_pipeline_init_state_identity` (`file_orchestration_journal.py:1413-1493`). The `completed_pipeline_init_state_id` wrapper and the discovery consumers (`scheduler_discovery.py:385-391,497,557`) inherit the change through the accessor, so no edit is needed there.
- Cycle-stage retry minting: `chain.py:_next_retry_attempt_for_stage` and `chain_forecast_orchestrator_cycle._retry_cycle_stage_job_id` (three call sites in `chain_forecast_execution.py:212,221,246`), plus the scheduler-side source of the budget attempt handed to the chain (retry evidence built in `scheduler_candidates.py`, for example `_strict_warm_start_terminal_retry_evidence` at `:2879-2899` and its siblings).

Governing invariant: **for any candidate, the newest recorded truth decides terminal classification and identity authority, and every retry the chain mints for a stage carries an attempt strictly greater than the stage-scoped attempt the budget has already charged — so no path resubmits once that attempt has reached `retry_limit`.**

D1 (#2401) — success-vs-failure recency. Success truth time for `terminal_pipeline_success` is the newest timestamp among the candidate-scoped terminal-success completion-stage jobs/events (the same set `_pipeline_terminal_success_is_candidate_scoped` accepts). For `terminal_completed_cycle`, it is the timestamp of the evidence `_completed_cycle_terminal_evidence` (`scheduler_state_decision.py:432`) binds. The implementer pins this down first; if that evidence carries no timestamp, fall back to the pipeline success time and record the choice. The tie rule is `success >= failure → terminal`, the same as the hydro leg. A success with no timestamp keeps today's behaviour, and that case must be tested. No third timestamp comparator: reuse the hydro-leg primitives.

D2 (#2397) — read-side authority by recency. In `completed_pipeline_init_state_identity`, compare the newest (by `created_at`) accepted-submit cohort master that names the model, is terminal-success for it, and carries exactly one self-bound identity against the matching completed `hydro_run` row (`created_at`). When the master is strictly newer, the master's identity wins. Otherwise `hydro_run` keeps priority (legacy per-basin semantics). The `hydro_run` comparison key must be one a same-run_id rerun does not refresh. `update_hydro_run_status` rewrites `updated_at` (`:2743`), so the key is established by the real-write-path test, not by reading the code. When no key survives the rerun, use the master's own terminal timestamp against the `hydro_run` creation/terminal time recorded before the rerun, and document the rule. The write path stays retriable-only.

D3 (#2404) — mint floor from the budget read. The mint is `max(prefix-scoped next, budget_attempt + 1)`, then scan forward past any id already present in the journal (the #1201 occupied-terminal-row case). The floor is stage-scoped (forecast), comes from the same `_state_retry_attempt(state, stage=...)` chain the budget consumes, and is carried to the chain as an explicit field on the retry decision evidence only. It is not a new run-manifest top-level key, because `schemas/run_manifest.schema.json` sets root `additionalProperties: false`. The floor is model-scoped: a sibling model's `_retry_<n>` rows never raise it, and a cohort master is minted above the largest member floor and charges every member that shared attempt; a lower-floor member can only block earlier (review round 1, CR-2; per-member charging is deferred to a follow-up because the master reservation writer lives in a guard-blocked file). The ordinary failure retry carries the same floor (review round 1, sibling of the governing invariant). #1845 is still open, so this is asserted here directly. It is **not** `context.retry_attempt`: #1201 showed that a pinned attempt deadlocks on an occupied row, and #2393 showed that field leaks across stages. The auto-retry service minter (`file_orchestration_journal.py:11289`, `f"{source['job_id']}_retry_{n}"`) is a sibling surface: audit it and either prove it cannot under-mint across prefixes or apply the same floor.

D4 (#2404 cohort) — before choosing code, produce evidence of whether the budget advances under `..._forecast_cohort_<digest>` run_ids, including a digest change between passes. If it does not advance, fix it in this change with a regression test. If it does, attach the evidence (test or trace) to the PR.

Must preserve:
- Old failure followed by a newer success is still terminal on all three legs (hydro, pipeline, completed-cycle).
- `terminal_hydro_success` behaviour is byte-identical.
- The §8.7 breaker still engages when a rerun records the stale token X again.
- Legacy shape (no accepted-submit master, identity only on `hydro_run`) resolves to the same identity.
- Same-prefix stacked suffix parsing (#2254), occupied terminal-row forward scan (#1201), the budget-exhaustion demotion `blocked_strict_warm_start_init_state_mismatch`, and #1555 operator re-entry confirmation (`quarantine_rerun_count` / `budget_reentry_count`) all keep their existing regressions green.
- Forced-resubmit whitelists (`chain_forced_resubmit.py`, `chain_runtime_utils.py:216-230`) keep their member sets.

Sibling surfaces:
- Every skip→forced-retry rewrite fed by the completed-type skips: `_journal_predecessor_identity_quarantine` (`scheduler_candidates.py:2542-2670`), `terminal_run_manifest_missing` (`:620-628`, no budget gate), and the strict warm-start mismatch leg (`:2771-2842`). D1 removes their input, and each gets a test showing it stops re-firing after a newer failure.
- Identity consumers: `completed_pipeline_init_state_id`, `completed_pipeline_init_state_id_occurrences` (the breaker count, which keeps its completed-only filter), and the `scheduler_discovery.py` §8.7 scoring.
- Mint sites: the three `chain_forecast_execution.py` call sites; the auto-retry service minter; `_pipeline_retry_job_id`.
- The DB lane `hydro_run` / identity: none. It is out of scope and must not change.

Seams under test: `scheduler._build_candidates` over a real `FileOrchestrationJournal` (the existing `_seed_budget_journal` / `_record_budget_attempt` / `_budget_pass` fixtures in `tests/test_production_scheduler.py`); the public `orchestrate_cycle` with `FakeCycleSlurmClient` (`tests/test_orchestration_chain.py:63`); the real `create_hydro_run_from_basin` plus pipeline terminal write path. No hand-written `hydro_run` rows for #2397.

Non-goals: breaker threshold; global retry limit; #1555 re-entry semantics; a general fix for #1845 model isolation in stage matching (only the D3 floor is asserted model-scoped here); the DB lane; `context.retry_attempt` residue (#2393).

Review focus:
1. Can D1 turn a genuinely completed cycle non-terminal (for example a stale failed placeholder or a repaired-stage row newer than success)? Check the `_pipeline_job_is_repaired_stage_evidence` and manual-marker exclusions that `_latest_failure_truth_timestamp` already applies.
2. Does D2's comparison key survive `update_hydro_run_status` on the real write path?
3. Does D3's floor avoid `context.retry_attempt`, and does it keep scanning forward on occupied ids?
4. Is the cohort evidence (D4) real (run, not inferred)?
5. After D1, does a failed quarantine rerun land in `retry_failed_candidate` and stop at `retry_limit` end to end?

## Risks / Trade-offs

- D1 makes more candidates retryable when a newer failure exists. That is the intended budgeted behaviour; the retry budget and the missing-forcing block still gate it.
- D2 lets a newer master override `hydro_run`, and a buggy newer master could mask a correct `hydro_run`. Mitigation: only terminal-success accepted-submit masters with a self-bound identity qualify, the same filter as today's second authority tier.

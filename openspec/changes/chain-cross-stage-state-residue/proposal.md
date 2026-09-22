## Why

Four defects in the forecast chain's stage loop share one failure class: state that belongs to one scope (an upstream stage, a sibling model, a stripped evidence marker, a retired stage) leaks into another scope's decision. The user asked for them in one PR (batch C).

- #2393 — `chain_stage_execution` writes the reservation's `submission_attempt` back into `context.retry_attempt`, and nothing resets it between stages. `_retry_cycle_stage_job_id` short-circuits on `context.retry_attempt or <own-row derivation>`, so a downstream stage targets the upstream stage's attempt, hits an occupied terminal row, loses the reservation, and ends as `skipped_duplicate_submission` with no durable cycle state. The same residue opens the `context.retry_attempt is None` gate in `_terminal_stage_needs_manual_retry`. Confirmed budget re-entry (#1768 / PR #2398) consumes the operator confirmation on the forecast stage and then wedges on `state_save_qc`.
- #1845 — `find_existing_stage_job` matches stage rows model-blind. On the unscoped (multi-model cohort) path, a sibling model's terminal `forcing` row satisfies the match and the chain resumes it instead of producing the current model's forcing. Since #2447, forcing rows carry complete `cohort_members` identity, and the forcing branch already uses it for blockers and same-model recovery, but falls through to the model-blind terminal list when neither exists.
- #2416 — `_restart_stage_from_basins` falls back to `state_evidence.restart_stage/restart_from_stage` when the basin manifest has no top-level `restart_stage`. That re-reads exactly the marker the manifest builder strips for fresh full-chain candidates (m23-255 defence), and because the result is cohort-wide, one residual marker skips convert/forcing for the whole `(0,"full")` cohort.
- #2394 — `cycle_download_success_missing_raw_manifest` (+ delegate + call branch) is unreachable since `24505db77` retired the download stage; it already misled the #1845 design once.

## What Changes

- #2393: the stage loop restores `context.retry_attempt` to the invocation's original claim (`_retry_attempt_from_basins`, i.e. an operator/API direct field or an active manual-retry marker; `None` when markerless) on entry to every stage. Within a stage, the reservation writeback still feeds that stage's manifest/placeholder/ambiguity consumers. Supersedes #1201 design D2 "继承保留" (the user assigning this issue is the domain-owner decision the issue requested).
- #1845: in the forcing-array branch of `find_existing_stage_job`, a row whose complete member identity (`forcing_member_model_ids`) is not exactly the current cohort's model set is never a resume/terminal match. When rows are excluded, the new submission takes the stage's next free `_retry_N`. Rows without complete identity keep today's match; since `cohort_members` is written only on the FileJournal lane, the DB-legacy/PG lanes keep the model-blind match entirely (lane-level limitation). Production never reaches the unscoped path today (`scheduler_execution` always stamps `orchestration_run_id`), so this is latent hardening.
- #2416: `_restart_stage_from_basins` reads only the top-level `restart_stage`; `_candidate_basin_manifest` writes the top-level key as the raw `restart_stage or restart_from_stage` (the chain canonicalizes on read), keeping the fresh-full-chain exception. A cohort start stage is the earliest claim among members that carry one; members without a marker do not pull the start forward (documented as intentional, since a `(0,"full")` cohort now has no marker at all). The `restart_from_stage`-only emitter census is recorded in design.md.
- #2394: direction A — delete the probe, its delegate, the call branch, and the `__all__` export; rename the legacy download-row test so it no longer implies a chain-side raw-manifest guard.

## Capabilities

### Modified Capabilities

- `slurm-job-chain`: retry-attempt precedence names the invocation claim (not a reservation writeback); new requirements for model-exact forcing stage matching and the manifest-only cohort restart stage.

## Impact

- Code: `services/orchestrator/chain_forecast_execution.py`, `chain_forecast_cycle.py`, `chain_forecast_orchestrator_cycle.py`, `chain_runtime_utils.py`, `scheduler_candidate_manifest.py`; `scheduler_state_failure.py` only if the census shows an emitter must change.
- Tests: new `tests/test_chain_cross_stage_state.py` (≤1000 lines), `tests/test_orchestration_chain.py` (#2394 rename + any fixture prerequisites), routed by `scripts/select_ci_tests.py`.
- Runtime: both lanes (DB-legacy/PG and FileJournal DB-free on node-22).

## Triage

```text
Issue type: bugfix x3 + dead-code removal x1 (one PR at user request)
Fixture level: expanded
Upstream suggested level: absent (issue-scribe issues)
Blast radius: forecast chain stage loop — a wrong fix resubmits production jobs that should resume (duplicate Slurm work), resumes what should resubmit (downstream runs on missing inputs), or skips convert/forcing for a whole cohort.
Selected risk packs: concurrency/shared state/ordering; legacy compatibility; error handling/partial outputs; documentation
Evidence floor: red-then-green orchestrate_cycle regressions per issue; uv run ruff check .; node-27 pytest tests/test_orchestration_chain.py tests/test_production_scheduler.py tests/test_chain_cross_stage_state.py at PR head
```

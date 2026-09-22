## Change surface

`ForecastOrchestrator.orchestrate_cycle` → `_run_cycle_chain_stages` stage loop (`chain_forecast_execution.py`), `_retry_cycle_stage_job_id` / `_terminal_stage_needs_manual_retry` (`chain_forecast_orchestrator_cycle.py`), `find_existing_stage_job` (`chain_forecast_cycle.py`), `_restart_stage_from_basins` / `_candidate_scoped_cycle_execution` (`chain_runtime_utils.py`), `_candidate_basin_manifest` (`scheduler_candidate_manifest.py`). Locate by symbol; every line number in the four issues is stale.

## D1 (#2393) — per-stage attempt scope

Capture `original_claim = context.retry_attempt` once before the loop (it is `_retry_attempt_from_basins(...)` from `chain_forecast_control`); at the top of every stage iteration set `context.retry_attempt = original_claim`. Consumers split:
- Stage-entry consumers (must see the claim): `_retry_cycle_stage_job_id` `or` short-circuit; `_terminal_stage_needs_manual_retry` `retry_attempt is None` gate; FileJournal reserve-time `submission_attempt = max(context.retry_attempt or 1, <own job-id suffix>+1)` in `chain_forecast_orchestrator_cycle` (computed before the writeback — after the reset it reads the claim plus this stage's own suffix, which is exactly the FileJournal-lane fix; task 2.2 asserts the downstream reservation's `submission_attempt`).
- In-stage consumers after the writeback (must still see this stage's reservation attempt): `chain_stage_execution` ambiguity/expected-attempt reads, `chain_manifests` `submission_attempt`.
- `_schedule_cycle_stage_retry` does not go through `_retry_cycle_stage_job_id`; the floor (`retry_attempt_floor`) is read from evidence, not from context. Neither is affected.
- Explicit claim N (operator/API direct field or active manual marker) is by design re-applied to every stage; an occupied `<stage>_retry_N` downstream can still wedge — that residual belongs to the #1201/#1593 claim contract and is stated in the spec delta, not fixed here.
Rejected alternative: `max(context.retry_attempt or 0, derived)` — inflates a fresh manual claim's precise identity and leaves downstream manifests inheriting upstream numbers.
Consequence (sibling surface, pinned by a test): after the fix the `is None` gate no longer opens for downstream terminal rows under a markerless non-whitelisted decision; they resume as a markerless candidate does (the #1201 markerless-equivalence rule).

## D2 (#1845) — model-exact forcing match

Options from the #1845 descoping comment: whole-stage fail-closed (cohort stalls until backfill), per-basin eviction (new eviction logic + positional misattribution in resume accounting), tightening `job_matches_stage` on `model_id` for all stages (unknown `model_id` fill rate in historical rows). #2447 since landed complete forcing member identity (`cohort_members`), which gives a narrower fourth option, picked here:

- In the forcing-array branch of `find_existing_stage_job`, filter `matches` BEFORE `active_matches` is computed: drop every row with `forcing_member_identity_is_complete` whose `forcing_member_model_ids` != `basin_model_ids(context.active_basins)`. Then the existing blocker branch (cycle-wide overlapping unresolved rows) and the `recovered` branch (exact-set resolved rows) run as today. A disjoint in-flight sibling is therefore neither polled as ours nor a blocker; an overlapping in-flight sibling stays a blocker.
- Id minting when rows were excluded: on the unscoped path the run_id (`cycle_<src>_<time>`) is shared by all models, so the bare id `job_<run_id>_forcing` may already be occupied by the excluded sibling row, and reserving it would end in `skipped_duplicate_submission`. When `existing_job is None` but excluded forcing rows exist under this run's stage base id, mint via `_mint_cycle_stage_retry_job_id` over the full stage row list (excluded rows included), i.e. the next free `_retry_N`.
- Accepted consequence: a terminal succeeded row whose complete member set overlaps but is not equal to the current cohort (cohort changed) is excluded and forcing is resubmitted for the whole current cohort — the overlap is recomputed. This matches the #2447 exact-set rule already used by `reordered_forcing_resume_basins`; recompute is correct-but-redundant, resuming is wrong-attribution.
- Lane scope: `cohort_members` is written only when `supports_accepted_submit_reconcile` is true, which today is only the FileJournal repository (node-22 DB-free production). On DB-legacy/PG/`StoreBackedCycleRepository` rows never have complete identity, so D2 does not apply there and those lanes keep the model-blind match (recorded limitation, pinned by a test).
- Reachability: the only production caller, `scheduler_execution` cohort execution, always stamps `orchestration_run_id` on every basin (per-candidate run id, or `_cohort_<digest>`), so `_candidate_scoped_cycle_execution` is true and the unscoped cycle-wide query is not reached in production today. The unscoped path is reached by direct `orchestrate_cycle` calls without `orchestration_run_id` (trigger/test paths). #1845 is therefore a latent hazard hardened here, not a live defect; tests use the direct entry on the FileJournal lane.
- Non-goal: model-blindness of non-forcing stages.
Red tests must reproduce on current code first; if a case does not go red, record the evidence in the PR and ship only the regression pin.

## D3 (#2416) — single source for the cohort restart stage

`_candidate_basin_manifest` writes top-level `restart_stage = state_evidence.restart_stage or state_evidence.restart_from_stage` (the fallback only when `_canonical_downstream_stage` recognizes it, so `restart_from_stage: "download"` never reaches the top level; value otherwise raw; the chain canonicalizes on read with `_canonical_restart_stage`, as `_retry_attempt_from_basins` does — no new chain import in the scheduler manifest module) except for fresh full-chain candidates (unchanged exception); `_restart_stage_from_basins` reads only top-level `restart_stage`. `min` stays: cohort start = earliest claim among marker-carrying members; a markerless member does not force stage 0 (fresh full-chain cohorts are grouped `(0,"full")` and now carry no marker at all). Census of `restart_from_stage`-only emitters is recorded in tasks 3.4; the `scheduler_state_failure` full_chain emitter (`restart_from_stage: "download"` → canonical `None`) is safe and changes only if the census requires it.
Second consumer `_candidate_scoped_cycle_execution`: basins carrying `orchestration_run_id` (every production basin) are unaffected; assert the single-basin scope decision for (a) a fresh full-chain manifest with residual marker and no run id, (b) an ordinary restart manifest.
Test fixtures that hand-build basins with only `state_evidence.restart_*` lose their restart after D3; each affected test gains the top-level key as a recorded prerequisite, assertions unchanged (census in 3.4).
Other chain readers of `state_evidence.restart_*` — `chain_forced_resubmit` and `_active_orchestration_conflicts` in `chain_runtime_utils` — are gated by retry decisions that a fresh full-chain candidate does not carry; they stay as-is (implementer confirms, else reports).

## D4 (#2394) — delete dead probe (direction A)

`docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md` lists `_cycle_download_success_missing_raw_manifest` as a protected forwarder read by the entropy audit (`scripts/governance/entropy_audit/schema.py`, `tests/test_entropy_audit_facade_guard_*.py`); remove those rows with the code. Re-run the reachability census at HEAD (stage lists, `.stages =` assignments, grep hits) and paste it into the PR body; delete probe, delegate, call branch, `__all__` entry; rename the legacy download-row test, keeping its assertions (first submission `convert`, no `download_retry_1`).

## Governing invariant

Every stage decision in one `orchestrate_cycle` call is derived from that stage's own rows, that model's own identity, and the manifest's top-level fields — never from another stage's reservation, another model's row, or a marker the manifest deliberately stripped.

## Sibling surfaces

- Producers: `chain_forecast_control` (`retry_attempt`, `restart_stage` derivation), `scheduler_candidate_manifest` (manifest top-level fields), `scheduler_state_failure` full_chain emitter, `chain_repository_state` emitters.
- Other `state_evidence.restart_*` readers: `chain_forced_resubmit`, `_active_orchestration_conflicts`.
- Consumers: `_retry_cycle_stage_job_id`, `_terminal_stage_needs_manual_retry`, `_candidate_scoped_cycle_execution`, `_restart_stage_index`, `find_existing_stage_job` forcing branch, `_resume_cycle_stage` accounting.
- Lanes: DB-legacy/`StoreBackedCycleRepository`, FileJournal (`supports_accepted_submit_reconcile=True`).

## Seams under test

`ForecastOrchestrator.orchestrate_cycle` (public), `_candidate_basin_manifest` → `_restart_stage_from_basins` (manifest→chain hop), `find_existing_stage_job` for identity unit pins.

## Non-goals

#2254 stacked-suffix parsing; reserve/reclaim predicates; marker freshness (#1593); non-forcing stage model matching; `_candidate_scoped_cycle_execution` criteria themselves; scheduler admission raw-manifest repair lane.

## Review focus

1. Stage-entry reset does not break in-stage reservation consumers or the accepted-submit ambiguity release.
2. Forcing exclusion cannot turn an overlapping in-flight sibling into a fresh duplicate submission.
3. No `restart_from_stage`-only emitter loses its restart after D3.
4. #2394 deletion leaves no reachable behaviour change.

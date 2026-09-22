## Risk Packs

- [x] Concurrency / shared state / ordering — **selected**: `context.retry_attempt` is shared mutable state across the stage loop; forcing overlap/blocker ordering. Covered by 2.x, 4.x.
- [x] Legacy compatibility — **selected**: fresh manual claims keep precise identity; legacy forcing rows without member identity keep resuming; ordinary restart manifests keep their restart; download legacy row behaviour unchanged. Covered by 2.4, 3.3, 4.3, 1.2.
- [x] Error handling / partial outputs — **selected**: no `skipped_duplicate_submission` wedge; no downstream run on a sibling model's forcing; no convert/forcing skip. Covered by 2.1, 3.2, 4.1.
- [x] Documentation / migration notes — **selected**: supersede #1201 D2 "继承保留" in the spec delta; `min` semantics comment; restart_from_stage emitter census. Covered by 3.4, 3.5, 5.3.
- [x] Public API / CLI / script entry — not selected: no route/CLI/payload change.
- [x] Config / project setup — not selected.
- [x] File IO / path safety — not selected: no new object-store probe (D2 uses row identity, not a forcing witness).
- [x] Schema / columns / field names — not selected: manifest keeps the existing `restart_stage` key; no job-row schema change.
- [x] Auth / permissions — not selected.
- [x] Resource limits — not selected: fixes remove resubmission paths, add none unbounded.
- [x] Release / packaging — not selected.

## 0. Setup

- [ ] 0.1 Branch `feat/issue-2393-1845-2416-2394-chain-cross-stage-state-residue` from `origin/master`. Locate code by symbol, not by issue line numbers (all stale).
- [ ] 0.2 New regressions go in `tests/test_chain_cross_stage_state.py` (≤1000 lines), importing helpers from `tests.test_orchestration_chain` / `tests.test_production_scheduler` rather than copying. Confirm `scripts/select_ci_tests.py` routes the new file from the touched sources (add a rule if needed, with its test).
- [ ] 0.3 Every new-behaviour test is shown red on pre-change source, then green.

## 1. #2394 — delete the dead download raw-manifest probe (direction A)

- [ ] 1.1 Re-run the reachability census at HEAD: stage names in `M3_STAGES`, `STAGES`, `LEGACY_FORECAST_STAGES`, `ANALYSIS_STAGES`, `stages_through`; `.stages =` assignments in tests; `grep -rn download_success_missing_raw_manifest` (expect 5 hits). Record the output for the PR body.
- [ ] 1.2 Delete `cycle_download_success_missing_raw_manifest`, its `__all__` entry, the delegate `_cycle_download_success_missing_raw_manifest`, and the call branch in `_run_cycle_chain_stages`. Rename `test_cycle_download_success_without_raw_manifest_is_not_resubmitted` to drop `raw_manifest`; keep its assertions (first submission `convert`, no `download_retry_1`). Remove the forwarder rows from `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md`. Post-grep over `services/` and `tests/`: zero hits (`docs/review-loop-log.jsonl` history stays). Run `tests/test_entropy_audit_facade_guard_forwarders.py tests/test_entropy_audit_facade_guard_aliases.py`.

## 2. #2393 — per-stage `context.retry_attempt` scope (design D1)

- [ ] 2.1 Red first (DB-legacy lane, `orchestrate_cycle`, the issue's two-round recipe: `RetryService(store, RetryConfig(max_retries=3, backoff_schedule=[0]))`, `FakeCycleSlurmClient` forecast failing x3 then succeeding, parse failing once, `StoreBackedCycleRepository`, `_marker_claim_basins(decision="retry_missing_forecast_output", claim=None)`). Round 2: parse targets its own next free attempt (`..._parse_retry_2` or whatever its own rows derive), really submits, no `skipped_duplicate_submission`, `result.status == "complete"`.
- [ ] 2.2 FileJournal lane (`supports_accepted_submit_reconcile=True`) same shape: upstream forecast reserves N+1; the downstream reservation's `submission_attempt` and job id derive from the downstream stage's own rows, not N+1.
- [ ] 2.3 Confirmed budget re-entry (#2393 comment): `retry_strict_warm_start_terminal_init_state_mismatch` re-entry runs forecast, then `state_save_qc` takes its own attempt and really submits.
- [ ] 2.4 Must-preserve: #1201 E1(a)/(b), `test_manual_retry_evidence_only_fresh_marker_keeps_precise_attempt_identity`, accepted-submit ambiguity release, stacked-suffix (#2254) downstream derivation stay green; within one stage the manifest/placeholder `submission_attempt` still equals the reservation attempt.
- [ ] 2.5 Sibling pin: markerless non-whitelisted decision with a downstream terminal failed row → downstream resumes (the `retry_attempt is None` gate is no longer opened by upstream residue).
- [ ] 2.6 Implement D1.

## 3. #2416 — manifest is the single source of the cohort restart stage (design D3)

- [ ] 3.1 Red first: fresh full-chain candidate with residual `state_evidence.restart_stage="forecast"` → `_candidate_basin_manifest` → `_restart_stage_from_basins([manifest]) is None` (issue table A reversed).
- [ ] 3.2 Cohort red: `(0,"full")` cohort of that manifest + two markerless members through `orchestrate_cycle`: first submission is `convert`; convert/forcing not skipped (table B reversed).
- [ ] 3.3 Must-preserve: an ordinary (non-fresh) candidate whose evidence carries only `restart_from_stage` (or only `restart_stage`) gets the same top-level `restart_stage` and the chain starts at that stage.
- [ ] 3.4a Test-fixture census: every test in `tests/` that hand-builds basins with only `state_evidence.restart_stage`/`restart_from_stage` and calls `orchestrate_cycle`/`_restart_stage_from_basins`/`_candidate_scoped_cycle_execution`; add the top-level key as a recorded prerequisite (assertions unchanged) or state why unaffected.
- [ ] 3.4 Census of `restart_stage` / `restart_from_stage` emitters (grep `restart_from_stage` in `services/`): list each emitter, which keys it writes, and its post-fix destination; record in this file under 3.4 and in the PR body. No emitter loses its restart.
- [ ] 3.5 `_candidate_scoped_cycle_execution` (second consumer): explicit single-basin assertions for (a) fresh full-chain manifest with residual marker, no `orchestration_run_id` and (b) ordinary restart manifest; plus (c) any basin with `orchestration_run_id` is unchanged. Record whether each flips. Confirm `chain_forced_resubmit` / `_active_orchestration_conflicts` evidence reads are unaffected (design D3).
- [ ] 3.6 Comment on `_restart_stage_from_basins` stating the `min` semantics (earliest claim among marker-carrying members; markerless members do not force stage 0) and why.
- [ ] 3.7 Implement D3.

## 4. #1845 — model-exact forcing stage match (design D2)

All 4.x tests: FileJournal lane (complete `cohort_members` + matched-bound fields as #2447 writes them), direct `orchestrate_cycle` without `orchestration_run_id`, multi-model cohort (unscoped path).

- [ ] 4.0 Record reachability in the PR: `scheduler_execution` always stamps `orchestration_run_id`, so production never takes the unscoped path (latent hardening).
- [ ] 4.1 Red first: a sibling model set's terminal succeeded forcing row under the shared run's base id → the chain really submits forcing for the current cohort, with a non-colliding id (`_retry_N`), not `skipped_duplicate_submission`, and does not resume the sibling row. If it does not go red on current code, stop and report the evidence.
- [ ] 4.1b Red first: a disjoint in-flight sibling forcing row → submit for the current cohort, the sibling row is not polled as ours.
- [ ] 4.2 Must-preserve: terminal forcing row whose members equal the current cohort → resume verbatim, no new submission.
- [ ] 4.3 Lane limitation: DB-legacy (`StoreBackedCycleRepository`) forcing row without complete member identity → today's model-blind resume is kept; test + comment pin it.
- [ ] 4.4 Overlapping unresolved sibling row (members intersect current models) still blocks, no fresh submission.
- [ ] 4.4b Accepted consequence pin: terminal succeeded row whose complete members overlap but are not equal → forcing resubmitted for the current cohort (design D2).
- [ ] 4.5 `test_candidate_scoped_full_cycle_ignores_sibling_cycle_jobs` and the unscoped resume tests (`test_crash_recovery_resumes_after_last_completed_stage`, `test_resume_array_status_override_*`) stay green unchanged.
- [ ] 4.6 Implement D2.

## 5. Verification

- [ ] 5.1 `uv run ruff check .`
- [ ] 5.2 Local: `uv run pytest -q tests/test_chain_cross_stage_state.py tests/test_orchestration_chain.py tests/test_production_scheduler.py`
- [ ] 5.3 Spec delta supersedes #1201 D2 "继承保留"; `openspec validate chain-cross-stage-state-residue --strict --no-interactive`
- [ ] 5.4 node-27 (oracle): `tests/test_chain_cross_stage_state.py tests/test_orchestration_chain.py tests/test_production_scheduler.py tests/test_warm_start_chaining.py tests/test_operator_reentry_confirmation.py` green at the PR head.

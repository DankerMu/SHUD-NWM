## Risk Packs

- [x] Concurrency / shared state / ordering — **selected**: the fixes depend on the order of the terminal-skip dispatch (which leg fires first, witness before or after upgrade, repair policy after witness). Covered by 1.x/2.x/3.x through the real `build_candidates` dispatch.
- [x] Legacy compatibility — **selected**: the None-lane `retry_terminal_run_manifest_missing` with its own forcing, the r2-01 confirmed refusal, and ordinary (non-quarantine) missing-forcing repair must stay unchanged. Covered by 1.3, 2.3, 2.4, 4.3.
- [x] Error handling / partial outputs — **selected**: a doomed forecast (#2396) or an uncounted rerun (#2408) must not submit, and the resulting blocker must be the drainable stable missing-forcing blocker. Covered by 1.1, 2.2.
- [x] Public API / CLI / script entry — not selected: no CLI, route, or payload change.
- [x] Config / project setup — not selected: no new env or config; existing `repair_missing_forcing*` knobs only.
- [x] Resource limits — not selected: no new submission loops; the change only removes submissions.
- [x] File IO / path safety — not selected: the witness reads are existing ones.
- [x] Schema / columns / field names — not selected: the new rejection reason string lives in in-memory decision evidence only, with no schema or manifest key.
- [x] Auth / permissions — not selected.
- [x] Release / packaging — not selected.
- [x] Documentation / migration notes — not selected: behaviour is recorded in the spec delta. The drain path (forcing backfill) is unchanged and already documented.

## 0. Setup

- [x] 0.1 Branch `feat/issue-2407-2408-2396-seal-quarantine-forecast-restart-guards` from `origin/master`. One implementer pass. Locate code by symbol, not by the issues' line numbers (all stale).
- [x] 0.2 New tests go in `tests/test_quarantine_forecast_restart_guards.py` (≤1000 lines; the large-file guard applies). Reuse helpers from `tests/test_scheduler_generation.py` / `tests/test_production_scheduler.py` by import and do not copy them. Ensure `scripts/select_ci_tests.py` routes the new file from a `scheduler_candidates.py` change (add a rule if name derivation does not reach it).

## 1. #2396 — None-lane `terminal_run_manifest_missing` consults the forcing witness

- [x] 1.1 Red first: None lane (db-free D8.9 compat: `NHMS_REQUIRE_FORECAST_WARM_START=false` with a journal-completed pipeline, i.e. a `_run_wiring_a_build_candidates` variant WITHOUT the seeded run manifest; the DB plane is also acceptable if a fixture exists) + terminal success + no `run_manifest_initial_state` + no forcing package for the candidate's own model_id. Seed with `seed_forcing_package=False` (add that passthrough kwarg to the `_run_wiring_a_build_candidates`-style helper, whose `_write_db_free_file_provider_fixtures` seeds the model's forcing sidecar by default). Assert the candidate is `blocked` with `missing_forcing_package_uri` or `forcing_version_row_absent`, `_decision_is_stable_missing_forcing_blocker` is true, and no forecast candidate/submission is emitted. Red on master (it emits `retry_terminal_run_manifest_missing`).
- [x] 1.2 Fix per design D1 and rewrite the post-upgrade consultation comment.
- [x] 1.3 Same fixture with the model's own forcing witnessed: the decision stays `retry_terminal_run_manifest_missing` with `restart_stage: "forecast"` and the evidence carries `forcing_provenance` (default `seed_forcing_package=True`).
- [x] 1.4 Each test asserts that `strict_warm_start` is `None` for that candidate (no strict warm-start block in its evidence).

## 2. #2408 — unconfirmed quarantine blocker is refused by the repair policy (decision (b))

- [x] 2.0 Strict-lane quarantine fixture (shared by 2.x and 3.x). The `_run_wiring_a_build_candidates` skeleton's `hydro_status="complete"` classifies as `terminal_hydro_success`, and the first strict branch captures that reason on the strict lane. To reach `terminal_completed_cycle` instead, use a hydro status outside `DURABLE_HYDRO_SUCCESS_STATUSES` plus a `forecast_cycle` with status `complete` and copyback evidence without `copyback_source_uri` (see `_completed_forecast_cycle_quarantine_state`, `tests/test_production_scheduler.py` ~L11405). The stale token must come from a source the accessor accepts, because the file journal ignores a non-completed hydro_run row. Use either an accepted-submit job row (the accessor's second source) or `repository_factory` with an injected accessor such as `_JournalIdentityRawCandidateStateRepository`, keeping the db-free strict env and the state-index fixture.
- [x] 2.1 Red first, reachability: strict db-free lane (`NHMS_SCHEDULER_DB_FREE_REQUIRED=true`, `NHMS_REQUIRE_FORECAST_WARM_START` not false) + `terminal_completed_cycle` quarantine (stale journal token) + the model's own forcing ABSENT + `repair_missing_forcing=True` for exactly this cycle + `require_direct_grid` with a valid direct-grid candidate + warm state and raw manifest ready. Concrete repair inputs:
  - config `nfs_raw_manifest_root` points at a ready on-disk manifest for (source, cycle);
  - the raw state has an `nfs_raw_manifest` gate with `status=ready`, `required=True`, `source=NFS_RAW_MANIFEST_READY_SOURCE`;
  - the strict `candidate_state` carries `state_id`, `uri`, `checksum`, `valid_time` equal to the candidate cycle, plus warm lineage, as `_verified_repair_warm_state` / `_verified_repair_raw_manifest` require.

  Pattern: `tests/test_production_scheduler.py` ~L14879 / ~L15271. On master, assert through real `build_candidates` that the decision is `retry_repair_missing_forcing` (proves the combination is reachable) and that the candidate carries no `operator_reentry_confirmation`. If some precondition makes it unreachable, stop and report which one, with evidence.
- [x] 2.2 Fix per design D2. Green: same fixture → `blocked`, repair rejection reason `journal_predecessor_quarantine_present`, the blocker still satisfies `_decision_is_stable_missing_forcing_blocker`, no candidate is submitted, and the evidence still carries `journal_predecessor_identity`.
- [x] 2.3 Must-preserve: an ordinary (non-quarantine) missing-forcing blocker with the same repair preconditions still reclassifies to `retry_repair_missing_forcing`. A confirmed candidate is still refused with `operator_reentry_confirmation_present` (r2-01 first).
- [x] 2.4 Unit-level: each predicate alone (the `journal_predecessor_identity` block; `artifact_guard.planned_retry_decision`) triggers the refusal; neither present → no refusal.

## 3. #2407 — strict-lane quarantine literal survives the upgrade helper (test only)

- [x] 3.1 Strict db-free lane + `terminal_completed_cycle` quarantine + own forcing witnessed + run manifest ABSENT or MISMATCHED against the strict evidence (design D3) + strict evidence ready. Through real `build_candidates`, assert `state_evidence["decision"] == "retry_journal_predecessor_identity_mismatch"`, `journal_predecessor_identity.quarantined_skip_reason == "terminal_completed_cycle"`, and a strict warm-start block present in the candidate evidence (lane proof).
- [x] 3.2 Temporarily delete the `native_shud_resubmitted`/`restart_stage` early return in `_upgrade_retry_for_strict_warm_start_manifest`: 3.1 goes red (the decision is rewritten to `retry_strict_warm_start_retry_run_manifest_mismatch` or its blocked form). Restore it: green. Record both outputs in the report. No runtime change remains for #2407. If 3.1 does not reach the quarantine (for example a plain skip with no judgement), stop and report which precondition failed, with evidence, rather than calling it unreachable.

## 4. Verification

- [x] 4.1 `uv run ruff check .`
- [x] 4.2 Local: `uv run pytest -q tests/test_quarantine_forecast_restart_guards.py tests/test_operator_reentry_confirmation.py tests/test_forced_resubmit_veto.py` and `tests/test_scheduler_generation.py tests/test_production_scheduler.py -k "quarantine or forcing or repair or manifest or reentry"`.
- [ ] 4.3 node-27 (oracle): `tests/test_production_scheduler.py tests/test_scheduler_generation.py tests/test_operator_reentry_confirmation.py tests/test_warm_start_chaining.py tests/test_forced_resubmit_veto.py tests/test_quarantine_forecast_restart_guards.py` all green at the PR head.
- [x] 4.4 `openspec validate seal-quarantine-forecast-restart-guards --strict --no-interactive`

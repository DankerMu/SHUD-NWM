## Risk Packs

- [x] Public API / CLI / script entry — **selected**: `list-operator-actions` exit codes and receipt keys; `confirm-operator-reentry` refusals. Covered by 2.x, 3.x, 4.x, 6.x.
- [x] File IO / path safety — **selected**: reservation mtime heartbeat touch; reader stays root-local. Covered by 3.3, 3.5.
- [x] Schema / columns / field names — **selected**: new optional evidence keys (`evidence_compaction`, `limit.source_cycles`, `backfill.mode`, reservation `lease`). Covered by 1.x, 2.x, 3.x.
- [x] Legacy compatibility — **selected**: older pass files (no marker, no `mode`, no `lease`), 188/188 live backfill shape, within-limit byte-identical output, legal reentry receipts. Covered by 1.3, 2.4, 3.4, 4.3, 6.3.
- [x] Error handling / rollback / partial outputs — **selected**: fallback still fail-closed; heartbeat touch failure non-fatal; orphan after crash. Covered by 1.2, 3.1, 3.3.
- [x] Concurrency / shared state / ordering — **selected**: heartbeat thread touching the reservation; reader ordering by `reserved_at`. Covered by 3.2, 3.3.
- [x] Documentation / migration notes — **selected**: runbook exit-code table (one edit), env provenance, compute.example. Covered by 5.x, 7.x.
- [x] Config / project setup — **selected** (ops only): node-22 `compute.env` alignment. Covered by 7.3.
- [x] Auth / permissions — not selected: no new secret; dbfree env read via sed in the runbook only.
- [x] Resource limits — **selected**: every tier keeps the byte bound; projection capped. Covered by 1.1, 2.1.
- [x] Release / packaging — not selected.

## 0. Setup

- [ ] 0.1 Branch `feat/issue-1905-2402-2443-2442-2405-2399-2426-operator-action-decidability` from `origin/master`. Locate by symbol.
- [x] 0.2 New behaviour tests in `tests/test_scheduler_evidence_decidability.py` (≤1000 lines; `tests.test_production_scheduler` imports inside functions only — `tests/test_select_ci_tests.py` freezes that importer count). `scripts/select_ci_tests.py` routes the new files from every touched source (add rule + test if needed).
  - Pass 1 evidence: new file `tests/test_scheduler_evidence_decidability.py` (727 lines, every `services.*`/`tests.*` import function-local; `tests/test_select_ci_tests.py::test_changed_suite_selects_its_direct_non_gated_module_scope_importers` and the six-importer anti-vacuity pin stay green). Routing added at two sites in `scripts/select_ci_tests.py` (the `services/orchestrator/**` directory rule; the `scheduler_runtime.py` stop rule) plus the new routing pin `test_evidence_decidability_suite_is_selected_by_every_writer_and_reader_it_pins`; three frozen selection lists in `tests/test_select_ci_tests.py` updated (file-journal read-state set, broad-orchestrator list, scheduler_runtime mutex list). Red before the rules: `assert 'tests/test_scheduler_evidence_decidability.py' in select_tests([...])` -> AssertionError for all six paths; green after: `1 passed`.
- [x] 0.3 Every new-behaviour test is shown red on pre-change source, then green; outputs recorded here.
  - Pass 1 red (pre-change source, `uv run pytest -q -p no:cacheprovider tests/test_scheduler_evidence_decidability.py`): `17 failed, 2 passed` — 8x `KeyError: 'evidence_compaction'`, 5x `KeyError: 'source_cycles'` (the `limit` marker), `ImportError: cannot import name '_BOUNDED_SOURCE_CYCLE_PROJECTION_LIMIT'`, `assert 'resource_limit_blocked' == 'planned'`, `assert 'resource_limit_blocked' == 'submitted'`, `assert ['size_fallback_source_cycles_absent'] == ['size_fallback_source_cycles_summarized']`. Green after D1+D2: `19 passed in 11.44s`. (The 2 that passed red are the regression pins 1.2/1.3: the fail-closed fallback and the within-limit shape.)

## 1. #1905 — non_blocking_summary tier (design D1)

- [x] 1.1 Red first: verbose candidate/model-run detail over a small `max_evidence_bytes` that fits after summary → on-disk status equals the computed status (e.g. `submitted`), `evidence_compaction.mode == "non_blocking_summary"`, verbose detail absent, `len <= max_evidence_bytes`; in-memory dict, on-disk JSON, `SchedulerPassResult.status` and CLI agree.
  - `test_non_blocking_summary_keeps_the_true_status_in_result_artifact_and_cli`: a real `run_once()` pass re-run under a bound computed from the pre-existing bounded-summary helper keeps `status == 'planned'` in `SchedulerPassResult`, `result.evidence`, the on-disk artifact and `cli._plan_production`; `evidence_compaction` exact-shape; verbose `state_evidence` gone; `source_cycles` verbatim; `len(bytes) <= max_evidence_bytes`. Red: `assert 'resource_limit_blocked' == 'planned'`.
- [x] 1.2 Still over after summary → existing bounded fallback (`resource_limit_blocked`, `limit.reason`, `limit.pre_limit_status`); `SchedulerEvidenceWriteError` hard bound unchanged (`test_write_evidence_persists_candidate_summaries_and_pre_limit_status`, `test_evidence_size_fallback_status_agrees_across_result_artifact_and_cli` stay green, retargeted only if their fixture now fits the new tier — record).
  - `test_a_summary_that_still_exceeds_the_bound_falls_back_fail_closed`: incident payload at 2_600 bytes -> `resource_limit_blocked` + `limit.reason` + `limit.pre_limit_status`; at 1_100 the hard `SchedulerEvidenceWriteError` is unchanged. `test_write_evidence_persists_candidate_summaries_and_pre_limit_status` and `test_evidence_size_fallback_status_agrees_across_result_artifact_and_cli` stayed green at their existing fixtures (no retarget needed).
- [x] 1.3 Within limit → byte-identical to today (no `limit`, no `evidence_compaction`).
  - `test_within_limit_evidence_carries_no_compaction_marker` (plus the pre-existing byte-identity pin `test_within_limit_evidence_keeps_full_candidate_detail_without_limit_observability`, green).
- [x] 1.4 Equivalence table (the coupling), rows WRITER-produced (`candidate.to_dict()` / `run_once()`), one per listed decision class, including 0/False retry values, plus one breaker-released source cycle: the full pass and the same pass forced into the tier give identical `list_operator_actions` output (`operator_actions`, `non_evaluating_passes`, exit code).
  - `test_a_summary_tier_pass_lists_exactly_what_the_full_pass_lists`, 7 legs: the five listed decision classes (rows from `_permanent_failure_evidence`, `_cancelled_state_evidence`, `_strict_warm_start_terminal_blocked_evidence(attempt=0, retry_limit=0)`, `_journal_predecessor_identity_blocked_evidence(occurrences=0)`, `_refuse_confirmed_candidates_off_forecast`, each through `SchedulerCandidate.to_dict()`), the breaker-released `source_cycles` entry of a REAL `run_once()` breaker pass, and the no-action leg. Full vs tier: identical `operator_actions`, identical `non_evaluating_passes` (`[]` on both), identical exit (1/1/1/1/1/1/0). Red: the tier write produced `resource_limit_blocked` + `size_fallback...` entries, and the released leg lost its action (exit 3 vs 1).
- [x] 1.4b Readiness: a summary-tier `submitted` pass still passes `readiness_scheduler_evidence` (model_run_evidence verbatim). `evidence_compaction` nests the admission record when both tiers ran.
  - `test_a_summary_tier_submitted_pass_still_passes_the_readiness_reader` (`model_run_evidence` byte-verbatim; `readiness_scheduler_evidence._scheduler_evidence_errors` equal to the full payload's, `_scheduler_readiness_status == 'passed'`) and `test_the_summary_tier_nests_the_admission_record_it_ran_after` (`evidence_compaction.admission == {status: applied, reason: pre_write_size_pressure, terminal_skipped_candidates_compacted: 1}`).
- [x] 1.5 #1168 floor unchanged (candidate summary rows, restart_reconcile compact, #1797 lanes). Update the `scheduler_runtime` comment.
  - `_serialized_evidence_within_limit` hands the ORIGINAL payload to `bounded_evidence_payload` as before; the #1168/#1797 floor pins in `tests/test_production_scheduler.py` are green (`2083 passed`). `scheduler_runtime.py` comment above `_finalize_timing_into_evidence` updated to name the summary tier as the first answer to an oversized payload. Two byte-band retargets were needed there (see deviations).

## 2. #2402 — source_cycles projection and marker (design D2)

- [x] 2.1 Red first: payload with breaker-released not-selected `source_cycles` over the bound → fallback keeps one compact row per such cycle with counts; `limit.source_cycles.status == "summarized"`; non-operator entries not retained; product within bound; projection capped.
  - `test_the_bounded_fallback_keeps_a_capped_marked_breaker_released_projection`: 70 breaker-released writer rows (`scheduler_discovery._source_cycle_evidence`) plus a selected cycle and the `backfill_audit` entry -> 64 rows kept (`_BOUNDED_SOURCE_CYCLE_PROJECTION_LIMIT`), `limit.source_cycles == {status: summarized, breaker_released_total: 70, retained: 64}`, non-operator entries absent, exact row shape in the reader's key spellings, product within the bound. Red: `ImportError` on the new constant.
- [x] 2.2 Both fit-tier loops (empty-assign and pop) clearing/removing a non-empty `source_cycles` → `dropped`, never downgraded; only the terminal `_compact_limit` floor may drop the marker (existing floor exception).
  - `test_a_fit_tier_that_clears_a_non_empty_source_cycle_list_marks_it_dropped` (empty-assign tier at 2_600 bytes -> `dropped`; re-summarizing that product at 60_000 keeps `dropped`) and `test_the_terminal_limit_floor_may_drop_the_source_cycle_marker` (`_fit_bounded_evidence_payload` at 600 -> `limit == {'reason': ...}`). The pop loop is marked symmetrically but is unreachable with a non-empty list through the real ladder (the empty-assign tier always runs first), so it is covered by construction, not by a test — recorded as a known limit.
- [x] 2.2b Writer-produced projection (reader key spellings `selection_status`, `cycle_time_utc`, `journal_predecessor_identity_quarantine.models[].{model_id, occurrences, recorded_init_state_id}`) run through `list_operator_actions`.
  - `test_a_summarized_size_fallback_lists_its_breaker_released_models`: the projection of a REAL `run_once()` breaker pass read back through `list_operator_actions` -> same `operator_actions` as the full pass (`model_a`, `recorded_init_state_id` live token), exit 1, reason `size_fallback_source_cycles_summarized`. Also `tests/test_operator_action_listing.py::test_a_size_fallback_pass_lists_the_breaker_release_its_projection_kept`.
- [x] 2.3 Reader: `summarized` → breaker-released models listed (exit 1), pass non-evaluating with reason `size_fallback_source_cycles_summarized`; with no action and newer than the newest evaluating pass → exit 3; `dropped` → exit 3 as today.
  - Reader: `operator_action_listing.SIZE_FALLBACK_SUMMARIZED_NON_EVALUATING_REASON`. Listed-with-action leg in 2.2b (exit 1); no-action leg in `test_a_summarized_size_fallback_without_an_action_is_undecidable` (exit 3); `dropped` leg in `test_a_dropped_or_absent_source_cycle_marker_reads_as_dropped[dropped]` (exit 3, reason `..._absent`). `LIST_OPERATOR_ACTIONS_HELP` names the new reason (`test_the_help_text_names_every_decision_and_every_non_evaluating_reason` extended).
- [x] 2.4 Legacy: a size-fallback product without `limit.source_cycles` → read as `dropped` (exit 3, reason `size_fallback_source_cycles_absent`).
  - `test_a_dropped_or_absent_source_cycle_marker_reads_as_dropped[absent]`: the marker removed from a real fallback product -> exit 3, reason `size_fallback_source_cycles_absent`.

### Pass 1 (#1905 + #2402) deviations

- `tests/test_operator_action_listing.py` (2_595 lines) and no other >1000-line file outside the guard list had to be edited (the #2402 reason literal and the size-fallback fixtures), so it is added to `.large-file-guard.json` `exclude`; splitting it is out of scope.
- Three `tests/test_production_scheduler.py` byte-band pins moved because `limit.source_cycles` adds ~85 bytes to the fallback's limit block: the `_bounded_evidence_payload` shim pin gains the marker in its exact-dict expectation; `test_bounded_evidence_drops_candidate_lists_before_restart_reconcile` re-measured 5_230-5_680/5_400 -> 5_430-5_960/5_700; `test_scheduler_evidence_context_accepts_exported_keyword_callbacks` 1_500 -> 1_700 (at 1_500 that payload now reaches the terminal `_compact_limit` floor instead of the observability floor it is about). No asserted shape was weakened.
- `tests/test_operator_action_listing.py` size-fallback fixtures that used a breaker-released cycle as "the thing the fallback hid" now use a deferred not-selected cycle (`_dropped_source_cycle`), because #2402 makes the breaker-released leg survive; the breaker-released leg gained its own test (it is listed, exit 1).
- Spec conflict for the parent to resolve (not edited here): the MODIFIED scenario "A window of size-fallback passes is undecidable" still says a size-fallback window whose ORIGINAL payload had a breaker-released cycle and no other listed decision exits 3, while D2 + the ADDED scenario "Summarized source cycles list a breaker release" make it exit 1. The implementation follows D2/2.3.
- The pop-loop branch of the `dropped` marking is unreachable with a non-empty `source_cycles` through the real ladder (the empty-assign tier always clears the list first); it is implemented symmetrically and recorded, not tested.
- The new tier re-checks the bound with the INDENTED serialization only, mirroring the adjacent admission tier; the compact serialization stays a fallback-only last resort.

## 3. #2405 — reservation lease (design D4)

- [ ] 3.1 Red first: old decidable pass (no action) + newer orphan reservation (stale lease, no terminal file) → exit 3, listed under `orphan_reservations`.
- [ ] 3.2 Newer in-flight reservation (fresh mtime within 2×ttl) → behaviour identical to today.
- [ ] 3.3a Heartbeat registration is opt-in and happens only after the reservation is `reserved`; other `_LeaseHeartbeat` users unchanged.
- [ ] 3.3 Writer: reservation payload carries `lease`; the heartbeat refreshes its mtime; a touch failure (e.g. file removed) neither raises nor marks the lease lost.
- [ ] 3.4 Reservation with its terminal file → nothing extra; legacy reservation without `lease`, newer than newest evaluating pass, no terminal file → orphan (fail-safe). Ordering compares `reserved_at` with the pass payload's `started_at`, not mtimes (test with a pass whose mtime and `started_at` disagree, and a freshly touched but older reservation).
- [ ] 3.5 Reader stays root-local and importer-free (existing guard tests green).

## 4. #2443 — executed backfill leg (design D3, write side)

- [ ] 4.1 Red first: `run_once()` evidence carries `backfill.mode` (`legacy` for `backfill_enabled=True` + 0 models; `backfill` with models; `legacy` when disabled) — red today because `mode` is absent. The zero-model listing stays exit 3 `no_models_evaluated` (already true; pin it).
- [ ] 4.2 `backfill_enabled=True` with models → listing behaviour unchanged (exit 0 when nothing waits).
- [ ] 4.3 Reader guard: `enabled` true + `mode` legacy + `selected_model_count > 0` → `scope_unknown`; pass without `mode` read as today; `test_run_once_backfill_disabled_evidence` green (add the key if asserted by equality; record).
- [ ] 4.4 `test_backfill_enabled_with_empty_models_falls_back_to_legacy` green; `backfill_leg` entry appended once per `discover_cycles` call on both legs, not counted as progress, and ignored by `_pass_actions` / the D2 projection (it appears in `source_cycles` like `backfill_audit`).

## 5. #2442 — status closure pin (design D5)

- [ ] 5.1 New `tests/test_operator_action_status_closure.py`: AST pin over the writer files (incl. `scheduler_evidence_payload.py` `resource_limit_blocked` and the `_evidence_status(..., "<literal>")` fallback arguments), `unresolved == []` or enumerated with reasons; passthrough declared dynamic source; reconciliation with `EVALUATING_PASS_STATUSES`/`TRANSPARENT_PASS_STATUSES`.
- [ ] 5.2 Mutation check executed: add a status literal to a writer → red; restore → green. Record outputs.
- [ ] 5.3 Docstring records the asymmetry. Remove the 24-literal self-copy from `tests/test_operator_action_listing.py` (add that file and any other touched >1000-line file to `.large-file-guard.json` `exclude`; record).

## 6. #2426 — reentry refusals (design D7)

- [ ] 6.1 `cycle_time_malformed` → `cycle_time_invalid` leg in `test_refused_preconditions_exit_two_and_write_nothing` (exit 2, refused, zero journal bytes).
- [ ] 6.2 Remove `decision_not_reentry_eligible` branch and the `type(pin) is not int` half.
- [ ] 6.3 Legal decisions' dry-run and `--attest` receipts unchanged (existing tests green).

## 7. #2399 + runbook (design D6)

- [ ] 7.1 Runbook step 1: root from `compute.scheduler-dbfree.env` (explicit command) + receipt checks (`evidence_root`, `passes_scanned > 0`); same fix in `qhh-22-business-bringup.md` bare dereference.
- [ ] 7.2 One coherent exit-code table edit: new reason `size_fallback_source_cycles_summarized`, `orphan_reservations`; reentry refusal list without `decision_not_reentry_eligible`. Same update to `LIST_OPERATOR_ACTIONS_HELP`.
- [ ] 7.3 node-22 (orchestrator, ops): read-only checks (`systemctl --user show nhms-compute-scheduler.service -p EnvironmentFiles`; units/cron sourcing `compute.env`); if none live, align scheduler-root keys + header with dated backup (mode 600) and receipt; else oracle-blocked. Run `list-operator-actions` per the new runbook command on node-22 via `/scratch/frd_muziyao/NWM/.venv/bin/python` (no `uv sync`, no bare `uv run`) and record the receipt. `compute.example` `nhms-production` → `nhms-prod`.

## 8. Verification

- [ ] 8.1 `uv run ruff check .`
- [ ] 8.2 Local: `uv run pytest -q tests/test_scheduler_evidence_decidability.py tests/test_operator_action_status_closure.py tests/test_operator_action_listing.py tests/test_operator_reentry_confirmation.py tests/test_scheduler_backfill.py tests/test_select_ci_tests.py` and `tests/test_production_scheduler.py -k "evidence or backfill or pre_execution or reservation"`.
- [ ] 8.3 `openspec validate operator-action-decidability --strict --no-interactive`
- [ ] 8.4 node-27 (oracle): the 8.2 files + full `tests/test_production_scheduler.py tests/test_scheduler_generation.py` at PR head.

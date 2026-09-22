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
- [ ] 0.2 New behaviour tests in `tests/test_scheduler_evidence_decidability.py` (≤1000 lines; `tests.test_production_scheduler` imports inside functions only — `tests/test_select_ci_tests.py` freezes that importer count). `scripts/select_ci_tests.py` routes the new files from every touched source (add rule + test if needed).
- [ ] 0.3 Every new-behaviour test is shown red on pre-change source, then green; outputs recorded here.

## 1. #1905 — non_blocking_summary tier (design D1)

- [ ] 1.1 Red first: verbose candidate/model-run detail over a small `max_evidence_bytes` that fits after summary → on-disk status equals the computed status (e.g. `submitted`), `evidence_compaction.mode == "non_blocking_summary"`, verbose detail absent, `len <= max_evidence_bytes`; in-memory dict, on-disk JSON, `SchedulerPassResult.status` and CLI agree.
- [ ] 1.2 Still over after summary → existing bounded fallback (`resource_limit_blocked`, `limit.reason`, `limit.pre_limit_status`); `SchedulerEvidenceWriteError` hard bound unchanged (`test_write_evidence_persists_candidate_summaries_and_pre_limit_status`, `test_evidence_size_fallback_status_agrees_across_result_artifact_and_cli` stay green, retargeted only if their fixture now fits the new tier — record).
- [ ] 1.3 Within limit → byte-identical to today (no `limit`, no `evidence_compaction`).
- [ ] 1.4 Equivalence table (the coupling), rows WRITER-produced (`candidate.to_dict()` / `run_once()`), one per listed decision class, including 0/False retry values, plus one breaker-released source cycle: the full pass and the same pass forced into the tier give identical `list_operator_actions` output (`operator_actions`, `non_evaluating_passes`, exit code).
- [ ] 1.4b Readiness: a summary-tier `submitted` pass still passes `readiness_scheduler_evidence` (model_run_evidence verbatim). `evidence_compaction` nests the admission record when both tiers ran.
- [ ] 1.5 #1168 floor unchanged (candidate summary rows, restart_reconcile compact, #1797 lanes). Update the `scheduler_runtime` comment.

## 2. #2402 — source_cycles projection and marker (design D2)

- [ ] 2.1 Red first: payload with breaker-released not-selected `source_cycles` over the bound → fallback keeps one compact row per such cycle with counts; `limit.source_cycles.status == "summarized"`; non-operator entries not retained; product within bound; projection capped.
- [ ] 2.2 Both fit-tier loops (empty-assign and pop) clearing/removing a non-empty `source_cycles` → `dropped`, never downgraded; only the terminal `_compact_limit` floor may drop the marker (existing floor exception).
- [ ] 2.2b Writer-produced projection (reader key spellings `selection_status`, `cycle_time_utc`, `journal_predecessor_identity_quarantine.models[].{model_id, occurrences, recorded_init_state_id}`) run through `list_operator_actions`.
- [ ] 2.3 Reader: `summarized` → breaker-released models listed (exit 1), pass non-evaluating with reason `size_fallback_source_cycles_summarized`; with no action and newer than the newest evaluating pass → exit 3; `dropped` → exit 3 as today.
- [ ] 2.4 Legacy: a size-fallback product without `limit.source_cycles` → read as `dropped` (exit 3, reason `size_fallback_source_cycles_absent`).

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

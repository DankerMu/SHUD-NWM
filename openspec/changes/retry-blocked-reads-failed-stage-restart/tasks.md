# Tasks — retry-blocked-reads-failed-stage-restart (#2566, #2567, #2600)

## Risk packs

- Public API / CLI / script entry — **selected.** `POST /runs/{run_id}/retry` outcome for blocked provenance: 200 → 503 `RETRY_RUNTIME_ROOTS_UNRESOLVED`. Covered by 2.x.
- Config / project setup — **selected.** The environment root candidate is excluded under blocked reads. Covered by 2.x.
- File IO / path safety / overwrite — **selected.** Submission roots decide where Slurm writes. Covered by 2.x.
- Schema / columns / units / field names — **selected.** New evidence key `candidate_counts.blocked_reads`. Covered by 2.x.
- Auth / permissions / secrets — not selected.
- Concurrency / shared state / ordering — **selected (light).** Covers the second read in a different lock window, and the grouping of restart cohorts. Covered by 1.x and 3.x.
- Resource limits / large input — not selected.
- Legacy compatibility / examples — **selected.** DB-lane evidence, and manual retries with no blocked reads. Covered by 2.3 and 3.3.
- Error handling / rollback / partial outputs — **selected.** The pending retry row is already minted when the unresolved outcome is raised. Covered by 2.2.
- Release / packaging — not selected.
- Documentation / migration notes — **selected.** Runbook for the new 503 cause and the manual-retry restart point. Covered by 4.1.

## 0. PR description first (#2600)

- [x] 0.1 Open the PR with the #2600 restart-stage criterion from design.md in its description, before any implementation commit.

## 1. #2566 companion read

- [x] 1.1 The same-run by-run read in `_file_retry_runtime_root_candidates` detects the blocked marker. On a hit it:
  - logs a warning with the journal `reason`/`field`;
  - contributes no companion candidate;
  - adds one to `blocked_reads`.
- [x] 1.2 Tests. Reuse the `tests/test_file_journal_read_blocked_consumers.py` fixtures (`_refuse_scoped_job_records` / `_refuse_job_id_reads` / `_blocked_fault`). Cover:
  - A blocked companion read produces evidence that differs from a run that genuinely has no companion download job (`blocked_reads >= 1`).
  - A genuine companion-less run is unchanged (`blocked_reads == 0`). This is a pin.
  - The allocator unreachability claim is re-verified (pin or comment).

## 2. #2567 blocked-read accounting and environment exclusion

- [x] 2.1 Add `blocked_reads` to `_RuntimeRootCandidateBatch`. It is carried out of both existing degrade branches and 1.1 (tuple or result object), accumulated across the walk, and emitted in `candidate_counts` only when > 0.
- [x] 2.2 When `blocked_reads > 0`, treat runtime roots as required for any job type, and neither append nor read the environment candidate (including the `_file_manual_retry_array_tasks` ~856 env fallback):
  - resolve from recorded candidates only;
  - otherwise go through the existing governed `RETRY_RUNTIME_ROOTS_UNRESOLVED` path (`submission_failed` row with that error code, API 503 with `details.runtime_root_resolution.candidate_counts.blocked_reads`);
  - make no gateway submit.
- [x] 2.3 Tests:
  - (a) A blocked provenance read versus no submission event produces different persisted event details.
  - (b) Recorded candidates are cleared by the blocked read and the environment candidate is complete: no submission, `RETRY_RUNTIME_ROOTS_UNRESOLVED`, and the evidence carries `blocked_reads`. Cover a download job and a non-download, non-db-free job (`run_shud_forecast_array`). The same holds through the API route (503).
  - (b') The flipped test `tests/test_file_journal_read_blocked_consumers.py` ~779 (previously 200 + gateway requests) now asserts `RETRY_RUNTIME_ROOTS_UNRESOLVED`, `blocked_reads >= 1`, and zero gateway requests.
  - (c) A blocked read on one provenance id while a recorded candidate still resolves: the submission uses the recorded roots and the evidence has `blocked_reads >= 1`.
  - (c') A blocked read with `NHMS_SCHEDULER_DB_FREE_REQUIRED=true` and a recorded candidate with complete roots but no db-free selector gives `RETRY_RUNTIME_ROOTS_UNRESOLVED` and zero submissions.
  - (d) No blocked read and no event: the environment fallback is unchanged (pin).
  - (e) Existing DB-lane evidence assertions and the file-lane golden `_FILE_LANE_LOCAL_ROOT_EVENT_DETAILS` stay byte-identical (no `blocked_reads` key).

## 3. #2600 manual-retry restart stage

- [x] 3.1 `_manual_retry_state_evidence` sets `restart_stage` according to the criterion:
  - the `_failed_stage` axis;
  - an attributable own output, where `incomplete` never counts and the override is not trusted;
  - permanence is ignored;
  - the forecast restart only with a witness.

  Wire the emitted stage through the existing restart-stage guards. On a guard failure, drop it and run the full chain (never blocked).
- [x] 3.2 Tests through the real scheduler decision and `orchestrate_cycle` in the file journal, using the `tests/test_cohort_membership_attribution.py` harness. Assert on the basin manifest's top-level `restart_stage` and on the stage sequence the fake gateway submitted:
  - A single-model `state_save_qc`-only failure plus a marker restarts at `state_save_qc`: no convert/forcing/forecast submission, and the attempt accounting is unchanged.
  - A multi-member cohort with the marker on its model-less cohort master (the incident shape) forms one `state_save_qc` restart cohort, with no forecast resubmission.
  - A `forecast` failure plus a marker restarts at `forecast` when the forcing witness is found. When the witness is missing it runs the full chain and is not blocked.
  - A downstream failure whose only forecast success row is `incomplete` membership runs the full chain (pin).
  - A `permanently_failed` `state_save_qc` plus a marker restarts at `state_save_qc`.
  - Strict warm-start lane (`NHMS_REQUIRE_FORECAST_WARM_START`) with a manual restart stage added and no witness: the manual retry falls back to the full chain, not blocked. Cold-start strict behavior is unchanged (pin).
  - A downstream failure without its own durable output runs the full chain (pin).
  - A `convert`/`forcing` failure is unchanged (pin).
  - A cold-start quarantined failure is unchanged (pin).
- [x] 3.3 The existing manual-retry tests stay green, or any change to them is justified by the spec. They are in `tests/test_production_scheduler.py`, `tests/test_cohort_membership_attribution.py`, `tests/test_scheduler_terminal_recency.py` and `tests/test_operator_reentry_confirmation.py`.

## 4. Docs

- [x] 4.1 Runbook: add the new `RETRY_RUNTIME_ROOTS_UNRESOLVED` cause (blocked provenance) to the manual-retry / typed-reasons runbook, and describe the manual-retry restart point (the failed stage when its own output is durable).

## Evidence Floor

- **Local:**
  - Behavior-changing tests are red on origin/master source and green afterwards. Pins are labelled.
  - `uv run ruff check .` passes.
  - `openspec validate retry-blocked-reads-failed-stage-restart --strict --no-interactive` passes.
  - `uv run pytest -q tests/test_file_journal_read_blocked_consumers.py tests/test_retry.py tests/test_production_scheduler.py tests/test_orchestration_chain.py tests/test_node22_manual_retry_failed_runs.py tests/test_select_ci_tests.py tests/test_cohort_membership_attribution.py tests/test_scheduler_terminal_recency.py tests/test_operator_reentry_confirmation.py tests/test_forecast_cohort_projection_restart_stage.py tests/test_file_orchestration_journal.py tests/test_api_contract_pipeline_ops.py` passes, plus any other test file that imports a changed module (list them).
- **node-27** (`TMPDIR=/home/nwm/tmp`): the same suites.

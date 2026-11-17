# retire-manual-retry-submit-lanes (#3157, PR 1 of 2)

Closes #3157 together with PR 2 (dead-helper removal). The owner chose **(b)** on 2026-11-16.

## Why
`RetryService.attempt_manual_retry` (DB lane, `services/orchestrator/retry.py:556-701`) and
`FileJournalRetryService.attempt_manual_retry` (file lane, `services/orchestrator/file_orchestration_journal.py:13054-13122`)
both commit a `pending` manual-retry row before they submit to Slurm. A crash between the two steps leaves a row that
blocks every later retry of the run. The only expiry path, `expire_stale_retries`, has no production caller, so the
living MUST "Stale pending retry is detected" (`retry-execution-contract`) has no implementation.

Both lanes are unreachable in the current production topology:
- node-27 is `display_readonly` and answers the route with 409 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED`;
- node-22's `compute_control` has no `DATABASE_URL`, so `get_pipeline_store` raises 500 `DATABASE_URL_MISSING`;
- the file lane has no production caller (test `dependency_overrides` only).

The supported operator recovery is the marker lane, `FileJournalRetryService.record_manual_repair`. It is driven by
`scripts/node22_manual_retry_failed_runs.py` and minted by the scheduler. It never writes a `pending` row in-process,
so it has no such window.

## What
1. **Route fails closed.** `POST /api/v1/runs/{run_id}/retry` keeps its path, operation and dependency order:
   - 400 `INVALID_RUN_ID`;
   - RBAC 401/403 (`pipeline.retry_run`);
   - the existing display_readonly 409.

   It then **always** raises 409 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED`, the code the operation already declares in
   OpenAPI, from a dependency that runs **before** anything constructs a DB session, `RetryService` or gateway. The
   `details` carry `run_id`, a `suggested_action` naming `scripts/node22_manual_retry_failed_runs.py` / the
   `record_manual_repair` marker, and `recovery_runbook=node22-control-plane-manual-recovery`. As a result,
   compute_control without `DATABASE_URL` answers 409, not 500.

   The display suggestion text for `retry` (`_manual_action_suggestion`) stops pointing at "the compute_control API"
   and points at the runbook and script.
2. **Delete both `attempt_manual_retry` methods**, the `ManualRetryService` protocol, and the route's success and 503
   branches (`_retry_execution_status`, the `submission_failed` handling, `get_retry_execution_context`'s
   service/gateway wiring).

   The helpers they leave dead are removed in PR 2. PR 1 keeps them so that this diff is the contract change only.
   Imports that become unused (for example `runtime_checkable`, `PolicyDecision`, `require_policy_evidence` and
   `trusted_internal_policy_decision` in `retry.py`, and `RetryService`, `RetryConfig` and the `Retry*Error` imports in
   `pipeline.py`) are removed in PR 1, and E1 catches any that are missed. **Surviving code is not touched:**
   - the marker lane (`record_manual_repair`, `_manual_retry_source_for_run`, `RetryConflictError`,
     `RetryNotFoundError`, `RetryEvidenceInvalidError`);
   - auto retry (`schedule_auto_retry` and the scheduler auto/permanent paths);
   - the cancel route;
   - `MANUAL_RETRY_SOURCE_STATUSES` and `MANUAL_RETRY_DURABLE_SUCCESS_STATUSES`.
3. **OpenAPI and the frontend are unchanged.** `openapi/nhms.v1.yaml`, `apps/frontend/src/api/types.ts` and
   `check:api-types` stay byte-identical. The operation still declares its 200 schema, now unreachable; removing the
   operation, the schema and the `JobsTable` retry button is follow-up #3179. The button now
   shows the 409 error toast in every mode; it already did on display.
4. **Tests.** Classify every test that reaches either `attempt_manual_retry` or the route's success path:
   - **delete** a test whose subject is the retired submit lane or the route's success/503 behaviour;
   - **reseed** a test that only uses `attempt_manual_retry` to set up a surviving subject (cancel consistency, cohort
     charge, read-blocked consumers, marker lane, auto retry) by writing the rows directly;
   - the response-model completeness oracle (`tests/test_response_model_preservation.py:757-770` with its sample in
     `tests/test_response_model_preservation_pipeline.py:103-116,179`) only captures successful serialisation. The retry
     sample is deleted, and the route gets a documented exemption: a `FAIL_CLOSED_ROUTES` entry checked in
     `_in_scope_route_objects`, with the in-scope count adjusted. `response_model` stays so that OpenAPI is unchanged;
   - **trim** a test that exercises both the marker lane and the retired method
     (`tests/test_file_orchestration_journal.py:4176,4216`): remove the retired arm and keep the marker arm. The
     retry-execution-contract ADDED scenario pins the file-journal refusal arm;
   - add **pins**: the route returns the stable 409 for compute_control with and without `DATABASE_URL`; RBAC 401/403
     and the display 409 still come first; no `pipeline_job` / event / journal / hydro write and no `submit_job`
     happens; both `attempt_manual_retry` attributes are absent.

   The per-test table is recorded in the PR.
5. **Selector and governance:**
   - the six pinned `tests/test_retry.py::test_retry_api_file_lane_*` node ids
     (`scripts/select_ci_tests_tables.py:1434-1463`) and the rule entries naming deleted tests
     (`scripts/select_ci_tests_rules.py:2090,2134,5522-5536`) follow the deletions;
   - `tests/fixtures/file_journal_cluster_map.json` and `tests/status_literal_allowlist.json` are updated if their
     subjects change.
6. **Docs.** Operator instructions that expect a successful API retry point at the marker lane instead:
   - `docs/runbooks/qhh-controlled-failure-retry-evidence.md:36-41`;
   - `docs/runbooks/qhh-continuous.md:401`;
   - `docs/runbooks/qhh-mvp-production-like-e2e-checklist.md:909`;
   - `docs/runbooks/two-node-production-e2e-plan.md:779,786` (node-22 "retry produces a new job id");
   - `docs/spec/04_api_design.md:162`.

   The display-side 409 refusal probes stay as they are, because they remain true and are protected under Must
   preserve: `infra/README.two-node-docker.md:659,699,707-711` and `two-node-production-e2e-plan.md:784`.

   `docs/runbooks/node22-control-plane-manual-recovery.md` gains the acceptance-criteria section: how to handle a
   `pending` row with no `slurm_job_id`, the supported marker path, and the side effects of `POST /runs/{id}/cancel` on
   such a row (it also cancels the hydro_run and forecast_cycle, and can orphan a real Slurm job in the
   submit-succeeded / unrecorded sub-window).
7. **Spec.** Deltas in 10 capabilities (PR 1), plus `orchestrator-structural-burndown` in PR 2, whose cluster-map
   `patched` count drops from nineteen to eighteen with the deleted allocator. `retry-runtime-roots` is retired whole: every requirement concerns the submit
   lane, so `.openspec.yaml` sets `retire_capabilities: true`. Each requirement is classified by reachability from the
   two retired methods and the route's success path, not by the word "manual", because the marker lane survives.

## Decisions taken under the owner's delegation
- **Button and operation:** the `JobsTable` retry button and the OpenAPI operation stay for now (no userspace break in
  this PR); they are removed in a follow-up issue.
- **Retired capability:** `retry-runtime-roots` is retired, not left as an empty shell.
- **Mixed requirements** become REMOVED plus ADDED under a new name that keeps the surviving behaviour. MODIFIED is
  used only where a scenario survives intact.
- **"Concurrent Retry Protection"** is removed whole. Scenario 1 is the route's success-path conflict. Scenario 2 names
  `RETRY_ALREADY_IN_PROGRESS` / `manual_retry_already_active`, neither of which exists in code, so it is already dead
  spec text rather than auto-retry behaviour. This is recorded here because the classification rule alone would not
  decide it.
- **`expire_stale_retries`** and its two tests go in PR 2. #3156 kept it only as the implementation of the stale-pending
  MUST, and this PR removes that requirement.

## Must preserve
- **Marker lane:** behaviour, events and errors of `record_manual_repair`, `scripts/node22_manual_retry_failed_runs.py`
  and the scheduler mint.
- **Auto retry and cancel route:** behaviour unchanged.
- **Display contract:** RBAC and the display 409 contract (`display-control-mutation-guard`), and the two-node E2E
  evidence that expects display retry to return 409 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED`.
- **API surface:** OpenAPI and the frontend types stay byte-identical.

## Fixture level
expanded. Risk packs: api-contract, spec-retirement, test-reseed, operator-runbook.

## Evidence Floor
- [x] E1 `uv run ruff check --no-cache .` — PR 1: all checks passed.
- [x] E2 Tests, all green locally:
  - the selector set for the diff;
  - `tests/test_retry.py`, `tests/test_retry_cancel_consistency.py`, `tests/test_file_orchestration_journal.py`,
    `tests/test_monitoring_api.py`, `tests/test_file_journal_read_blocked_consumers.py` and
    `tests/test_retry_route_fail_closed.py` (`tests/test_retry_runtime_root_blocked_reads.py` is deleted);
  - `tests/test_file_orchestration_migration.py`, `tests/test_cohort_member_charge.py`,
    `tests/test_hydro_run_attempt_triage.py`, `tests/test_api_contract*.py`, `tests/test_e2e_m3.py`,
    `tests/test_response_model_preservation.py` and `tests/test_response_model_preservation_pipeline.py`;
  - `tests/test_readonly_db_validation_routes.py`, `tests/test_slurm_gateway_auth*.py`,
    `tests/test_slurm_gateway_openapi_security.py`, `tests/test_two_node_e2e_evidence.py`, the node-22 manual-retry
    script tests and `tests/test_select_ci_tests.py`.

  The new pins are red on the old code. The per-test delete/reseed/trim table is recorded.
  — PR 1: 174 files, 10430 passed / 3 env skips locally; pins 6 failed / 3 passed on the old code, 9 passed after;
  doc-consumer re-run after the review fixes 3459 passed. Table in the PR body.
- [x] E3 The same set passes in a node-27 temporary worktree. — PR 1 at 0c976db65: 10433 passed, 0 leftover
  `nhms_it_*` DBs; C2 readonly boundary validator PASS (`deny_write` and `read_routes`).
- [x] E4 `git diff origin/master -- openapi/ apps/frontend/src/api/types.ts` is empty. Frontend `pnpm test` and
  `check:api-types` pass locally, or the PR records why the frontend lane is untouched.
  — PR 1: diff empty; no frontend file changed (the retry button goes in #3179).
- [x] E5 Outside `openspec/changes/archive/`, this change and the absence-pin test, a grep for
  `def attempt_manual_retry|\.attempt_manual_retry\(` has zero hits. Docstrings and comments that describe the retired
  method as live are rewritten:
  - `file_orchestration_journal.py:13413,13449` and `scheduler_state_failure.py:315`;
  - `tests/test_hydro_run_attempt_triage.py:24`, including its stale `pipeline.py` line references.

  The entropy hard gate has `hard_gate_failing_count 0`. — PR 1: 0 hits; hard gate 0.
- [x] E6 `openspec validate retire-manual-retry-submit-lanes --strict --no-interactive` — valid.

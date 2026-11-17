# Proposal — retire-retry-surface (#3179)

Closes #3179.

```text
Issue type: refactor (retire dead surface)
Fixture level: expanded
Upstream suggested level: absent (override: public API contract + frontend control change)
Blast radius: an operator loses a visible control that can only fail; API callers see the same 401/403/409 as today
Selected risk packs: Public API / CLI, Legacy compatibility, Auth / permissions, Error handling, Documentation / migration notes
Evidence floor: E1-E6 below
```

## Why

#3157 retired manual-retry submission. Since then `POST /api/v1/runs/{run_id}/retry` always answers in this order:
`400 INVALID_RUN_ID`, then RBAC `401`/`403`, then the display `409`, then the retired `409
CONTROL_PLANE_MANUAL_ACTION_REQUIRED` (`apps/api/routes/pipeline.py:154-195`, `:555-566`). It kept the visible
surface on purpose, to avoid breaking callers in the same change:
- the `JobsTable` retry button, which now only ever shows an error toast;
- the `retryRun` 200 response and the `RetryRunResult` schema, which can never be returned;
- the `pipeline.retry_run` action id.

## Decisions (issue items 2 and 3, settled here)

- **The route stays as the 409 stub.** It is not deleted. Removing it would turn today's documented `409` (with its
  `suggested_action` and `recovery_runbook`) into a `404`/`405` for any caller still posting there. That includes a
  cached old frontend bundle and two deny-write probes that post to it on purpose:
  `tests/test_two_node_e2e_evidence.py:9473` and the live classifier `playwrightConfig.test.ts:187-193`.
  `ops-monitoring-api` already says "The API SHALL keep the endpoint". The stub costs one handler. Only the success
  contract that cannot happen is removed.
- **`pipeline.retry_run` is kept, unchanged.** It is not a dead id. The node-22 marker lane authorizes with it
  (`services/orchestrator/file_orchestration_journal.py:12586-12594`), and production closure and readiness evidence
  name it (`services/production_closure/ops_validation.py:2434`, `readiness_live_proofs.py:79`). Renaming it would
  break those evidence consumers. No RBAC change.

## Deviation from the issue (recorded)

The issue's title and Fix direction 2 ask to remove the route and the `retryRun` operation. Its item 2 allows a 409
stub only "for display … for one release". This change keeps the route and the operation as the 409 stub in every mode,
with no exit condition:
- `ops-monitoring-api` already requires the endpoint to stay;
- the stub is what keeps the documented 409 for existing callers and the deny-write probes;
- the owner delegated in-flight decisions to the best-practice choice (2026-11-16).

The PR records this under 偏离记录 and reports it to the owner. Removing the route later is a separate, explicit
owner decision.

## What

1. **Frontend: remove the retry control.**
   - `apps/frontend/src/components/monitoring/JobsTable.tsx` drops:
     - `retryableStatuses`, the `retryControlsEnabled` prop, `retryActionRole`/`retryHeaderRole`, and the `'retry'`
       arm of `runAction` (which keeps its signature for cancel);
     - the retry button (`:325-335`), the retry toast strings, and the now-unused `RotateCcw` import.
   - `controlsVisible` no longer counts retry. Cancel behaviour is unchanged.
   - Every caller that passes `retryControlsEnabled` stops passing it:
     - `apps/frontend/src/pages/MonitoringPage.tsx:324`;
     - `apps/frontend/src/__tests__/c4DisplayLane.test.tsx:229`;
     - `JobsTable.test.tsx:418`, `:431`, `:453`.
   - `retry_count` stays as a column, because it is data.
2. **`JobsTable.test.tsx`: no loss of `runAction` coverage.** Every shared-path case that runs only through retry
   today is rewritten onto cancel, using a cancellable (active) job row and the cancel strings `取消请求已提交` and
   `取消失败`:
   - `:163` (it.each over `operator`/`model_admin`/`sys_admin`: the only sender of the model_admin/sys_admin header);
   - `:267` (duplicate-click suppression while pending);
   - `:301` (refresh after success);
   - `:339` (refresh of the current selection when the selection changes before the action settles);
   - `:567` (error-envelope toast plus refresh, no success toast);
   - `:591` (success toast plus pending-clear);
   - `:619` (rejected promise);
   - `:650` (terminal error refresh).

   The other retry cases change as follows:
   - the retry-only cases are deleted or inverted: `:226` it.each and `:243` become "no retry button for any status
     or role";
   - `:193` keeps its cancel half;
   - `:257` becomes "cancel and log controls are independent";
   - `:142`, `:153` and `:538` drop their retry wording.

   One new case: an authorized dev-override operator with a `failed` job row that has a `run_id` sees no retry
   button, and no `POST` to `/retry` is made.
3. **Mocked e2e.**
   - `e2e/monitoring.mocked.spec.ts:111` ("shows retry for dev override operator") is replaced by "no retry button for
     dev override operator".
   - `:138` ("proves the /ops controlled failure log and retry lifecycle") keeps its evidence through marker-lane
     recovery instead of a click. The test asserts the following:
     - the controlled failure and its log, as today;
     - no retry button, and no `/retry` request;
     - then the mock flips to the recovered state through a new `opsApi.recoverViaMarkerLane()`, which replaces
       `retrySubmitted()` in `e2e/support/monitoring.mocked.ts:234-263`, `:359`, followed by a page refresh;
     - the same recovered job row (`succeeded`, `slurm_retry`, `2`), the stage succeeding, and the scoped
       `observedJobsQueries`.
     - Rename it to "proves the /ops controlled failure log and marker-lane recovery lifecycle".
   - `:245` (configured API base for operator actions) uses cancel as its operator action instead of retry.
   - The `/retry` handlers in `e2e/support/monitoring.mocked.ts:201` and `:359` are removed.
   - `src/__tests__/playwrightConfig.test.ts:187-193` is unchanged, because the route still exists.
4. **API contract: drop the success response, keep the operation.**
   - Handler (`apps/api/routes/pipeline.py:555-566`): `response_model=None`, return annotation `NoReturn`, body still
     raise-only. Update the `#3157`/`#3179` comments.
   - `apps/api/response_models/pipeline.py:131`, `:239`: delete `RetryRunResult` and `RetryRunResultEnvelope`, and
     their import in `routes/pipeline.py:28`.
   - OpenAPI patch:
     - delete `_retry_run_result_schema` at its definition (imported at `apps/api/openapi_patching_pipeline.py:31`;
       `git grep` it) and `schemas["RetryRunResult"]` (`:61`);
     - `_patch_pipeline_operation` gets an explicit opt-out (`data_schema=None` means no success response). When it is
       used, the helper writes no `"200"` and also removes FastAPI's default `"200"`;
     - the `retryRun` operation keeps its path, `operationId`, parameters, `security` and `409` response, and has no
       2xx key.
   - `openapi/nhms.v1.yaml`: same change. `tests/test_openapi_drift.py` keeps static and runtime equal.
   - `apps/frontend/src/api/types.ts`: regenerated with `pnpm generate:api`, then `check:api-types` passes.
5. **Oracles that pin the old contract.**
   - `tests/test_response_model_schema_parity.py`:
     - the route leaves `ROUTES`, so the count at `:236` goes 38 → 37 with a dated comment;
     - `test_every_unmodelled_business_route_is_annotated_to_return_a_response` (`:239-247`) gets an explicit
       retired-stub exemption, `RETIRED_STUB_ROUTES = {"POST /api/v1/runs/{run_id}/retry"}` with its reason. The
       exemption requires a `NoReturn` annotation, so a new unannotated JSON route still fails;
     - the unmodelled count at `:246-247` (`len(unmodelled) == 7`) stays 7: `RETIRED_STUB_ROUTES` is filtered out
       before counting, and a separate assertion pins the exempt set to exactly that one route.
   - `tests/test_response_model_preservation.py`:
     - `:792` is inverted: every `FAIL_CLOSED_ROUTES` entry has `response_model is None` and no 2xx in `app.openapi()`;
     - the texts at `:117-121` and `:123-126` are reworded;
     - the entry stays, with a reason that no longer claims an envelope is kept.
   - `tests/test_api_contract_pipeline_ops.py:467` and `:496-500` assert that `RetryRunResult` is absent from the
     schemas and from `types.ts`, and that `retryRun` has no 2xx.
6. **Docs.** The four operator runbooks lose every instruction to press retry in `/ops` and every description of the
   mocked retry request, and point to the marker lane (`scripts/node22_manual_retry_failed_runs.py`, runbook
   `node22-control-plane-manual-recovery`):
   - `docs/runbooks/node22-control-plane-manual-recovery.md`;
   - `docs/runbooks/qhh-continuous.md`;
   - `docs/runbooks/qhh-controlled-failure-retry-evidence.md`, whose `:46-47` evidence description follows the new
     marker-lane e2e;
   - `docs/runbooks/qhh-mvp-production-like-e2e-checklist.md`.

   Historical plans under `docs/plans/` and `IMPLEMENTATION_PLAN.md` are left as history.
7. **Specs.** MODIFIED requirements:
   - `ops-monitoring-api`: the retry endpoint declares no success response;
   - `pipeline-monitoring-frontend`: Role-Based Access Control, Center Job List Table;
   - `ops-mvp-pipeline-control`: Ops MVP entry, Formal pipeline-backed stage and job status, Orchestrator boundary;
   - `display-readonly-ops-ui`: Ops display mode is read-only;
   - `monitoring-migration`: Retry and cancel actions;
   - `frontend-testing`: Monitoring page E2E test.

   Scenario names are unchanged; their bodies now say "no retry control".

## Non-goals

- Retry route deletion, RBAC action id rename, and changes to `display-control-mutation-guard` or
  `rbac-policy-enforcement` (behaviour unchanged).
- The marker lane, `scripts/node22_manual_retry_failed_runs.py` and `services/orchestrator/retry.py`.
- The cancel route and its contract.

## Must preserve

- The retry route: same order and bodies of `400`/`401`/`403`/display `409`/retired `409`. There is still no DB
  session, retry service or gateway construction.
- The cancel UI and cancel API behaviour. All non-retry JobsTable and monitoring tests stay green.
- The OpenAPI `security` on the retry operation (`apps/api/openapi_patching_security.py:33`,
  `tests/test_slurm_gateway_openapi_security.py`).
- The two-node deny-write probe and the live display classifier.

## Evidence Floor

- [ ] E1 Lint and leftover references:
  - `uv run ruff check --no-cache .` passes, `git diff --check` is clean, and the entropy hard gate reports
    `hard_gate_failing_count 0`;
  - `git grep -n RetryRunResult -- ':!openspec/changes/archive' ':!docs/review-loop-log.jsonl'` hits only:
    - absence assertions in `tests/test_api_contract_pipeline_ops.py`;
    - this change's `openspec/changes/retire-retry-surface/**`;
    - historical `docs/plans/**` or `IMPLEMENTATION_PLAN.md`, if present;
  - `git grep -n -E "retryControlsEnabled|retryableStatuses|retrySubmitted" -- apps/frontend` is empty.
- [ ] E2 Backend, local:
  - `uv run pytest -q tests/test_monitoring_api.py tests/test_retry_route_fail_closed.py
    tests/test_api_contract_pipeline_ops.py tests/test_api_contract.py tests/test_openapi_drift.py
    tests/test_response_model_preservation.py tests/test_response_model_schema_parity.py
    tests/test_slurm_gateway_openapi_security.py tests/test_slurm_gateway_auth.py tests/test_auth_policy_matrix.py
    tests/test_two_node_e2e_evidence.py tests/test_retry_cancel_consistency.py`;
  - plus the selector set for the diff.
- [ ] E3 Frontend, local, in `apps/frontend`: `pnpm typecheck`, `pnpm test`, `pnpm build`, `pnpm check:api-types`,
  `pnpm test:e2e:mocked-regression e2e/monitoring.mocked.spec.ts`.
- [ ] E4 Mutation reds, each restored:
  - re-adding a retry button to `JobsTable` makes the new "no retry button" unit case and the e2e replacement red;
  - re-adding a `200` to `retryRun` makes the contract test and the preservation inversion red;
  - breaking the cancel duplicate-click guard makes the rewritten `:267` case red;
  - dropping `refreshSelectedContext` from `runAction`'s `finally` makes the rewritten error-path case red.
- [ ] E5 node-27, in a temporary detached worktree, with `NHMS_RUN_INTEGRATION=1` and `NHMS_INTEGRATION_DATABASE_URL`
  pointing at the node-27 PG (creds from the container env, never printed):
  - the E2 pytest set passes;
  - the deny-write probe set passes: `tests/test_two_node_e2e_evidence.py`, `tests/test_retry_route_fail_closed.py`,
    and `tests/test_monitoring_api.py -k "display and retry"`;
  - the expectation is that a display-readonly retry `POST` answers `409 CONTROL_PLANE_MANUAL_ACTION_REQUIRED` with
    `display_mode=display_readonly`, and no gateway, session or retry service is constructed;
  - `leftover_it_dbs=0`.
- [ ] E6 `openspec validate retire-retry-surface --strict --no-interactive`.
- [ ] E7 Docs: `git grep -n -i -E "重试按钮|retry button|click(s)? retry|点击.{0,4}重试|/retry" -- docs/runbooks/node22-control-plane-manual-recovery.md docs/runbooks/qhh-continuous.md docs/runbooks/qhh-controlled-failure-retry-evidence.md docs/runbooks/qhh-mvp-production-like-e2e-checklist.md`
  hits only lines that say the UI has no retry, or that describe the 409 stub.

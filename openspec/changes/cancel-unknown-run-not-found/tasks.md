# Tasks

Fixture level: `standard`. Risk packs: API contract (status code and error
code of a published operation), control-plane mutation (no write on the new
path).

Must preserve: every existing test in `tests/test_retry_cancel_consistency.py`,
`tests/test_monitoring_api.py`, `tests/test_openapi_drift.py`,
`tests/test_api_contract.py`,
`tests/test_api_contract_pipeline_ops.py` and
`tests/test_response_model_preservation_pipeline.py` unchanged and green,
except the one contract table row task 2.7 adds; the success body of cancel
(field names and shapes); the 400/401/403/409/503 refusals and their order;
display-readonly cancel never builds the store or the gateway; a run with
active jobs is cancelled exactly as today; the retry route is not touched.

## 1. Code

- [ ] 1.1 `apps/api/routes/pipeline.py`: add a read-only helper next to
  `_cancel_hydro_run` that answers whether `hydro.hydro_run` has a row for a
  run id. It uses `_table_columns(store, "hydro_run", "hydro")`; when
  `run_id` is not among the columns it answers false; otherwise it runs
  `SELECT 1 FROM hydro.hydro_run WHERE run_id = :run_id LIMIT 1`. It does not
  commit and does not write.
- [ ] 1.2 `cancel_run`: keep the result of `store.query_jobs_by_run(run_id)`
  in a local, derive `active_jobs` from it as today, and when the full list is
  empty and the helper answers false, raise
  `ApiError(status_code=404, code="RUN_NOT_FOUND", message=f"Run not found: {run_id}", details={"run_id": run_id})`
  before anything else in the body. No other statement of the function
  changes.
- [ ] 1.3 `apps/api/openapi_patching_pipeline.py`: register a typed error
  response for `RUN_NOT_FOUND` (same builder as the other typed responses)
  and inject it as the cancel operation's `404`, next to the existing `409`
  injection. Update the comment there.
- [ ] 1.4 `openapi/nhms.v1.yaml` must equal the runtime schema as a whole
  (`tests/test_openapi_drift.py` compares the two dictionaries). Add the
  named entry under `components/responses` with the same name and the same
  description, character for character, as the runtime registration of 1.3,
  in the finalized 3.1 form the neighbouring `ControlPlaneManualActionRequired`
  entry uses (`details` typed as `[object, 'null']`, not `nullable`), and
  reference it from the cancel operation's `404` by `$ref`, in the position
  the runtime schema gives it. Regenerate `apps/frontend/src/api/types.ts`
  with the repository's own script (`pnpm generate:api` in `apps/frontend`);
  `pnpm check:api-types` passes. The generated file is not edited by hand.
- [ ] 1.5 `docs/bugs.md`: BUG-20260527-011 gets `status: resolved` and a
  `resolved_by` field in the form the resolved entries above it use, naming
  #3245 and what the route answers now (the pull request is linked on the
  issue; its number does not exist before the final push). The prose below
  the block is rewritten to describe the fixed state and no longer says the
  decision is open. The `retest_command` stays (it accepts 404).
  `scripts/validate_bugs_ledger.py` passes.

## 2. Tests

In `tests/test_retry_cancel_consistency.py`, with that file's `_store`,
`_client`, `_MockGateway` and seeding helpers:

- [ ] 2.1 Unknown run id (nothing seeded): HTTP 404, `status == "error"`,
  `error.code == "RUN_NOT_FOUND"`, `error.details.run_id` equals the id; the
  gateway recorded no call; the pipeline event table has no row; no
  `hydro.hydro_run` row exists afterwards.
- [ ] 2.2 Unknown run id while another run has an active job and a
  non-terminal hydro run row: 404 for the unknown id, and the other run's job
  status, hydro run status and event count are unchanged (the check is per
  run id, and the 404 path touches nothing).
- [ ] 2.3 Run with only terminal jobs and no hydro run row: HTTP 200,
  `cancelled_jobs == []`, `partial_failure is False` (existence by job rows
  of any status). Parametrized over the job statuses `succeeded`, `failed`
  and `cancelled`.
- [ ] 2.4 Hydro run row (non-terminal) and no job row: HTTP 200 and the row
  is cancelled as today (existence by run row).
- [ ] 2.5 The `hydro.hydro_run` table is absent and no job row exists: 404
  (the decision about an unreadable table). The file's `_store` always
  creates the table: drop it inside this test (`DROP TABLE hydro.hydro_run`);
  `_store` itself is not changed.
- [ ] 2.6 Earlier refusals still win for an unknown run id, each with the
  setup the file's existing tests use for that refusal (the display-mode
  cancel tests and the auth/RBAC denied-path tests): display-readonly API and
  an authorized actor -> 409 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED`;
  unauthenticated -> 401; a role without the cancel permission -> 403. If an
  existing test already posts a cancel for an unseeded run id in one of these
  setups and asserts the same status, name it in the report instead of
  duplicating it.
- [ ] 2.7 `tests/test_api_contract.py`: add the row
  `("/api/v1/runs/{run_id}/cancel", "post", "404", ["RUN_NOT_FOUND"])` to the
  table that already holds the cancel 409 row, so the published document and
  the runtime schema are both checked.

## 3. Evidence

- [ ] 3.1 Red first: tests 2.1, 2.2, 2.5 and 2.7 fail on the unmodified
  source (2.1/2.2/2.5 with 200 instead of 404; 2.7 on the missing
  declaration); 2.3, 2.4 and 2.6 pass before and after (they pin behaviour
  that must not change). Output saved to `.workplans/3245/red-runs.log`.
- [ ] 3.2 Mutations, each applied by hand, checked, recorded, restored
  (`cmp` against a scratch copy):
  - M1: remove the raise -> 2.1 red.
  - M2: decide existence from active jobs only (use `active_jobs` in the
    condition) -> 2.3 red.
  - M3: drop the hydro run read from the condition -> 2.4 red.
  - M4: drop the job list from the condition (hydro run read only) -> 2.3 red
    (a run with terminal jobs and no run row).
  - M5: remove the 404 injection in `openapi_patching_pipeline.py` -> 2.7
    red on the runtime schema; remove the 404 from the yaml only -> 2.7 red
    on the published document, and `tests/test_openapi_drift.py` red.
  - M6: drop `WHERE run_id = :run_id` from the helper's query -> 2.2 red
    (another run's row makes the unknown id look known).
  - M7: drop the `_table_columns` guard from the helper -> 2.5 red (the
    query on the missing table raises instead of answering 404).
- [ ] 3.3 `uv run --no-sync ruff check --no-cache .` clean;
  `uv run --no-sync pytest -q tests/test_monitoring_api.py tests/test_retry_cancel_consistency.py tests/test_openapi_drift.py tests/test_api_contract.py tests/test_api_contract_pipeline_ops.py tests/test_response_model_preservation_pipeline.py`
  green; `cd apps/frontend && pnpm check:api-types` passes and
  `pnpm exec tsc --noEmit` (or the repository's type-check script) passes.
- [ ] 3.4 node-27: the same pytest files in a detached temporary worktree.
- [ ] 3.5 CI: full suite, OpenAPI Validate, Frontend Build.

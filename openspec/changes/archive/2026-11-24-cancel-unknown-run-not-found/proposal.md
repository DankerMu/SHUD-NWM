# Cancel of an unknown run is a not-found error

## Why

#3245 (`docs/bugs.md` BUG-20260527-011). `POST /api/v1/runs/{run_id}/cancel`
builds its work list from the active jobs of the run. For a run id that names
nothing the list is empty, nothing is cancelled, and the route answers HTTP
200 with empty `cancelled_jobs`, `hydro_run: null` and `forecast_cycle: null`.
An operator who mistypes a run id gets a success envelope. No test pins either
behaviour.

## What Changes

- `cancel_run` (`apps/api/routes/pipeline.py`): when the run id has no
  `pipeline_job` row of any status and no `hydro.hydro_run` row, the route
  raises the stable error envelope with HTTP 404 and
  `error.code = "RUN_NOT_FOUND"`. Nothing is written and the Slurm gateway is
  not called.
- OpenAPI: the cancel operation declares the 404 (runtime patching and the
  published `openapi/nhms.v1.yaml`); the frontend API types are regenerated.
- `ops-monitoring-api`: the cancel requirement is modified. It gains the
  not-found rule and four scenarios, and its "No active jobs to cancel"
  scenario is corrected: the spec said HTTP 409 `NO_ACTIVE_JOBS`, the route
  answers the idempotent 200 (`cancel_run` returns `_ok` unconditionally,
  and `tests/test_response_model_preservation_pipeline.py` asserts 200 for a
  repeated cancel). Without the correction the new text would contradict
  the old scenario. The requirement's first two scenarios are carried over
  verbatim, as the delta format requires, and are not re-verified here.
- `docs/bugs.md`: BUG-20260527-011 becomes `resolved`.

## Decisions

- **404, not retiring the route.** The bug entry left both options open. The
  issue settles on 404: cancel is the one control-plane mutation the compute
  API still performs, and its other behaviour is pinned by tests and specs.
  Whether a deployment exists that can reach the route body at all is a
  separate question and is not answered here.
- **"Unknown" means no evidence of the run in either place the route reads.**
  Jobs of any status count, not only active ones: a run whose jobs are all
  terminal exists and keeps the idempotent 200. A `hydro.hydro_run` row with
  no jobs also counts: the route cancels such a run today and keeps doing so.
- **An unreadable `hydro.hydro_run` counts as no row.** The existing helper
  `_table_columns` answers an empty set when the table cannot be inspected;
  the existence read uses the same helper and treats a column set without
  `run_id` as "no row". With no jobs either, the answer is 404. This matches what
  `_cancel_hydro_run` already does with such a database (it changes nothing).
- **Reuse `RUN_NOT_FOUND`.** `apps/api/routes/hydro_display.py` already uses
  this code for a run id with no `hydro.hydro_run` row, with the message
  `Run not found: <run_id>` and details `{"run_id": <run_id>}`. The cancel
  route uses the same code, message form and details.
- **Order of refusals is unchanged.** The check sits in the route body, after
  `_validated_run_id` and the job query. Authentication (401), RBAC (403),
  release block (503), the display-readonly manual-action refusal (409) and a
  missing database URL (500) are all decided by dependencies before the body
  runs, so each of them still wins over the 404. None of them persists an
  audit record: the policy decision is kept on the request and only a success
  envelope prints it, so the 404 leaves no trace that claims a cancel happened.
- **The existence read has no side effect.** It is a `SELECT 1 … LIMIT 1`
  and runs only when the job query returned nothing, so runs that have jobs
  pay nothing extra.

## Impact

- Code: `apps/api/routes/pipeline.py`, `apps/api/openapi_patching_pipeline.py`.
- Contract: `openapi/nhms.v1.yaml`, `apps/frontend/src/api/types.ts`
  (generated). The only frontend caller (`JobsTable.tsx`) already shows the
  error message of a failed cancel in a toast; no frontend code changes.
- Tests: `tests/test_retry_cancel_consistency.py`, `tests/test_api_contract.py`.
- Clients that relied on 200 for an unknown run id: none found in the
  repository. Every existing cancel test that reaches the route body seeds a
  job or a run row; the ones that seed nothing are refused with 400, 401,
  403 or 409 before the body runs.

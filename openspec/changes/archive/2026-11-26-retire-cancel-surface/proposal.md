# Retire the control-plane "cancel a run" route and button (#3394 slice 1)

Fixture level: `expanded` (see `design.md`). Risk packs: public API contract (a path and five schemas leave
OpenAPI), auth/RBAC (an action id leaves the policy; tests that used the route as their vehicle move), frontend
behaviour (a control leaves the monitoring page), spec breadth (fifteen capabilities name the surface).

## Why

Owner decision of 2026-11-26: retire the surface. Part of #3217.

- **No production process can serve it.** The display API on node-27 runs in the read-only role
  (`control_mutations_enabled: false`) and answers the route with 409. The `nhms-compute-api` unit on the compute host
  is disabled and not running, and never had a `DATABASE_URL`, so the route's store dependency answers 500 there.
- **It is the last run action left** after #3179 retired retry
  (`openspec/changes/archive/2026-11-17-retire-retry-surface/` is the precedent).
- **Its 409 points nowhere.** The display role tells the operator to "stop this run from the compute_control API", a
  path that does not exist.
- **It bypasses a check.** The route cancels through the in-process gateway and skips the backend check #3246 added
  (found there as an out-of-scope note).

## What is retired

| Layer | Goes |
|---|---|
| API | `POST /api/v1/runs/{run_id}/cancel`, its handler `cancel_run`, its dependency chain and every helper only it uses (`apps/api/routes/pipeline.py`); the response models only it returns; its OpenAPI patching and security override; the `cancel` probe of the readonly validation; the RBAC action `pipeline.cancel_run` |
| Contract | the path and its schemas in `openapi/nhms.v1.yaml`; the generated `apps/frontend/src/api/types.ts` follows |
| Vocabulary | `HYDRO_RUN_TERMINAL_UI` and `CYCLE_TERMINAL_UI` in `packages/common/status_vocab.py` and the route constants bound to them: the cancel helpers are their only readers (D4) |
| Frontend | the cancel button of `JobsTable`, the request, its pending state and role plumbing; `controlMutationsEnabled` and the disabled-controls text of `MonitoringPage`; `canUseDevRoleActions` / `OperatorRole` / `isTestMode` in `stores/auth.ts` when nothing else uses them |
| Tests | the tests whose only subject is the route or the button |
| Docs | every non-history doc line that tells a reader the route or button exists; the runbook step that names the route as the exit for a stuck `pending` job row is rewritten to what an operator can do |

## What stays

The scheduler's own cancellation (`services/orchestrator/**`, `cancel_active_cycle_jobs`, the `cancel` events it
writes), the Slurm gateway's `DELETE /api/v1/slurm/jobs/{job_id}` and `cancel_job` with the action `slurm.cancel_job`,
the `cancelled` status and every enum and filter that shows it, the retry 409 stub and `pipeline.retry_run`, the
`compute_control` role and `control_mutations_enabled`, `PipelineStore.insert_event` / `query_jobs_by_run` /
`update_job_status` (the issue keeps them; after this change the first two have test callers only: reported, not
removed), the display-lane negative checks ("no retry or cancel control", the C4 DOM guard and its evidence field
`retry_cancel_control_count`), the live e2e guard that forbids non-GET requests to run endpoints.

## Decisions

### D1 — one PR, two ordered commits

1. **Commit A, with the route still in place: the tests that use cancel as a vehicle move to another vehicle and are
   green.** Only while the route exists can a moved test be shown to prove what it proved (old and new side by side,
   red run on the new one).
2. **Commit B: the deletion**, backend and frontend together. The regenerated `types.ts` ties them.

### D2 — the vehicle tests (ten, the issue counted four)

The inventory (`.workplans/3394/recon.md` §4 and its corrections D, E) and the fixture review found ten tests that are
not about cancel but ride on it: in `tests/test_monitoring_api.py` the tests at lines 1914, 1990, 2126, 2237 (the
runbook slug), and five release-blocked tests at 2342, 2363, 2418, 2443, 2480 (the last is the #2169 non-ASCII
regression); in `tests/test_retry_cancel_consistency.py` the `error_message` redaction test. Each gets one of three
outcomes, and the PR lists all ten by test id:

| Outcome | When | Evidence |
|---|---|---|
| moved | another route still shows the property | red run on the new test: the audit record / decision / redaction dropped → red |
| deleted as a duplicate | an existing test already asserts every assertion | the duplicate cited assertion by assertion |
| property leaves with the route | no surviving route can show it | stated in the PR with the reason; no weaker substitute is written |

Known from the inventory, to be confirmed in code:

- The five release-blocked tests move to the retry route: the 503 comes from the RBAC dependency before the 409 stub.
  "No mutation" on retry is vacuous and is not what they claim there; the no-mutation pin for release-blocked lives on
  the `models` routes (`tests/test_model_registration.py`).
- The live-proof metadata test moves to a `models` mutation with `FakeModelRegistryStore`.
- The test of an allowed mutation's canonical audit evidence (1990) is covered by two tests of
  `tests/test_model_registration.py` together (about lines 962-989: action, decision, roles, execution mode; about
  1360-1395: the `request_id` echo), on the `models` response path with `model_admin`. It is a duplicate only if every
  assertion is found in one of the two; the decisions branch of the pipeline route's `_ok` has no caller after the
  deletion and goes with it (task 3.1).
- The test that an **operator's** allowed mutation answers 200 with its decision (1914): the `models` routes admit
  `model_admin` / `sys_admin` only and retry answers 409, but `slurm.cancel_job` and `slurm.submit_job` admit
  `operator` and their routes are mounted in `compute_control` and `dev_monolith`. "The dev token's default role is
  operator and an operator's allowed mutation succeeds over HTTP" moves to `DELETE /api/v1/slurm/jobs/{job_id}` with a
  fake gateway. Those routes' bodies carry no decision: "the decision in an operator's 200 body" leaves with the route.
- The runbook-slug test (2237): moved or left according to what it reads; the slug of the retry 409 survives.
- `test_cancel_success_projection_redacts_persisted_log_uri_and_error_message` is the only HTTP test of the
  `error_message` redaction in `_job_payload`: that pin moves to the `/api/v1/jobs` test before the file's cancel tests
  go.

### D3 — spec deltas: REMOVED plus a new header where a scenario goes

OpenSpec refuses a MODIFIED block that drops a scenario (tried on `display-control-mutation-guard` in a scratch copy:
validation and archive both refuse). So a requirement that loses a scenario is REMOVED and its surviving text is ADDED
under a new header; a requirement whose scenarios all survive is MODIFIED in place, with two scenario titles "kept for
archive continuity" where the title still says cancel. Fifteen capabilities (the issue named five). While `api-contract-alignment`'s block is restated, its stale count of
unprotected operations ("39"; the contract has 42 today and after) is dropped instead of corrected to another number.

Two scenarios of the affected requirements were not route-specific in wording:

- "A cancel event records the Slurm action" stays, restated for its surviving writer: the scheduler's
  `cancel_active_cycle_jobs` writes a `cancel` event with the Slurm job id and the reason
  (`services/orchestrator/chain_forecast_control.py`), pinned by
  `tests/test_orchestration_chain.py::test_cancel_active_cycle_jobs_calls_gateway_and_records_no_replacement`. So
  "State transitions are auditable" is MODIFIED in place, not removed.
- "A cancelled run does not block a re-run" is not re-homed: its only test ran a test-local SQL guard through the
  route. That a `cancelled` job is not active is the vocabulary's `PIPELINE_JOB_ACTIVE` set, pinned by
  `status-vocabulary`.

### D4 — the two "UI terminal" vocabulary sets go with their only reader

`_TERMINAL_HYDRO_STATUSES` / `_TERMINAL_CYCLE_STATUSES` in the pipeline route are read by the cancel helpers only, and
they are the only readers of `HYDRO_RUN_TERMINAL_UI` / `CYCLE_TERMINAL_UI`. Code is not kept alive for its parity test:
the four names, their rows in `tests/test_status_vocab_parity.py` and their allowlist entries go, and `status-vocabulary`
stops naming the two sets. If the implementer finds another reader of either set, that set stays and the spec delta is
corrected.

### D5 — what the retired path answers: 405, not the issue's 404

The issue's verification line says "404 in every role". The app cannot answer that: `apps/api/startup_wiring.py`
registers a GET fallback for every path (it serves the single-page app), so a POST to any unregistered path is a
method mismatch and Starlette answers 405 with `Allow: GET` (observed; the fallback is registered for GET only, so
HEAD is not offered either). Nothing answers earlier: the pre-body guard of
`apps/api/main.py` does not cover `/runs/…`, and with the route gone no RBAC dependency is left on the path. The
fixture reviewer read this from the code and expected `Allow: GET, HEAD`; the implementer observed the nine
combinations on the real app with the route removed: 405 everywhere, status, `Allow` and body identical to an
unregistered POST path, and `Allow` is `GET` alone. Before the deletion the same probe answered 500/403/401 in the
compute roles and 409/403/401 in the display role.

The requirement therefore says: in the three service roles, with operator credentials, viewer credentials and none,
the retired path answers exactly as an unregistered POST path under `/api/v1/` does, and that answer is 405. The test
sends both requests to the real app in each of the nine combinations and compares them. If a combination answers
anything else, the implementer stops on that item and reports it.

### D6 — docs this change makes false are in scope

Besides the lines that name the route or button: any statement that the pipeline routes of the API **write**
(`docs/runbooks/**/oncall-sql.md` describes the `nhms-api-pipeline` connection that way), and the runbook's stuck
`pending` row step.

## Not in this change

- Slice 2 of #3394: the unused `nhms-compute-api` unit (its own change; it also has to restate
  `node22-unit-tracking`'s "the two receipts record exactly the eight tracked files").
- Removing the `compute_control` role, `control_mutations_enabled`, or the `models` mutation routes.
- Removing `PipelineStore.insert_event` / `query_jobs_by_run`.
- A CLI for the scheduler's `cancel_active_cycle_jobs` (it has test callers only today: reported).

## Evidence plan

| Claim | Evidence |
|---|---|
| the moved tests prove what they proved | commit A green with the route in place; per test the red run of D2 |
| the path is gone | the D5 test (405, equal to an unregistered POST path); no `cancel` operation in `openapi/nhms.v1.yaml`; OpenAPI drift test; `check:api-types` clean |
| nothing else referenced it | the completeness grep of tasks 6.x, every hit classified |
| the page works without the control | frontend unit tests and the mocked Playwright spec of `/monitoring` |
| nothing else moved | touched pytest files and every reader of an edited file; `ruff`; entropy hard gate unchanged; node-27 run of the touched backend test files |

## After the merge (on the issue)

node-27 serves the old code until the display API is restarted and the frontend is rebuilt. Both are production
actions that need the owner's go-ahead (the go-ahead of 2026-11-26 for #3390 does not cover them). Until then the old
bundle's button is not rendered anyway (the display role hides it), and a direct call keeps answering 409 (405 after the restart). The PR
therefore says "Refs #3394".

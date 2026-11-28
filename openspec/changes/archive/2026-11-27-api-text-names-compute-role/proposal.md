# The retry refusal and two OpenAPI descriptions name the compute role (#3423)

Slice 8.1 of `openspec/changes/sweep-compute-role-narration` (#3345). Fixture level: expanded (a
public API message, two descriptions of the published OpenAPI document and the types generated from
it). Risk packs: Public API contract / generated clients - selected (the checked-in OpenAPI document
is compared with the runtime schema, and the frontend types are generated from it; both must move
with the code in one change). Deployment / live evidence - selected (the display API on node-27
serves the old text until it is restarted, which is slice 8.3 and needs the owner's go-ahead).
Legacy compatibility - selected (status code, error code, response shape, the script name and the
runbook id in the response are what callers and probes read; none changes). Others - not selected.

## Why

The pipeline retry route answers every request with a 409 that tells the caller where the supported
path is. Its texts say "a manual node-22 action" and "Ask a node 22 operator …". Two descriptions of
the published OpenAPI document say the same ("the node-22 manual-repair marker lane", "the
compute_control runbook on node 22"). Since 2026-11-23 the compute role runs on two other hosts and
the original host is retired; a reader of the API who takes the text literally looks for a machine
that no longer does the work. `production-topology-contract` asks every current text to name the
compute role, the compute control host or the compute hosts, and to keep the codename for
identifiers and for the retired host.

## What Changes

- **The retry refusal** (`apps/api/routes/pipeline.py`): the message says the retry is "a manual
  action on the compute control host". The suggested action says "Ask an operator of the
  compute role …" in both of its texts: the retry one, which both 409 paths return, and the
  fallback for any other control action, which has no caller since #3394 removed the cancel route
  (it is reworded like the other; whether the branch should go is not this slice's question).
- **Two OpenAPI descriptions** (`apps/api/openapi_patching_pipeline.py`): the description of the
  operation `retryRun` ("the manual-repair marker lane of the compute role") and the description of
  the response `ControlPlaneManualActionRequired` ("the compute_control runbook on
  the compute control host"). `openapi/nhms.v1.yaml` carries the same two texts and
  `apps/frontend/src/api/types.ts` is regenerated from it.
- **One test** (`tests/test_retry_cancel_consistency.py`): the expected retry suggestion and the
  comment above it follow.
- **`ops-monitoring-api`**: the sentence of the retry requirement that names the supported operator
  path says "the manual-repair marker lane of the compute role"; at archive the Purpose line says
  "a manual action on the compute control host".

## What does not change

- HTTP status 409, the code `CONTROL_PLANE_MANUAL_ACTION_REQUIRED`, the keys of `details`, the
  order of the checks before the refusal.
- The script name `scripts/node22_manual_retry_failed_runs.py`, the function name
  `FileJournalRetryService.record_manual_repair`, the runbook id
  `node22-control-plane-manual-recovery`, the service role name `compute_control`.
- UI text (slice 8.2) and the frontend comments that repeat the lane's name.
- Two test files that name the lane in their own prose
  (`tests/test_response_model_schema_parity.py`, a reason string of the test's own allowlist;
  `tests/test_file_journal_read_blocked_consumers.py`, a docstring): neither is a copy of an API
  text, and the issue's PR boundary is one test.
- What node-27 serves: the display API keeps the old text until slice 8.3 restarts it.

## Impact

- Code: two modules under `apps/api/`, text only.
- Contract: `openapi/nhms.v1.yaml` (two descriptions), `apps/frontend/src/api/types.ts` (two
  generated comments). No schema, path, parameter or response code changes.
- Tests: one expected string and one comment.
- Specs: `ops-monitoring-api` (one sentence of one requirement; the Purpose line at archive).

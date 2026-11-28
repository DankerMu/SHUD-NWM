# Tasks: the retry refusal and two OpenAPI descriptions name the compute role (#3423)

## Must preserve

- HTTP 409, `CONTROL_PLANE_MANUAL_ACTION_REQUIRED`, the keys of `details`, the order of the checks.
- `scripts/node22_manual_retry_failed_runs.py`, `FileJournalRetryService.record_manual_repair`,
  `node22-control-plane-manual-recovery`, `compute_control`: unchanged wherever they appear.
- No schema, path, parameter, operation id or response code of the OpenAPI document changes: the
  diff of `openapi/nhms.v1.yaml` is the two descriptions and nothing else.
- `apps/frontend/src/api/types.ts` is only ever written by the generator.
- Nothing is restarted or deployed.

## 1. Code and contract

- [x] 1.1 `apps/api/routes/pipeline.py`: the retry message and both suggested-action texts
  (`design.md` D1).
- [x] 1.2 `apps/api/openapi_patching_pipeline.py`: the description of `retryRun` and of
  `ControlPlaneManualActionRequired` (D1).
- [x] 1.3 `openapi/nhms.v1.yaml`: the same two descriptions (D3).
- [x] 1.4 `apps/frontend/src/api/types.ts`: regenerated with `pnpm generate:api` (D3).
- [x] 1.5 `tests/test_retry_cancel_consistency.py`: the expected retry suggestion and the comment
  above it.

## 2. Spec

- [x] 2.1 `ops-monitoring-api`: the MODIFIED block of the retry requirement (D4).
- [x] 2.2 The change is archived in the PR's last push: the requirement in the main spec equals the
  generated block; the Purpose line is edited there, with the term on one line (D1, D4).
- [x] 2.3 Task 8.1 ticked in `openspec/changes/sweep-compute-role-narration/tasks.md`.

## Evidence Floor

- Red runs, each logged with its command and failing assertion:
  - R1: the route module changed, the test not yet: the test of the `display_readonly` retry
    refusal fails on the old suggestion.
  - R2: the patching module changed, the YAML not yet:
    `tests/test_openapi_drift.py::test_static_openapi_matches_runtime_schema` fails.
  - R3: the YAML changed, the types not yet: `pnpm check:api-types` fails.
- Green: `tests/test_retry_cancel_consistency.py` (the `display_readonly` 409),
  `tests/test_retry_route_fail_closed.py` and `tests/test_monitoring_api.py` (the 409 of
  `compute_control` / `dev_monolith`: status, code, keys of `details`, order of checks, no side
  effect), `tests/test_openapi_drift.py`,
  `tests/test_api_contract.py`, `tests/test_api_contract_pipeline_ops.py`,
  `tests/test_api_contract_resources.py`, `tests/test_response_model_schema_parity.py`;
  `pnpm check:api-types`; the frontend type check (`pnpm typecheck`).
- The five assembled texts, printed by calling the code: the retry message from the 409 of a
  `dev_monolith` or `compute_control` client, the retry suggestion from that response and from the
  409 of a `display_readonly` client (whose own message does not change), the two descriptions from
  `app.openapi()`, and the fallback suggestion by calling `_manual_action_suggestion` with a value
  other than `"retry"` (no route reaches it).
- `git diff` of `openapi/nhms.v1.yaml` and of `types.ts`: only the two descriptions.
- Hard gate: the finding multiset (check id, path, line) identical before and after.
- `narration_count.py` plain and `--tokens` before and after; every moved name token explained.
- `openspec validate api-text-names-compute-role --strict --no-interactive`; after the archive,
  `openspec validate ops-monitoring-api --strict` (or the spec-level equivalent) and
  `openspec validate sweep-compute-role-narration --strict --no-interactive`.
- `uv run ruff check --no-cache .`; markdownlint on the edited documents; CI green on every shard.
- node-27: the same Python test files in a temporary worktree, with the hard gate line
  (`tests/test_api_contract.py` regenerates the types with `npx` and needs the registry; if
  node-27 cannot reach it, that one test is reported as not run there and rests on the local run
  and on CI).
- Deviation recorded in the PR: the live receipt of the served text follows in slice 8.3.

Suggested fixture level: expanded - a public API message and description with a generated chain.
Minimal mergeable slice: atomic - the code, the YAML, the generated types and the test must move together or the drift test and `check:api-types` fail.

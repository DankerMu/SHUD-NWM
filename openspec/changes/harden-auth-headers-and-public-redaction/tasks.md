# Tasks

Issues: #2081, #2169, #2038, #1975, #1976 (all closed by this PR).
Fixture level: expanded; repair intensity: high (Invariant Matrix + boundary checklist in design.md).
Seams under test: HTTP routes via FastAPI TestClient (forecast-series, models lifecycle/active, runs retry,
GET jobs), `request_auth.auth_context_from_request`, `public_evidence` renderer functions.

## Risk packs

- Public API / CLI / script entry: selected - forecast-series 403→200, lifecycle/active response narrowing, retry 503 body, GET job payloads.
- Config / project setup: not selected - no env var or config change.
- File IO / path safety / overwrite: not selected - no file access; path *rendering* covered under Schema/Error packs.
- Schema / columns / units / field names: selected - `ModelInstance.mesh_properties_json` removed from OpenAPI + generated TS types.
- Auth / permissions / secrets: selected - role-header gate removal, constant-time live-proof compare.
- Concurrency / shared state / ordering: not selected - no state transitions touched.
- Resource limits / large input / discovery: not selected - no bounded reads/discovery.
- Legacy compatibility / examples: selected - existing tests pinning old behavior are rewritten explicitly (#1976 pinned asserts, conformance mutation), sibling consumers unchanged (R5, R8).
- Error handling / rollback / partial outputs: selected - 503 retry body, 503 lifecycle audit-failure body, non-ASCII header no-500.
- Release / packaging / dependency compatibility: not selected - no dependency change.
- Documentation / migration notes: not selected - spec deltas are the documentation; no migration.
- Domain packs (profile): Service-to-service auth and OpenAPI security parity - selected (R5: override set unchanged); others not selected (no forcing/CRS/SHUD/Slurm surface).

## 1. #2081 hindcast role-header gate removal

- [x] 1.1 Delete `_require_hindcast_access_role`, `_HINDCAST_ACCESS_ROLES`, and the call in
  `get_forecast_series` (`apps/api/routes/forecast.py`). Remove now-unused imports only.
- [x] 1.2 HTTP tests in `tests/test_forecast_api.py` (store dependency overridden with a recording stub):
  R1 no headers + `run_types=hindcast` → 200 and stub received `run_types` containing `hindcast`;
  R2 `X-User-Role: viewer` and `X-User-Role: garbage` → 200 identical body.
- [x] 1.3 Evidence: `rg -n 'X-User-Role' apps packages services --glob '*.py'` shows reads only in
  `packages/common/request_auth.py` (OpenAPI docs strings / smoke-client senders are not reads).

## 2. #2169 constant-time live-proof comparison

- [x] 2.1 `packages/common/request_auth.py::_internal_live_proof_token_matches`: encode header and token as
  ASCII bytes, `UnicodeEncodeError` → `False`, then `hmac.compare_digest`.
- [x] 2.2 Delete unreferenced helpers `apps/api/auth.py:143-183` and any symbol ruff then reports unused.
- [x] 2.3 Route seam `POST /api/v1/runs/{run_id}/cancel` in `tests/test_monitoring_api.py` (same fixture as
  `test_live_auth_requested_with_wrong_proof_release_blocks`), env `AUTH_BACKEND=oidc`,
  `NHMS_TRUSTED_LIVE_PROOF_MODE=test_internal`:
  (a) token `proof-token`, header sent as raw latin-1 bytes `{"X-NHMS-Internal-Live-Proof": b"\xe4"}` (a `str` value fails client-side in httpx) → server-returned 503 `RELEASE_BLOCKED` (not a client-side exception),
  `policy_decision.auth_mode == "live_idp"`, gateway not called, job still `running`, no 500;
  (b) configured token `pröof-token` (non-ASCII), header `"pröof-token".encode("latin-1")` bytes → same 503 `RELEASE_BLOCKED`, no 500.
  Existing R3/R4 cases stay green.
- [x] 2.4 Evidence: `rg -n 'Live-Proof.*==|== *configured_token' apps packages/common --glob '*.py'` → no matches.

## 3. #2038 lifecycle/active projection drops mesh_properties_json

- [x] 3.1 `packages/common/model_registry.py::_model_public_projection` pops `mesh_properties_json`.
- [x] 3.2 Remove the field (and asymmetry docstring) from `apps/api/openapi_restored_schemas.py`;
  hand-edit `openapi/nhms.v1.yaml` (no generator exists; pinned by `tests/test_openapi_drift.py`); `cd apps/frontend && pnpm generate:api`;
  remove stub injection + mutation in `tests/test_openapi_response_conformance.py`.
- [x] 3.3 Regression (R6/R7): a projection-level test feeding a row whose `mesh_properties_json` carries
  `source_path`, `resolved_source_path`, `package_checksum`, `source_inventory_checksum`, `manifest_uri`
  values, plus route- or result-level coverage for all five outcomes (`already_current`, `blocked`,
  `allowed`, `refused`, audit-persistence 503 via `_lifecycle_audit_persistence_failure_result`) and
  `PUT /models/{id}/active`; assert no raw token appears in the serialized JSON (token scan, as in
  `tests/test_model_registration.py` detail test) and no `mesh_properties_json` key in `model`/`previous_model`.
- [x] 3.4 R8 unchanged: preflight lineage redaction assertion and GET detail/list tests stay green.
- [x] 3.5 node-27 live receipt (non-mutating, recorded deviation from the issue's "run a lifecycle operation":
  production has no user credential path and a real lifecycle POST would mutate production model state):
  `psql` confirms a Basins-imported model's `core.mesh_version.properties_json` is non-empty (not `{}`);
  a read-only script fetches that model's row via `_fetch_model_lifecycle_row`'s SQL in a read-only
  transaction, passes it through `_model_public_projection`, `json.dumps` the result, and asserts no
  `mesh_properties_json` key and none of the row's raw `source_path`/`resolved_source_path`/`package_checksum`
  values appear. Output captured to the PR evidence. No POST, no writes.
  Evidence: node-27 @ `54922f6b`, model `basins_dth_ls_shud`, `properties_json` 7 keys; receipt
  `RECEIPT PASS` (`/home/nwm/tmp/2305/receipt-54922f6b.log`). v1 of the receipt also required the
  `manifest_uri` value to be absent and failed on pre-existing `resource_profile.manifest_uri` (same on
  GET detail); spec delta narrowed accordingly (design.md Risks).

## 4. #1975 route-level error_message path rendering

- [x] 4.1 `apps/api/routes/pipeline.py`: retry 503 `error_message`, `_basin_result.error_message`,
  `_job_payload.error_message` render via `services.orchestrator.public_evidence._public_message`
  (secrets redaction + path/URI token rendering).
- [x] 4.2 Route test (R9) in `tests/test_retry.py`: DB lane, gateway raises
  `RuntimeError("sbatch: error: cannot open <workspace_root>/run-42/job.sbatch for writing")` →
  503 body `error.message` and `details.error_message` contain `[local-path]`; neither
  `str(workspace_root)` nor `str(object_store_root)` occurs anywhere in the body; existing secrets
  assertions green.
- [x] 4.3 R10: the same gateway text through the file lane's retry 503 yields the identical
  `error_message` `sbatch: error: cannot open [local-path] for writing` (assert equality to the literal on both lanes).
- [x] 4.4 R11: persisted DB-lane event `message` / `details.error_message` keep raw text (existing
  assertions stay green).
- [x] 4.5 R12: `GET /api/v1/jobs` with a job whose `error_message` is
  `sbatch: error: cannot open /srv/nhms/workspace/run-42/job.sbatch for writing` → item `error_message` ==
  `sbatch: error: cannot open [local-path] for writing`.
- [x] 4.6 R12b: `GET /api/v1/pipeline/stages` (`_stage_summaries` → `_basin_result`) with a stage job carrying the
  same `error_message` → `basin_results[].error_message` == `sbatch: error: cannot open [local-path] for writing`.

## 5. #1976 spaced scheme-anchored URI classification

- [x] 5.1 `services/orchestrator/public_evidence.py::_sanitize_public_path_or_uri_scalar`: anchored
  `^[A-Za-z][A-Za-z0-9+.-]*://` check before whitespace bail-out; bare `s3:`/`published:` stay after;
  rewrite the comment that cites #1976 as residual.
- [x] 5.2 Rewrite pinned asserts in `tests/test_file_orchestration_journal.py` (`"s3://bucket/my key"` →
  `"[object-uri]"`; `_public_evidence({"note": ...})` → `{"note": "[object-uri]"}`) with a comment stating
  tightening, not widening. R13 incl. `"file:///srv/nhms data/ws"` → `"[uri]"`.
- [x] 5.3 R14: prose assertion stays; implementer temporarily applies the "`://` anywhere → whole" mutant
  and pastes red output for BOTH the prose assertion and
  `test_public_evidence_scalar_classifier_matches_file_provider_classifier` (corpus incl. a prose-with-URI entry), then reverts.
- [x] 5.4 R15: literal fixtures `workspace_dir="file:///srv/nhms data/ws"`,
  `object_store_root="s3://nhms prod/objects"`, `object_store_prefix="s3://nhms-prod/pre fix"` on both DB and
  file lanes' retry 503 bodies pass `_assert_public_runtime_root_resolution`; serialized body contains none of
  `"data/ws"`, `"prod/objects"`, `" fix"`/`"pre fix"` tail tokens.
- [x] 5.5 R16: parity corpus adds spaced URIs; parity compares full scalar sanitizer outcome, not only classifier.

## 6. Verification (Evidence Floor)

- [x] 6.1 Local: `uv run ruff check .`; `openspec validate harden-auth-headers-and-public-redaction --strict --no-interactive`;
  `cd apps/frontend && pnpm check:api-types`.
- [x] 6.2 Local focused pytest (non-DB): `uv run pytest -q tests/test_forecast_api.py tests/test_monitoring_api.py
  tests/test_slurm_gateway_openapi_security.py tests/test_api.py tests/test_auth_policy_matrix.py
  tests/test_openapi_drift.py tests/test_openapi_response_conformance.py tests/test_model_registration.py
  tests/test_retry.py tests/test_file_orchestration_journal.py`.
- [x] 6.3 node-27 exact-head rerun of 6.2 with real DB env (`TMPDIR=/home/nwm/tmp`) + task 3.5 receipt.
  Evidence: node-27 @ `54922f6b`, Python 3.11.15, scratch PG via `NHMS_INTEGRATION_DATABASE_URL`,
  ruff pass, 15 files → `1443 passed`, rc 0 (`/home/nwm/tmp/2305/run-54922f6b*.log`).
- [x] 6.5 Frontend: `cd apps/frontend && pnpm typecheck` exit 0 (types.ts regenerated).
- [x] 6.4 red-proof: new-behavior tests fail against pre-change source (batched, reported by implementer).

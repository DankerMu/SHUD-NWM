## Context

Batch of five S-sized pre-existing defects (#2081 #2169 #2038 #1975 #1976) in one PR. All touch
auth/permissions, secrets, or public redaction helpers, so repair intensity is `high` even though
each diff is a few lines.

Triage:

```text
Issue type: bugfix (batch of 5)
Project profile: NHMS (openspec/project-profile.md)
Blast radius: medium (each S; shared helpers request_auth.py / public_evidence.py)
Fixture level: expanded; repair intensity: high
Upstream suggested level: absent (hand-written issues)
Why:
- auth / secret / token triggers (#2081, #2169)
- public API response field + OpenAPI schema + generated types (#2038)
- shared public renderer used by DB and file lanes (#1975, #1976)
Selected risk packs: Public API; Auth/permissions/secrets; Schema/field names; Legacy compatibility;
  Error handling (503 bodies)
OpenSpec change: harden-auth-headers-and-public-redaction (generated)
```

## Goals / Non-Goals

Goals: close both invariants below on every listed surface with HTTP/route-level regressions.
Non-goals: see proposal.md (no `request_auth`/`auth_policy` semantic change, no nginx, no persisted
text change, no `scheduler_file_providers` change).

## Decisions

- **D1 #2081 delete the gate** (user decision). Rationale: hindcast data is same-shape as public
  forecast series; no spec requires hindcast restriction; production has no user credential path, so
  wiring into `require_action` would close hindcast to everyone. `PROTECTED_OPERATION_OVERRIDES` and
  `test_exactly_fifteen_protected_operations_override_root_security` stay unchanged.
- **D2 #2169** mirror `service_bearer_matches` (ascii encode, `UnicodeEncodeError` → False, then
  `hmac.compare_digest` on bytes). Dead helpers in `apps/api/auth.py:143-183` deleted; unused
  `_TRUTHY`/`_LIVE_AUTH_BACKENDS`/`os` import removed only as ruff reports (confirmed: no test imports them).
- **D3 #2038** pop (not sanitize): zero consumers; avoids a long-term redaction promise over
  arbitrary `POST /mesh-versions` `properties_json`. Contract + generated artifacts change in the same commit.
- **D4 #1975** reuse `public_evidence._public_message` (leaf module, no cycle). Scope includes the
  GET read payload builders `_basin_result` / `_job_payload` in `pipeline.py` (same helper, same field,
  broader audience than the operator-only 503) — recorded scope decision, answering the issue's open
  question. Other `_safe_redacted_text(error.message)` call sites (API error translation of domain
  exceptions) are out of scope: they render exception messages we author, not gateway stderr.
- **D5 #1976** only the anchored `://` check moves ahead of the bail-out, using the same pattern as
  `retry._URI_STYLE_RE`; bare `s3:`/`published:` stay behind. The code comment that points to #1976 is rewritten.

## Invariant Matrix

```text
Invariant I1 (#2081, #2169)
Governing invariant: inbound identity/secret request headers influence authorization only through
  packages/common/request_auth.py's env-gated path, and secret headers compare in constant time.
Source-of-truth identity/contract: request_auth.auth_context_from_request (+ X-NHMS-Internal-Live-Proof
  vs NHMS_INTERNAL_LIVE_PROOF_TOKEN); OpenAPI root `security: []` for forecast-series.
Surfaces:
- Producers: none - headers are client input.
- Validators/preflight: request_auth._internal_live_proof_token_matches; service_bearer_matches (unchanged reference).
- Storage/cache/query: none - no persistence.
- Public routes/entrypoints: GET /api/v1/basin-versions/{bv}/river-segments/{seg}/forecast-series;
  all require_action routes (models/pipeline) and slurm_mutation_auth_context via shared builder.
- Frontend/downstream consumers: frontend never requests hindcast (unchanged); OpenAPI security metadata unchanged.
- Failure paths/rollback/stale state: non-ASCII live-proof header → release-blocked, never 500.
- Evidence/audit/readiness: rg for production `X-User-Role` reads and `==` secret compares.
Regression rows:
- R1 forecast-series?run_types=hindcast, no headers -> 200 (store called with run_types incl. hindcast)
- R2 forecast-series?run_types=hindcast, X-User-Role: viewer / garbage -> 200, header has no effect
- R3 live-proof correct token -> live_idp context (existing test_monitoring_api cases stay green)
- R4 live-proof wrong token / non-ASCII "\xe4" header -> release-blocked, no TypeError/500
- R5 unchanged sibling: protected mutation routes' OpenAPI override set and count test unchanged

Invariant I2 (#2038, #1975, #1976)
Governing invariant: public response bodies never carry raw local paths, package/source checksums, or
  post-space URI tails when a sibling field of the same body renders them; persistence keeps raw text.
Source-of-truth identity/contract: services/orchestrator/public_evidence renderer
  ([local-path]/[uri]/[object-uri]/[redacted]); model_registry public projections.
Surfaces:
- Producers: workers/model_registry/basins_registry_import (mesh properties shape);
  retry._record_retry_submission_failure (raw error_message persisted).
- Validators/preflight: public_evidence._sanitize_public_path_or_uri_scalar, _public_message.
- Storage/cache/query: model_registry lifecycle SQL (mv.properties_json AS mesh_properties_json) unchanged;
  pipeline_job.error_message / pipeline_event raw text unchanged.
- Public routes/entrypoints: POST /models/{id}/lifecycle; PUT /models/{id}/active; POST /runs/{id}/retry 503;
  GET job/basin-result payloads in pipeline.py.
- Frontend/downstream consumers: apps/frontend/src/api/types.ts regenerated (no runtime consumer of
  mesh_properties_json); error_message remains a string.
- Failure paths/rollback/stale state: 5 lifecycle outcomes incl. 503 audit-persistence failure body.
- Evidence/audit/readiness: preflight.lineage.mesh_properties stays redact_audit_payload-shaped.
Regression rows:
- R6 lifecycle allowed/already_current/blocked/refused + audit-503 body with 5-key mesh properties -> no raw token in JSON, no mesh_properties_json key
- R7 PUT /models/{id}/active -> same as R6
- R8 unchanged: GET /models/{id} detail, GET /models list, POST /preflight lineage redaction
- R9 retry 503 (DB lane) gateway error with /srv/nhms/workspace/run-42/job.sbatch -> error.message and details.error_message contain [local-path], no workspace/object-store root; secrets assertions still green
- R10 DB and file lane 503 error_message both equal "sbatch: error: cannot open [local-path] for writing"
- R11 persisted pipeline_event message / details.error_message keep raw text
- R12 GET /api/v1/jobs item error_message with /srv/nhms/workspace/run-42/job.sbatch -> "sbatch: error: cannot open [local-path] for writing"
- R12b GET /api/v1/pipeline/stages basin_results[].error_message -> same literal
- R13 scalar "s3://bucket/my key" -> "[object-uri]"; "file:///srv/nhms data/ws" -> "[uri]"
- R14 prose "see s3://x for details" -> "see [object-uri] for details" (unchanged); mutant "`://` anywhere" must go red
- R15 workspace_dir="file:///srv/nhms data/ws", object_store_root="s3://nhms prod/objects", object_store_prefix="s3://nhms-prod/pre fix" on DB and file lanes pass _assert_public_runtime_root_resolution, no "data/ws"/"prod/objects"/"fix" tail
- R16 parity corpus with spaced URIs: public_evidence vs scheduler_file_providers agree
```

## Boundary-surface checklist

- Shared helper roots: `request_auth.py` (one function), `public_evidence.py` (one function) — changed.
- Public entrypoints: forecast-series, models lifecycle/active, runs retry, GET job listings — changed behavior.
- Read surfaces: `_basin_result`, `_job_payload` — changed (D4).
- Unchanged downstream consumers: `_model_asset_detail`, `list_models`, `_build_model_operation_preflight`,
  file-lane journal renderer, `scheduler_file_providers._sanitize_file_provider_scalar`, OpenAPI protected override set.

## Risks / Trade-offs

- Hindcast becomes explicitly anonymous (it was effectively anonymous via header already). Accepted by user.
- `error_message` on GET job listings loses absolute path detail for operators; raw text remains in DB/logs.
- Removing `mesh_properties_json` is a response-narrowing change; zero frontend consumers verified by grep.

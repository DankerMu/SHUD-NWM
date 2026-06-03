## 1. Production State Machine Contract

- [ ] 1.1 Add characterization tests for current retry/cancel behavior that demonstrate `pending` and `cancelled` production enum incompatibilities.
- [ ] 1.2 Decide status policy for retry and cancel: add enum values through a forward migration or map to existing legal enum values with explicit response/event metadata.
- [ ] 1.3 Implement the selected retry status policy in `services/orchestrator/retry.py`, OpenAPI schemas, JSON schemas, and frontend generated types.
- [ ] 1.4 Implement the selected cancel status policy in `apps/api/routes/pipeline.py`, including changed/preserved/not-found response states, terminal preservation, and idempotent Slurm cancellation.
- [ ] 1.5 Add PostgreSQL-oriented or migration-backed tests covering final retry/cancel behavior against enum-valid `hydro_run` and `forecast_cycle` statuses.

## 2. Real Slurm Gateway Contract

- [ ] 2.1 Add route-level RealSlurmGateway tests for `/api/v1/slurm/jobs` and `/api/v1/slurm/job-arrays` proving nested `manifest` fields reach template rendering, top-level overrides work, and lower-case `object_store_root`/`object_store_prefix` export to worker env.
- [ ] 2.2 Define and implement an explicit array submit request contract for `job_type`, `cycle_id`, `stage_name`, `tasks`, and `manifest`.
- [ ] 2.3 Align legacy/analysis single-job submission with real gateway templates or implement a constrained validated script mode, with tests for unsupported legacy `job_type` validation before submission.
- [ ] 2.4 Add and test Slurm raw-state to stable `error_code` mapping for timeout, node failure, preemption, out-of-memory, and unknown failures.
- [ ] 2.5 Persist and test poll timeout as a failed pipeline job/run/cycle event and route it through retry eligibility.
- [ ] 2.6 Persist or reconstruct real Slurm job metadata needed for post-restart log lookup, with restart simulation tests.
- [ ] 2.7 Implement and test array log aggregation for `%A_%a.out` and `%A_%a.err`, preserving task id context and ensuring missing task logs do not discard existing logs.
- [ ] 2.8 Update Slurm template ownership docs so `infra/sbatch`, `workers/sbatch_templates`, orchestrator defaults, and README do not contradict each other.

## 3. Flood Tile Delivery Contract

- [ ] 3.1 Decide release format for flood return-period map data: true MVT/PBF or GeoJSON fallback.
- [ ] 3.2 If MVT is selected, implement backend vector tile bytes, `application/x-protobuf`, source-layer naming, OpenAPI binary protobuf schema, and z/x/y bbox filtering.
- [ ] 3.3 If GeoJSON is selected, rename or document the endpoint as JSON, define `.pbf` compatibility behavior, update OpenAPI schema, avoid misleading z/x/y tile semantics, and change the frontend MapLibre source type accordingly.
- [ ] 3.4 Define and implement flood tile feature properties: segment id, displayed value, unit, quality flag, return period, and warning level.
- [ ] 3.5 Add backend tests asserting flood tile content type, payload decodability or JSON structure, spatial filtering semantics, and not-frequency-ready error envelope.
- [ ] 3.6 Add frontend tests asserting `FloodReturnPeriodLayer` source type matches the selected backend format and does not render a broken layer on not-ready errors.
- [ ] 3.7 Update `docs/spec/04_api_design.md` and tile module docs with the selected tile contract, `map.tile_cache` versus `map.tile_asset` naming, feature properties, and performance caveats.

## 4. API Contract Convergence

- [ ] 4.1 Add an OpenAPI/FastAPI drift test comparing public path and method sets, with explicit allowlists only for internal or deferred routes.
- [ ] 4.2 Fix OpenAPI prefix strategy so `servers` and paths cannot double-prefix `/api/v1`.
- [ ] 4.3 Add request/response shape contract tests for `GET /api/v1/data-sources` and related list endpoints, then align implementation and OpenAPI envelope/page schema.
- [ ] 4.4 Add request body contract tests for `PUT /api/v1/models/{model_id}/active`, then align body naming with OpenAPI and compatibility handling if both `active` and `active_flag` are temporarily accepted.
- [ ] 4.5 Add forecast-series contract tests for `include_analysis`, `run_types`, raw/enveloped response policy, and frontend store parsing; then align OpenAPI and implementation.
- [ ] 4.6 Reconcile documented-but-missing public read endpoints in batches: lineage, layers, model detail, station series, river-network tiles, hydro tiles, and met tiles.
- [ ] 4.7 Reconcile implemented-but-undocumented public routes in batches: state snapshots, Slurm endpoints, best-available, mesh versions, river networks, and crosswalk APIs.
- [ ] 4.8 Update frontend API base configuration so README, `.env.example`, and `client.ts` describe one executable base URL behavior.
- [ ] 4.9 Regenerate `apps/frontend/src/api/types.ts` and add/refresh the generated-type freshness check.

## 5. Verification and Issue Traceability

- [ ] 5.1 Run `openspec validate m7-second-review-remediation --strict` and `openspec status --change m7-second-review-remediation`; resolve incomplete artifacts.
- [ ] 5.2 Run three-way Codex review for design consistency, spec completeness, and task executability; fix all P0 findings in this change.
- [ ] 5.3 Run backend verification: ruff, targeted enum/Slurm/tile/API contract tests, and full `.venv/bin/python -m pytest -q`; record which tests use PostgreSQL/PostGIS/real Slurm contracts versus mocks.
- [ ] 5.4 Run frontend verification: typecheck, unit tests, production build, API type freshness check, and flood layer format tests.
- [ ] 5.5 Create one Epic issue and 4-6 delivery-oriented child issues linked to this OpenSpec change.
- [ ] 5.6 Record final verification output and issue links in the change or implementation tracking document.

## Issue Traceability

- Epic: https://github.com/DankerMu/SHUD-NWM/issues/96
- State machine: https://github.com/DankerMu/SHUD-NWM/issues/97
- Real Slurm submit/template: https://github.com/DankerMu/SHUD-NWM/issues/98
- Real Slurm failure/logging: https://github.com/DankerMu/SHUD-NWM/issues/99
- Flood tile delivery: https://github.com/DankerMu/SHUD-NWM/issues/100
- API/OpenAPI convergence: https://github.com/DankerMu/SHUD-NWM/issues/101
- Frontend/config/final verification: https://github.com/DankerMu/SHUD-NWM/issues/102

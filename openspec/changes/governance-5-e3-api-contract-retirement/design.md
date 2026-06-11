## Context

The repository contains compatibility API paths created during QHH/MVP and M25/M26 transitions. Some names look legacy, but they remain active shared contracts because frontend bootstrap, generated types, docs, or tests may still depend on them.

API contraction is riskier than doc cleanup. It must be handled as a contract migration with inventory, replacement, staged deprecation, OpenAPI/type synchronization, and compatibility evidence.

## Goals / Non-Goals

**Goals:**

- Inventory legacy-looking API endpoints and classify them as active, deprecated, replacement-ready, or removal-ready.
- Migrate current frontend/backend consumers before contracting OpenAPI.
- Preserve backward compatibility until explicit deprecation evidence exists.
- Keep generated frontend types aligned with OpenAPI changes.

**Non-Goals:**

- No immediate endpoint deletion.
- No frontend old page retirement; that is Governance-5 E2.
- No database migration unless a later contract issue proves it is required.
- No removal of shared contract directories such as `openapi/`, `schemas/`, `db/migrations/`, or `packages/common/`.

## Decisions

### D1. Consumer inventory comes before deprecation

An endpoint can be marked deprecated only after direct code, generated type, docs, and test consumers are known. Removal can happen only after current consumers are migrated.

### D2. OpenAPI remains the contract source

Any API change must update `openapi/nhms.v1.yaml`, generated frontend types, API tests, and frontend build/type checks together.

### D3. Compatibility endpoints remain until replacement evidence exists

Endpoints such as the QHH MVP latest-product compatibility route are not dead code while current bootstrap or docs still rely on them.

## Risks / Trade-offs

- **Risk: breaking display bootstrap.** Mitigation: require consumer migration and frontend verification before endpoint contraction.
- **Risk: OpenAPI/type drift.** Mitigation: require generated type checks and contract tests in every implementation issue.
- **Risk: confusing deprecation with cleanup.** Mitigation: separate API contract retirement from display/docs cleanup and layer-inversion cleanup.

## Migration Plan

1. Inventory legacy-looking API routes and all consumers.
2. Decide replacement endpoints or compatibility policy.
3. Migrate consumers.
4. Add deprecation docs or headers only where appropriate.
5. Update OpenAPI and generated types.
6. Remove endpoint only in a later issue after compatibility evidence.

## Open Questions

- Which compatibility endpoints are still required by external users beyond repository consumers.

## Issue Fixtures

### #411 legacy-looking API contract inventory slice

Fixture level: expanded

Project profile: NHMS

Change surface:

- API route and OpenAPI inventory for legacy-looking forecast/latest-product compatibility contracts.
- Repository consumer inventory across backend tests, frontend display/bootstrap code, generated frontend types, docs, and runbooks.
- Governance documentation or inventory artifact that classifies each candidate endpoint.
- E3 OpenSpec task/evidence rows only.

Must preserve:

- No endpoint deletion, route behavior change, OpenAPI contraction, generated type regeneration, or API response-shape change.
- No frontend/node-27 implementation changes; frontend files may be read for consumer evidence only.
- Active compatibility endpoints stay active while repository consumers exist.
- `/api/v1/mvp/qhh/latest-product` remains compatible until #414/#415/#416 prove migration and contraction readiness.

Must add/change:

- Identify candidate legacy-looking API endpoints and classify each as `active`, `deprecated`, `replacement-ready`, or `removal-ready`.
- For each candidate, record direct route definition, OpenAPI path, generated type presence, backend/test consumers, frontend consumer evidence, docs/runbook references, owner role, and follow-up issue.
- Explicitly distinguish current active compatibility contracts from dead code.
- Identify backend follow-up needs for #413 and node-27/frontend follow-up needs for #414 without performing those migrations.

Risk packs considered for #411:

- Public API / CLI / script entry: selected - inventory covers public HTTP routes and OpenAPI paths.
- Config / project setup: not selected - no runtime config changes.
- File IO / path safety / overwrite: not selected - no file IO behavior changes beyond docs.
- Schema / columns / units / field names: selected - route/query/response contract names must be identified without changing them.
- Auth / permissions / secrets: not selected - no auth or credential surface changes.
- Concurrency / shared state / ordering: not selected - no runtime state transition changes.
- Resource limits / large input / discovery: selected - search/inventory must cover generated, test, frontend, and docs surfaces without overclaiming completeness beyond repository scope.
- Legacy compatibility / examples: selected - compatibility endpoints are the main risk surface.
- Error handling / rollback / partial outputs: not selected - no runtime failure behavior changes.
- Release / packaging / dependency compatibility: not selected - no dependency/package changes.
- Documentation / migration notes: selected - issue output is governance inventory and follow-up mapping.
- Published NHMS artifacts / display identity: selected - latest-product identity contracts feed display/bootstrap consumers.
- PostGIS / TimescaleDB domain behavior: not selected - no DB query behavior changes.
- Other NHMS domain packs: not selected - no geospatial, forcing, SHUD, Slurm, provider, manifest, or numerical runtime behavior changes.

Required #411 evidence:

- Repository search covers route definitions, OpenAPI, generated frontend types, backend tests, frontend consumers, E2E/mocked specs, and docs/runbooks.
- Inventory artifact lists each candidate endpoint with status, consumers, replacement/deprecation stance, owner issue, and removal readiness.
- No endpoint is marked removal-ready while current consumers remain.
- OpenSpec validation passes.

Non-goals for #411:

- No API endpoint removal or deprecation headers.
- No OpenAPI contraction or frontend type regeneration.
- No frontend/node-27 migration; #414 owns frontend implementation.
- No backend consumer migration; #413 owns backend implementation if inventory finds one.
- No live API receipt requirement; this is repository contract inventory.

Review focus:

- Candidate list is broad enough for legacy-looking API contracts in the current repository.
- Consumer matrix does not miss generated types, tests, docs, or frontend display/bootstrap users.
- Status labels do not confuse active compatibility with dead code.
- Follow-up issues map backend, node-27 frontend, OpenAPI/type sync, and removal/defer decisions without doing them in this slice.

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

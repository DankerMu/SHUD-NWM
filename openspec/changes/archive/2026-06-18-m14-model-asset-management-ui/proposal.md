## Why

M9 delivered Basins-backed model registry and frontend modelAssets store tests, but design §14 / effect image 7 still lacks a model asset product page. Model administrators need readonly browsing of basin/model versions, active status, lineage, dependencies, products, and spatial context.

## What Changes

- Expose a `/system/model-assets` route gated for `model_admin` and `sys_admin`; treat `version_admin` in the design doc as a legacy alias requiring explicit mapping if used.
- Build basin/model tree with search/filter, active model highlighting, and safe empty/error states.
- Render six summary KPI cards, selected model metadata, source/package lineage, activation status, checksums, mesh/river/calibration dependencies, version history, dependency graph, product asset list, and mini map with geometry budget states.
- Reuse `/api/v1/models` and `/api/v1/models/{model_id}` first; add graph/assets endpoints only after endpoint decision note.
- Preserve public URI/path redaction and do not expose local absolute source paths or URI userinfo/query/fragment in top-level fields, nested `resource_profile`, graph nodes, product links, tooltips, screenshots, or test fixtures.
- Update `progress.md` to mark effect image 7 as readonly UI scope while keeping mutating operations deferred.

## Capabilities

### New Capabilities

- `model-asset-navigation-rbac`
- `model-asset-tree-browser`
- `model-asset-detail-summary`
- `model-asset-version-lineage`
- `model-asset-products-map`

## Impact

- Frontend nav/RBAC, modelAssets store, possible read-only API/OpenAPI additions for graph/assets, visual conformance work later in M15.
- Existing model registry API consumers and M11 overview/forecast route handoffs must remain compatible.
- Screenshot/evidence artifacts may be generated locally, but product commits should not include volatile test output.

## Non-Goals

- Creating/editing model packages.
- Changing activation semantics or adding mutating admin operations.
- Leaking raw local paths or sensitive URI components.
- Adding `version_admin` as a production role unless it is explicitly mapped to existing `model_admin`/`sys_admin` semantics in code and tests.
- Full geometry publication or MVT performance work; that remains M16.

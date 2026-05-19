## Context

M14 model asset management UI follows the completed M11 overview/basin drill-down delivery and turns a documented product gap into implementable, testable work. Existing production-like closure and M11 behavior must remain stable.

## Fixture

Fixture level: expanded
Project profile: other
Repair intensity: high
Blast radius: high user-visible admin route with RBAC and sensitive path/URI redaction. No write/delete/publish behavior is in scope, but the page consumes model registry metadata that can contain local paths and URI credentials.

Mandatory expanded triggers:
- Public admin route and navigation entry: `/system/model-assets`.
- RBAC/access-control behavior for `viewer`, `operator`, `model_admin`, and `sys_admin`.
- Schema/field/unit contracts for model registry list/detail responses and nested `resource_profile`.
- Sensitive path/URI redaction across top-level fields, nested lineage, graph nodes, product assets, tooltips, tests, and screenshots.
- Geospatial mini-map geometry budgets and unavailable/over-budget states.
- Legacy compatibility for existing modelAssets store/API tests and existing model registry consumers.
- Browser/screenshot evidence for effect image 7 page state.

Change surface:
- Frontend route/nav/RBAC gate for `/system/model-assets`.
- `modelAssets` store/view models for tree grouping, search/filter, active highlighting, selected model, stale detail clearing, redaction, and bounded geometry/product surfaces.
- Readonly page components for tree, KPI summary, metadata, lineage, dependency graph, product assets, mini map, and degraded states.
- API/OpenAPI/types only if new read-only graph/assets endpoints are added.
- `progress.md` and local screenshot/evidence artifacts.

Must preserve:
- Existing `/api/v1/models` and `/api/v1/models/{model_id}` response compatibility and frontend store tests.
- Existing overview, basin, forecast, flood-alert, meteorology, segment, and monitoring routes.
- Non-admin users must not trigger sensitive detail fetches through the page.
- UI, tests, screenshots, and evidence must never display local absolute paths or URI userinfo/query/fragment values.

## Design Decisions

- Accepted role taxonomy is `model_admin` and `sys_admin`; `version_admin` is documented as a legacy design term unless explicitly added elsewhere.
- First implementation is readonly; activation and package mutation remain separate audited backend work.
- Public projection must use existing model response redaction rules and tests for `source_path`, `resolved_source_path`, URI-like fields, nested `resource_profile.source_lineage`, graph/product labels, and screenshot-visible text.
- Dependency graph can be derived client-side from model detail fields unless an endpoint decision proves a server graph endpoint is required. If new endpoints are added, OpenAPI and type freshness checks are mandatory.
- Product asset list is readonly metadata only; download/copy/open actions must not expose unredacted private URIs.
- Mini map may use contract geometry fixtures or existing geometry fields, but must enforce point/vertex/feature budgets and show unavailable/over-budget states rather than rendering unsafe large geometry.
- `version_admin` is not added as a new role in this issue. Any visual copy may mention the legacy design term only as mapped to `model_admin`/`sys_admin`.

## Test Oracles

KPI card contract:
- Render exactly six cards in this order: `流域版本`, `河网版本`, `网格版本`, `率定版本`, `SHUD / 模型`, `河段 / 面积`.
- Field mapping: basin version from `basin_version_id`; river network from `river_network_version_id`; mesh from `mesh_version_id` plus `mesh_checksum` when present; calibration from `calibration_version_id`; SHUD/model from `shud_code_version` plus `model_id`; segment/area from `segment_count` and optional `area_km2` in `resource_profile`.
- Missing or null values render `暂不可用`; the UI must not invent placeholder IDs, checksums, relationships, or areas.

Redaction contract:
- Local absolute paths such as `/volume/data/nwm/Basins/qhh`, Windows absolute paths such as `C:\nwm\Basins\qhh`, and `file://` URIs are represented as `null` in store/view-model path fields and as `受限来源` in UI text.
- URI userinfo, query, and fragment are removed before display or copying: `https://user:pass@assets.example.test/pkg?token=abc#frag` becomes `https://assets.example.test/pkg`; `s3://key:secret@nhms/private/package?sig=x#frag` becomes `s3://nhms/private/package`.
- The same sanitizer applies recursively to top-level model fields, nested `resource_profile`, derived graph nodes/edges, product asset labels/targets, copy/open affordances, tooltips, tests, screenshots, and PR evidence.

Resource budgets:
- Product asset display budget is 12 items. More than 12 products renders the first 12 stable IDs/checksums plus `仅显示前 12 个资产`.
- Mini-map geometry budget is 50 features and 2,000 total coordinate vertices. Over-budget fixtures render `空间几何超出预览预算`; missing geometry renders `暂无空间预览`.
- Tree/list rendering must use bounded frontend projections of the API page returned by `/api/v1/models`; this issue does not introduce unbounded client discovery.

## Dependency Order

- RBAC/navigation before tree browser.
- Tree/store before detail summary and lineage.
- Detail data before graph/products/map.

## Risks and Mitigations

- Risk: exposing sensitive paths. Mitigation: redaction tests and no raw local absolute paths in UI.
- Risk: role drift. Mitigation: explicit role map and denied-state tests.
- Risk: missing graph fields. Mitigation: endpoint decision note and degraded graph state.
- Risk: stale detail after filter/search/URL changes. Mitigation: selected model restoration and stale-detail clearing tests.
- Risk: large geometry/product lists. Mitigation: explicit feature/product budgets and unavailable/truncated states.

## Risk Packs Considered

- Public API / CLI / script entry: selected - new public frontend admin route and possible read-only API additions.
- Config / project setup: not selected - no new deployment/configuration is required.
- File IO / path safety / overwrite: selected - UI consumes path/URI fields and must redact them; no runtime file reads/writes are added.
- Schema / columns / units / field names: selected - model registry fields, checksums, dependency fields, and nested resource profile drive the UI.
- Geospatial / CRS / shapefile sidecars: selected - mini map consumes basin/river geometry-like data and must enforce budgets; no shapefile sidecars are added.
- Time series / forcing / temporal boundaries: not selected - no time-series product data is added beyond version history timestamps.
- Numerical stability / conservation / NaN: not selected - no solver math is computed.
- Solver runtime / performance / threading: not selected - no SHUD runtime/threading behavior changes.
- Resource limits / large input / discovery: selected - tree/detail/product/geometry rendering must be bounded and handle empty/over-budget states.
- Legacy compatibility / examples: selected - existing modelAssets store/API tests and M11 handoff semantics must remain stable.
- Error handling / rollback / partial outputs: selected - denied access, list/detail failure, partial lineage, unavailable geometry/products, and stale detail states must be explicit.
- Release / packaging / dependency compatibility: selected - avoid unnecessary dependencies; build/type/test must remain green.
- Documentation / migration notes: selected - `progress.md`, endpoint decision, and screenshot evidence must be updated.

## Boundary Surface Checklist

- Public entrypoints: `/system/model-assets`, nav item, RBAC gate, optional API endpoints if added.
- Read surfaces: model list/detail, `resource_profile`, URI/path fields, dependency fields, product assets, geometry fields.
- Write/delete/overwrite surfaces: none; mutating activation/package operations are out of scope.
- Producer/consumer evidence boundaries: redacted API/store output, graph/product/map view models, screenshot/evidence artifacts.
- Stale-state/idempotency boundaries: URL-selected model, search/filter changes, list reload, detail fetch failure, selected model no longer in filtered tree.
- Unchanged downstream consumers: M11 overview/model handoffs, forecast model selectors, existing backend/OpenAPI model tests.

## Verification

- OpenSpec strict validation.
- Frontend store/component/RBAC tests.
- API/OpenAPI/type checks if new graph/assets endpoints are added.
- Browser screenshot evidence for supported desktop viewport showing admin tree/detail and safe redacted values.
- Redaction regression tests with local absolute paths, URI userinfo, query, and fragment in top-level and nested fields.

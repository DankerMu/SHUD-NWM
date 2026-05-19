## 1. RBAC and Navigation
- [x] 1.1 Add `/system/model-assets` route and nav entry gated to `model_admin`/`sys_admin`; document any `version_admin` alias decision.
- [x] 1.2 Add route tests for admin access, viewer/operator denial, hidden/disabled navigation, and proof that denied users do not fetch model detail.
- [x] 1.3 Keep `version_admin` out of runtime roles unless explicitly mapped to `model_admin`/`sys_admin` with tests.

## 2. Tree and Store
- [x] 2.1 Extend/reuse `modelAssets` store for basin/model tree grouping, search, filters, active highlighting, selected model state, and stale detail clearing.
- [x] 2.2 Add tests for empty registry, list failure, detail load failure, active/inactive/all filtering, search no-results, selected model no longer in filtered tree, and URL-selected model restoration.
- [x] 2.3 Add redaction tests for local absolute paths and URI userinfo/query/fragment in top-level model fields and nested `resource_profile`/graph/product surfaces.

## 3. Detail, Lineage, Products
- [x] 3.1 Implement six KPI cards and selected model metadata using public `/api/v1/models/{model_id}` fields.
- [x] 3.2 Implement redacted source/package lineage, version history timeline, dependency graph, product asset list, and degraded partial-lineage states without exposing raw paths or private URI parts.
- [x] 3.3 Implement mini map with basin boundary/river geometry budgets and unavailable/over-budget states; no unsafe large geometry rendering.
- [x] 3.4 Add endpoint decision note before adding graph/assets APIs; update OpenAPI/types/tests if added.
- [x] 3.5 Add tests for missing checksums, missing dependency nodes, unavailable products, over-budget product/geometry lists, and safe copy/open affordances if present.

## 4. Validation
- [x] 4.1 Run OpenSpec strict validation, frontend tests, `tsc --noEmit`, build, and API type freshness if API changes.
- [x] 4.2 Capture screenshot evidence for model asset tree/detail at supported desktop viewports; screenshots must not contain raw local paths, URI credentials, query strings, or fragments.
- [x] 4.3 Update `progress.md` with readonly asset-management scope and deferred mutating operations.

## Evidence Matrix

- RBAC route: with role `viewer` or `operator`, opening `/system/model-assets?modelId=basins_qhh_shud` renders `权限不足`, hides/disables the nav entry, and records zero `/api/v1/models/{model_id}` calls; with `model_admin` or `sys_admin`, the route shell and nav entry are visible and list loading is allowed.
- Tree/store: list fixture with `basins_qhh_shud` active and `basins_heihe_shud` inactive groups by `basin_name`, active filter `true|false|all` returns the expected model IDs, search `qhh` shows only QHH, search `missing` shows `无匹配模型`, empty page shows `暂无模型资产`, and list/detail failures clear stale detail and show the API error message.
- URL/state: `/system/model-assets?modelId=basins_qhh_shud` restores QHH detail after list load; changing search/filter so QHH is excluded clears or marks the detail as out-of-filter and does not keep showing stale QHH metadata.
- KPI/detail: selected QHH detail renders six KPI cards in order `流域版本`, `河网版本`, `网格版本`, `率定版本`, `SHUD / 模型`, `河段 / 面积`; missing IDs/checksums/area render `暂不可用`.
- Redaction: `/volume/data/nwm/Basins/qhh`, `C:\nwm\Basins\qhh`, and `file:///volume/data/nwm/Basins/qhh` become `null` in restricted store path fields and `受限来源` in UI; `https://user:pass@assets.example.test/pkg?token=abc#frag` becomes `https://assets.example.test/pkg`; `s3://key:secret@nhms/private/package?sig=x#frag` becomes `s3://nhms/private/package`.
- Detail/lineage: partial model detail missing mesh/river/calibration/checksum fields renders missing graph nodes as `暂不可用` and does not invent relationships.
- Resource limits: 13 product assets render 12 displayed rows and `仅显示前 12 个资产`; geometry with 51 features or more than 2,000 vertices renders `空间几何超出预览预算`; missing geometry renders `暂无空间预览`; no unbounded rendering.
- Compatibility: existing modelAssets API/store tests, AppRoutes tests, and current model registry consumers remain green.
- Dependency/API: no dependency/API change -> document reuse; new endpoint/dependency -> rationale plus OpenAPI/type/install/build/test evidence.

## Non-Goals / Explicit Exclusions

- No model package create/edit/delete/publish operations.
- No active model switching or mutation workflows.
- No production role named `version_admin` unless explicitly mapped and tested.
- No full national geometry publication or MVT performance work.
- No display of raw local paths or sensitive URI components in UI, tests, screenshots, or PR evidence.

## Why

`master` 上前端类型检查是红的且**没有任何门拦它**：`cd apps/frontend && pnpm exec tsc --noEmit -p tsconfig.app.json` 报 179 errors / 36 files（本分支实测复现）。`pnpm build` 是裸 `vite build`（逐文件转译，不做全程序类型检查），`.github/workflows/ci.yml` 的 `frontend-build` job 只跑 `install && build && test`，`check:api-types` 只比对重生成的 `types.ts` 与 yaml、不编译应用代码。结果是 API↔前端类型契约在整个生产展示面（`/`、`/ops`、`/monitoring`、`/system/model-assets`）上事实失效两个月，且正阻塞 #2003 Epic 下至少 5 个 issue 的 Evidence Floor。

`b97c16e2`（2026-06-30）把 `tests/test_openapi_drift.py` 从 1042 行压到 239 行、改为全文档等值断言（`assert static_spec == app.openapi()`），并据此从运行时重生成 `openapi/nhms.v1.yaml`。该断言只有把静态 yaml 对齐到 `app.openapi()` 才能满足，于是**所有没有运行时支撑的具名 schema 被剥离**。

关键考古结论（推翻 issue #2022 的前提）：这些 schema **从来没有** Pydantic 类或 `response_model=` 支撑。`b97c16e2^` 全仓仅有 7 处 `response_model=`，全部在已被该 commit 删除的 `apps/api/routes/flood_alerts.py`（:413,467,556,606,671,764,868），产出 `LayerListResponse` / `LayerValidTimesResponse` / `TileFeatureCollection` / `dict[str, Any]`，**无一对应这批名字**。旧的 1042 行 drift 测试只做路由路径对等 + 一小批**不相关**名字的抽查等值，从未覆盖它们。它们是**手工维护、从未被校验过的静态 yaml 文档**。因此本次是**如实补写（authoring）**，不是 revert，也不是恢复曾经存在过的后端校验。

## What Changes

- 补齐 **25 个 OpenAPI 具名 schema**。起点是 issue 列的 23 个种子名，传递闭包（`allOf`/`oneOf`/`anyOf`/`items`/`additionalProperties`/`properties` 统一遍历）得 33 个，再按实测分流掉 8 个：
  - **3 个转前端本地**：`LineageResponse` 及其独家子节点 `ForcingVersion` / `QcResult`（后两者在旧 yaml 里的唯一父节点就是 `LineageResponse`，实测确认）。理由见下条。
  - **1 个只重指、不补写**：`ModelLifecycleRequest` 在旧 yaml 里**只被 requestBody 引用**（`/preflight` 与 `/lifecycle` 两处），今天这两个 requestBody 已由 FastAPI 从 `models.py:143` 的 `ModelLifecyclePayload` 自动生成。它不是响应体，补写它既凑不进"每个新增组件都必须被某条 operation 的响应 `$ref` 到"的规则，也只会造出 `ModelLifecyclePayload` 的重复孤儿组件。做法是把前端消费方重指过去。
  - **4 个整支删除**：`HydroRun.product_quality` 字段今天已不存在（`product_quality` 在 `apps/ packages/ services/ workers/` 与整个前端均**零命中**，当前 yaml 的 `QhhLatestQuality` 也不再引用它），随之删掉只经它入闭包的 `QhhLatestProductQuality` / `FloodReturnPeriodProductQuality` / `FloodReturnPeriodQualityState`；`frequency_thresholds` 同样零命中（`_empty_forecast_response` 只返回 `{segment_id, issue_time, unit, series}`），它出现在 **`RiverSeriesResponse` 与 `SplicedForecastResponse` 两处**（前者还把它列进了 `required`），两处都删，随之删掉 `FloodFrequencyThresholds`。`RunStatus` 的 `frequency_done` 枚举值同理剔除。
- **形状权威是当前 store / handler 实现，不是旧 yaml。** `b97c16e2^:openapi/nhms.v1.yaml` 只作**起草稿**：它记录了前端消费的字段名与结构，但它本身是从未被校验过的文档（见上），既可能多写今天不存在的字段（已实测到 3 处），也可能漏写今天已新增的字段（已实测：`SeriesSegment` 运行时发 `variable`，`forecast_store.py:4031`，旧 schema 未声明）。每个 schema 必须从对应 store 函数实际构造的 key 集合反推。

- 载体统一为 `apps/api/openapi_patching.py` 家族的**手写 JSON-Schema 注入 + operation 响应改写**（仓库既有模式，见 `_patch_station_series_openapi` / `_set_operation_response_glue`）。**不采用 `response_model=`**：实测证明它会按模型过滤响应字段（多余字段被丢弃、形状不符时 500），违反 issue 自身"只补类型声明，不改响应内容"的边界，且这些是生产展示面路由。
- 所有新增可空字段一律写 OpenAPI 3.1 原生空联合（`anyOf`/type union），**不用 `nullable: true`** —— 后者会顶穿 `tests/test_openapi_31_contract.py:20` 的 `BASELINE_NULLABLE_COUNT = 111`（该计数只统计手写 patch 注入的 3.0 风格 `nullable`）。
- `LineageResponse` / `ForcingVersion` / `QcResult` 走**第四种归宿**（issue 只列了三种）：`LineageResponse` 的后端路由 `/api/v1/lineage/river-point` **从未实现过**（`b97c16e2^` 的旧测试自己把它标在 `DEFERRED_ROUTE_REASONS` 里，今天仍不存在），前端每次调用必然 404、被 `overviewData.ts` 的 try/catch 吞成 `'河段追溯暂不可用'`。没有路由就不可能有 OpenAPI schema，因此在前端就地声明本地 interface，**行为逐字不变**（仍 404、仍进 `partialErrors`）；后端缺口另行立 issue。
- 补 (b) 配置面：`@types/node` + `@types/geojson`（后者当前仅靠 `maplibre-gl` 的传递依赖提供 `GeoJSON` 全局命名空间，无直接 devDependency 保底）。
- 补门：`apps/frontend/package.json` 增加 `typecheck` script，并在 `.github/workflows/ci.yml` 的 `frontend-build` job 里作为 `pnpm build` **之前的独立命名 step** 执行。

## Capabilities

### Modified Capabilities
- `api-contract-alignment`：`Frontend uses generated contracts for API calls` 增加一条场景 —— 前端消费的响应体必须由**具名** components schema 描述，而不是匿名 `type: object, additionalProperties: true`；`components['schemas'][X]` 形式的消费引用必须能在生成的 `types.ts` 里解析到。

### New Capabilities
- `ci-contract-baseline` 增加 `Frontend type check MUST gate the frontend build`：`frontend-build` job 必须在 build 之前以独立 step 执行全程序 `tsc --noEmit`，且该 step 不得被 skip 或降级为警告。

## Impact

- 后端：`apps/api/openapi_patching.py`（+ 新增的 schema 定义模块），不改任何路由 handler 的返回值构造。**零运行时行为变更**。
- 契约产物：`openapi/nhms.v1.yaml` 从运行时重生成；`apps/frontend/src/api/types.ts` 由 `pnpm generate:api` 重生成。
- 前端：14 个消费方文件的 `components['schemas'][X]` 引用重新可解析；`LineageResponse` 消费方改为本地 interface。
- 门控：`.github/workflows/ci.yml` `frontend-build` job（`ci.yml:395` 附近）。
- 受本次变更牵动的既有测试：`tests/test_openapi_drift.py`（全文档等值，自动守住 yaml 同步）、`tests/test_openapi_31_contract.py`（`BASELINE_NULLABLE_COUNT` 不得变动）、`tests/test_api_contract.py`、CI 的 Redocly 1.25.13 lint（`ci.yml:158`，基线实测绿、3 warnings）。
- **非目标**：把 `responses={200:...}` 升级为 `response_model=`（会改响应体，另立 issue）；实现 lineage 路由；把 `check:api-types` 接进 CI（issue 明确排除，同族但独立）；#2004 / PR #2020。

## Why

`/api/v1/layers` 在调用方传 `run_id` 时把 `discharge` 层的 `tile_url_template` 切到 `/api/v1/tiles/hydro/{run_id}/q_down/...`（单 run/单 basin），调用方不传 `run_id` 才返回 `/api/v1/tiles/hydro-national/q_down/...`（每流域 latest 的全国并集）。

前端 `loadOverview` 在 enrichment 阶段（[apps/frontend/src/stores/overviewData.ts:1331](apps/frontend/src/stores/overviewData.ts:1331)、[:1511](apps/frontend/src/stores/overviewData.ts:1511)）用 `useSingleRunFloodSurfaces ? latestRun?.run_id : null` 重新 `fetchLayers(...)` 以拿到 flood-return-period / warning-level 的 per-run `metadata.valid_times`；非 compare 模式 `useSingleRunFloodSurfaces=true` → 实际带上了 `run_id`，于是 discharge 层一并被改成单 run 模板。后端 enrichment 阶段返回的 layer 列表覆盖了 mapBootstrap 阶段 `fetchLayers(null)` 拿到的国家级模板（参考已存在的 spec scenario `Bootstrap minimal request set`，line 103）。

node-27 实测后果：用户在默认 `best+discharge` 视图下只能看到 `latestRun` 所属 basin 的河段（当前是 qhh），**heihe 等其他 basin 的河段完全没有 tile 下发，不能点击、不能渲染**。

这是 PR #582 / PR #584 留下的语义 drift：`useSingleRunFloodSurfaces` 这个 flag 的本意是给 flood **surfaces** 用，被复用到 `fetchLayers(run_id)` 这一层时把 discharge 一并拖下水。PR #584 已经把 run-selection 的 flood-ready 耦合解开了，但 tile-URL 这一层没修。

## What Changes

- **BREAKING (API 行为)**：`/api/v1/layers` 对 `discharge` 层始终返回 national 模板（`/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf`），无论是否传 `run_id`。`flood-return-period` / `warning-level` / `river-network` 行为不变。
- `_default_layer_catalog`（[apps/api/routes/flood_alerts.py:2278](apps/api/routes/flood_alerts.py:2278)）函数体 [:2302](apps/api/routes/flood_alerts.py:2302)：把 `national_discharge = national and layer_id == "discharge"` 改为 `national_discharge = layer_id == "discharge"`，去掉对 caller `national` 标志的依赖；其余 layer 仍按 `run_id` 路径走 `valid_times_for_layer`。
- 新增 regression 单元测试：`/api/v1/layers?run_id=<X>` 必须返回 discharge.tile_url_template = hydro-national 模板、必须仍返回 flood-return-period / warning-level 的 `{run_id}` 模板（不能误伤）。
- 新增 spec requirement：`overview-data-contracts` capability 中新增 *Default discharge tile URL is national* 要求，把这条不变量固化到 spec。
- 不动前端 `useSingleRunFloodSurfaces` 语义；保留 enrichment `fetchLayers(latestRun.run_id)` 调用（flood layers 仍依赖它取 per-run valid_times）。**修复在后端单点完成**。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `overview-data-contracts`: 新增 *Default discharge tile URL is national across all `/api/v1/layers` callers* requirement + 7 条 scenario（runless catalog、带 run_id catalog、cache identity、flood/warning 不受影响、frontend enrichment 不降级、invalid run_id 拒绝、空 DB）。
- `mvt-tile-contract`: MODIFIED *MVT tile API contract* requirement 与 *Canonical endpoint disposition* scenario —— 把 canonical discharge URL 改为 hydro-national；新增 *Discharge canonical URL is national across all callers* scenario，明确 `/api/v1/tiles/hydro/{run_id}/...` 是 direct-deeplink-only 路由，不在 catalog 暴露。

## Impact

- **代码**：`apps/api/routes/flood_alerts.py:2302` 一行；`tests/test_layers_catalog.py`（或邻近）新增 ≥1 个 regression test。
- **API 契约**：`/api/v1/layers` 对 discharge 行为收紧——以前传 run_id 会得到单 run 模板，现在永远 national。**前端无需修改**；compare 模式（不带 run_id）行为不变。直接深链 `/api/v1/tiles/hydro/{run_id}/q_down/...` 路由仍存在、仍可用，只是不再出现在公开 catalog 中。
- **下游消费**：前端不需要修改 —— enrichment 拿到的 discharge layer 永远是 national 模板。
- **缓存 / ETag**：`_NATIONAL_DISCHARGE_METADATA` 的 `source_refs={}` 决定缓存键不再绑 run_id；新 run 上线时由 tile URL 中的 valid_time 自然换 key，不会卡缓存。
- **OpenAPI**：`tile_url_template` 字段已是 nullable string（[openapi/nhms.v1.yaml:4699-4701](openapi/nhms.v1.yaml:4699)），不强制 enum，**不需要修改 schema**。
- **相邻 spec 反向引用**：`frontend-mvt-layer-consumption/spec.md:7` 已有"hydrology 层 consume vector tile sources for national rendering"的抽象框架，本变更把它具体到 discharge layer 永远是 national —— 不需要单独 spec delta，但在 `overview-data-contracts` 新场景 narrative 中已点名引用 [overviewData.ts:1331](apps/frontend/src/stores/overviewData.ts:1331) / [:1511](apps/frontend/src/stores/overviewData.ts:1511) 形成可追溯链。`mvt-tile-contract` 的 canonical endpoint disposition 在本变更中升级（见 Modified Capabilities）。
- **Receipts**：node-27 部署后须实拍 heihe basin 河段可见、可点击曲线作为 live oracle。

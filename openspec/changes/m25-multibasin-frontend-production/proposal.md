## Why

22 节点已业务化产出多流域（qhh + heihe）published 产品，且后续会持续新增流域；但 27 节点 display 前端仍是 QHH 单流域硬编码（`QHH_BASIN_ID = "basins_qhh"`），无法展示新流域，河段列表（250 条、无搜索）不可用于生产，洪水重现期产品状态在 `/hydro-met` 完全缺失，且 `/ops` 控制面入口对只读展示节点无业务意义。需要把展示前端转为**多流域、可扩展（加流域不改代码）的正式业务化**，并把运维控制面从主交付收缩。

## What Changes

- 后端 latest-product **去 QHH 硬编码 + `basin_id` 参数化**（向后兼容：默认 `basins_qhh`，保 cross-plane 旧路径不破）。
- 后端 `list_basins` 增加 `has_display_product` 过滤，提供"仅含可展示产品的流域"动态发现（复用现有 `QHH_LATEST_READY_RUN_STATUSES` ready 判定）。
- 后端 latest-product 响应补**洪水重现期可用性标注**（复用 best-available 的 `RETURN_PERIOD_RESULT_UNAVAILABLE` 判断，纯追加 `unavailable_reasons`，零 OpenAPI schema 改、向后兼容）。
- 前端 `/hydro-met` 升为**多流域主展示**：数据驱动的流域选择器、河段搜索/分页/stream-order 过滤、站点搜索/变量·QC 筛选、诚实产品状态条（q_down / forcing / return-period 三类 ready·degraded·unavailable）。
- 前端新增**洪水重现期静态图例预览区**（内嵌 `/hydro-met`）：真实 unavailable 状态 + 静态分级图例（2y…100y），**零造假河段数据、不建独立页、不新增接口**。
- 前端 `/ops` + `/monitoring` 在 `display_readonly` 下从主导航降级为内部诊断入口（保留 role-gated 访问 + display_readonly 边界防护测试不变）。
- **不做（明确排除）**：新建平行 `flood-return-period/status|preview` 接口、preview 假河段 fixture、删除 `/ops` 代码、修改 display_readonly 后端边界、修改 DB schema。

## Capabilities

### New Capabilities

- `multibasin-product-discovery`: 流域动态发现——后端 `list_basins` 的 `has_display_product` 过滤 + 发现契约，使前端流域选择器数据驱动、新流域 DB 注册即自动出现。
- `latest-product-multibasin`: latest-product 去 QHH 硬编码并按 `basin_id` 参数化，多流域共用同一产品发现路径，向后兼容默认 QHH。
- `return-period-availability`: latest-product 响应中诚实标注洪水重现期产品可用性（ready / unavailable + reason code），不伪造产品。
- `hydromet-multibasin-display`: `/hydro-met` 多流域主展示——流域选择、河段搜索/分页/过滤、站点筛选、产品状态条，strict identity 贯穿。
- `return-period-legend-preview`: `/hydro-met` 内洪水重现期区块——真实 unavailable 状态 + 静态分级图例，显著标注"暂未发布正式产品"，零造假数据。
- `ops-display-downgrade`: `display_readonly` 下 `/ops` 与 `/monitoring` 主导航降级为内部诊断，保留边界防护。

### Modified Capabilities

<!-- 本变更不修改既有 spec 的需求契约（display_readonly 后端边界、retry/cancel fail-closed 等 M22 行为保持不变）。 -->

## Impact

- **后端**：`packages/common/forecast_store.py`（去硬编码 + return-period JOIN）、`packages/common/model_registry.py`（list_basins 过滤）、`apps/api/routes/forecast.py`、`apps/api/routes/models.py`、`openapi/nhms.v1.yaml`、`apps/frontend/src/api/types.ts`（regen）。
- **前端**：`apps/frontend/src/pages/hydroMet/*`、`components/layout/NavBar.tsx`、`App.tsx`、`pages/MonitoringPage.tsx`、新增 `pages/hydroMet/` 下 return-period 与流域选择/列表组件。
- **复用（零开发）**：`GET /api/v1/basins`、`workers/model_registry/basins_discovery.py`、`basins_registry_import`、DB `core.basin`/`core.basin_version`、`OverviewPage` basin 分组、`QHH_LATEST_READY_RUN_STATUSES`、best-available return-period 判断逻辑。
- **不变**：DB schema、display_readonly 后端边界（retry/cancel 409、queue 503、no-slurm）、`flood.return_period_result` 表结构。
- **向后兼容**：latest-product 默认 `basin_id=basins_qhh`，M22 cross-plane 旧调用与 strict identity 契约不破。
- **解耦的平行任务（不在本变更）**：洪水重现期**真实产品**需 22 侧 hindcast 洪频基线（`flood.flood_frequency_curve` 当前 0 行），属数据/科学任务。

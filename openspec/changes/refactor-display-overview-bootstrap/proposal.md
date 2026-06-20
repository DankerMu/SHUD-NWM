## Why

node-27 display 首次进入 `/` 单页地图时出现 ~22 秒「总览数据加载中」遮罩，期间河段不可点击。canonical 基线（下表 row 1）= **21.8s** runless cold（2026-06-20 force-refresh 实测）。force-refresh 瀑布：

| 端点 | cold | 备注 |
|---|---|---|
| `/api/v1/layers` (runless) | **21.80s** | 内部循环为每个 layer 算 valid_times；撞到 water-level cold SQL |
| `/api/v1/layers?run_id=...` | **21.07s** | 同因 |
| `/api/v1/layers/discharge/valid-times` | 0.33s | 单独调正常 |
| `/runs?flood_product_ready=true` | 0.030s | 表 0 行，无聚合压力 |
| `/flood-alerts/{summary,ranking}` | 0.006s | 表 0 行；ranking 前端拿到后**不消费** |
| `/pipeline/status`、`/queue/depth` | 0.002–0.009s | 健康 |

根因实测 EXPLAIN：`hydro.river_timeseries`（92,005,680 行，**全部 q_down，0 行 water_level**）上 `SELECT DISTINCT valid_time WHERE variable='water_level' ORDER BY valid_time DESC LIMIT 21` 走 `river_timeseries_valid_time_idx` SkipScan，每个 timescale chunk 都过滤千万行才能确定空集，`Buffers: shared hit=2,429,536`（~18.5 GB），cold 21.8s。无 `(variable, valid_time)` 索引可命中。

产品决策已确认：**water-level 后续永不需要**。

次因（架构耦合，不影响本次延迟但增熵 / 阻碍后续演进）：

- `useOverviewDataStore.loadOverview` 把地图可交互所需的 layer/MVT 注册和 enrichment 类请求（ranking/summary/pipeline/queue/per-basin versions/per-layer valid-times fan-out）绑在同一个 `loading` 闸门；任何一个慢点 = 整页等待。
- `/flood-alerts/ranking?limit=200` 在首屏被无条件请求，但 `normalizeOverviewSummary()` 收到后并不使用（仅在打开预警面板/切换 flood layer 时有意义）。
- `/api/v1/layers` 返回的 `metadata.valid_times` 前端不消费，反而对每个图层再独立请求一次 `/layers/<id>/valid-times`，重复 RTT。
- 默认 discharge 图层强制依赖 `flood_product_ready=true` 选 latest run；流量展示语义与洪频完整性无关。

## What Changes

- **BREAKING (backend)**：从代码与 OpenAPI 中**整条删除 `water-level` layer 与 `water_level` hydro MVT variable**——`SUPPORTED_HYDRO_MVT_VARIABLES` 收紧为 `("q_down",)`、`apps/api/routes/flood_alerts.py::_default_layer_catalog` 内 `definitions` 列表移除 `water-level` 项、tile/feature/popup/valid-times 路径中所有 `water-level | water_level` 分支删除、`HydroMvtVariable` enum 收为单值。
- **BREAKING (frontend)**：`M11Layer` enum 删 `'water-level'`、layer 选择器/legend/paint/UI option/event 处理中 `water-level` 分支删除；`OverviewDataSnapshot` 不再承载 water-level layer state；测试用例与 mock fixture 同步删除。
- **Frontend perf**：`useOverviewDataStore.loadOverview` 拆 `loading` 为 `mapBootstrapLoading`（注册 MVT hit layer 所需的关键路径——layers catalog + 当前 layer 的 valid_time + 最小 basin 身份）与 `enrichmentLoading`（pipeline/queue/summary/per-basin versions 等非关键）；OverviewPage `surfaceSettling` 改为只看 `mapBootstrapLoading || !overview?.bootstrap`，使地图可点击不再被 enrichment 拖住。
- **Frontend dedupe**：`normalizeLayerStates` 直接消费 `apiLayer.metadata.valid_times`，仅当 metadata 不带 valid_times 时才单独 fetch；首屏默认 layers 的 valid-times fan-out 被消除。
- **Frontend prune**：删除 `loadOverview` 中对 `/flood-alerts/ranking` 的默认调用与对应 `normalizeOverviewBasins`/`normalizeOverviewSummary` 入参；ranking 仅在 ranking 面板挂载或 layer 切到 flood/warning 时按需 fetch。
- **Frontend decouple**：默认 discharge 路径选 latest run 使用 `frequency-ready`（已是后端默认）即可，前端 `fetchRuns(query)` 不再固定 append `flood_product_ready=true`；该过滤只在 flood-return-period / warning-level layer 激活时启用。
- **DB**：不动 schema、不加 index、不改约束（dead variant 删后零回归路径，YAGNI）。

## Capabilities

### New Capabilities

无。所有改动落地到既有 capability 的 delta（避免新增 spec 噪声）。

### Modified Capabilities

- `frontend-mvt-layer-consumption`：layer 候选集合从 `discharge | water-level | flood-return-period | warning-level | river-network` 收紧为 `discharge | flood-return-period | warning-level | river-network`；layer valid_times 来源契约改为 `apiLayer.metadata.valid_times` 优先，per-layer endpoint 单独 fetch 仅作 fallback；新增 BREAKING note `water-level` 不再属于消费 layer。
- `overview-data-contracts`：`loading` 单一闸门被拆为 `mapBootstrapLoading` / `enrichmentLoading`，并新增 Requirement 规定「地图可交互不得阻塞于非关键 enrichment」；删除「ranking 默认随总览首屏加载」的隐含承诺，明确 ranking 由面板/layer 驱动；明确默认 discharge 不依赖 `flood_product_ready=true` 选 run。
- `segment-detail-data-contract`：移除 `water-level` 在 missing-value 列表中的提及。
- `basin-drilldown-page`：移除 detail panel 中 `water-level delta` 显示条目。

## Impact

- **代码**（删除为主）
  - 后端：`services/tiles/mvt.py`（~10 处 water-level 分支 + `SUPPORTED_HYDRO_MVT_VARIABLES` enum），`apps/api/routes/flood_alerts.py`（`_default_layer_catalog::definitions` water-level 项 + handler 注释，~3 处）
  - 前端：`apps/frontend/src/stores/overviewData.ts`（loading 拆分 + ranking 删除 + valid_times 来源 + flood_product_ready 解耦），`apps/frontend/src/pages/OverviewPage.tsx`（`surfaceSettling` 改写 + water-level branch），`apps/frontend/src/components/map/M11MapLibreSurface.tsx` `M11FloatingControls.tsx` `pages/m11/M11Controls.tsx`，`apps/frontend/src/lib/m11/queryState.ts` `overviewDataContracts.ts`，`apps/frontend/src/components/m11/BasinDetailPanels.tsx`（`warningDistribution` 空态处理 — ranking 懒加载前不渲染"全 0 警告"误导态）
  - 契约：`openapi/nhms.v1.yaml` 仅收紧 `HydroMvtVariable` enum 为 `["q_down"]`（`layer_id` 在 schema 中是无 enum 的 `string`，无需 OpenAPI 改动；`HydroMvtVariable` 是唯一受影响 enum） + `apps/frontend/src/api/types.ts` regen
- **测试**：`tests/test_flood_alerts_api.py`、`apps/frontend/src/pages/__tests__/M11Shell.test.tsx`、`apps/frontend/src/lib/__tests__/m11OverviewDataContracts.test.ts`、`apps/frontend/src/stores/__tests__/overviewData.test.ts`、`apps/frontend/src/lib/hydroMet/__tests__/riverForecast.test.ts` 删除/重写 water-level 用例；新增 overviewData loading 拆分 + ranking 不默认调用 + valid_times metadata 优先消费的覆盖测试
- **DB**：不动；`hydro.river_timeseries` 现存 0 行 water_level 行历史上零写入，无 backfill / migration 风险
- **运维**：node-27 PR merge 后 `git pull --ff-only` + `pnpm build && rsync dist` + restart api-web/worker 容器；浏览器 cold first-paint 自查
- **客户端兼容**：自家前端是唯一消费者，OpenAPI enum 收紧不破坏外部 client
- **回滚**：单 epic 多 issue 拆分；每个 PR 独立可 revert；如发现新数据源真要 water_level，回滚 OpenAPI 收紧 + 加 partial index 即可恢复

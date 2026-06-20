<!--
  Cross-group ordering (REQUIRED before sub-issue split):

  - 1 → 2 → 3 → 4 → 5 → 6 → 7
  - 1 (backend water-level removal + OpenAPI) MUST land on master before 2 begins
    so that `pnpm check:api-types` regenerates `apps/frontend/src/api/types.ts`
    against a `HydroMvtVariable: "q_down"`-only enum; otherwise 2's frontend
    union narrow will fail to compile.
  - 3 (loading split) MUST land before 4 (dead-call removal) and 5 (flood
    decoupling) — 4 and 5 rebase onto post-3 master so the enrichment stage
    they edit already reflects the split. If 3 has not landed when 4 or 5
    PR is opened, hold the PR in draft.
  - 6 (node-27 live receipt) runs AFTER 1-5 are all on master; the receipt
    measures post-merge cold latency, not interim states.
  - 7 (docs + archive) runs AFTER 6 so the receipt link in docs/runbooks is real.
  - Inside a group, code edits (e.g. 2.1-2.5) and test edits (e.g. 2.7) MUST
    land in the SAME PR; opening test deletion before code is in master leaves
    code branches untested.
-->

## 1. Backend: water-level dead variant removal

- [ ] 1.1 删除 `services/tiles/mvt.py` 中 `SUPPORTED_HYDRO_MVT_VARIABLES` 的 `water_level` 项；收紧为 `("q_down",)`；同步删除 `valid_times_for_layer`、tile feature encoders、popup builders 等所有 `if layer_id == "water-level"` / `if variable == "water_level"` / `"water-level"` / `"water_level"` 分支。**执行前先 grep 锁定全部命中**：`rg -n 'water[-_]level' services/tiles/mvt.py`（参考已知命中行 29、164、807、906–916、1161–1184、1489–1490），每一条都需删除或重构为 `discharge` 单分支直路
- [ ] 1.2 删除 `apps/api/routes/flood_alerts.py::_default_layer_catalog` 内 `definitions` 列表中的 `("water-level", ...)` 元组项（line 2278 附近，列表起点 line 2276，函数 def 在 line 2254）；同步删除 handler 注释中 `water-level` 词（执行前 `rg -n 'water[-_]level' apps/api/routes/flood_alerts.py` 锁定全部命中行）；不动其它 layer 项
- [ ] 1.3 收紧 `openapi/nhms.v1.yaml` 的 `HydroMvtVariable` enum 为 `["q_down"]`（line 2225 附近）；标 BREAKING note。**Scope 校正**：`/api/v1/layers/{layer_id}/valid-times` 的 `layer_id` 在当前 schema 中是 `type: string` **无 enum**（line 1795），`/api/v1/layers` 响应 schema 的 `layer_id` 同（line 4627）；本次仅触 `HydroMvtVariable` 一处 enum，path/response 字段无需 OpenAPI 改动（runtime rejection 通过后端 catalog 删项 + 422 路径保证，对应 spec scenario「water_level variable is rejected at the backend boundary」）
- [ ] 1.4 `tests/test_flood_alerts_api.py` 删除所有 `water-level` parametrize 与 fixture（line 1830、1832、1842、2736、4496、4517、4584、4680、4836、4899、5081、5088–5090、5221 — 执行前 `rg -n 'water[-_]level' tests/test_flood_alerts_api.py` 复核行号）；保留 discharge/flood-return-period/warning-level/river-network 覆盖；加 OpenAPI drift assertion 验证 enum 不含 `water_level`；额外新增 `GET /api/v1/layers/water-level/valid-times` 返回 422 的 deny test（覆盖 spec scenario「water_level variable is rejected at the backend boundary」）
- [ ] 1.5 新增 `tests/test_flood_alerts_api.py::test_runs_does_not_require_flood_product_ready_for_discharge`：`GET /api/v1/runs?source=best`（无 `flood_product_ready` filter）应返回 frequency-ready 但 flood-incomplete 的 run（如 QHH/Heihe 在洪频未跑完时仍可作为 discharge 候选），固化 spec scenario「Discharge layer is active」在后端契约层的语义
- [ ] 1.6 本地 `uv run ruff check . && uv run pytest -q tests/test_flood_alerts_api.py tests/test_api_contract.py` 全绿

## 2. Frontend: water-level dead variant removal

- [ ] 2.1 `apps/frontend/src/lib/m11/queryState.ts` 删 `'water-level'` 从 `M11Layer` union（line 4、29）。**M11Layer 删后 = `'discharge' | 'flood-return-period' | 'warning-level' | 'met-stations' | 'met-raster'`（5 项；`met-stations` / `met-raster` 是已存在的 raster overlay 选项，本任务保留不动）**。注意区分：(a) `M11Layer` TS union 是前端 user-selector（5 项含 raster overlays）；(b) backend MVT-emitting layer set（spec `frontend-mvt-layer-consumption` 的 capability 对象）= `discharge | flood-return-period | warning-level | river-network`（4 项，含 `layer_type='base'` 的 river-network，由后端自动渲染不在 `M11Layer` 内）。本任务只触 `M11Layer` 中删 `'water-level'` 一项；拆 `M11Layer` 为 `M11HydroLayer` + `M11RasterOverlay` 是独立未来 refactor，不在本 change 范围
- [ ] 2.2 `apps/frontend/src/lib/m11/overviewDataContracts.ts` 删 `water-level` 从 `M11_LAYER_LABELS`（line 332）、`requiredLayers`（line 572）、layer_id discriminator（line 1242）、`layerLegend` 内部函数 water-level 分支（line 1310-1316，被 `getM11LayerLegend` line 619 委托调用）；保留 4 个其它 layer 直路
- [ ] 2.3 `apps/frontend/src/components/map/M11MapLibreSurface.tsx` 删 `water-level` 与 `water_level` 所有分支（line 601、666、805、1375 — 执行前 `rg -n 'water[-_]level' apps/frontend/src/components/map/M11MapLibreSurface.tsx` 复核）；`variable` 推导退化为 `'q_down'` 常量
- [ ] 2.4 `apps/frontend/src/components/map/M11FloatingControls.tsx` 删 `水位图例` legend label（line 137）；`apps/frontend/src/pages/m11/M11Controls.tsx` 删 `'water-level' | '河段水位'` UI option（line 80）+ legend map（line 105）+ fallback ternary `: '水位图例'`（line 318）
- [ ] 2.5 `apps/frontend/src/pages/OverviewPage.tsx` 中 `state.layer === 'water-level'` 分支删除（line 301，文件总长 466 行；勿与 M11MapLibreSurface.tsx:1375 混淆，后者已在 2.3 覆盖）
- [ ] 2.6 `apps/frontend/src/components/m11/BasinDetailPanels.tsx` 复核：如有 `water-level`/`water_level` 直接渲染或 KPI（执行 `rg -n 'water[-_]level' apps/frontend/src/components/m11/`），同步删除；对应 spec MODIFIED「Selected segment detail provides forecast context」和「Normalized segment detail view model」code-side 落地
- [ ] 2.7 `cd apps/frontend && corepack pnpm run check:api-types` 重 generate `apps/frontend/src/api/types.ts`；验证 `HydroMvtVariable: "q_down"` 单值（**前置：task 1.3 已 merge 到 master**）
- [ ] 2.8 同步删除测试用例（与 2.1-2.6 同 PR）：`apps/frontend/src/pages/__tests__/M11Shell.test.tsx`（line 315–334、346、382、803、898–923、971–973、1125–1131、1338、1634、1665）、`apps/frontend/src/lib/__tests__/m11OverviewDataContracts.test.ts`（line 330、337、347、355–356）、`apps/frontend/src/lib/hydroMet/__tests__/riverForecast.test.ts`（line 112、127 — 这两条本就是 negative assert，可改文案/保留意图）
- [ ] 2.9 新增前端单测：URL/query 路径设置 `layer=water-level` 时 layer parser 回退到 default `discharge`、不注册 `water-level` MVT source（覆盖 spec scenario「water-level layer id is rejected at the URL/query boundary」）
- [ ] 2.10 本地 `cd apps/frontend && corepack pnpm test -- --run && corepack pnpm exec tsc --noEmit` 全绿

## 3. Frontend: map bootstrap vs enrichment loading decoupling

- [ ] 3.1 `apps/frontend/src/stores/overviewData.ts` 修改 `OverviewDataState`：删 `loading: boolean`，加 `mapBootstrapLoading: boolean` + `enrichmentLoading: boolean`；初始值都为 `false`；同步加 `OverviewDataSnapshot.bootstrap` 字段（type 定义 + 初始 null）。**契约固化**：`OverviewDataSnapshot.bootstrap` 的最小 shape = `{ basins, layers, layerStates, currentLayerValidTime }`；该形状由本 task 决定后续 (`normalizeLayerStates` in task 4.4 等) 必须按该形状消费，不得重命名 `layerStates` 等关键字段，避免 PR 3 + PR 4 沿着同一文件接力时 snapshot contract drift
- [ ] 3.2a 重写 `loadOverview` 阶段 1（`mapBootstrapLoading=true`）= `fetchBasins`、`fetchLayers(null)`、当前 `query.layer` 的 `metadata.valid_times` 解析；阶段 1 settle 后 `set({ mapBootstrapLoading: false, overview: { bootstrap: ..., ... } })`，使 OverviewPage 可注册 MVT hit layer。**显式失败处理**：阶段 1 fetch reject 时 `mapBootstrapLoading=false` + 写入 scoped bootstrap error（覆盖 spec scenario「Map bootstrap rejection」）
- [ ] 3.2b 重写 `loadOverview` 阶段 2（`enrichmentLoading=true`，与阶段 1 异步并行后台）= `fetchRuns`、`fetchModels`、`fetchQueueDepth`、`fetchPipelineStatus`、`fetchFloodSummary`（非默认 discharge 跳过；不含 ranking — 由 task 4.1 删除）、`fetchBasinVersions`、`fetchModel` 等；settle 后合并到 snapshot 并 `set({ enrichmentLoading: false })`；阶段 2 内单点 reject 仅产 scoped enrichment error 不传播
- [ ] 3.3 `apps/frontend/src/pages/OverviewPage.tsx` `surfaceSettling = mapBootstrapLoading || !overview?.bootstrap`（位于 `loading` 旧字段的同一函数内；执行时按 `surfaceSettling` 标识符搜索，不靠 line cite）；地图 hit layer 注册逻辑不再依赖 enrichment 字段；enrichment 失败仅在对应面板显示 scoped error
- [ ] 3.4 `apps/frontend/src/stores/__tests__/overviewData.test.ts` 新增测试：
  - (a) (00) 初始未 loadOverview、(10) mapBootstrap=true 阶段 1 进行中、(01) mapBootstrap=false enrichment 进行中、(11) loadOverview 同帧调用 — 4 个状态的快照断言
  - (b) 阶段 1 settle 不依赖 fetchRuns
  - (c) 阶段 1 reject → `mapBootstrapLoading=false` + scoped bootstrap error 暴露
  - (d) **enrichment 内 fetchPipelineStatus / fetchQueueDepth / fetchFloodSummary / fetchBasinVersions 单点 reject** → scoped error 仅暴露在对应 panel；`mapBootstrapLoading` 保持 `false`；map 可交互不受影响（覆盖 spec scenario「Enrichment failure does not block map」）
- [ ] 3.5 `apps/frontend/src/pages/__tests__/M11Shell.test.tsx` 加测试：`overview?.bootstrap` 就绪 + `mapBootstrapLoading=false` 后 MVT hit layer 已注册、地图可点击；enrichmentLoading=true 不挡 surface

## 4. Frontend: dead-call removal + valid_times metadata consumption

- [ ] 4.1 `apps/frontend/src/stores/overviewData.ts` 从默认 `loadOverview` 删除 `fetchFloodRanking(...)` 调用；`normalizeOverviewSummary`、`normalizeOverviewBasins` 入参移除 `ranking`（参考 [overviewDataContracts.ts](apps/frontend/src/lib/m11/overviewDataContracts.ts)）
- [ ] 4.2 `apps/frontend/src/components/m11/BasinDetailPanels.tsx` 复核 `warningDistribution` 消费点（约 line 290）：在 `warningCounts === undefined` / ranking 尚未 settle 时显示 `pending` 占位或"未加载"态，**而非误导性"全 0 警告"**（覆盖 spec scenario「Default overview bootstrap omits ranking」的 4th AND clause）
- [ ] 4.3 提供 `loadFloodRankingOnDemand(runId, query, basinId)` 函数；ranking 面板组件 mount 时调用，in-flight cache 去重；切到 `flood-return-period`/`warning-level` layer 时主动触发；切回 `discharge` / 面板 unmount 时清除 in-flight cache 条目，不允许 setState 到已卸载组件（覆盖 spec scenario「Ranking fetch is cancelled on unmount or layer change」）
- [ ] 4.4 `apps/frontend/src/lib/m11/overviewDataContracts.ts::normalizeLayerStates` 改为先消费 `apiLayer.metadata.valid_times`：非空数组直接用；`=== []` 视为 time-less layer（不发 fallback）；`undefined`/`null` 才发 `/layers/<id>/valid-times` fallback。**同步从 `loadOverview` 删除 `layerIdsForOverview(query).map((layerId) => fetchLayerValidTimes(layerId, ...))` 默认 fan-out**，fallback 仅 metadata 缺失时调用（4.3 旧 4.4 合并为本任务）
- [ ] 4.5 单测覆盖：
  - (a) ranking 不默认 fetch + ranking 面板挂载触发 fetch（写在 `overviewData.test.ts`）
  - (b) `BasinDetailPanels` `warningDistribution` 空态降级渲染（写在 `BasinDetailPanels.test.tsx`）
  - (c) ranking 面板 unmount / layer 切回 discharge 时 in-flight cache 清理 + 无 setState（写在 `overviewData.test.ts`）
  - (d) **专项 `normalizeLayerStates` 单测对**写在 `apps/frontend/src/lib/__tests__/m11OverviewDataContracts.test.ts`：metadata.valid_times 非空数组 → 不调用 fetchLayerValidTimes；metadata.valid_times `=== []` → 不调用 fallback；metadata.valid_times `undefined`/`null` → 调用 fallback 单端点（覆盖 spec scenario「Layer valid_times are consumed from metadata.valid_times first」的 MUST 子句）
  - (e) `layerIdsForOverview(query).map(fetchLayerValidTimes)` 默认 fan-out 已不在 loadOverview 路径上的回归断言

## 5. Frontend: default discharge decoupling from flood_product_ready

- [x] 5.1 `apps/frontend/src/stores/overviewData.ts::fetchRunsPageByStatus`（line 583）移除固定 `flood_product_ready: true` query；改为按 `query.layer` ∈ {`flood-return-period`, `warning-level`} 时才注入；`fetchRuns`/`fetchRunsPage`（line 594/599）作为 thin delegate 同步遵循
- [x] 5.2 同步 `fetchRunsForBasinVersion` 等相关函数遵循同一 layer-based gating；layer toggle 后调用必须重新计算 `flood_product_ready` query 串并重选 latest run（覆盖 spec scenario「Layer toggle re-evaluates flood_product_ready filter」）
- [x] 5.3 测试：
  - (a) **修订现有断言** `apps/frontend/src/stores/__tests__/overviewData.test.ts:286/292/522/560/2508`：discharge 路径下原 `expect(...flood_product_ready === true).toBe(true)` 改为 `expect(...flood_product_ready).not.toBe(true)`（要么 undefined 要么不传该 param）；flood-return-period / warning-level 路径保留 `=== true` 断言
  - (b) 新增 layer toggle 测试：discharge → flood-return-period 切换后下一次 `fetchRunsPageByStatus` 请求参数变化 + latest run 重选

## 6. node-27 live receipt

- [ ] 6.1 新增 `scripts/diagnostic/display-cold-waterfall.sh`（注意目录名是已存在的单数 `diagnostic/`，不新建 `diagnostics/`）：force-refresh 全瀑布 timing 脚本，输出 markdown 表格 + canonical 字段名（与 21.8s 基线 diff 用）
- [ ] 6.2 node-27 实测 cold receipt 写入 `docs/runbooks/receipts/display-bootstrap-decoupling-<date>.md`：
  - merge 前后 `/api/v1/layers` cold timing 对比表（baseline = canonical 21.8s；预期 < 200 ms p95，对应 spec scenario「Cold `/api/v1/layers` budget」）
  - 完整 force-refresh 瀑布表，每个端点 < 500 ms p95
  - **浏览器 cold first-paint 必需证据**（PNG + 时间戳表）：Network panel TTFB 截图、Performance panel 首个 "click a river segment" interaction 时间戳，证明 `mapBootstrapLoading=false` 到首次河段可点 < 1 s（对应 spec scenario「Cold first-paint interactivity budget」）。screen recording 是 nice-to-have；截图 + 时间戳表是必需
- [ ] 6.3 worklog 记录到 `worklogs/<date>-display-bootstrap-decoupling.md`：execution_mode=live_proof
- [ ] 6.4 `docs/bugs.md` ledger 增加条目：归因 `code-contract`（dead variant + 闸门耦合）+ live receipt 链接

## 7. Docs + archive

- [ ] 7.1 `docs/runbooks/api-latency.md` 同步：当前文件（29 行）无 `/runs`/`/layers` 对 `return_period_result` 6100 万行聚合段落。**新增**一个 `## water-level dead variant 22s cold path (已移除)` 段落到 Recovery Steps 之后，引用 receipt 链接（task 6.2）+ canonical 21.8s 基线；不"替换"不存在的段落
- [ ] 7.2 同步 `docs/runbooks/display-readonly-live-mvt.md`：layer 列表更新为 4 项（删 water-level，对齐 line 47-48），并加一句 history note 说明 water-level 在 2026-06-20 移除
- [ ] 7.3 `openspec validate refactor-display-overview-bootstrap --strict --no-interactive` 全绿（archive 命令本身由 epic owner 在所有 sub-PR merge 后执行，不在本任务范围）

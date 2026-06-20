## Context

node-27 是 `display_readonly` 节点，承担 `/` 单页地图 + `/ops` 的生产化展示。用户报"首次进入十几秒后才能点击河段"。force-refresh 实测瀑布锁定主因为 `/api/v1/layers` **cold 21.8s**（canonical 基线 = proposal 瀑布 row 1，2026-06-20 force-refresh `x-nhms-cache-warm: refresh` 实测）。

`/api/v1/layers`（[apps/api/routes/flood_alerts.py::list_layers](apps/api/routes/flood_alerts.py)，handler 约在 line 395-430）内部为每个注册 layer 计算 valid_times metadata（[services/tiles/mvt.py::valid_times_for_layer](services/tiles/mvt.py)，约在 line 1118-1184）。注册 layer 包括 `discharge`、`water-level`、`flood-return-period`、`warning-level`、`river-network`。其中 `water-level` 走 `SELECT DISTINCT valid_time FROM hydro.river_timeseries WHERE variable='water_level' ORDER BY valid_time DESC LIMIT 21`。

node-27 prod DB 实测：

```
 variable | count
----------+-----------
 q_down   | 92,005,680
```

`water_level` 在 `hydro.river_timeseries` 中**永远 0 行**（历史从未写入）。但表上仅有 `(variable, valid_time)` 复合索引以 `run_id` 为前缀；无 run_id 的查询退化到 `river_timeseries_valid_time_idx`（仅 valid_time DESC），TimescaleDB SkipScan 在每个 chunk 上以 variable 谓词逐行过滤至空集，`Buffers: shared hit=2,429,536`（~18.5 GB），cold 21.8s。

产品决策（用户 2026-06-20 显式确认）：**water-level 后续永不需要**。

### 架构耦合的次因诊断（implementer 在 node-27 live 实测交叉验证）

本 change 的次因清单（loading 闸门、ranking 不消费、metadata.valid_times 重复 fan-out、discharge ↔ flood_product_ready 错耦合）由 implementer 在 2026-06-20 force-refresh 瀑布上直接观察并交叉验证，部分受外部 review 启发但**不依赖外部文件留存**。下表对应 row-by-row receipt（脚本输出归档于 `scripts/diagnostic/display-cold-waterfall.sh`，receipt 在 `docs/runbooks/receipts/display-bootstrap-decoupling-<date>.md`）：

| 候选诊断 | node-27 live 实测 | 判定 |
|---|---|---|
| `loading` 闸门绑死 ~12 请求阻塞地图可交互 | `useOverviewDataStore.loadOverview` 1059-1170 行单 `loading` flag 串行控制 3 阶段；MVT hit layer 注册条件（basins + layers + valid_time）属于阶段 1+2 子集 | ✅ 真实架构耦合 |
| `/flood-alerts/ranking` 前端拿到不消费 | `normalizeOverviewSummary`/`normalizeOverviewBasins` 入参 `ranking?: ...` 在默认 best+discharge 路径下未驱动任何 view model 字段（仅 `warningDistribution` 用，且自带空态降级） | ✅ dead-call |
| `metadata.valid_times` 前端不消费、重复 fan-out | `/api/v1/layers` 响应已含 `metadata.valid_times`；`normalizeLayerStates` 走单独 `/layers/<id>/valid-times` 路径，5 个图层 = 5 个额外 RTT | ✅ 重复请求 |
| discharge 强依赖 `flood_product_ready=true` | `fetchRunsPageByStatus` (line 583) 无条件 append；discharge 展示语义与洪频完整性正交 | ✅ 错耦合（历史 m25 副产品） |
| `/runs` 冷查询 12-15s | force-refresh 实测 30 ms cold（runless + run-scoped 各 ~30 ms）| ❌ 不成立（receipt: probe table row 4） |
| `flood.run_product_quality` 表缺失 | node-27 `\d flood.run_product_quality` 返回完整 schema | ❌ 不成立 |
| `return_period_result` 6100 万行聚合 | node-27 `SELECT count(*) FROM flood.return_period_result` = 0 | ❌ 不成立 |

**关键判读**：真因（22s cold）= water-level dead variant 的 valid_times SQL；其他四项次因都是架构熵增不是延迟驱动（量级 ms / 个位 RTT），删除/重构是熵下降，不是 perf 修复。后端那张表存在 / 行数 = 0 的事实使"加 partial index 或预计算"路径都不成立，**删除才是最低熵**（详 D1）。

> Receipts：表中的 receipt 行 (`/runs` 30ms / `\d` schema / `count(*)=0`) 在 Stage 4.5 验证门接通 node-27 时刷新到 `docs/runbooks/receipts/pre-bootstrap-decoupling-evidence-<date>.md`；Stage 1 实施 receipt 与此同位归档。

## Goals / Non-Goals

**Goals**

- 删除 `water-level` 整条 dead variant（后端 enum、catalog、tile/feature/popup/valid-times 分支；前端 layer enum、UI、paint、legend；OpenAPI；测试）；使 `/api/v1/layers` cold path 不再触发 water-level 22s SQL。
- 解耦前端 `useOverviewDataStore.loadOverview` 单一 `loading` 闸门：地图可交互（MVT hit layer 注册）走 `mapBootstrapLoading` 快路径，pipeline/queue/summary/per-basin versions 等走后台 `enrichmentLoading`。
- 消除首屏可观察的纯冗余调用：`/flood-alerts/ranking` 仅在 ranking 面板或 flood/warning layer 激活时请求；`/layers/<id>/valid-times` 仅在 `apiLayer.metadata.valid_times` 缺失时 fallback。
- 默认 discharge 路径不再固定 append `flood_product_ready=true`；此过滤只在 flood-return-period/warning-level layer 激活时启用。
- 落地 node-27 实机 receipt：`/layers` cold < 200ms、首屏 cold < 1s 可点击河段。

**Non-Goals**

- 不新增数据库 index、不改 schema、不改 CHECK 约束（YAGNI + dead variant 删除后无收益）。
- 不新增聚合端点（如 `/display/bootstrap`）；最小契约改动仅复用 `metadata.valid_times` 字段。架构演进到聚合端点是更大 epic，本 change 不纳入。
- 不动 `flood.run_product_quality` migration/backfill 链路；node-27 已有该表，无 deploy gap。
- 不改 SHUD/forcing/ingestion；变量集合从未写入 water_level，无 backfill。
- 不在浏览器端做 Service Worker / SPA prefetch；本 change 聚焦后端 dead variant 删除 + 前端 loading 拆分。

## Decisions

### D1：删 water-level（不做 partial index / cheap-exists fallback / TTL 延长）

**选**：从代码、OpenAPI、测试整链删除 water-level layer 与 water_level hydro MVT variable。
**未选**：
- (a) 加 partial index `CREATE INDEX ... ON hydro.river_timeseries (valid_time DESC) WHERE variable='water_level'` — 90M 行 hypertable，每 chunk sub-index 写入开销 + 磁盘；变量永无数据，index 是 dead 维护项。
- (b) `valid_times_for_layer` 入口加 cheap exists 探针（`SELECT 1 ... LIMIT 1`）— 仍保留 dead path，未来新加 layer 同雷会重演；增加分支熵。
- (c) 延长 TTL + 服务启动预热 — 不修根因，restart 后第一个用户仍受影响；新 cycle run_id 又冷一次。

**理由**：YAGNI + 永远 dead 的代码路径无价值，**删除是最低熵的修法**（消除分支、消除测试、消除 OpenAPI enum 值、消除前端 UI option）。回滚成本（万一未来要 water_level）= 一次完整的恢复 PR，不构成阻碍。

### D2：`loading` 拆 `mapBootstrapLoading` / `enrichmentLoading`

**选**：`useOverviewDataStore` 状态新增两个独立 boolean；`loadOverview` 内部维护两阶段 settle：阶段 1 fulfilled 即 `mapBootstrapLoading=false`、阶段 2 fulfilled 即 `enrichmentLoading=false`。OverviewPage 的 `surfaceSettling = mapBootstrapLoading || !overview?.bootstrap`（line 不固定，目前位于 OverviewPage.tsx 约 350 行附近；实施时按代码位置而非 line cite 定位）。
**未选**：
- (a) 引入新 `/display/bootstrap` 聚合端点 — 大改动，OpenAPI/前端 type/服务端 schema 全面新增；契约成本高于本次延迟收益；留待独立 epic。
- (b) 把 enrichment 改为 lazy on-demand（点击才拉）— 用户报 pipeline/queue panel 仍需要默认展示数据；破坏现有 UX。
- (c) 保留单 `loading` 但用 `Promise.race` 提前 set false — race condition 风险高，部分数据 normalize 顺序不可预测。

**理由**：拆 boolean 是最小改动 + 最大行为收益；快路径只需 `layers + 当前 layer 的 valid_time + 最小 basin 身份`（MVT hit layer 注册条件），其余背景 settle。

### D3：mapBootstrap 关键路径定义

**契约**：mapBootstrap 完成必备：
1. `fetchBasins()` 完成（用于 layer 选择器与 basin 身份映射）
2. `fetchLayers(null)` 完成（runless catalog，自带 `metadata.valid_times`）
3. 当前 query.layer 在 layers catalog 中可解析为 available + 有 valid_time + MVT metadata 完整

未在 mapBootstrap 关键路径：`fetchRuns`、`fetchModels`、`fetchQueueDepth`、`fetchPipelineStatus`、`fetchFloodSummary`、`fetchFloodRanking`、`fetchBasinVersions`（per-basin）。

**理由**：MVT hit layer 注册条件（[M11MapLibreSurface.tsx](apps/frontend/src/components/map/M11MapLibreSurface.tsx) `buildM11RegisteredOverlay`）= layers + available + validTime + metadata 完整。前 3 条用 runless catalog 即可满足；run 选择只在用户切到具体 cycle 才必要。

### D4：`metadata.valid_times` 优先消费

**选**：`normalizeLayerStates` 改为先读 `apiLayer.metadata?.valid_times`，存在则用；不存在才 fallback 到 `/layers/<id>/valid-times` 单独 fetch。
**未选**：
- (a) 后端 layers metadata 不再 inline valid_times，前端永远走 single endpoint — 把 N RTT 暴露给前端，更慢。
- (b) 后端新增 `/layers?include=valid_times` 参数 — 已 inline 在 metadata 里；新参数纯冗余。

**理由**：消费已有契约字段是最小熵；fallback 兜底保证未来某 layer 不携带 metadata.valid_times 时仍可工作。

### D5：ranking 改为面板/layer 驱动

**选**：`loadOverview` 删除默认 `fetchFloodRanking` 调用；ranking 面板挂载或 query.layer ∈ {flood-return-period, warning-level} 时主动 fetch；`normalizeOverviewSummary` 入参移除 ranking。
**未选**：
- (a) 保留默认 fetch 但移到 enrichment 阶段 — 仍是冗余请求（前端不消费）；放后台也是浪费 RTT + DB 工作。
- (b) ranking 与 summary 合并为单端点 — 后端 schema 变化，contract drift 风险大；ranking 当前 6ms cold 不值得。

**理由**：ranking 在默认 overview 路径**未被消费**（`normalizeOverviewSummary` 收到不用），删除是 dead-call 清理。

### D6：discharge 不依赖 `flood_product_ready=true`

**选**：`fetchRuns(query)` 不固定 append `flood_product_ready=true`；仅当 `query.layer` ∈ {flood-return-period, warning-level} 时启用该 filter。
**未选**：
- (a) 后端 `/runs` 自动按 layer 推断 — 后端不该承担 layer 语义；保持 dumb endpoint。
- (b) 前端两套查询并行 — 增加 RTT，无收益。

**理由**：discharge 展示语义与洪频完整性正交；强耦合是历史 m25 多流域改造时的副产品，已无理由保留。

## Risks / Trade-offs

- **R1**：OpenAPI 删除 enum 值是 BREAKING change。
  **Mitigation**：repo 唯一消费者是自家前端；外部 client 不存在；CI 类型 drift 测试会强制前后端同步。
- **R2**：拆 `loading` 引入新 state，OverviewPage 多处旧代码（test/ui）仍读单一 `loading`。
  **Mitigation**：单 PR 内同步迁移所有读点；tasks 列入回归测试用例覆盖 `mapBootstrapLoading` 与 `enrichmentLoading` 的 4 个状态组合（00/01/10/11）。
- **R3**：删 ranking 默认 fetch 后，预警面板首次打开会"懒加载"等几毫秒；UX 上是新延迟。
  **Mitigation**：面板挂载即触发 fetch，6ms cold 用户感知不到；面板期间维护 in-flight cache 避免抖动。
- **R4**：metadata.valid_times 在某些 layer 缺失（如 river-network 仅 1 元素）会触发 fallback。
  **Mitigation**：fallback 路径保留，且 river-network 当前 endpoint < 5ms；新增 unit test 覆盖 fallback 命中。
- **R5**：node-27 cold receipt 用 `x-nhms-cache-warm: refresh` 才真实复现；merge 后日常 warm 测量看不到 22s。
  **Mitigation**：归档 force-refresh 瀑布脚本到 `scripts/diagnostic/display-cold-waterfall.sh`（注意目录是已存在的单数 `diagnostic/`，不新建 `diagnostics/`）；CLAUDE 评审 PR 时要求附 cold receipt；receipt 与 Scenario「Overview bootstrap cold latency budget」（specs/overview-data-contracts/spec.md）形成回归契约，未来 regression 触发 Scenario 违约而非"只是一次性证据缺失"。
- **R6**：未来若引入新 hydro variable（例如 sediment）再次踩同样雷。
  **Mitigation**：本 change 不修这类未来风险（独立 epic「hydro-variable catalog hygiene」可考虑加 `(variable)` 列上的轻量 index 或 ingestion 时 sentinel 行）；本 change scope 只删 water_level。

## Migration Plan

- **Pre-merge**：本地 ruff + pytest + tsc + pnpm test + openspec validate；force-refresh 瀑布在 node-27 cold 实测前后对比记录到 worklog。
- **Merge**：单 epic 拆 4-5 个 sub-issue（按模块边界），逐 PR 合入 master；每个 PR 独立可 revert。
- **Post-merge node-27**：`git pull --ff-only` → `cd apps/frontend && pnpm install --frozen-lockfile && pnpm build` → `rsync dist/ to /home/nwm/NWM/apps/frontend/dist/` → `docker restart api-web-1 api-worker-1` → cold force-refresh 瀑布 receipt（diff against canonical 21.8s 基线，预期 < 200 ms p95）+ 浏览器 cold first-paint 截图 + Network panel TTFB + Performance panel "first interactive river segment click" 时间戳（screen recording 是 nice-to-have；time-stamp 表是必需，对齐 Scenario「Cold first-paint interactivity budget」< 1 s 阈值）。
- **Rollback**：每 PR git revert + node-27 重 ff + 重 build + restart；最坏情况整 epic revert 后 cold 回到 22s（无数据丢失风险，纯前端 + dead variant 删除）。

## Risk Pack Coverage

显式 selected / not-selected 矩阵（核心 11 pack + NHMS 8 domain pack）：

| 核心 risk pack | 状态 | 理由 |
|---|---|---|
| Public API / CLI / script entry | **selected** | OpenAPI `HydroMvtVariable` enum 收紧 + `/api/v1/layers` 路径 + `/api/v1/layers/{layer_id}/valid-times` 路径 + `/api/v1/runs` query；BREAKING note 见 R1；deny 测试 422 见 task 1.4 |
| Config / project setup | not selected | 未触 .env / `infra/env/compute.env` / settings / pyproject |
| File IO / path safety / overwrite | not selected | 无 disk / object store / temp file / publish/delete；不触 forcing / SHUD output / receipt 文件写入路径 |
| Schema / columns / units / field names | **selected** | OpenAPI enum 单点收紧（不动 DB schema / migration / column）；`OverviewDataSnapshot.bootstrap` 形状 PR 3/7 固化；4 spec capability deltas 的 scenarios 即 schema 合同 |
| Auth / permissions / secrets | not selected | 不触 display readonly role（`nhms_display_ro`）、token、secret、模型 admin auth |
| Concurrency / shared state / ordering | **selected** | (a) ranking in-flight cache + cancel-on-unmount（PR 4/7）；(b) loadOverview 两阶段 settle 时序（PR 3/7）；(c) PR 3 → 4 → 5 同一文件接力顺序，preamble 显式声明 |
| Resource limits / large input / discovery | **selected** | (a) `metadata.valid_times` 优先消费消除 N RTT fan-out；(b) cold path 不再扫 92M 行 hypertable；(c) latency budget Scenarios `/api/v1/layers ≤ 200ms` / `first-paint ≤ 1s` 作为回归契约 |
| Legacy compatibility / examples | **selected** | (a) 自家前端唯一消费者（R1）；(b) URL `?layer=water-level` 旧分享链回退到 `discharge`（spec scenario）；(c) BasinDetailPanels `warningDistribution` 空态降级避免误导前 API 消费者 |
| Error handling / rollback / partial outputs | **selected** | (a) backend 422 enum reject；(b) mapBootstrap 阶段 1 reject → scoped bootstrap error；(c) enrichment 阶段 2 单点 reject → scoped panel error 不传播；(d) ranking unmount cancel 不允许 setState |
| Release / packaging / dependency compatibility | **selected** | (a) BREAKING OpenAPI enum；(b) 每 sub-PR 独立可 revert（migration plan）；(c) node-27 部署链 `pnpm build + rsync + restart api-web/worker` |
| Documentation / migration notes | **selected** | (a) `docs/runbooks/api-latency.md` 新增段落（task 7.1）；(b) `docs/runbooks/display-readonly-live-mvt.md` layer 列表更新（task 7.2）；(c) `docs/bugs.md` ledger 加条目（task 6.4） |

| NHMS domain pack | 状态 | 理由 |
|---|---|---|
| Geospatial / CRS / basin geometry | not selected | 不触 PostGIS geometry / projection / basin shapefile / pyproj |
| Hydro-met time series / forcing windows | **selected** | `hydro.river_timeseries` 上 `water_level` variable 从 hot path 退出；删除路径自然失活；不动 ingestion（无 backfill 风险） |
| SHUD numerical / conservation / NaN | not selected | 不触 SHUD runtime / IC / restart / forcing |
| PostGIS / TimescaleDB domain behavior | **selected** | TimescaleDB SkipScan + chunk filter pattern 是 22s 真因，删除 dead variant 后该 hot path 不再触发；不加 index、不改 hypertable、不改 chunk policy |
| Slurm production lifecycle / mock-vs-real parity | not selected | 不触 Slurm / sbatch / production_closure |
| External hydro-met providers / snapshot reproducibility | not selected | 不触 GFS / ERA5 / IFS / CDS provider 接入 |
| Run manifest / QC provenance | not selected | 不触 `run_manifest` / `qc_result` / provider snapshot |
| Published NHMS artifacts / display identity | **selected** | display readonly API 是 published 表面；contract 收紧 + 前端消费方式重组属于 published-artifact identity 维度；node-27 live receipt（PR 6/7）是该 pack 的 evidence floor |

## Invariant Matrix

Governing invariant: water-level dead variant 在 backend (services/tiles/mvt.py + apps/api/routes/flood_alerts.py) / OpenAPI (HydroMvtVariable enum) / frontend (M11Layer union + UI + tests) 整链零残留；`useOverviewDataStore` 单 `loading` 闸门拆为 `mapBootstrapLoading`（地图可交互快路径）与 `enrichmentLoading`（背景），首次河段可点击不阻塞于 enrichment；`/flood-alerts/ranking` 仅在面板挂载或 flood/warning layer 时按需 fetch；`normalizeLayerStates` 优先消费 `apiLayer.metadata.valid_times` 三态分支；默认 discharge 不强制 `flood_product_ready=true`；`/api/v1/layers` cold path 不触 92M-row TimescaleDB SkipScan。

Source-of-truth identity/contract:
- `HydroMvtVariable` enum in `openapi/nhms.v1.yaml`（收紧为 `["q_down"]`）
- `M11Layer` TS union in `apps/frontend/src/lib/m11/queryState.ts`（4-layer canonical）
- `OverviewDataSnapshot.bootstrap` shape `{ basins, layers, layerStates, currentLayerValidTime }`（PR 3/7 固化）
- 4 capability spec deltas（scenarios 即测试合同）

Surfaces:
- Producers: `services/tiles/mvt.py::valid_times_for_layer / tile encoders / popup builders`、`apps/api/routes/flood_alerts.py::_default_layer_catalog / list_layer_valid_times`
- Validators/preflight: FastAPI enum 422、TypeScript `M11Layer` union narrow、`pnpm check:api-types` types regen 一致性、`pnpm tsc --noEmit`
- Storage/cache/query: `hydro.river_timeseries` SkipScan 历史路径（删 water-level 后再也不触发；无 schema 改动）
- Public routes/entrypoints: `GET /api/v1/layers`、`GET /api/v1/layers/{layer_id}/valid-times`、`GET /api/v1/runs`
- Frontend/downstream consumers: `useOverviewDataStore.loadOverview`、`OverviewPage.surfaceSettling`、`M11MapLibreSurface.buildM11RegisteredOverlay`、`M11Controls`、`BasinDetailPanels.warningDistribution`、`normalizeLayerStates`
- Failure paths/rollback/stale state: backend 422 on water_level 请求；frontend URL `?layer=water-level` 回退到 discharge 默认；mapBootstrap 阶段 1 reject → scoped bootstrap error；enrichment 阶段 2 单点 reject → scoped panel error 不传播；ranking 面板 unmount/layer 切回时 in-flight cache 清理
- Evidence/audit/readiness: `docs/runbooks/receipts/display-bootstrap-decoupling-<date>.md`（PR 6/7 产出，含 21.8s 基线 diff + 浏览器 cold first-paint 时间戳）；`openspec validate refactor-display-overview-bootstrap --strict --no-interactive` 全程绿；CI path-scoped gates（backend pytest + frontend tsc + markdown lint + openapi validate）

Regression rows:
- `/api/v1/layers`（runless cold，force-refresh `x-nhms-cache-warm: refresh`）→ p95 ≤ 200 ms（基线 21.8s 21.07s）
- 其他 bootstrap-critical 端点（/runs/pipeline/queue/summary/basin-versions）cold p95 ≤ 500 ms
- `GET /api/v1/layers/water-level/valid-times` → HTTP 422（FastAPI enum validation）
- `GET /api/v1/runs?source=best`（无 `flood_product_ready` filter）→ 返回 frequency-ready 但 flood-incomplete 的 run（如 QHH/Heihe）
- URL `?layer=water-level` → frontend parser 回退到 `discharge`，无 MVT source 注册
- 默认 best+discharge `loadOverview` → 不发 `fetchFloodRanking`、不发 `layerIdsForOverview.map(fetchLayerValidTimes)` fan-out
- `mapBootstrapLoading=false` + `overview.bootstrap` ready → MVT hit layer 已注册 + 河段可点击；`enrichmentLoading=true` 不阻塞 `surfaceSettling`
- `BasinDetailPanels.warningDistribution === undefined` → pending/「未加载」占位（不渲染「全 0 警告」误导态）
- `query.layer` 切到 `flood-return-period`/`warning-level` → 下一次 `fetchRunsPageByStatus` 注入 `flood_product_ready=true`；切回 `discharge` → 不注入 + latest run 重选
- Ranking 面板 unmount / layer 切回 discharge 时 in-flight cache 清理 + 无 setState-after-unmount 警告
- 未变更 sibling consumer：`discharge` / `flood-return-period` / `warning-level` / `river-network` 4 个图层 + segment detail 面板 + basin drill-down 面板的非 water-level 字段渲染不发生回归

## Open Questions

- **Q1 (resolved 2026-06-20)**：mapBootstrap 阶段是否需要等 `fetchModels` 完成？
  **决议**：`fetchModels` 放 **enrichment**（task 3.2b 内执行；非 mapBootstrap）。理由：MVT hit layer 注册（[M11MapLibreSurface::buildM11RegisteredOverlay](apps/frontend/src/components/map/M11MapLibreSurface.tsx)）仅需 layers + 当前 layer 的 valid_time + basin 身份；model→basin 映射用于 basin 详情面板而非首次可点击，归 enrichment 不影响 first-paint < 1 s 阈值。
- **Q2 (resolved 2026-06-20)**：`river-network` layer 的 metadata.valid_times 是否真的可用？
  **决议**：在 #585 node-27 live receipt 阶段必须探测并记录其形状（`==[]` 或 `null` 或非空），并在 receipt markdown 中以表格固化。`normalizeLayerStates` 已设计三态分支（spec scenarios `Metadata.valid_times is intentionally empty (time-less layer)` 不发 fallback；`Metadata.valid_times is missing or null (schema gap)` 才 fallback），无论 node-27 实测哪种形态，前端行为都符合 spec。Receipt 用于固化历史事实，不会触发 spec 改动。
  **反向校验注释**：如 node-27 receipt 探测到 river-network 返回 `undefined`/`null`（即 schema gap 而非 time-less），PR 7/7 docs sync 需在 `docs/runbooks/api-latency.md` 新增段落里追加一条 "fallback hit rate is bounded to 1 layer (river-network)" 说明 + 回链 receipt；如返回 `[]`（time-less），无需 docs 追加，只在 receipt 表内固化即可。任一形态均不构成 spec drift（三态分支已固化）。

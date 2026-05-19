# 项目进度

最后更新：2026-05-19，测试环境。

用途：作为跨 session 继承的项目真实进度索引。本文只保留“当前能做什么、已完成哪些闭环、还剩哪些明确方向”，避免把历史 review 细节继续堆进进度页。

## 一句话状态

M11 全国总览与流域钻取 Epic #159 已完成并关闭。#160-#165 全部关闭，最后 PR #171 已合并到 `master`，merge commit 为 `da26dc331453988612ce4d4d5818948309d117ef`。

项目已经具备一套可重复运行的 production-like 证据链：Slurm、对象存储/Basins、气象源/QC、staging E2E、全国规模性能、生产运维安全与 runbook readiness 都有 opt-in validation lane、结构化 evidence、fast regression test 和文档入口。

前端入口已经从旧的单一预报地图推进到 M11 map-first 产品骨架，并开始补齐 M12 河段详情：`/` 和 `/overview` 为全国总览，`/basins/:basinId` 为流域钻取，`/segments/:segmentId` 为可恢复的河段预报详情，`/forecast`、`/flood-alerts`、`/monitoring` 保持可访问。

重要边界：默认 fast/production-like lane 不会伪造最终生产就绪。真实 final production readiness 仍需要在目标环境补齐 live backend auth、live alert sink delivery、live rollback execution、accepted live dependency proofs，以及真实外部系统运行证据。

## 已完成里程碑

- Epic #120 已完成并关闭；子 issue #121-#126 全部关闭。
- M9 Basins 已完成并关闭：Epic #133，子 issue #134-#139 全部关闭；PR #144、#145 已合并。
- M10 Production Closure 已完成并关闭：Epic #146，子 issue #147-#152 全部关闭；PR #154-#158 已合并。
- M11 Overview + Basin Drilldown 已完成并关闭：Epic #159，子 issue #160-#165 全部关闭；PR #166-#171 已合并。
- M12 Segment forecast detail 已完成本地实现：新增 `/segments/:segmentId` 全屏详情路由、basin/forecast/flood handoff、scoped segment identity 恢复、KPI/缩略图/多源曲线/阈值/partial panels/timeline，并保留无 station/forcing/weather 合同时的显式不可用状态。
- 当前远端主干：`origin/master` @ `da26dc331453988612ce4d4d5818948309d117ef`。

## M10 生产闭环结果

M10 对应 OpenSpec change：`openspec/changes/m10-production-closure/`。

| Issue | 状态 | 交付能力 |
|---|---:|---|
| #147 Real Slurm + SHUD workload closure | 完成 | `nhms-production validate-slurm`，Slurm preflight、sbatch 渲染、fake/real sacct schema、array partial success、retry/cancel、SHUD QC blocker、redacted environment evidence |
| #148 Production object store + Basins copied-data closure | 完成 | `nhms-production validate-object-store`，synthetic copied Basins、local production-like object store、package publish/checksum verification、registry/API/runtime object URI contract、cleanup/rollback evidence |
| #149 Live meteorology ingestion + QC closure | 完成 | `nhms-production validate-met`，GFS/IFS/ERA5 deterministic fixture、CLDAS restricted evidence、raw/canonical/forcing/QC/best-available lineage、negative QC blockers |
| #150 Staging E2E forecast/analysis closure | 完成 | `nhms-production validate-e2e`，source -> canonical -> forcing -> Slurm -> parse -> frequency -> tile -> API/frontend 的 deterministic stage evidence bundle |
| #151 National-scale MVT/performance closure | 完成 | `nhms-production validate-scale`，large fixture、thresholds、query plan/hash/p95、GeoJSON/MVT blocker、frontend timing、resource bounds |
| #152 Production ops/security/runbook readiness | 完成 | `nhms-production validate-ops`，preflight、config validation、auth/RBAC matrix、audit/redaction、alerts、rollback drills、dependency closure、summary evidence |

M10 最终 CI 状态：PR #158 最新 head 的 Markdown Lint、OpenAPI Validate、JSON Schema Validate、SQL Migration Dry Run、Unit Tests、Frontend Build 全部通过。最终 cross-review round 27 clean，Phase 7 independent final review clean。

## M11 全国总览与流域钻取结果

M11 对应 OpenSpec change：`openspec/changes/m11-overview-basin-drilldown/`。

| Issue | 状态 | 交付能力 |
|---|---:|---|
| #160 M11 route foundation | 完成 | `/overview`、`/forecast`、`/flood-alerts`、`/monitoring` 路由骨架，导航标签，基础 app shell，查询状态基础 |
| #161 M11 data contracts | 完成 | overview/basin view-model、数据归一化、API 组合、缓存/去重、聚合端点决策规则 |
| #162 M11 map controls | 完成 | M11 shared MapLibre surface、terrain/satellite/vector 底图、source/scenario 控制、水文/气象/基础图层控制、legend、valid-time timeline |
| #163 National overview page | 完成 | `/` 和 `/overview` 默认全国总览，左侧流域/图层面板，中央全国地图，右侧运行态势，底部时间轴，流域 popup 和 drill-down handoff |
| #164 Basin drill-down workflow | 完成 | `/basins/:basinId` 流域分析页，流域身份/版本、河段列表、搜索/预警筛选、行选择、URL 状态恢复、forecast handoff |
| #165 Basin map delivery | 完成 | 流域河网地图、选中河段高亮、segment detail panel、趋势 sparkline、lineage/forecast handoff、route state 不变量和 PostGIS SQL CI 修复 |

M11 最终 PR #171 CI 状态：Markdown Lint、OpenAPI Validate、JSON Schema Validate、SQL Migration Dry Run、Unit Tests、Frontend Build 全部通过。最后一次本地验证覆盖：`uv run ruff check .`、后端重点测试 `171 passed, 3 skipped`、OpenSpec validate、`git diff --check`、前端单测 `270 passed`、`tsc --noEmit`、`check:api-types`、frontend build、Playwright E2E `34 passed`、preview E2E `3 passed`。

## 当前系统能力

### 后端与数据链路

- FastAPI 后端已实现 forecast、models、pipeline、hindcast、flood alerts、best-available、state snapshots、data-source 等路由。
- 数据库 migration `000001`-`000014` 覆盖 core/met/hydro/flood/map/ops schema、索引、pipeline 字段、best-available lineage 等。
- OpenAPI 契约位于 `openapi/nhms.v1.yaml`，前端类型由该文件生成。
- JSON Schema 覆盖 run manifest、run status、QC result、pipeline job，并有 examples 校验。
- GFS、ERA5、IFS adapter 已实现并有 mock/test 覆盖；IFS 多源预报能力已接入。
- Canonical conversion、forcing production、SHUD runtime adapter、output parser、state manager、洪水频率拟合、重现期计算、tile publisher 已实现。
- Orchestrator 支持 forecast/analysis/hindcast 链路、Slurm job array、retry/cancel、partial success、publish stage、pipeline persistence。
- Real Slurm gateway 支持 `sbatch`、`sacct`、`scancel`、`sinfo`、array job、日志读取、模板白名单，并有 fake-binary smoke。
- Basins discovery、publish、registry import、runtime/API consumption、frontend asset fixture 已形成完整 M9 资产链路。

### 前端

- 有效前端为 `apps/frontend`：Vite + React + TypeScript + MapLibre + ECharts + Zustand + OpenAPI-generated types。
- 已实现路由：
  - `/`、`/overview`：M11 全国总览，左侧 basin/source/layer 控制，中央全国地图，右侧运行态势，底部 valid-time timeline。
  - `/basins/:basinId`：M11 流域钻取，流域身份/版本、河段列表、搜索/预警筛选、按径流/重现期/预警着色的有界河网、河段 hover/click、URL `segmentId` 同步、河段详情、趋势 sparkline、forecast/lineage handoff。
  - `/segments/:segmentId`：M12 河段预报详情，保留 `source`、`cycle`、`validTime`、`basinVersionId`、`riverNetworkVersionId`、`segmentId` 身份；先校验 scoped river segment，再请求同一 basin/network/segment 的 forecast series；展示 KPI、120x90 位置缩略图、多源曲线、analysis/forecast 分割、IFS 6d 标签、Q2/Q5/Q10/Q20/Q50/Q100 阈值、station/forcing/weather/frequency partial 状态、底部时间线和 stale valid-time correction。
  - `/forecast`：预报河网地图、河段选择、预报侧栏，并可与 M11 流域页互相传递 basin/version/segment/source/cycle/validTime 上下文。
  - `/flood-alerts`：洪水预警统计、排名、ticker、地图、时间轴、详情。
  - `/monitoring`：流水线监控工作台、阶段、作业表、队列摘要、趋势面板、operator RBAC gate。
- M11 shared controls 支持 terrain/satellite/vector 底图切换，GFS/IFS/GFS+IFS/Best Available source state，hydrology layer legend，API `valid_times[]` 驱动时间轴，stale valid-time correction。
- M11 overview/basin 数据层复用 basins、basin versions、models、river segments、forecast series、flood alerts、pipeline、jobs、queue、metrics、layers、lineage 等现有 API，并在前端归一化 freshness、quality、source provenance、partial error 和 unavailable reason。
- M11 数据合同在 `overviewDataContracts.ts` / `overviewData.ts` 归一化；缺失/超预算几何、compare 聚合缺口、lineage/trend/comparison 不可用均以局部状态呈现，不伪造地图或预报数据。
- Forecast UI 支持 GFS/IFS scenario、多曲线图、analysis/forecast 区分、来源/周期归因、IFS 144h 可用时效标注。
- Flood warning UI 支持预警等级过滤、时间轴播放、排名、河段详情、API-base-aware tile URL。
- Monitoring UI 支持 pipeline status/jobs 轮询、source/cycle 选择、作业筛选/分页、日志弹窗、队列深度、趋势组件。
- 前端测试覆盖 M11 路由/query state、overview 和 basin interaction、map controls、timeline、data contracts、API base 行为、route preview、mock API E2E、build、bundle size。

### Basins 与样例数据

- 开发环境通过 `data/Basins -> /volume/data/nwm/Basins` 软链接接入河网/流域等 Basins 数据；这是开发期依赖，不是可迁移 artifact。
- `data/Basins` 当前可发现 13 个 SHUD 模型目录：`qhh`、`heihe`、`kashigeer`、`weiganhe`、`xinanjiang_upstream`、`hetianhe`、`qinyijiang`、`keliya`、`tailanhe`、`zhaochen/{WEM,HHY,MC,BST}`。
- Basins package/registry import 已支持 inventory、checksum、runtime/GIS/CALIB evidence、forcing metadata、manifest URI、registry lineage、inactive model activation audit。
- 真实 Basins smoke 为 opt-in：仅在环境变量和路径满足时运行。

## 设计、效果图与当前实现缺口

设计基准主要来自 `docs/spec/06_frontend_gis_design.md`、`docs/spec/06B_frontend_ui_design_spec.md`、`docs/modules/15_frontend_application_design.md`，效果图索引目前落在 `docs/images/roadmap_*.png`。M11 已覆盖效果图 1 和效果图 2 的核心操作路径，但不是完整产品视觉终态。

| 设计/效果图目标 | 当前实现 | 主要缺口 |
|---|---|---|
| 效果图 1：全国总览 | `/`、`/overview` 已实现 map-first shell、左侧流域/图层/source 控制、中央全国 MapLibre surface、右侧运行态势、底部 timeline、basin popup、monitoring/flood handoff | 视觉仍偏工程化组件，未按效果图做最终像素级打磨；一级/二级流域树依赖现有 basin metadata，不保证完整八大流域层级；真实气象/DEM/base overlay 未接入，只显示 unavailable；全国真实边界/河网覆盖取决于 Basins/registry 数据 |
| 效果图 2：流域详情 / drill-down | `/basins/:basinId` 已实现流域身份、版本、bbox/fallback、河段列表、搜索、预警筛选、地图选择、URL segment state、选中河段 detail、trend sparkline、forecast handoff | 城市/站点标签缺数据合同，当前明确显示暂不可用；河段 hover tooltip 和地图动画效果仍有限；右侧趋势为轻量 sparkline，不是完整 48h/7d 专业分析面板；流域内真实视觉密度依赖河网 geometry 数据质量 |
| 效果图 3：河段预报曲线全屏详情 | `/segments/:segmentId` 已实现可恢复全屏 route、basin/forecast/flood handoff、scoped identity 校验、KPI、120x90 位置缩略图、多源主图、scenario toggles、analysis/forecast 分割、IFS 6d 标注、Q2/Q5/Q10/Q20/Q50/Q100 阈值、partial panels、底部时间线 | station/forcing/weather 合同仍缺，当前只显示 unavailable/restricted/partial，不伪造数据；频率曲线参数合同缺失，当前展示离散阈值而非完整曲线；segment route 未携带 run_id，lineage API 无法安全查询时显示不可用；box zoom/crosshair 和像素级视觉打磨仍未完成 |
| 效果图 4：洪水预警总览 | `/flood-alerts` 已有统计、ranking、ticker、GeoJSON/return-period map、timeline、segment detail；PostGIS SQL 和 CI dry-run 已修复 | 仍未升级到真正生产 MVT/PBF tile delivery；和 M11 overview/basin 的视觉语言还需统一；点击 ranking 到全屏 segment detail 的目标页仍缺失 |
| 效果图 5：气象空间栅格 | `/meteorology?tab=grid` 已启用 query-state tab、PRCP/TEMP/RH/wind/Rn/Press 与 GFS/IFS/ERA5/CLDAS/Best Available 合同夹具、严格 validTime 规范化、色标、透明度/等值线/站点叠加控件、格点查询弹窗（lon/lat/source/cycle/validTime/unit/time resolution/spatial resolution）、可请求的区域统计 bbox 状态、多源对比状态、CLDAS restricted 和 tile/query/area-stat unavailable/validation 显式状态；URL normalization 会保留 over-limit 搜索证据；未伪造气象值 | 仍缺真实 TiTiler/PNG/MVT 栅格瓦片服务、真实 grid-cell query/area-stat 返回值、真实 live best_available 栅格差值；当前为前端合同夹具和不可用状态，不代表生产全国栅格发布已完成 |
| 效果图 6：气象代站查询 | `/meteorology?tab=stations` 已启用 station inventory、流域筛选、搜索、排序、完整度/QC、站点 marker/popup/detail、PRCP/TEMP/RH/wind/Press forcing 图、缺测/QC 区间、相邻站点高亮语义、无结果与 forcing unavailable 状态；deep-linked `stationId` 先在筛选后/分页前集合中解析，默认页外的有效站点会显式追加/标注，不回退成其他站点；筛选排除站点时清理旧详情 | 仍缺后端扩展站点合同中的完整 QC/adjacent/forcing metadata、真实分页大列表、真实 Thiessen/Voronoi、真实长时段 sample limit 服务端校验；当前不伪造未接入站点或 forcing 样本 |
| 效果图 7：流域/模型资产管理 | `/system/model-assets` 已实现只读管理页，按 `model_admin`/`sys_admin` gated；NavBar 仅对允许角色显示“模型资产”；复用 `/api/v1/models` 和 `/api/v1/models/{model_id}`，不新增后端/OpenAPI endpoint。页面包含流域/模型树、搜索、启用/停用筛选、URL `modelId` 恢复、筛选排除后的 stale-detail 清理、六张 KPI 卡、元数据、来源/包 lineage、版本时间线、依赖图、产品资产列表、空间小地图 degraded state；`modelAssets` store/view-model 递归清理本地路径、Windows 绝对路径、`file://`、URI userinfo/query/fragment，并限制产品列表 12 条、空间预览 50 features/2,000 vertices。`version_admin` 未作为运行时角色加入。 | 当前范围明确为 readonly；模型包创建/编辑/删除/发布、active model 切换、真实大规模 MVT/geometry publication 和 mutating audit workflow 仍按 M14 OpenSpec non-goals 后续处理 |
| 效果图 8：产品监控 | `/monitoring` 已有 pipeline summary、stage cards、jobs table/filter/pagination、queue、trend、log modal、operator RBAC gate | 与效果图的最终 dashboard 信息层级/视觉密度仍需打磨；当前 local dev role override 不是生产 auth；缺 live alert sink、真实 backend identity provider 证明 |
| 全局 UI 规范 06B | 已引入 M11 visual tokens、56px nav、左右面板、64px timeline、状态色/预警色、icon buttons、responsive collapse | 仍需做真实截图对照和像素级 visual QA；部分控件仍是 Tailwind 工程样式而非完整 design-token 抽象；缺正式 effect-image visual baseline 自动比对 |
| 地图与性能 | MapLibre surface 已复用，支持 basemap switch、GeoJSON budget、geometry guards、stale-state 修复 | 真实生产 MVT、全国真实数据压测、PostGIS tile clipping、`application/x-protobuf` 响应路径仍未完成；当前 M11 可用但不是最终全国规模视觉性能终态 |

## 剩余生产化边界

这些不是 M10/M11 fast closure 的缺口，而是真实生产上线前必须在目标环境补齐的 live proof：

- live backend auth / identity provider 行为。
- live alert sink delivery。
- live rollback execution。
- accepted live dependency proofs。
- 真实 Slurm 生产 workload、长日志回收、accounting、失败重试长链路。
- 真实对象存储，如 MinIO/S3，而不是 local object store。
- live GFS/IFS/ERA5 数据下载稳定性与凭据管理。
- CLDAS adapter、授权数据接入和 best_available 生产路径。
- 全国规模真实数据、真实 PostGIS query plan/压测、真正 `application/x-protobuf` MVT。

## 有效代码入口

- 后端：`apps/api`
- 前端：`apps/frontend`
- 编排：`services/orchestrator`
- Slurm gateway：`services/slurm_gateway`
- Tile publisher：`services/tile_publisher`
- Workers：`workers/*`
- sbatch 模板：`infra/sbatch`
- 生产闭环 lanes：`services/production_closure/*`

已清理 legacy 占位目录：`apps/web`、hyphenated worker/service 目录、`workers/sbatch_templates`。后续不要在这些路径恢复实现。

## 常用验证命令

```bash
uv run ruff check .
uv run pytest -q
openspec validate m10-production-closure --strict --no-interactive
openspec validate m11-overview-basin-drilldown --strict --no-interactive
```

真实 DB integration：

```bash
NHMS_RUN_INTEGRATION=1 \
NHMS_INTEGRATION_DATABASE_URL=postgresql://nhms:nhms_dev@localhost:5432/nhms \
uv run pytest -q -m integration
```

前端：

```bash
cd apps/frontend
corepack pnpm test
corepack pnpm exec tsc --noEmit
corepack pnpm run check:api-types
corepack pnpm build
corepack pnpm check:bundle
corepack pnpm exec playwright test
corepack pnpm run test:e2e:preview
```

M10 production-like lanes 均为 opt-in，典型形式：

```bash
NHMS_RUN_PRODUCTION_CLOSURE=1 \
uv run nhms-production validate-ops \
  --evidence-root artifacts/production-closure \
  --run-id <run_id>
```

完整验证说明见 `docs/VALIDATION.md`。

## 下一步优先级

1. **M12 后续数据合同**：为 `/segments/:segmentId` 补 station/forcing/weather/frequency-curve/run lineage 合同后接入真实 PRCP/TEMP/RH/wind/Press、forcing 小图、完整频率曲线和 lineage；继续禁止伪造未接入数据。
2. **效果图 5/6：气象数据产品页**：把 M13 前端合同夹具升级为后端 API/OpenAPI 合同和真实 tile/query/area-stat/station-series 数据源；继续禁止伪造未接入图层。
3. **效果图 7 后续**：在 readonly 页面基础上补后续经审计的 mutating workflow；创建/编辑/删除/发布、active model switching 仍需单独 OpenSpec/backend auth/audit 设计。
4. **真实 MVT 与全国规模性能**：从 GeoJSON 兼容路径升级到 PostGIS tile clipping + MVT 编码，并补全国真实数据压测。
5. **真实生产验证**：在目标环境跑 live auth、alert sink、rollback、真实 Slurm、真实对象存储、真实气象下载。
6. **CLDAS 接入**：实现 adapter、授权数据质量检查、best_available 生产路径。
7. **生产身份认证/授权**：把当前 dev/test override 和前端 gate 推进到完整 backend auth/RBAC 系统。

## M15 前端视觉收敛状态

- GitHub issue #176 / OpenSpec `m15-frontend-visual-conformance` 已实现机械视觉证据：`/overview`、`/basins/basin-demo?...segmentId=seg-009`、`/flood-alerts`、`/monitoring` 在 `1920x1080`、`1440x900`、`1280x900` 均由 deterministic Playwright 夹具截图并校验。
- 扩展证据已覆盖 `/segments/seg-009?...`、气象 grid/stations 两条 URL、`/system/model-assets?modelId=model-demo`；非 happy-state 证据在 canonical `1440x900` 覆盖 overview loading/partial/error、basin empty/partial/error、flood empty/warning/error、monitoring empty/failed/RBAC denied、segment missing/chart error、meteorology grid unavailable/restricted、stations empty/detail unavailable、model-assets denied/loading/redacted error。
- 证据命令：`cd apps/frontend && corepack pnpm run test:e2e:m15-visual`。Manifest：`.codex/evidence/issue-176/manifest.json`；截图：`.codex/evidence/issue-176/screenshots/`。本地 manifest SHA 解析为运行时当前 git `HEAD`；CI/PR 证据必须使用 `GITHUB_SHA` / `CI_COMMIT_SHA` 或等价 frozen PR SHA，禁止 `local-uncommitted` 等 placeholder。
- 治理文档：`apps/frontend/e2e/m15-visual-evidence.md` 记录 review checklist、acceptable deltas、no-overlap criteria 和 blocking criteria。截图二进制仍为本地 volatile evidence，不纳入仓库。
- 已收敛共享根：06B/M15 CSS token aliases、focus ring、shared card/button/badge/select/tabs/dialog/toast radius/shadow/spacing/control-height/z-index、M11 nav/panel/timeline tokens、warning/status palette。未改变 backend/OpenAPI contract、生产 time-series semantics、RBAC 数据边界、M14 redaction/model-assets sanitization。

## 注意事项

- 工作区可能存在 `.codex/`、`data/`、`docs/images/`、`node_modules/`、`dist/`、`__pycache__` 等本地或生成文件；不要误 stage。
- 当前主 worktree 的 `master` 可能落后 `origin/master`；判断 M11 完成度时以 `origin/master` 和已合并 PR #166-#171 为准，不要只看本地 `HEAD`。
- 历史 OpenSpec proposal/tasks 保留当时路径和任务状态用于审计；判断当前完成度以源码、测试、`docs/VALIDATION.md` 和本文为准。
- 生产环境迁移不能复用 macOS `.venv` 或 `node_modules`；Linux 目标环境按 `AGENTS.md` 重新 `uv sync` 和 `corepack pnpm install`。

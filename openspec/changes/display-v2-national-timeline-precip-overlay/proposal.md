## Why

全国展示页今天没有时间轴与起报时次，河段颜色是 100h 滚动窗口末端时次、且全国流量瓦片不看数据源（同一张图 gfs/IFS 混源）；全国比例尺下静态河网过淡；页面没有任何格点降水场可看，代站图层也不携带降水值。用户需要 V2.0 版本：更醒目的品牌标题、更密的全国河网、一个与流量同源同周期同时次的「过去 24h 累积降水」叠加层，以及底部时间轴 + 起报时次 + GFS/IFS 选择，让「降水 → 汇流」在同一时间轴上可对照。

设计 oracle：`docs/plans/2026-09-03-display-v2-header-river-precip-timeline.md`（含 14 条已拍板决策与实机事实）。

## What Changes

- 标题改为「全国水文模拟系统（V2.0）」（全角括号），加粗放大；header 抬高；赞助商图放大。
- 全国静态河网 `stream_type` 阈值每级下移一档（缓存查询版本递增），前端 z<6 不再因流量叠加打折且加粗；以 node-27 z3/z4 坐标数 <50k 为合并 go/no-go。
- **BREAKING（目录契约）**：`/api/v1/layers` 的 `discharge` 条目 `tile_url_template` 改为带 `{source}/{cycle}` 的新全国路由 `/api/v1/tiles/hydro-national/{source}/{cycle}/{variable}/{valid_time}/{z}/{x}/{y}.pbf`，`required_placeholders` 统一为 `["source","cycle","valid_time","z","x","y"]`（沿用后端现有含 z/x/y 的形状），`metadata.valid_times` 改为 `(default_source, default_cycle)` 的列表；旧无源路由保留为别名、行为不变。
- 新增 `GET /api/v1/layers/discharge/cycles?source=`（各活动河网交集，fail-closed）；`valid-times` 支持 `source`+`cycle` 查询，返回从起报时刻起 3h 步长的全范围列表。全部 API 时间实例统一 `YYYY-MM-DDTHH:MM:SSZ`（秒精度、无小数秒），路径段、列表与缓存文件名同一拼写。
- 新增「过去 24h 累积降水」栅格图层：node-22 发布 copyback 把 canonical 降水 `.nc` + `grid.json` 镜像到 NFS；node-27 display API 跨周期取 8 个 3h 切片求和，渲染 Web-Mercator 六级调色板 PNG（numpy+zlib，无新依赖），文件缓存下发；`/api/v1/layers` 新增 `precip` 条目；一次性回填现存周期；镜像纳入 retention 剪枝。
- 前端：底部玻璃风格控制条（起报时次选择、GFS/IFS 分段开关、复用 `M11Timeline`），默认停在 lead=0；降水为布尔开关（默认开，`precip=0` 关）跟随水文 source/cycle/valid_time；全国尺度不再提供 Best Available；图例叠加降水六级；浮层位移。
- 预热脚本扩展到 z3–4 × 双源 × 各源自己的最新周期全部时次 + 降水 PNG；某源 cycles 为空则该源零请求，不伪造周期。
- `openapi/nhms.v1.yaml` 手工补齐 4 条新路由与 `/api/v1/layers`、`valid-times` 的形状变化（无生成器；drift 测试按等值比对），再 `pnpm generate:api` / `check:api-types`。
- 降水 PNG 文件缓存 `precip/<storage_source>/<cycle_token>/` 与 canonical 镜像同水位剪枝。

## Capabilities

### New Capabilities
- `precipitation-raster-overlay`: 过去 24h 累积降水 PNG 渲染/索引端点、跨周期切片解析、fail-closed 窗口、`precip` 目录条目与前端 image-source 叠加/图例/URL 开关。
- `national-river-density`: 全国静态河网 `stream_type` 阈值表、查询版本换代、前端低缩放不打折加粗、node-27 坐标数 go/no-go。
- `canonical-precip-copyback`: q_down 发布后镜像 canonical 降水产品与 grid.json 到 copyback root（幂等、不阻塞发布）、一次性回填脚本、retention 同水位剪枝。

### Modified Capabilities
- `overview-data-contracts`: `discharge` 目录条目的全国模板/占位符/`valid_times` 来源随 BREAKING 改动更新（新增 `default_source`/`default_cycle`，交集为空时 `default_cycle: null` + `valid_times: []`）。
- `mvt-tile-contract`: 全国 discharge 瓦片新增 `{source}/{cycle}` 路由并成为目录 canonical URL；旧路由保留为别名；缓存 key/查询版本纳入 source+cycle；M11Shell fixture 随之更新。
- `map-layer-timeline-controls`: 全国尺度源选择改为显式 GFS/IFS（无 Best）；新增起报时次选择（交集 fail-closed）；时间轴 3h 步长全范围、默认 lead=0；降水气象图层由「未实现禁用」改为「已实现可切换」。
- `frontend-mvt-layer-consumption`: 有效时次按 `(source, cycle)` 从 cycles/valid-times 端点获取，`metadata.valid_times` 仅作默认周期；overlay builder 用 per-cycle 列表校验；`precip` 布尔状态序列化规则。
- `national-overview-page`: header 品牌标题文案/字号与赞助商尺寸。

## Impact

- 后端：`services/tiles/mvt.py`（阈值表、hydro-national source/cycle SQL、cycles/valid-times、两个查询版本号）、`apps/api/routes/hydro_display.py`（新路由、cycles、precip 目录条目）、新模块 `services/precip/`、新路由 `apps/api/routes/precip.py`、`services/tile_publisher/publisher.py`（canonical 镜像）、`scripts/canonical_precip_copyback_backfill.py`（新）、`scripts/node27_raw_retention.py`、`scripts/node27_mvt_prewarm.py`。
- 前端：`SiteHeader.tsx`、`m11MapPrimitives.tsx`、`m11MapBuilders.ts`、`queryState.ts`、`overviewDataContracts.ts`、`OverviewPage.tsx`、`M11FloatingControls.tsx`、`M11Controls.tsx`（复用 timeline）、新 `M11PrecipOverlayPrimitive`、stores。
- 契约文件：`openapi/nhms.v1.yaml`（手工维护，4 条新路由 + `/api/v1/layers` 与 `valid-times` 形状）、`apps/frontend/src/api/types.ts`（`pnpm generate:api` 产物）。
- 测试：`tests/test_hydro_display_mvt_scaling.py:175`、`tests/test_api_contract.py:1409`、`tests/test_openapi_drift.py`、`tests/test_openapi_31_contract.py`、`tests/test_node27_mvt_prewarm.py`、`M11Shell.test.tsx` fixture、新增 precip/copyback/cycles 测试与 vitest。
- 运维：node-22 一次回填（钉住解释器，不 `uv sync`）；node-27 部署 receipt（河网坐标数、瓦片冷热耗时、PNG 耗时、`df -h /home`）；NFS 新增 ≈1.2 GB 有界镜像。
- 无 DB schema 变更；node-22 Slurm/SHUD 链路不变。

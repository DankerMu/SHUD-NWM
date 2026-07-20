# display_readonly Live PostGIS MVT Runbook

排查/启用 node-27（`display_readonly`）的 live PostGIS 矢量瓦片（MVT）。对应 issue #343（[M26-7]），已由 #351 以 2026-06-08 live receipt 闭合。

> **Current topology warning (2026-06-29)**: 本 runbook 保留 2026-06-08
> live receipt 的历史排障上下文；不要把其中的 node-22 `210.77.77.22:55433`
> 当作当前 display DB 配置。当前物理部署中 active primary PostgreSQL 在 node-27
> 本机 `:55432`，node-22 `:55433` 是 historical PG，已 archived/stopped，
> 仅作显式 rollback archive，当前不应连接。
> 新配置以 `infra/env/display.example`、`docs/governance/ROLE_BOUNDARY.md` 和
> `docs/runbooks/two-node-production-e2e-plan.md` 为准。

## 背景与原始症状

M26 初验时 node-27 实测：`/api/v1/layers` 返回 `[]`、river-network 瓦片 **424**、hydro 瓦片 **409**。
需确认是只读节点故意关闭 live tile、图层未注册、还是只读副本缺 tile 函数/数据。

## 根因（7.1）

只读节点 **未启用 live PostGIS MVT 特性开关**，且图层 catalog 因此未注册：

- `NHMS_ENABLE_LIVE_POSTGIS_MVT` 未置 `true` → `_require_live_postgis_mvt()` 直接抛 **424**（`MVT_LIVE_POSTGIS_UNAVAILABLE`）。
- 开关关闭时 `/api/v1/layers` 图层目录为空（`[]`），前端 overlay 无从点亮。
- 2026-06-08 historical receipt 当时数据并不缺：`display_readonly` 经只读角色 `nhms_display_ro`
  连 node-22 的 `nhms` 库（`210.77.77.22:55433`），
  业务表分布在 `core` / `hydro` / `map` / `flood` / `met` 等 schema（`public` 仅 PostGIS 系统表），
  几何与时序数据齐备。所以 424/409 是**运行配置**问题，不是只读副本能力缺失。

## 决策（7.2）

**在 display_readonly 启用 live MVT**（已落地），分工：

- 河网几何走 **national river-network MVT**（
  `/api/v1/tiles/river-network-national/{z}/{x}/{y}.pbf`）：低缩放按持久化
  `stream_type`/`Type` 先筛选再简化，负责视觉常显且不注册为点击层。
  历史 `/geo/national-basin-river.geojson` 已退出浏览器关键路径，避免 45 MB
  解码和未来新增流域造成静态包线性增长。
- 流量 / 水位 / 洪频走 **live PostGIS MVT overlay**（`hydro-national` 等端点）做上色与点击。
  output segment 应继承 `river.shp.Type`，全国低 zoom MVT 用真实河级优先筛选，
  `Type` 缺失的历史 segment 才使用流量分位回退；点击 feature 必须携带完整的
  segment / basin-version / river-network-version 身份。
- 不引入离线预生成 tile 发布层；live 查询必须走热路径缓存。若 DB role 有
  `map.tile_layer`/`map.tile_cache` 写权限则写 DB cache，否则用
  `NHMS_MVT_FILE_CACHE_DIR` 本地 PBF 文件缓存兜底。业务性能优先于把 display
  角色机械地维持为全表只读。

## 当前启用配置

当前 node-27 `display_readonly` runtime env 应使用 node-27 active PG（本机 `:55432`）
上的只读角色。不要复制历史 receipt 中的 node-22 `210.77.77.22:55433`；
它是 historical do-not-connect archived/stopped rollback-only 状态。

`infra/env/display.env`（站点实际 secret 不入库）：

```bash
NHMS_SERVICE_ROLE=display_readonly
NHMS_ENABLE_LIVE_POSTGIS_MVT=true
NHMS_MVT_FILE_CACHE_DIR=/home/nwm/.cache/nhms/mvt
NHMS_DISPLAY_WORKERS=2
DATABASE_URL=postgresql://nhms_display_ro:change-me@127.0.0.1:55432/nhms
OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store
```

`infra/env/display.example` 记录 display env 模板；当前物理 DB 归属以
`docs/governance/ROLE_BOUNDARY.md` 为准；node-27 display 启动检查见
`docs/runbooks/two-node-production-e2e-plan.md`。`infra/compose.display.yml`
将 `NHMS_ENABLE_LIVE_POSTGIS_MVT` 透传给 `display-api`。

改后重启服务：`bash scripts/ops/start-display-api.sh`（issue [#597](https://github.com/DankerMu/SHUD-NWM/issues/597)）。
脚本流程：preflight 检查 env file + venv →
source `infra/env/display.env`（`set -a` 全量 export）→
断言 `DATABASE_URL` / `NHMS_ENABLE_LIVE_POSTGIS_MVT` 非空 →
创建并校验 `NHMS_MVT_FILE_CACHE_DIR`（未设置时默认 `$HOME/.cache/nhms/mvt`）→
SIGTERM 既有 uvicorn（10s timeout + SIGKILL 兜底）→
安装并启动 `nhms-display-api.service`（开发环境无 user systemd 时才走 detached fallback）→
等 `/health`（root）200 →
跑 `/api/v1/models` basin_id 非空 smoke check
（PR [#596](https://github.com/DankerMu/SHUD-NWM/pull/596) 同类回归即时报警）。
原先 runbook 引用的 `/tmp/start_display.sh` 不存在于仓库，
且其 ad-hoc 流程不 source env file，已被本脚本取代。

node-27 autopipeline 每次 publish/coverage 后调用
`scripts/node27_mvt_prewarm.py`，有限并发预热中国默认视野 z3/z4 的基础河网与
当前最新 valid time 流量瓦片。同一 cache key 由跨进程 `flock` single-flight
保护，多 worker 和预热并发不会重复执行 PostGIS 生成。

## node-27 Live Receipt（2026-06-08，本机实测）

```text
NHMS_ENABLE_LIVE_POSTGIS_MVT=true
/api/v1/layers                        http=200  layers=5  [discharge, water-level,
                                      flood-return-period, warning-level, river-network]
hydro-national/q_down z6/49/24        http=200  370276 bytes  0.66s  (稳定 ×3)
river-network/<bv> z9/394/198         http=200  0 bytes        (空瓦片：该坐标无河段，正常)
river-network/<bv> z6/49/24           http=413  353 bytes      (低 zoom 整流域超 MVT 预算)
```

> **History note (2026-06-20)**: 上方代码块保留 2026-06-08 当日实测原文以维持
> 历史档案的完整性。**catalog 自 2026-06-20 起已变更**：`water-level` 层（`q_down`
> 之外的第二个 hydro variant）已在 Epic [#579](https://github.com/DankerMu/SHUD-NWM/issues/579)
> PR 1/7..PR 5/7 中从后端 catalog + SQL path + 前端 bundle 全链路删除（live PostGIS
> MVT 上从未被前端真实消费，但在 `/api/v1/layers` 冷路径里贡献了 SkipScan 21.8 s
> 主导成本）。**当前 catalog 4 项**：`discharge | flood-return-period | warning-level |
> river-network`。最新实测见 PR 6/7 receipt
> [`receipts/display-bootstrap-decoupling-20260620.md`](receipts/display-bootstrap-decoupling-20260620.md)
> （node-27 master `122ea95`，冷启 413 ms，≥ 51.9× 提速 lower bound）。

结论：live PostGIS MVT 在只读节点**完全可用**，424/409 根因（开关未启用 + 图层未注册）已消除；#351 已闭合 #343。

## 残留风险与处置

- **首请求偶发 424（瞬态）**：冷连接池 fast-fail，立即重试即 200（receipt 复测 ×3 全 200）。
  客户端应对 tile 424 做一次静默重试，不要据此判定 live MVT 不可用。
- **river-network 低 zoom 413**：整流域河网在 z≤6 超 `MVT_MAX_BYTES`。当前**不阻塞**——河网常显已由静态
  shp 底图承担；river-network MVT 仅在高 zoom 点击/上色用到。若日后要让 river-network MVT 全 zoom 可用，
  按 `services/tiles/mvt.py` 中 `hydro-national` 的渐进 trunk 过滤（按 zoom 提高 `percent_rank` cutoff +
  几何 simplify）同法处理，不要无脑放宽预算。
- **只读边界**：display 侧控制面/业务数据写入仍应拒绝；MVT tile cache 是性能例外，可授予
  `map.tile_layer` / `map.tile_cache` 最小写权限，或保持 DB 只读并依赖
  `NHMS_MVT_FILE_CACHE_DIR` 文件缓存。denied-write live 验证只应用于非缓存控制/业务写。
- **station-MVT**：#342 仍是独立 open backend issue，不属于 #343 的 live MVT closure。

## 相关

- 图层目录：`/api/v1/layers`（`map.tile_layer` 注册 + 代码 metadata）
- 端点：`apps/api/routes/flood_alerts.py`（river-network / hydro-national tile 路由、`_require_live_postgis_mvt`）
- 预算门：`services/tiles/mvt.py`（`MVT_MAX_BYTES`、percent_rank/simplify 分级）
- 静态河网底图：`scripts/geo/build_national_river_geo.py`、`apps/frontend/src/pages/m11/useNationalBasinGeo.ts`

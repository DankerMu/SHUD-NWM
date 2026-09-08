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
`scripts/node27_mvt_prewarm.py`，有限并发预热中国默认视野的以下包络（#2013 起逐源
cycle-aware）：

- 基础河网 `river-network-national` z3/z4/z5（`--zooms`，语义不变——该路由无
  source/cycle 维度），43 张；
- `gfs` 与 `ifs` **各自**经 `GET /api/v1/layers/discharge/cycles?source=` 发现自己的
  最新周期，对该周期 `valid-times` 中**落在「该列表最早那个时次」起算
  `PREWARM_LEAD_HOURS`（12 h）窗口内**的时次预热 z3/z4 全国流量瓦片
  （`DISCHARGE_ZOOMS`，固定，不随 `--zooms` 变）；3 h 网格上即每源 5 个时次 ×
  13 张 = 65 张；
- 窗口内每个时次一张降水 PNG `/api/v1/precip/{source}/{cycle}/{valid_time}.png`，
  每源 5 张。

合计每轮 43 + 2 × 70 = **183** 条预热请求；这个**计划条数**有断言钉住（`tests/test_node27_mvt_prewarm.py`
从 `NATIONAL_DISCHARGE_VALID_TIME_STRIDE_HOURS` 起，全程走生产路径现算），动了步长、
窗口、zoom 集合或源列表都会变红。窗口按**时间戳**截断（`select_lead_window`），
不是取列表前 N 项——`/valid-times` 的排序与步长都不是本脚本可以假设的事实。窗口锚在
**该源已发布的首个时次**（`min()` 取最早，不是 `valid_times[0]`，也不是 cycle 起点、更不是
墙钟）：前端打开时停在的就是这一项（`map-layer-timeline-controls` 的 “lead 0” = 已公告
`valid_times[]` 的首项），而它只有在覆盖未被裁剪时才等于 cycle 起点——
`services/tiles/mvt.py:2171` 的 `window_start = max(cycle, max(start …))` 一旦 clamp，
首项就晚于 cycle。锚在 cycle 的旧口径下，一个首项比 cycle 晚 12 h 以上的源会**一条都预热不到**
且 rc=0；锚在首项之后，「发布了至少一个时次就至少预热一个」是有断言的代码性质。
clamp 后的时次仍落在以 cycle 为原点的 3 h 网格上；**只要它同时还在 168 h 的 PNG horizon 之内**
（`horizon_valid_times` 只列到 `cycle+168h`，见 `services/precip/mirror.py:106-115` 与
`services/precip/constants.py:30` 的 `PRECIP_FORECAST_HORIZON_HOURS`），就照常有降水 PNG。
**「在 3 h 网格上」并不蕴含「在 horizon 之内」**：网格没有上界而 horizon 有，`cycle+171h` 就是
一个在网格上、却在 horizon 之外的时次。真出现这种时次时按下文的越界口径走——该时次的 PNG
不发、计入 `png_out_of_contract` 并置非零退出码，汇总照常打印，这是**有意的吵闹**，不是脚本 bug。
另注：「以 cycle 为原点的 3 h 网格」本身依赖
`NATIONAL_DISCHARGE_VALID_TIME_STRIDE_HOURS`（`services/tiles/mvt.py:122`）与
`PRECIP_STEP_HOURS`（`services/precip/constants.py:28`）这两个各自声明的 3 相等，仓内没有断言把它们锁在一起。

某源 `default_cycle` 为 `null` 是合法终态（该源零请求）；`default_cycle` 有值但
`valid-times` 返回 `[]` 则是**两跳互不一致**：`/cycles` 逐候选周期先算一遍
`_national_cycle_valid_times`，时次列表为空的周期直接 `continue`
（`services/tiles/mvt.py:1985-1987`），
所以 `default_cycle` 不可能是这种周期；真正的来源是两跳之间的**发布竞态**（同周期重跑
落地而 `run_display_coverage` 矩形尚未补齐，`mvt.py:2129-2135` 返回 `None`），下个 tick
自愈，故保持 rc 不变——**有记录、不告警**（`scripts/node27_autopipe_cron.sh:244` 每 tick
把整份汇总 JSON 写进 `$LOG`，`:245` 只在失败时另加一行）。发现失败是**另一种**终态，
按源记 `per_source[<s>].error` 并置非零退出码，另一源照常预热。汇总 schema 为
`nhms.node27-mvt-prewarm.v2`，含 `requests_total`（只计预热请求，不含发现请求）、
`elapsed_seconds`、`lead_hours`、`workers`（**实际生效**的并发数），以及每源的 `valid_times_available`（目录发布了多少个
时次）与 `valid_times_warmed`（截断后实际预热多少个）——两者相等才说明整条时间轴都热。
job 提交顺序是河网优先、之后双源按 lead 交错（`k=0 gfs, k=0 ifs, k=1 gfs, …`），这样
deadline 命中时两源对称降级，而不是永远截断同一个源的默认视图。**MVT 瓦片**同一 cache key
由跨进程 `flock` single-flight 保护（`services/tiles/mvt.py:260-280`；唯一调用点
`apps/api/routes/hydro_display.py:689` 在持锁后二次查缓存），多 worker 与预热并发不会
重复执行 PostGIS 生成。该保护有前提：`NHMS_MVT_FILE_CACHE_DIR` 未配置时
`_file_cache_lock_path` 返回 `None`（`services/tiles/mvt.py:2338`），
`tile_generation_lock` 直接 `yield`（`services/tiles/mvt.py:271-273`），只剩进程内
线程锁；生产由
`infra/systemd/nhms-display-api.service:9` 的默认值兜住。**降水 PNG 不在此保护内**：
跨 worker 的文件缓存竞争是 by design 的无锁双写（D4，`services/precip/field.py:113-115`），
靠确定性渲染让两个写者产出相同字节。

整轮预热另有**总墙钟上限** `--deadline-seconds`（默认 540 s）：`--timeout`（默认 30 s）
只管单个 socket 操作。本仓能证明的只有不等式 (A) `540 + 30 ≤ 600`（600 s 是
`infra/systemd/nhms-node27-autopipe.timer` 的 `OnUnitActiveSec`），有断言钉在
`tests/test_node27_mvt_prewarm.py`。**(A) 说的是「prewarm 的预算不大于 tick 周期」，
不是「整个 tick 装得下」**——prewarm 是同一把 `flock` 里 ingest → coverage backstop →
prewarm 三个串行阶段的第三个。真跑超时的后果是**下一个 tick 被 `flock -n` 跳过**
（`scripts/node27_autopipe_cron.sh:185-186` 取非阻塞锁失败后，`:187` 记一行
“previous run still active, skipping tick”、`:188` exit 0；`infra/systemd/nhms-node27-autopipe.service` 是 `Type=oneshot` +
`TimeoutStartSec=0`，没有任何东西会掐掉长跑），即**有界、有日志的节奏降级**，不是堆积或丢数据。

540 这个数字背后的成本估算是 **UNVERIFIED 的**，只写在这里与 `tasks.md` 设计点 🔟 的 prose 里，
**不进常量、不进断言**：`(43 × 0.92 + 2 × 70 × 13.26) / (8 / 2) ≈ 474 s`。其中 13.26 s 是
`docs/runbooks/receipts/2026-09-05-issue-2009-discharge-cycles-node27.md` 里 gfs 11.63 /
ifs 13.26 两次实测中**较慢的那一次**（没有证据说它是 13 张里最贵的那张，也不能当成 13 张
均值的上界）；0.92 s 是 `docs/runbooks/receipts/2026-07-20-node27-display-scaling.md` 的
「基础河网 **cold SQL** 首次 918.182 ms」，那是 **SQL 阶段**耗时，不是端到端瓦片耗时；冷 PNG
成本与线程池能否线性伸缩**都没有实测**（8 路线程池打 2 个 uvicorn worker，`infra/systemd/nhms-display-api.service:9`；每个 pool_size 4 + max_overflow 2，`apps/api/routes/hydro_display.py:206-207`），估算按流量瓦片同价、按标称并发的一半计费。而实际并发
由 cron 的 `--workers`（`AUTOPIPE_MVT_PREWARM_WORKERS`，默认 8）决定，运维改小了本仓测不到——
所以汇总里有顶层 `workers` 记录**实际生效值**，receipt 必须记它。**这个估算在本仓没有 oracle，
能 settle 它的只有 node-27 实跑 receipt（task 7.2 / #2017）**；在那之前不得把它写成保证。越界后剩余请求**不再发起**（不是
取消在途请求），计入汇总的 `deadline_skipped`，退出码非 0；`requests_total` 只计实际发起
的请求，故 `requests_total + deadline_skipped` 才是本轮计划的请求总数。退出码为 0 当且仅
当 `failed_count == 0`、所有 `per_source[<s>].error` 为 `null`、所有
`png_out_of_contract == 0` 且 `deadline_skipped == 0`；`rc=2` 只留给进程级失败
（参数错误一类），此时打印的是一行式失败信封而不是完整汇总。

> **已知代价（不粉饰）**：`PREWARM_LEAD_HOURS` 之外的时次**不预热**，仍是**每张约十秒量级
> 的冷读**——目录发布 56 个时次，预热只覆盖自首项起 12 h 内的那 5 个。lead 窗口是绕开「单张全国流量瓦片
> 229.5 ms（`docs/runbooks/receipts/2026-07-20-node27-display-scaling.md`）→ 11.63 s
> （`docs/runbooks/receipts/2026-09-05-issue-2009-discharge-cycles-node27.md`）」这个
> pre-existing 回归的**权宜**，不是修好了它；修那个成本回归超出 #2013 范围。包络自身的成本
> 估算也只有 node-27 实跑 receipt（task 7.2 / #2017）才能定论：在那之前上面的 474 s 是
> **UNVERIFIED 的推算值**，不是实测值，本仓也没有任何断言在验它。

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

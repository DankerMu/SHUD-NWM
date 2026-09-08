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
由跨进程 `flock` single-flight 保护（`services/tiles/mvt.py::tile_generation_lock`；唯一调用点
`apps/api/routes/hydro_display.py:689` 在持锁后二次查缓存），多 worker 与预热并发不会
重复执行 PostGIS 生成。该保护有前提：`NHMS_MVT_FILE_CACHE_DIR` 未配置时
`_file_cache_lock_path` 返回 `None`（`services/tiles/mvt.py::_file_cache_lock_path`），
`tile_generation_lock` 直接 `yield`（`tile_generation_lock` 的 `lock_path is None` 分支），只剩进程内
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

### 强制刷新身份（issue #2079）

目录缓存的强制刷新（`display_catalog_cached` 跳过 store 查找、直接重算并写回）**只认两种身份**，
其余请求无论带什么头都按 TTL / stale 规则命中：

1. **进程内预热**：`_replay_targets` 把 app 包一层，在 ASGI `scope["state"]` 里打
   `nhms_display_cache_warm=True` 再交给 `httpx.ASGITransport`。这个键由服务器侧构造，
   网络上写不进去，也不需要任何配置——所以**预热永远有效，与 token 是否部署无关**。
2. **持有 token 的调用方**：请求头 `x-nhms-cache-warm` 的值与 `NHMS_DISPLAY_CACHE_WARM_TOKEN`
   按 `hmac.compare_digest`（两侧都先 `encode("utf-8", "surrogateescape")` 成 bytes，
   因为 Starlette 按 latin-1 解头值、而 `compare_digest` 对非 ASCII `str` 抛 `TypeError`）相等。
   唯一的合法持有者是 node-27 prewarm。

**字面值 `refresh` 已退役**：它曾是唯一的特权值，而 display 侧 GET 无鉴权、nginx 也不剥这个头，
于是公网任意客户端都能逐请求把目录端点打回冷路径（实测 2–4 ms → 73–92 ms，约 30–40 倍：
[`receipts/2026-09-08-issue-2079-cache-warm-measurement-node27.md`](receipts/2026-09-08-issue-2079-cache-warm-measurement-node27.md)）。
现在它和任何其它头值一样命中缓存。

**部署（两处，同值，均 0600）**：`infra/env/display.env` 的 `NHMS_DISPLAY_CACHE_WARM_TOKEN`
供 display 进程比对，`infra/env/node27-ingest.env` 的同名键供 autopipe → prewarm 发送
（`scripts/node27_autopipe_cron.sh` 硬拒 source `display.env`，所以必须写两遍）。改完
`bash scripts/ops/start-display-api.sh` 重启 display API。**token 值不得进 receipt。**
两个 `.example` 模板里该键是**注释掉的**（unset 即安全默认），部署时才取消注释并填真值——
模板里留生效占位值等于给每个照抄的部署发一个仓库公开 token。用 `openssl rand -hex 32` 生成。
**token 必须是 ASCII**（`openssl rand -hex 32` 就是）：prewarm 经 `http.client.putheader` 发头，
值里出现任何 latin-1 之外的字符（如 `令牌`）会在发出前抛 `UnicodeEncodeError`，该 tick 的两跳发现
全部失败；而且非 latin-1 的 token 在 display 侧永远比不中（Starlette 按 latin-1 解头值）。
运行时不做校验（design D3），这是运维纪律。

**缺失时的退化（安全默认，不是失败）**：token 未配置 → 外部永远无法强制刷新，进程内预热照常每 45 s
刷新热 key；prewarm 不发该头、每进程向 stderr 打一条
`prewarm: NHMS_DISPLAY_CACHE_WARM_TOKEN unset; discovery may see up to 45 s stale catalog`
（可见于 autopipe 日志），发现流程照常返回。代价是 publish 后的发现请求最长看到 45 s 陈旧目录
（热 key；冷 key 最长 600 s），该 tick 可能预热上一周期，下一 tick 自愈。两处 token 不一致的后果相同，
但**没有 warning**——这是配置漂移唯一不吵的失败面，改 token 时两边一起改。

**已知缺口**：`infra/compose.display.yml` 逐条列举 `environment:`，**不透传**该键；当前生产不走 compose
（走 `nhms-display-api.service` / `scripts/ops/start-display-api.sh` 的 `set -a; . display.env` 整份注入）。
日后若改走 compose，必须同时加透传并把该键加进 `scripts/validate_two_node_docker_runtime.py` 的
`DISPLAY_AUDITED_INTERPOLATION_ENV`，否则 token 声明了却进不了应用，prewarm 发着应用不认的 token 静默退化。

### 准入与淘汰（issue #2078）

**空结果不进缓存**：`/api/v1/runs` 的空页（自由文本 `basin_id`/`source`/`status` 或越界 `offset` 不匹配）与
`/api/v1/layers/{layer_id}/valid-times` 的空 `valid_times`（交集外 cycle 的 fail-closed 列表）照常返回 200，
但**不写缓存、不登记热 path**，且会把该 key 从两表移除——这些是客户端可控的无界 key 维度。
代价是这类请求每次都真跑一次查询（实测 43–85 ms 量级，与今天的冷 miss 同量级）。
`/api/v1/layers/discharge/cycles` 不设谓词（`source` 是 `Literal`，key 空间为 2）。

**两表都是 LRU 256**：`_store` 与 `_hot_paths` 到顶只淘汰最旧一条（`popitem`），**永不整表清空**。
改前到顶的动作是 `clear()`，公网串行注入 256 个不同 key 就能把整份缓存连同热 path 冲空
（实测合法 key 2 ms → 71–80 ms：[`receipts/2026-09-08-issue-2078-cache-flush-measurement-node27.md`](receipts/2026-09-08-issue-2078-cache-flush-measurement-node27.md)）。
诚实边界：一次 > 256 个**可缓存**不同 key 的突发仍会挤掉期间没被访问的合法 key，代价是它一次冷 miss。

**预热回放每 tick ≤ 32 条**：`DISPLAY_CATALOG_WARM_REPLAY_MAX = 32`，取活跃窗口内按「命中计数降序、
最近访问降序」排序的前 32 条（改前是全部热 path，实测 254 条 = 9.2 s 串行 DB 工作 / 45 s tick）。
回放本身不登记也不刷新 `_hot_paths`，所以 1800 s 活跃窗口只由真实访问续期。
回放拿到不可缓存的空结果时该 key 被忘记，不再是回放目标。

**`/api/v1/layers` 分页在缓存之后**：key 收敛为 `layers:{run_id!r}`，缓存的是完整目录，路由再切
`[offset : offset + limit]`。所以越界 `offset` 是不落 DB、不产生缓存条目的空页 200（契约写在 OpenAPI
的 `offset` `description` 里，**不加** `maximum`：目录长度不是常量，安全性来自 key 不再含 offset）。

## MVT 文件缓存回收（issue #2032）

`NHMS_MVT_FILE_CACHE_DIR` 从 M16 起**只写不删**：node-27 实测 5 天 4380 张 `.pbf` / 757 MB、
4383 个锁文件，增长由 autopipe 每 tick 的 prewarm 主动推动（实测见
[`docs/runbooks/receipts/2026-09-08-issue-2032-mvt-cache-measurement-node27.md`](receipts/2026-09-08-issue-2032-mvt-cache-measurement-node27.md)）。
DB 侧 `map.tile_cache` 为 0 行（display 角色只有 SELECT），所以增长全部落在文件侧。

**回收 runner**：`scripts/node27_mvt_cache_retention.py`（仅 stdlib，**不连 DB**）。
wrapper `scripts/node27_mvt_cache_retention_once.sh`，user 级 unit
`infra/systemd/nhms-node27-mvt-cache-retention.{service,timer}`（`OnCalendar=*-*-* 04:05:00 UTC`、
`Persistent=true`），env 模板 `infra/env/node27-mvt-cache-retention.example`（装到
`infra/env/node27-mvt-cache-retention.env`，**0600**）。

只剪**三种精确形状**，按墙钟 `mtime` 早于 `reference_time − NODE27_MVT_CACHE_RETENTION_DAYS`
（默认 14）：

| 形状 | 生产者 | `kind` |
|---|---|---|
| `<root>/<hh>/<sha256>.pbf` | `services/tiles/mvt.py::_write_file_cache` | `pbf` |
| `<root>/<hh>/.<sha256>.pbf.<pid>.tmp` | 同上（崩溃时残留的中间文件） | `tmp` |
| `<root>/.locks/<hh>/<sha256>.lock` | `services/tiles/mvt.py::tile_generation_lock` | `lock` |

`<hh>` 是 cache key 的前两位，必须匹配 `[0-9a-f]{2}` 且是**非 symlink 目录**；枚举**固定两级**，
`lstat` 必须是常规文件。因此 **`<root>/precip/**` 天然不可达**（`precip` 不匹配 `[0-9a-f]{2}`），
它由 `scripts/node27_raw_retention.py` 按 display watermark 口径负责——两个 runner 共享
`NHMS_MVT_FILE_CACHE_DIR` 这一个值，各自只碰自己的子树，回执里的 `precip_root_untouched`
就是给运维直接断言这一点的。锚不同是有意的：瓦片被剪掉只是下次请求走一次 miss 重新生成，
不是 404，所以 `#2011` 的 `L ≤ R − 1` 下限**不适用**于本 runner。

锁文件删除是并发安全的：runner 用 `os.open(path, O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK)`（**无 `O_CREAT`**；`O_NONBLOCK` 对常规文件无影响，只为让被替换成 FIFO 的路径不会把 open 永久挂住）
取 fd 后先 `fstat` 判常规文件（被换成 symlink/目录/FIFO → `skipped[not_regular_file]`，在任何 flock 之前），
再 `flock(LOCK_EX|LOCK_NB)`，拿不到就记 `skipped[lock_held]`；拿到后再用
`fstat(fd)` 与 `lstat(path)` 的 `(st_dev, st_ino)` 复核一次，不等即 `already_gone` 不删
（路径已被活 miss 重建）。`already_gone` / `lock_held` / `not_regular_file` 都是 **skip 不是 failure**；
根、`.locks` 或某个 `<hh>` 目录本身无法枚举（权限/ESTALE）则是 `failed[]` 里的 `enumeration_unavailable`（rc 1），修权限而不是清盘。

**锁文件自清理**（同一 issue 的另一半）：`tile_generation_lock` 现在在 `finally` 里**先 unlink
后 `LOCK_UN`**，获取时以 `(st_dev, st_ino)` 复核 flock 到的 inode 仍是路径上的 inode，不是则重开
（`_TILE_LOCK_REACQUIRE_LIMIT = 8` 次尝试，耗尽则记 warning 后**无锁生成**，绝不挂起请求）。
顺序是硬约束：先释放后 unlink 会让等待者拿到一个已被 unlink 的 inode，与下一个到来者重复生成。
所以稳态下 `.locks/**` 只含**在途 miss**，不再随请求数单调增长；runner 的锁 lane 是遗留文件的兜底。

**门与回执**：`NODE27_MVT_CACHE_RETENTION_ENABLED`（默认 true）/ `_PLAN_ONLY`（默认 false），
`--summary-path` 写 JSON 回执（未给时打 stdout），rc `0` 正常 / `1` 有 `failed[]` / `2` preflight
blocked（零删除）。健康判据（env 模板里逐字给出）：

```bash
jq -e '
  (.execution_mode == "production_execute")
  and ((.finished_at | fromdateiso8601) > (now - 26*3600))
  and (.failed | length == 0)
' "$(ls -t /home/nwm/node27-mvt-cache-retention-logs/mvt-cache-retention-*.json | head -1)"
```

**首次安装 / 回滚**：装 env（0600）+ unit + timer → 先跑
`NODE27_MVT_CACHE_RETENTION_PLAN_ONLY=true` 看 `planned[]`（确认无任何 `precip/` 路径）→
`systemctl --user enable --now nhms-node27-mvt-cache-retention.timer`。锁改动要
`bash scripts/ops/start-display-api.sh` 重启 display API 才生效。回滚：
`systemctl --user disable --now nhms-node27-mvt-cache-retention.timer`，或在 env 里置
`NODE27_MVT_CACHE_RETENTION_ENABLED=false`。

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

## 预算窗口截断信号（#2030）

- **信号**（WARNING）：`MVT_TILE_BUDGET_TRUNCATED layer_id z x y feature_count=<入选>/<相交> max_features
  coordinate_count=<入选>/<相交> max_coordinates`；logger `apps.api.routes.hydro_display` → `apps.api` stderr
  handler（`apps/api/main.py::_install_api_log_handler`）→ systemd `StandardError` → `/tmp/display-api.log`。
- **每次生成一条，缓存命中不重进 bind site**：记录发在 `_cached_or_generated_mvt_response` 的 `producer()` 内，
  即 DB 层（`map.tile_cache`）与文件层（`NHMS_MVT_FILE_CACHE_DIR`）**双层缓存都 miss** 时才到达。故
  `grep -c MVT_TILE_BUDGET_TRUNCATED /tmp/display-api.log` 数的是**生成次数，不是被截断的响应数**——部署前
  已截断、缓存仍热的瓦片照常服务且不产生任何行。
- **要枚举「此刻哪些瓦片被截断」**：跑一次冷缓存路径（清掉相关 key 后的 prewarm，或
  [`receipts/2026-09-08-issue-2030-budget-truncation-signal-node27.md`](receipts/2026-09-08-issue-2030-budget-truncation-signal-node27.md)
  的取证方法），不要拿历史日志裸 grep 当现状。
- **清单增长引入的新截断会自动现形**：新 run / 新流域轮换全国瓦片的 `source_version` 与 `cache_key`，首次重新
  生成即触发本记录。
- **`layer_id=discharge` 在本记录里恒指 `hydro-national`**：预算窗口只在全国层（`services/tiles/mvt.py` 的
  `national_budget_window` CTE），按 run 的 `hydro` 层没有该窗口。

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

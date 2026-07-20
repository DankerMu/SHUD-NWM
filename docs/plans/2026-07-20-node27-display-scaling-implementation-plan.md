# node-27 全国展示扩展性能实施方案

## Goal

在不改变 node-22 计算链路、流量产品语义和河段点击契约的前提下，把 node-27 全国展示从“浏览器先下载并解析全国全量河网”改为“轻量边界先显示、版本化 MVT/PBF 分片加载、热点瓦片提前生成”。完成后，18 个流域的首页刷新不再请求 45 MB 的 `national-basin-river.geojson`，新增流域也不会线性放大全国首屏静态包；有预报的河段仍可点击，无预报或首次 cold 的新流域仍能显示基础河网。

## Scope

- 全国基础河网改为由 `core.river_segment` 生成的 national river-network MVT，以 `Type`/`stream_type` 做低缩放分级。
- 全国流量 MVT 的冷查询改为“先按瓦片和 stream type 缩小河段集合，再取最新 run 的时序值，仅对缺失 stream type 的少量行做流量分位回退”。
- 为 `core.river_segment` 增加由 `properties_json.Type` 自动派生的持久化 `stream_type`，并补齐查询索引。
- national river-network 和 national discharge 的缓存 key 使用真实数据代际摘要；新 run、新网络或重新入库后自动换代，不复用旧瓦片。
- 同一瓦片跨线程/跨 uvicorn worker 只允许一个生成者，其余请求等待并复用结果。
- node-27 autopipeline 在 publish/coverage 后预热当前默认时次的全国 z3/z4 基础河网与流量瓦片。
- `/geo` 静态资源走支持 ETag/Last-Modified 条件请求的静态挂载；SPA catch-all 不再直接发送这些文件。
- node-27 display API 纳入 user systemd 托管，以 2 个 worker 起步并保留有界连接池；部署后产出冷/热性能和点击功能实机证据。

## Not In Scope

- 不修改 node-22 Slurm、forcing、SHUD、warm-state 或调度链路。
- 不改变 GFS/IFS 选择、流量值、展示有效时次或河段弹窗的数据契约。
- 不引入第三方瓦片服务、CDN 或新的数据库。
- 不在本次删除历史 Git 对象中的 43 MB GeoJSON；它退出运行关键路径，保留一个版本作为可回滚资产，后续可单独做仓库瘦身。
- 不把首次 cold 的新流域伪装成 warm，也不为缺失产品生成假流量；基础河网与流量叠加层保持语义分离。

## What Already Exists

- `apps/frontend/src/pages/m11/useNationalBasinGeo.ts` 当前并行请求 domain 与 45 MB river GeoJSON，`Promise.all` 让二者共同决定加载完成。
- `apps/frontend/src/components/map/m11MapPrimitives.tsx` 已有 MapLibre vector source 组件和按 `Type` 分级的河网样式。
- `apps/api/routes/hydro_display.py` 已提供单流域 river-network MVT 与全国 discharge MVT，已有文件缓存和 5 分钟浏览器缓存。
- `services/tiles/mvt.py` 已实现 PostGIS `ST_AsMVT`、瓦片预算、简化、缓存 key 和最新 display-ready run 选择。
- node-27 已配置 `NHMS_MVT_FILE_CACHE_DIR`，并已有 enabled 但当前未接管进程的 user systemd unit；现场 API 仍是 PPID 1 的单 worker 手工进程。
- node-27 autopipeline 已在每次 ingest 后执行 coverage backstop，适合作为 MVT 预热挂点。

## Constraints

- node-27 是唯一真实 DB、display API 和浏览器验收 oracle；node-22 不连接活 DB。
- 远端仓库只能 `git pull --ff-only`；部署前必须确认工作树，不自动 stash，不覆盖远端本地证据。
- display 角色保持只读：MVT 缓存数据库写失败时仍只写文件缓存，预热不得要求 display DB 写权限。
- 低缩放必须严格受 feature/coordinate/byte budget 约束；不能用提高预算掩盖查询和传输问题。
- 新增流域尚无 forecast 时，边界和基础河网可见，但不生成可点击的伪流量河段。

## Success Criteria

- 冷刷新首页不发起 `/geo/national-basin-river.geojson` 请求；主线程不再解析 45 MB 全量河网。
- `/geo/national-basin-domain.geojson` 首次返回 GeoJSON，带短缓存；相同 ETag 条件请求返回 `304`。
- 全国基础河网来自 `/api/v1/tiles/river-network-national/...pbf`；全国流量来自 `/api/v1/tiles/hydro-national/...pbf`，二者均按真实 generation 换代。
- 18 个现有流域边界和河网可见；有该有效时次产品的河段可点击并打开原有流量弹窗；无产品流域只显示基础河网，不可伪点击。
- node-27 代表性 z3 cold SQL `EXPLAIN ANALYZE` 执行时间不高于 800 ms；如果真实数据/硬件使该目标不可达，至少相对改造前 3.735 s 提升 3 倍且留下查询计划证据。
- z3/z4 预热后，本机 MVT 命中不高于 50 ms，公网命中 p95 不高于 800 ms；代表性 z3 PBF 不高于 300 KB。
- 浏览器硬刷新到首个可点击河段 p95 不高于 2 s；失败时不得回退请求 45 MB GeoJSON。
- 相同 tile 的并发 cold 请求只产生一次 PostGIS 生成；其他请求返回同 checksum/ETag。
- node-27 API 由 enabled+active 的 user systemd unit 托管，2 worker 均通过 `/health`、只读边界和 display live receipt。
- 以 18、36、72 个网络的查询计划或合成 identity 集验证 generation 与低缩放筛选不随全国静态包线性传输。

## Assumptions

- `river.shp` 的 `Type` 已保存在 `properties_json`；当前 node-27 约 98% 河段可派生 1–5 级 stream type。
- `hydro.hydro_run` 的 display-ready 状态仍为 `succeeded/parsed/published`，最新 run 的选择规则维持 `cycle_time DESC, run_id DESC`。
- node-27 user systemd manager 可用；现有 unit 已 enabled，部署只需安装仓库内权威 unit 并完成平滑接管。
- 2 worker 是当前安全起点；扩 worker 只在 DB pool 和冷查询达标后进行，不以 worker 数掩盖冷 SQL。

## Open Decisions

- 已决：不新建离线瓦片框架，复用现有 PostGIS MVT、文件缓存和 MapLibre vector source。
- 已决：基础河网与流量河网分层；基础层保证新流域可见，流量层保持点击与产品真实性。
- 已决：低缩放优先使用持久化 stream type；只有历史缺失 type 的行才使用 q_down 分位回退。
- 已决：预热只覆盖全国默认视野 z3/z4，其他缩放按需生成，避免无界预计算。

## Phases

### Phase 1: 数据模型、瓦片身份与查询路径

- Outcome: stream type 成为可索引列，national 两类瓦片都有数据代际，冷查询不再给 20 万时序行统一做窗口排序。
- Files / components: `db/migrations/`、`services/tiles/mvt.py`、`apps/api/routes/hydro_display.py`、相关 pytest。
- Steps:
  1. 新增 generated `stream_type` 与 network/type 索引。
  2. 增加 active national river-network SQL/路由及 source generation。
  3. 重写 national discharge SQL，typed 分支先筛选，untyped 分支才做 `PERCENT_RANK`，最后再取 geometry。
  4. 把两类 national generation 写入 tile cache key 和 layer metadata cache version。
- Verify: migration dry-run；SQL shape 单测；node-27 列覆盖率；z3/z4 `EXPLAIN (ANALYZE, BUFFERS)`；新旧 tile feature identity 抽样一致。
- Depends on: 无。
- Review attention: decision-dense — review closely。

### Phase 2: 浏览器关键路径改造

- Outcome: 首页只加载 domain GeoJSON 和分片 MVT，不再加载全国 river GeoJSON；基础河网先显示，流量 MVT 到达后保持点击。
- Files / components: `useNationalBasinGeo.ts`、`M11MapLibreSurface.tsx`、`m11MapPrimitives.tsx`、MapLibre/overview tests。
- Steps:
  1. domain 与 river 解耦，运行时只取 domain。
  2. 注册 national river-network vector source，以 `Type` 做缩放分级并置于流量 overlay 下层。
  3. generation 进入 source key/URL query，换代时强制 MapLibre 重建 source。
  4. 保持 basin boundary、station、selection 与 discharge interactive layer 不变。
- Verify: Vitest 覆盖无 river fetch、vector source、图层顺序和点击层；pnpm build；浏览器 network/heap/点击验收。
- Depends on: Phase 1 API 合约。
- Review attention: decision-dense — review closely。

### Phase 3: 缓存互斥、静态缓存与预热

- Outcome: 冷启动风暴不会重复打 DB，默认全国视野在新产品发布后主动转热，domain 支持 304。
- Files / components: `services/tiles/mvt.py`、`apps/api/startup_wiring.py`、`scripts/node27_mvt_prewarm.py`、`scripts/node27_autopipe_cron.sh`、相关测试。
- Steps:
  1. 文件缓存 key 对应跨进程 `flock`；锁内二次读缓存后再生成。
  2. `/geo` 使用 `StaticFiles` 的条件响应与明确 cache-control。
  3. 预热器从 API 获取当前默认 valid time，计算中国 bbox 的 z3/z4 XYZ，有限并发请求 base+discharge。
  4. autopipeline coverage backstop 后运行预热；预热失败记录且不篡改 ingest 结果。
- Verify: 并发单生成测试；ETag 304 测试；预热空数据、部分失败、成功路径测试；node-27 新 generation 冷转热证据。
- Depends on: Phase 1、2。
- Review attention: concurrency-sensitive — review closely。

### Phase 4: 服务托管与受控扩容

- Outcome: display API 由 systemd 自动恢复，2 worker 共享文件缓存并由 single-flight 防止重复生成。
- Files / components: `infra/systemd/nhms-display-api.service`、`scripts/ops/start-display-api.sh`、`infra/env/display.example`、运行手册。
- Steps:
  1. 仓库内固化 unit，worker 数由 `NHMS_DISPLAY_WORKERS` 控制，默认 2。
  2. wrapper 改为安装/启动同一权威 unit，不再制造 PPID 1 手工孤儿进程。
  3. 部署时先验证 unit/env，再停止旧进程、启动 unit、探活与只读 smoke；失败立即恢复旧 wrapper。
- Verify: `systemctl --user is-enabled/is-active`；两 worker 存活；kill 一个 worker 后恢复；API health/models/layers/tile/deny-write receipt。
- Depends on: Phase 3 跨进程互斥。
- Review attention: operationally risky — review closely。

### Phase 5: 全链路验收与容量证据

- Outcome: 性能目标、18 流域功能和 36/72 网络扩展行为均有可复现证据。
- Files / components: `docs/runbooks/receipts/`、诊断命令、浏览器证据。
- Steps:
  1. 本地全量相关 pytest、ruff、Vitest、TypeScript/build。
  2. node-27 ff-only 部署、迁移、预热和 live receipt。
  3. 冷/热 curl 与浏览器 waterfall；抽查 HHE/QHH 和新流域的边界、河网、点击、弹窗。
  4. 用复制 identity 的只读 SQL/CTE 或查询参数评估 36/72 网络，不写生产假数据。
- Verify: 对照 Success Criteria 逐项 PASS；未达标项必须附测量、原因和 containment，不能标记完成。
- Depends on: Phase 1–4。
- Review attention: evidence-dense — review closely。

## Verification

- Local backend: `uv run pytest -q` 的相关 API/MVT/migration/prewarm/static suites；`uv run ruff check .`。
- Local frontend: `cd apps/frontend && pnpm test && pnpm build`。
- Migration: 现有 migration harness + node-27 事务性应用/列与索引检查。
- node-27 SQL: 代表 z3/z4 tile 的 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`，保留 execution time、rows、sort/window、shared hits。
- node-27 HTTP: cold/hit 的 status、`X-Tile-Cache`、bytes、total time、checksum、ETag；条件静态请求应 304。
- Browser: 新会话硬刷新，记录 river GeoJSON 请求数（必须 0）、MVT 请求、首个可点击河段时间、JS heap 稳态；完成后关闭浏览器会话。
- Regression: HHE 河道、QHH 河段曲线、18 流域边界、GFS/IFS 切换、代站、下载入口保持正常。

## Risks

- Risk: generated column migration 重写 20 万行并短暂持锁。
  - Impact: 部署窗口内 river_segment 写入/读取可能等待。
  - Mitigation: node-27 当前量级先实测 migration 时长；在 autopipeline 空闲窗口执行，设置 lock timeout，失败回滚。
- Risk: 查询改写在缺失 Type 的历史网络上减少低缩放河段。
  - Impact: 基础河网局部缺线。
  - Mitigation: base 层 z>=9 保留全量；discharge 层仅对 untyped 做分位回退；部署前输出每网络 Type 覆盖率并抽查。
- Risk: 多 worker 放大 DB 连接数或启动期开销。
  - Impact: DB pool 争用、内存增长。
  - Mitigation: 默认 2，连接池按 worker 有界；先让缓存/SQL 达标，再决定是否增加。
- Risk: 预热与用户冷请求同时到达。
  - Impact: 同 key DB 重复生成。
  - Mitigation: 跨进程 single-flight + lock 内二次缓存检查。
- Risk: national generation 选择规则与 tile SQL 漂移。
  - Impact: 缓存错误命中或无谓失效。
  - Mitigation: 两者共用同一 latest-run 语义和契约测试，generation 明确包含 network/run/revision。

## Rollback Or Containment

- Trigger: migration 超时、SQL p95 回退、任一现有流域河网/点击缺失、tile 413/5xx 增加、systemd 接管失败。
- Action:
  1. systemd 失败时停止新 unit，使用部署前保留的 wrapper/旧 SHA 恢复单 worker。
  2. 前端失败时回滚 bundle；旧 river GeoJSON 在一个版本内仍保留，可恢复旧 fetch，不需要重新生成资产。
  3. MVT SQL/route 失败时回滚应用 SHA；新增 generated column和索引对旧代码无破坏，可留存，必要时在独立维护窗口移除。
  4. generation/prewarm 失败时禁用预热环境开关；按新 generation 的缓存文件可留存，旧 generation 不会被错误复用。
  5. 所有回滚先保留日志、EXPLAIN、cache key 和 live receipt，不删除现场证据。

## Next Step

本方案已获用户直接授权执行。按 Phase 1→5 实施；每阶段必须通过对应测试后再进入 node-27 部署，最终以 Success Criteria 和 node-27 live receipt 判定完成。

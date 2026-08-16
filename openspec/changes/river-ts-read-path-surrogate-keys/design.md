# Design: river_timeseries 读路径代理键切换（issue #1341）

## 风险三角与 fixture 级别

- Fixture level: **expanded**（生产 display 读路径 + MVT wire 契约 +
  schema/索引迁移 + 十亿行级实机运维战役；命中 mandatory triggers:
  public API/schema/column/migration + domain triggers: Timescale/
  hypertable/display identity）。
- Project profile: NHMS（`openspec/project-profile.md`）。
- Upstream suggested level: absent（issue 旧格式无该字段）；本裁决记录
  于此。
- Blast radius: high——display 生产面 + 国家图层 + coverage 判定；但
  回滚极简（revert 部署即回文本读，文本列/索引全保留）。

## 上游契约偏差（消费不重谈，记录）

1. Boundary"不改迁移" vs In-Scope"复核索引需求"：按 In-Scope 实质交付
   000051（proposal 已述），偏离进 PR body。
2. In-Scope 列 `services/production_closure/` 四文件为"校验查询"；探索
   实测其中 deny-write 探针是表级 INSERT 试探（列无关，切键无意义），
   只有携带 identity 谓词的读查询需要切；`scale_validation.py`
   plan_lines 现引用已删除索引（#1338 后陈债）——处置按实际形态，
   逐文件在 PR 记录"切/不切 + 理由"。
3. AC-4"MVT 图层渲染正常"依赖 `nhms_display_ro` 对权威表的 SELECT 权限
   ——仓库内无 GRANT（out-of-band 管理），列入实机 pre-flight。

## D1 谓词切换形态：文本入参 → 键解析 InitPlan → 整型索引谓词 → join 还原输出

对外入参不变（run_id/basin_version_id/river_network_version_id/variable
文本）。每条切换查询内：

- **键解析**：标量子查询（planner InitPlan，每查询各权威表至多一次
  pkey 点查）：
  `ts.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = :run_id)`
  等四键同形。未知身份 → 子查询 NULL → 谓词恒假 → 空结果，与现行
  "查无此行"行为一致（不 500）。
- **variable 谓词**：`ts.variable_e = (SELECT e FROM
  unnest(enum_range(NULL::hydro.river_variable)) e WHERE e::text =
  :variable)`——sargable 且 OOV 值落空集而非 22P02 cast 错误。直接
  `:variable::hydro.river_variable` cast 被否：OOV 会把"空结果"契约
  升级成 SQL 错误（must-preserve 破坏）。
- **输出还原**：等值谓词身份列直接回显绑定参数或经既有权威 join 取
  文本（hydro 图层的 `core.river_segment` join 从 (segment_id, rnv_id)
  文本双列改为 `rs.river_segment_key = ts.river_segment_key` 单键，
  `river_segment_id` 输出取 `rs.river_segment_id`）；`unit`/
  `quality_flag` 输出取 `ts.unit_e::text`/`ts.quality_flag_e::text`
  （#1339/#1340 同源值构造 + 等值审计保证 label=旧文本）；
  `feature_id` 拼接结果字节不变。
- **排序不变量**：任何既有 ORDER BY 若落在身份列，必须保持在**还原后
  的文本表达式**上——整型键序 ≠ 文本序，按键排序会打乱响应数组顺序，
  破坏逐字段等价。
- hydro-national 的 DISTINCT ON 选身份子查询本就 join 权威表：同一子
  查询同时取出文本（输出）与键（fact 谓词），零额外 join。
- **UNION ALL 两腿同切不变量（fixture 复审 round-1 补钉）**：
  hydro-national 的 `typed_values`（mvt.py:603-623）与
  `untyped_ranked`（mvt.py:624-652）是同一 source_cte 里 UNION ALL 的
  两条 `hydro.river_timeseries` 读腿，必须同时切键；任何一腿留文本
  谓词都会让同一 tile 的 z<9 / z>=9 分支对 NULL 键旧行可见性分裂，
  直接违反本 change 的 spec delta。
- `valid_times_for_layer` 无具名分支（mvt.py:1236-1243，仅
  `variable = :variable` 谓词）：**一并切** `variable_e`（enum_range
  安全形态）——它在边界内文件里，留文本会违反"fact 谓词只落键/枚举
  列"的 delta 措辞，且 #1342 删文本列时必炸；仓库内唯一 caller
  （hydro_display.py:270）恒传非空 run_id，该分支无生产流量，形态
  断言 unit 覆盖即可，不进 EXPLAIN 六形态集。
- `display_coverage.py` river 扫描：fact 侧 GROUP BY 四键 +
  `COUNT(DISTINCT river_segment_key)`（网络内 1:1，计数等值），汇总后
  join 权威表还原文本身份（join-and-reconstruct）；`candidate_runs`
  CTE 本就来自权威表，直接带键下推。
- **混合下推谓词（round-1 P1 补救，用户裁定，issue #1341 评论在案）**：
  压缩车道的 `compress_segmentby='run_id, river_network_version_id,
  river_segment_id'` / `compress_orderby='variable, valid_time'`
  （000047，文本三列）在 #1342 cutover 前不变；纯键谓词对压缩 chunk
  零下推（键列不在 segmentby∪orderby，TSDB 2.10.2 无 sparse-minmax）
  ——node-27 实测 valid_times 键形态对 chunk 51 从 Index Scan cost 4.8
  翻转为全解压 Seq Scan cost 598,280（3.05M batches），display 预热
  线程每 45s 触发即生产塌方。补救：**凡计划可达压缩 chunk 的 fact
  查询，在键谓词同一合取（AND）中保留 `run_id` /
  `river_network_version_id` / `variable` 三个冗余文本谓词**作为声明
  的过渡期下推辅助。不变量：(a) 受批集合恰为这三列——`basin_version_id`
  / `river_segment_id` 文本谓词与任何文本列 fact join 仍然禁止；
  (b) 每个文本下推谓词必须与其键/枚举对应物成对出现在同一合取，
  语义为带键行严格 no-op、NULL 键行仍由键谓词排除，只窄不宽；
  (c) 随 #1342 删文本列一并移除——漏删即列不存在报错，显式失败而非
  静默退化。范围判定从宽：display 边界内全部 `hydro.river_timeseries`
  fact 读查询统一携带，省去"哪条会命中"逐条论证的维护负担。coverage
  扫描按模式拆开（round-2 裁决修正原措辞）：`--run-id` 模式 scan
  窗口常量折叠后可排除压缩 chunk，辅助真冗余；`--all` 模式 scan
  守卫整体折叠消失，剩余时间界是 candidate_runs 关联列（非计划期
  常量）、无 chunk 排除，唯一文本辅助 `q_down` 单值不消 batch——
  该形态对压缩 chunk 无有效辅助，但与 master 等价**非回归**
  （master 的 CTE join 文本等值同样不可下推，模块头注释自证），
  据此记录为不入 2.5 形态集的理由。

## D2 索引（000051）

- 唯一新索引：`river_ts_selected_identity_key_valid_time_idx ON
  hydro.river_timeseries (run_key, basin_version_key,
  river_network_version_key, variable_e, valid_time DESC)`——现役
  `river_timeseries_mvt_selected_identity_valid_time_discovery_idx`
  的键形等价物。一个索引服务全部边界内切换形态：tile 点查（全五列
  等值+valid_time）、valid_times named-identity 分支（严格前缀 +
  valid_time DESC，恰是 #1378 病灶的形状解）、coverage run 域扫描
  （run_key 前缀）。不加第二索引（YAGNI；#1342 终态另议）。
- 裸 CREATE INDEX：hypertable 拒 CONCURRENTLY（000049 头注释 live 实
  测钉死）；SHARE 锁阻塞 ingest 写不阻塞读 → node-27 构建排 12h
  cycle 间歇窗口，起止时刻与时长进 receipt。迁移头注释写明该运维
  约束（模式照 000049）。压缩 chunk 32/51 在场不阻碍构建（000049
  同款前提下 plain CREATE INDEX 实测被接受）。
- CI/hermetic DB 表量小，构建瞬时——迁移可重放性由
  `tests/test_migrations.py` 既有链路覆盖，RETAINED 集合 +1。

## D3 回填战役（实机前置，用户裁定路线；仓库零新代码）

- 实测基线（2026-08-16 直连 SQL，per-chunk）：
  chunk 55（08-06→08-13，未压缩）266,091,168 全 NULL；
  chunk 58（08-13→08-20，active）217.2M 中 183,277,080 NULL；
  chunk 62（08-20→08-27，未压缩）20.4M 中 6,788,040 NULL；
  chunk 67（07-16→07-23）0 NULL（#1340 后新写的 hindcast 数据，
  retention 下个 tick 自然清除，非异常）；
  压缩 chunk 32（07-23→07-30）329,687,316 全 NULL、chunk 51
  （07-30→08-06）266,091,168 全 NULL。
- 编排（active 判定与 lag 均已实测钉死，非假设）：
  `is_active_chunk` = `range_end >= now - lag`，lag 取
  `NODE27_RIVER_IDENTITY_BACKFILL_LAG_SECONDS` 或回落压缩车道共享值
  `NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS`（两车道对 active 不许
  分歧）。**生产实测 lag = 172800（2 d，2026-08-16 读自部署 env，
  今日 12:25 CST 压缩 receipt `lag_seconds:172800` 双确认）；committed
  模板 604800（7 d）是陷阱——read the box, not the template**（runbook
  tier-node27 已有同款警告）。按 2 d 分类：**chunk 55（range_end
  08-13）terminal → 普通 `--enforce`；chunk 58（08-20）/62（08-27）
  active → `--final-sweep`**。多次 nohup-detached 循环（单次 ≤500 批
  × 1250 页 × 30s 墙，receipt 兼 resume cursor，可重入），final-sweep
  腿排 12h cycle 间歇窗口。**不覆写 lag**。
- 安全闸分层如实声明：active chunk 的闸是 runner 内建**每 chunk 写
  计数静默断言**（采样窗口内有写即 `ingest_not_quiescent` 拒跑）；
  terminal chunk（55）不经静默闸，其安全性来自 lag 判据本身（终态
  chunk 无 ingest 写入面）+ 写守卫未压缩断言。
- **时间线硬约束（实测推导）**：lag=2 d 下 chunk 55 自 08-15 起已是
  压缩候选；2026-08-16 12:25 CST tick 的 receipt 显示 55 恰在边界上
  被跳过（`range_end inside lag window`，receipt now_utc=08-15T00Z），
  **下一 tick（08-17 12:25 CST）即可能把 55 连同 266,091,168 行 NULL
  压死**（解压恢复代价数百 GB，且把不可见窗口拉长到 09-03）。因此
  **战役启动前强制 stop 压缩 timer**（runbook §4.5 user-scope 程序；
  stop → 回填 → 收敛核验 → 恢复 timer，起止时刻全部进 receipt），
  不是条件分支而是前置步骤；`--probe` 吞吐实测仅用于推算总时长。
- **收敛 oracle = 直连 SQL 每 chunk NULL COUNT 归零**（可回填 chunk
  集合上）。#1408 三缺陷在案：totals.pending_rows 会把 skipped chunk
  的 None 折叠成 0——receipt 只作过程留痕，不作收敛判据。
- 附带前置校验：`verify_river_identity_normalization()` 读侧等值审计
  计数对**已回填域**为零（受 #1408 口径约束时改用 run 域定向 SQL，
  照 #1340 receipt 2.6 先例）。
- 压缩 chunk 32/51：**不解压不回填**（decompress-replay 是既有恢复
  车道，但 595.8M 行解压重压只为 ≤11 天窗口，成本荒谬）。影响面：
  切键后对 valid_time 07-23→08-06 的旧行键读不可见；当前 display
  选取的 latest ready run 均为 #1340 后新 run（cycle 12h 全流域滚
  动），用户可见面≈零；pre-flight 实测"每网络 selected run 是否含
  NULL 键行"兜底确认，若有流域会因此变暗则升级为 merge 门决策项。
- **口径拆分（round-1 交叉观察修正）**：上述 "retention 08-20/08-27
  收敛" 只对 **NULL 键行可见性**成立；**性能面不随 retention 收敛**
  ——timer 恢复后新压缩 chunk 仍是文本 segmentby，纯键谓词的下推
  缺失持续到 #1342 cutover。性能面的当期解是 D1 的混合下推谓词，
  不是等 retention。PR body 影响面表述按此双口径书写。

## D4 部署顺序与逐字段等价证据（AC-2）

顺序硬约束：**回填收敛 → 000051 应用 → 旧代码快照 → 部署新代码 →
新快照对比**。回填不改文本读行为（旧代码响应与回填无关），故快照
基线在回填后采集即可成立；若先切读后回填，NULL 键行会对键读瞬时
消失——被排序钉死为不可接受。

- JSON 端点（hydro_display、coverage、valid_times）：同一钉住身份
  pre/post 响应**字节相等**。
- MVT tile：protobuf 编码对行序敏感，承诺**解码后 feature 集合相等**
  （properties 全字段 + 几何），不承诺 tile 字节相等——偏离口径先行
  声明。取样：hydro 图层 ≥2 流域 × ≥2 tile + hydro-national ≥2 zoom。
- **快照缓存旁路程序（round-1 P2 补钉）**：tile cache key 不含查询
  形态，部署本身不失效缓存——pre/post 快照之间必须**清空
  `map.tile_cache` 表与 `NHMS_MVT_FILE_CACHE_DIR` 文件缓存**（hydro
  与 national 两图层都做），否则 post 快照可能读到 pre 代码的缓存
  产物，等价性证据失真。配套：`NATIONAL_DISCHARGE_QUERY_VERSION`
  常量随本 change 递增（查询形态变更即缓存世代变更，防部署后
  stale/fresh 分裂窗口）；hydro 单 run 图层不 bump——存在性探针在
  缓存读之前跑且已切键，NULL 键身份直接 404 不触缓存，全回填身份
  的新旧 tile 逐字段相等（round-2 裁决：不对称正确）。**同款双图层
  缓存清空也适用于回滚与后续重部署路径**（回滚 = revert 部署，若
  期间存在部分回填 chunk，须随部署动作清 `map.tile_cache` +
  `NHMS_MVT_FILE_CACHE_DIR`），不只限快照窗口。
- EXPLAIN (ANALYZE, BUFFERS) before/after **六形态**：tile 点查、
  valid_times named-identity（#1378 基线对照）、coverage run 域扫描、
  存在性探针、**national identity-stats 探针、national
  typed_values/untyped_ranked 腿**（后两者是唯一有 000049 实测基线
  的形状——Q1 10.4ms / Q8 2.9ms pkey fallback，且文本 pkey 对键形态
  零可用前缀，退化风险最高，不测即盲区）。判据：切换后计划走
  000051 索引、fact 表无 Seq Scan、latency 不高于文本基线（#1378
  形态应数量级改善）。**每一形态至少含一次绑定命中压缩 chunk 的
  valid_time**；压缩段验收判据**分形态**（round-2 裁决修正——原
  "每形态 segmentby 下推" 对 national 三形态物理不可满足）：绑定
  字面量四形态（tile 点查、valid_times named、coverage run 域、
  存在性探针）须显示文本 segmentby 下推（compression 内部关系上有
  Index/Filter Cond）；national 三形态（identity-stats、typed/
  untyped 腿）身份经 latest_runs join 到达、无 segmentby 字面量，
  判据为**不得全解压 Seq Scan**——batch 消除通道是 orderby 第 2 列
  `valid_time` 等值绑定的 min/max 元数据（`variable` 单值 q_down、
  其元数据不消 batch，受批辅助在这三形态是声明性 no-op，如实进
  receipt）。这与 spec delta 的 "segmentby/orderby columns" 措辞
  一致。
- deny-write（AC-5）与 `/`、`/ops` 浏览器 e2e（AC-4）照 C1-C4 惯例。

## D5 测试策略（Evidence Floor 映射）

- unit：切换后 SQL 形态断言（谓词含键解析子查询、ORDER BY 落文本
  表达式、feature_id 拼接不变）；文本谓词断言按混合口径重写：受批
  下推三列必须与键对应物成对出现，`basin_version_id` /
  `river_segment_id` 文本 fact 谓词与文本列 fact join 为负向钉子；
  OOV variable →空结果路径；`scale_validation.py` plan_lines 新旧
  对齐。红证配对：新断言在 pre-change 代码上必红（stash 法）。
- **`tests/test_sql_shape_helpers.py`（自 `tests/sql_shape_helpers.py` 迁址，pytest 可采集）是测试 oracle，本身必须有自测**
  （round-1 P1 教训：`strip_scalar_subqueries` 把 CTE 开头
  `(SELECT` 误当标量子查询整段剥除，5 条负向钉子在未切换 master 上
  假绿）。要求：(a) 剥除逻辑区分标量子查询与 CTE/派生表开头，括号
  配平跳过字符串字面量与注释；(b) helper 自测为真实 pytest 测试
  函数（CTE 不剥、标量剥、字面量/注释穿越）；(c) 每条负向钉子对
  master 源码红证复验；(d) helper-only diff 在
  `scripts/select_ci_tests.py` 的选择规则下必须能带上消费方测试
  文件（CHANGED_TEST_FILE_RULES 或等价机制），不得出现 exit 5 假红。
- integration（real-db marker）：seed/dual-write 数据上，切换前后同
  身份响应逐字段相等（JSON 字节等、tile 解码集合等）；**national
  图层与 hydro 图层同强度 oracle**——per-segment value/valid_time/
  几何逐字段断言或 text-era source CTE 全行比对，不得只断言 run/
  network 常量字段；未知 run_id / OOV variable → 空结果非错误；
  NULL 键行（手工造旧形态行）对键读不可见——**该行为是设计后果，
  测试把它钉成显式契约**；**coverage 刷新对全 NULL 键 legacy run 的
  行为显式钉死**（segment_count 归零 + valid_time 边界丢失是切键后
  的真实后果，测试独立断言该 run 的刷新结果，运维面在 runbook/
  receipt 记录"legacy run 禁止无 `--skip-fresh` 重扫"）。
- `uv run pytest -q` 定向 + `uv run ruff check .`；前端零改动，
  `check:api-types` 不触发（OpenAPI 零变更自证于 diff）。

## Invariant Matrix（自愿附带，expanded 级）

- Governing invariant: 边界内读路径的 fact 谓词只落代理键/枚举列，
  而对外响应与文本谓词时代逐字段等价。
- Source-of-truth identity: 四权威表 (text id ↔ surrogate key) 双射 +
  枚举 label=文本值（#1339 建立、#1340 写侧维持、等值审计监督）。
- Producers: 写路径（#1340，不动）；backfill runner（#1339，不动）。
- Validators/preflight: `verify_river_identity_normalization()`；
  直连 per-chunk NULL COUNT；pre-flight 权限/selected-run 检查。
- Storage/query: 000051 整型索引；文本索引保留（回滚 + 边界外读者）。
- Public routes: hydro_display 路由、MVT tile 端点、coverage 端点。
- Downstream consumers: 前端（零感知）；forecast_store 等边界外读者
  （不动，跟踪单挂 #1342）。
- Failure paths: 未知身份/OOV → 空结果；部署回滚 = revert 代码，文本
  列仍权威；压缩 chunk NULL 行不可见窗口由 retention 收敛。
- Evidence: pre/post 快照、EXPLAIN 六形态、回填 receipts + SQL 计数、
  deny-write、浏览器 e2e。
- Regression rows:
  - 钉住身份 tile/JSON 请求 → 切换前后逐字段等价；
  - 未知 run_id / OOV variable → 空结果，无 SQL 错误；
  - 同一 national 身份 z<9 与 z>=9 → NULL 键行可见性一致（两腿同切）；
  - 边界外读者（forecast-series 端点）→ 响应与 master 基线不变。

## 非目标

写路径、回填 runner 代码、文本列/文本索引删除、cutover 函数执行、
压缩 segmentby 切换、压缩 chunk 解压回填、forecast_store 等边界外
读者改造（跟踪单挂 #1342 blocker）、OpenAPI/前端变更、#1378 的正式
关闭验证（本 change 交付形状解 + EXPLAIN 证据，#1378 按其 issue 口径
另行验收）。

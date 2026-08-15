# Design: river_timeseries 身份列规范化（issue #1339）

fixture level: **expanded**。理由：生产库（node-27 active primary）迁移 +
回填、代理键模式在本仓库零先例、需 ADR 0002 amendment。issue 为旧格式（无
`Suggested fixture level` / `Minimal mergeable slice` 字段），起点由本
fixture 自定并记录，无上游偏离可言。

前置复核（2026-08-15，#1233 教训例行项）：issue/epic #1336 均 OPEN；000047
segmentby 仍三文本列；最新迁移号 000049（下一号 000050）；node-27 实测
TSDB 2.10.2 / PG 15.2、459.9M 行 / 249 GB（probe 复测口径）/ 6 chunk
（2 压缩）、chunk 级 pg_stats 与 issue 剖面一致（run_id 537 distinct/
56 B、segment 56k/37 B、variable/unit 单值）——前提成立且收益扩大
（行数较立案 +46%）。

## 实测硬约束（设计地基）

1. **TSDB 2.10.2 无压缩 DML**：UPDATE 压缩 chunk 直接报错（2.11+ 才有）。
2. **已有压缩 chunk 时 ALTER segmentby 报错**（000047:24-29 实锤）。
3. **TSDB 2.10 要求 segmentby∪orderby 覆盖全部 unique/PK 列**
   （000047:11-12、ADR 0002:77）——压缩设置切换与 pkey 切换**不可分**，
   必须同一 cutover 原子交付（D4）。
4. **migrate.py autocommit 逐语句**（packages/common/migrate.py:161）：
   回填必须是外部 runner（house 式样 = #1069 compression runner）。
5. **CI 的 TSDB 是 `pg15-latest` 非 2.10**（ci.yml:161）：压缩语义相关
   Evidence 一律以 node-27 throwaway 库为 oracle（conftest.py:110-124 建/
   删独立库，**绝不直连 live `nhms`**），CI 绿只算迁移 replay 证据。
6. **`timescaledb.compress = true` 状态下（哪怕零压缩 chunk）所有 unique
   相关 DDL 全被拒**（probe d-2 实测：CHECK NOT VALID / VALIDATE /
   SET NOT NULL / CREATE UNIQUE INDEX / DROP CONSTRAINT pkey|fkey 一律
   `operation not supported on hypertables that have compression
   enabled`；唯 nullable 加列与非 unique CREATE INDEX 放行）。解锁 =
   `SET (timescaledb.compress = false)`，其前置是**全部 chunk 已解压**。
7. **FK 列必须被 segmentby∪orderby 覆盖**（probe (c) 实测，无 pkey 隔离
   复现）：文本 FK 不可能在整型 segmentby 下存活；任何新列上的整型 FK
   也会触发同一约束（basin_version_key 不在目标 segmentby 内）。
8. **`ADD CONSTRAINT ... PRIMARY KEY USING INDEX` 在 hypertable 上不支持**
   （probe d-1）；`transaction_per_chunk` 与 UNIQUE 不兼容（d-3）；CIC
   被拒（d-4 = 000049:37-40）。`CHECK NOT VALID→VALIDATE→SET NOT NULL`
   免扫**成立**（d-5，3M 行上 ~1500x 差距，chunk 自动传播）。

（探针全记录：`probe-1339-throwaway.md`，2026-08-15 node-27 throwaway 库
`nhms_1339_probe`，已 DROP。实测活库基线较 fixture 初稿漂移：
core.river_segment **209,126** 行、hydro_run **3,609** 行、事实表
**459.9M 行 / 249 GB**——下文以实测为准。）

## D1 — ADR 0002：提前触发重评，落 amendment（覆盖 Decision 4 与 6）

Decision 6 defer star schema，重评条件"measured growth curves … national
scale (~100 basins), with compression receipts as the baseline"。**字面
条件未满足**：流域数仍 18（current-production-ops.md:176），a3e2264d 是
display/MVT 渲染扩容而非流域扩张。amendment 如实写成**提前触发重评**，
理由：增长曲线 132M（07-04）→459.9M 行/249 GB（08-15，6 周 3.5×行数，
周期累积驱动）+ epic #1336 携带的新剖面；并按 ADR 自设口径引 probe 实测
数字——压缩比 **45.09x / 44.63x**（_hyper_3_32 268 GB→6096 MB、
_hyper_3_51 215 GB→4924 MB）、当前索引占比 **137 GB 索引 vs 112 GB 堆
= 55%**（ADR 原文口径 ~70%，#1338 裁剪后已降）。**双刃如实写**：45x 恰
支持 Decision 6 原始论证"压缩已拿走大部分收益"——但只对 terminal chunk
成立；amendment 把规范化收益范围明确限定在仍未压缩的热数据（249 GB 中
约 239 GB）与比堆还大的索引上，且如实写明总量 264→249 GB 因又压一个
chunk 而**下降**、增长压力体现在行数曲线（挑数=造假，禁止）。**Decision 4（segmentby 覆盖现 pkey
的具体列清单）在 cutover 后同被改写，amendment 一并覆盖。**不落
amendment 就动手 = 静默推翻 Accepted ADR，禁止。

## D2 — Schema 形状（迁移 000050，全幂等）

- **不另建维表：给四张既有权威表加整型代理键**（`hydro.hydro_run`、
  `core.basin_version`、`core.river_network_version`、
  `core.river_segment` 各加 `<name>_key INTEGER GENERATED ALWAYS AS
  IDENTITY UNIQUE`——IDENTITY 隐含 NOT NULL，不再重复）。**锁代价如实**：
  IDENTITY 默认值是 volatile `nextval()`，走不了 fast-default → 权威表
  加列是**全表 rewrite + ACCESS EXCLUSIVE**。实测（probe b）：hydro_run
  3,609 行/两张 version 表各 20 行毫秒级；`core.river_segment`
  **209,126 行 / 355 MB / 七个索引**（pkey+GIST+3×gin_trgm+2×btree，
  rewrite 成本由重建索引主导）且在 MVT 生产读路径上
  （services/tiles/mvt.py 六处 JOIN）——**活库 AEL 预算 ~10 s**（209k
  复制品 6.6 s 外推），lock_timeout 2 s 快败重试实测可用；迁移
  对权威表 ALTER 前显式 `SET lock_timeout`（超时报错重试而非无限排队
  堵读），迁移头注释记录锁代价与 throwaway 实测耗时（house 先例
  000049:34），runbook 注明低峰应用。裁决理由：(a) 避免双份身份源漂移；(b)
  seeding 从"DISTINCT 扫 264 GB 事实表"降为零（权威表已存在，加列即成
  键）；(c) `river_segment` 的自然键是二元组
  `(river_segment_id, river_network_version_id)`（000004_core.sql:33-42
  pkey），事实表既有 FK 恰打在该二元组上（000006:57-58）——权威行代理键
  天然正确粒度，规避"跨 network version 同名河段被静默合并"的粒度错误。
  issue 文本的"维表"按意图（整型代理键）而非字面（新表）交付，PR body
  记录。与 house BIGSERIAL 先例的偏离（选 IDENTITY，标准且防手工插值）
  顺带记录。
- **覆盖率风险**：`run_id`/`basin_version_id` 是否有事实表→权威表 FK 未
  证实（segment 对有 FK 保证全覆盖）。回填 JOIN 不上的值 = 引用腐坏证据，
  receipt 单列 unmatched 计数并 fail-closed 停机（不静默留 NULL 混进
  收敛统计）；dry-run 先行报告。若 node-27 实测出现 unmatched，处置是
  数据修复决策，上报不擅改。
- 3 个 native ENUM（`hydro.river_variable` / `river_unit` /
  `river_quality_flag`，house 式样 000003）。**值集 = parser.py 写入字面
  量 ∪ db/seeds/seed_demo.py（`q_down`/`y_stage`/`m3/s`/`m`）∪ node-27
  实测 distinct**，来源逐条注释在迁移里。测试孤例字面量**不焊进生产
  ENUM**：tests/test_tile_publisher.py:275 的 `'m3 s-1'` 改为 `'m3/s'`
  （tests/ 在 PR 边界内，改测试比污染枚举干净）。
  回填遇不可映射文本值：fail-closed 停机 + receipt 单列计数（与时长墙
  重试逻辑**分流**，不混淆）。
- 事实表 7 个新列（`run_key`/`river_network_version_key`/
  `basin_version_key`/`river_segment_key` INT + `variable_e`/`unit_e`/
  `quality_flag_e` enum）全 **nullable 无默认值**（probe (a) 实测
  metadata-only，0.8-1.1 ms/列，压缩 chunk 在场照常，`atthasmissing=f`。
  `quality_flag_e` 不镜像现列 `DEFAULT 'ok'`——**理由据实修正**（probe
  a'）：2.10 对常量默认走 fast-default 同样不 rewrite，无默认的真实理由
  是保持 `atthasmissing=false` 钉可查 + 语义上"未回填=NULL"哨兵纯净，
  迁移注释按此措辞，不得写"带默认会 rewrite"的错误理由）。**不建 FK
  约束、不建索引**（FK 部分从 YAGNI 升级为实测硬约束：硬约束 7——
  basin_version_key 上的 FK 会被压缩配置校验直接拒绝；索引属切换 issue）。
- **前置实验（tasks 1.0）已完成**（2026-08-15，probe-1339-throwaway.md）：
  (a)(b) 确认，(c)(d) 证伪原 D4 形状——结果已吸收进硬约束 6-8 与 D4
  第三稿；后续任何新的"TSDB 不让做某事"假设仍适用"推翻即回炉"规则。

## D3 — 回填 runner（scripts/node27_river_identity_backfill.py）

House 式样对齐 #1069 runner：dry-run 默认、`--enforce` 才变异、bounded、
schema 版本化 receipt、`fcntl` flock 单实例互斥（house 式样
node27_timeseries_compression.py:22,44）。两条 chunk 级不变量：
**绝不 UPDATE 压缩 chunk**（跳过 + receipt 清单；解压→回填→再压走
runbook 编排复用既有 replay + compression runner，本 runner 不内嵌解压
——单一职责）；**绝不 UPDATE active chunk**（parser 窗口 DELETE+INSERT
正在写的那个——回填它必与 ingest 抢行锁且永不收敛，顶撞 issue"不得阻塞
ingest"；只回填非 active 的未压缩 chunk，active chunk 待其 terminal 后
下一轮追平，receipt 单列 active 跳过）。**active 判定口径不自造**：复用
compression runner 的 terminal/lag 判据（按 `range_end` 滞后，与 #1069
车道同口径），非 terminal 即 active。**`--final-sweep`**：显式旗标才
允许覆盖末 chunk，且必须先断言 ingest 已暂停（探测近窗写入活动，测不
到静默才放行）——这是 cutover 前置"7 列全表零 NULL"与 active 不变量
的唯一合法交汇点。压缩判定不自造：
**扩展共享写守卫** `packages/common/timescale_write_guard.py` 增加 chunk
级 `assert_chunk_uncompressed` helper（#851 D5 契约：写路径统一走共享
helper，禁止 per-path 复制；本 runner 是第 4 个生产写者，必须入约——
对既有 3 条写路径行为零变化）。

- **批形状：ctid 块区间推进**（否决 `ctid IN (SELECT … LIMIT n)`：无索引
  前提下每批从页 0 重扫已回填前缀，O(n²)，与时长墙组合成永久停机，且
  probe 采样系统性偏乐观）。每批
  `UPDATE ONLY <chunk> SET … FROM <四权威表显式 JOIN，谓词逐条写全>
  WHERE <chunk>.ctid >= '(p,0)' AND <chunk>.ctid < '(p+k,0)'
  AND <chunk>.run_key IS NULL`（外层显式重复 NULL 谓词，EPQ 重取下幂等）；
  块游标 `(chunk, next_page)` 持久化进 receipt，按 `pg_class.relpages`
  推进。重入 = 哨兵谓词 + 块游标双保险：游标丢失时退化为全扫但结果幂等。
- **unmatched 的检测机制**（inner-join UPDATE 对 JOIN 不上的行只是不
  更新、不报错）：每批先取"区间内 `run_key IS NULL` 候选行数"，与
  UPDATE 实际 rowcount 比对，差值即 unmatched/unmappable → fail-closed
  触发；否则这些行永远留 NULL 且每轮 receipt 假性非零。
- 单批页数可调（默认按 ~100k 行折算），批间 sleep 可调；单批事务时长墙：
  超时 abort 该批→页区间减半重试一次→仍超记 receipt 停机（fail-closed；
  减半作用于区间宽度，扫描成本随之线性下降——与被否决方案的本质区别）。
- **dry-run = 计划 + rollback-probe**：逐 chunk 报行数/字节/待回填行数/
  批计划、预计堆+索引膨胀量与 `/home` 余量对照（P2-4）；`--probe` 对
  样本批真实 UPDATE 计时 + `pg_locks` 等待观测后 ROLLBACK（AC-3 证据，
  零持久化；probe 同受时长墙与压缩 chunk 跳过约束）。
- **收敛性口径收窄**：哨兵谓词只保证 INSERT 新行与未回填行收敛；writer
  的 `ON CONFLICT DO UPDATE` 分支（parser.py:786-793 更新
  basin_version_id/unit/quality_flag 文本列）会让已回填行文本↔代理列
  静默背离。receipt 增**等值审计计数**（`run_key IS NOT NULL AND
  (basin_version_key 经 JOIN 不等 OR unit_e::text <> unit OR
  quality_flag_e::text <> quality_flag)` 的行数）；非零即报告，修复属
  切换 issue 的 re-sweep 步骤。
- 维表 seeding phase 取消（D2 权威表路线的直接红利）。

## D4 — cutover：全解压维护窗口内的单函数序列（按 probe 实测重写第三稿）

前两稿路线（CIC prepare / plain-index prepare + USING INDEX 收编）分别
被 000049 记录与 probe d-1/d-2/d-3 实测否定：**prepare 段不存在**——
`compress=true` 状态下所有 unique DDL、CHECK/VALIDATE、SET NOT NULL、
DROP CONSTRAINT 全被拒（硬约束 6），窗口外无事可做。"AEL 分钟级"的
目标死亡，如实接受：**cutover 是一次 ingest 全程暂停、压缩全程关闭的
维护窗口操作**；probe d-6 已端到端验证目的地可达（含 45x 往返零丢行）。

两段形状：

1. **`hydro.verify_river_identity_normalization()`（只读，无锁，窗口前
   跑）**：报告 7 列 NULL 计数、等值审计差异计数（与 runner receipt 同
   口径）、压缩 chunk 计数。等值审计零差异是**runbook 层人工闸**（进
   窗口前 verify receipt 必须为零），函数层不重查（重查=又一次全扫，
   且 VALIDATE 已兜底零 NULL）。
2. **`hydro.cutover_river_identity_normalization()`（窗口内，单事务）**，
   前置：零压缩 chunk（catalog 查，否则 RAISE——`SET compress=false`
   自身也会拒绝）。体内顺序（= probe d-6 实证序列）：
   `SET (timescaledb.compress = false)` → drop 文本 FK（硬约束 7 实测
   强制，非可选——文本 FK 在整型 segmentby 下必死，其退役由此**提前于**
   文本列下线，记入 ADR amendment 与偏离记录）→ 7× `CHECK (col IS NOT
   NULL) NOT VALID` + `VALIDATE CONSTRAINT`（**VALIDATE 本身就是零 NULL
   闸**：有 NULL 即报错整体回滚，fail-closed 由构造保证；d-5 实测约
   0.5 s/3M 行/列，460M 行 7 列约 10 分钟量级）+ 7× `SET NOT NULL`
   （借 validated CHECK 免扫，d-5 实测 ~1500x）→ drop 旧 pkey →
   `ADD CONSTRAINT ... PRIMARY KEY (run_key, river_network_version_key,
   river_segment_key, variable_e, valid_time)`（**窗口内建索引是主成本
   项**，AEL 阻塞读写——实测基数 2.969 s / 3.024M 行（probe d-6 步 6），
   460M 上受排序与 maintenance_work_mem 支配、超线性风险高，**排窗口前
   必须在更大 toy 上重测**后外推入 runbook；USING INDEX 戏法不存在，
   d-1）→ ALTER
   `compress=true, compress_segmentby='run_key,
   river_network_version_key, river_segment_key',
   compress_orderby='variable_e, valid_time'`（d-6 步 7 实测 6 ms 接受）。
   （进一步瘦身如借 segment_key 蕴含 network 省列，留切换 issue 评估。）

**窗口成本如实入 runbook（D6）**：全量解压前置（live 当前两 chunk 解压
后 +~470 GB，对照 `/home` 576 G free——**执行时必须重算**，且需叠加新
整型 pkey 索引与排序临时空间两项；压缩 chunk 随时间增多、解压需求单调
上涨，宜早不宜迟）；解压与再压缩走既有 replay / compression runner；
压缩 timer 全程 mask；PK 建索引期间 display 读被阻塞（低峰窗口）。

**"单事务原子"是未实测断言**（probe d-6 为逐语句 autocommit）——其可
观测性质由 **负路径 integration 测试**钉住（Evidence Floor：toy 库故意
留一行 NULL → 调 cutover → 断言抛错 **且** compress=true、文本 FK、旧
pkey 三者原状未变）。若 `SET (timescaledb.compress=...)` 不能与该 DDL
串共处事务块，implementer 停机上报而非降级语义。

**迁移本体不调用任何一段**（空库自动应用会造成 CI/prod 默认 settings
与 pkey 分叉——oracle 完整性红线）。**有序步骤（生产与 toy 测试同一
顺序）**：ingest 暂停 → runner `--final-sweep`（补齐末 chunk）→
verify（receipt 三计数零）→ 全量解压 → cutover 函数 →
compress_chunk→decompress_chunk→逐行相等（AC-4 往返证据；integration
在 **node-27 throwaway 库**显式走全序列——probe d-6 已手工验证，测试
将其自动化）。生产执行属切换 issue 运维窗口，前置清单（写入路径 ON
CONFLICT 目标已适配、ingest 暂停、磁盘余量重算、低峰）入 runbook 节。
000047 文件文本零触碰（字面钉
tests/test_node27_timeseries_compression.py:1619-1625 不动）；#1069 车道
读 catalog 代码零触碰（生产 settings 本 PR 不变）。

## D5 — 生产 cutover 与旧列下线：路由（记录的偏离）

In-Scope 的"更新唯一约束/pkey"以**已测机制**（D4 函数）+ 前置清单 +
runbook 步骤交付；**生产执行**随写入路径适配与旧列下线 issue（issue 明示
独立 issue；pkey 项亦不在 AC 清单）。PR body 偏离记录节明示。

## D6 — 运维编排义务（runbook 节，task 1.6）

- 回填 enforce 窗口**必须 mask/stop** `nhms-node27-timeseries-compression.timer`
  （每日 04:25 UTC、PER_TICK_BOUND=4——否则跨天回填中未完成 chunk 被压
  掉，NULL 行困进压缩 chunk，只能 230 GB 级解压救回）；receipt 记录
  timer 状态。
- 每 chunk 回填完成后 `VACUUM <chunk>`（恢复 visibility map——#1338 后
  display 读形状依赖 pkey Index Only Scan，000049:20；全行 UPDATE 清光
  all-visible 位）；磁盘余量前置检查（回填膨胀 ≈ 堆+索引近翻倍，直到
  vacuum 回收）；display 读性能回归观测点。
- 压缩 chunk 的解压→回填→再压步骤（复用 replay + compression runner）。
- cutover 窗口有序步骤：ingest 暂停 → `--final-sweep` → verify（三计数
  零，人工闸）→ 全量解压（既有 replay runner，headroom 执行时重算，
  含新整型 pkey 索引（460M 行 ×5 列，数十 GB）与排序临时空间两项）→
  cutover 单函数 → 再压缩（compression runner）。
- **中止/回退路径**：cutover 事务回滚后表停在"compress=true、零压缩
  chunk、旧 pkey/文本 FK 完好"的窗口前状态，恢复动作 = 直接跑
  compression runner 重压，无需额外补偿。
- 权威表加列（IDENTITY = rewrite + AEL，活库预算 ~10 s）挑低峰应用（D2）。

## Must-preserve（reviewer 核对面）

- 000047 文件文本 + 字面钉测试零触碰；#1069/#1237/#1369 车道零行为变化；
  生产 compression settings 与 pkey 本 PR 零变化。
- workers/output_parser/parser.py、apps/、services/ 查询路径零触碰
  （PR Boundary：db/ + scripts/ + packages/common/timescale_write_guard.py
  的**加法扩展** + tests/ + docs/）；write guard 既有 3 条写路径断言行为
  逐字不变（tests/test_timescale_write_guard_wired.py 全绿）。
- 迁移 000050 幂等、空库 replay（CI real-db）通过；23 个引用
  river_timeseries 的测试文件零破坏（旧文本列语义不变）。
- 权威表加列不改其现有 pkey/FK/既有列数据（IDENTITY 自动填充新列；但
  **是** rewrite + AEL，锁代价按 D2 记录与管控，不得轻描淡写）。
- 回填 runner 绝不 UPDATE 压缩 chunk 与 active chunk（D3 双不变量）。
- write_guard 既有 3 条写路径断言行为逐字不变；其 docstring 与
  tests/test_timescale_write_guard_wired.py:513 的 "three production
  write paths" 描述随第 4 写者入约同步更新（描述性文字，非断言弱化）。
- node-27 实机操作限定：throwaway 库实验 + 000050 应用 + dry-run/probe
  receipt；**不做** enforce 全量回填、不跑 cutover。

## 测试义务（D7）

1. unit：块区间批计划/双保险重入/时长墙减半重试/压缩 chunk 跳过/
   unmatched 与 unmappable fail-closed 分流/receipt schema/flock 互斥。
2. integration（real-db marker，CI 空库 + node-27 throwaway 双跑；压缩
   语义子集以 node-27 为 oracle）：000050 双次 replay 幂等 +
   `pg_attribute.atthasmissing = false`（7 列，机械可查的"无 rewrite"
   钉）；toy 端到端回填含重入（两遍零差异）；D4 cutover + 压缩往返；
   ENUM 值集 ⊇ parser/seed/test 字面量断言。
3. `uv run ruff check .`；`openspec validate
   river-identity-normalization-backfill --strict --no-interactive`。
4. node-27：tasks 1.0 throwaway 实验、000050 应用计时、dry-run/probe
   receipt（AC-3）、定向真实 DB pytest。

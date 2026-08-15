# Design: river_timeseries 身份列规范化（issue #1339）

fixture level: **expanded**。理由：生产库（node-27 active primary）迁移 +
回填、代理键模式在本仓库零先例、需 ADR 0002 amendment。issue 为旧格式（无
`Suggested fixture level` / `Minimal mergeable slice` 字段），起点由本
fixture 自定并记录，无上游偏离可言。

前置复核（2026-08-15，#1233 教训例行项）：issue/epic #1336 均 OPEN；000047
segmentby 仍三文本列；最新迁移号 000049（下一号 000050）；node-27 实测
TSDB 2.10.2 / PG 15.2、449M 行 / 264 GB / 6 chunk（2 压缩）、chunk 级
pg_stats 与 issue 剖面一致（run_id 537 distinct/56 B、segment 56k/37 B、
variable/unit 单值）——前提成立且收益扩大（行数较立案 +42%）。

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

## D1 — ADR 0002：提前触发重评，落 amendment（覆盖 Decision 4 与 6）

Decision 6 defer star schema，重评条件"measured growth curves … national
scale (~100 basins), with compression receipts as the baseline"。**字面
条件未满足**：流域数仍 18（current-production-ops.md:176），a3e2264d 是
display/MVT 渲染扩容而非流域扩张。amendment 如实写成**提前触发重评**，
理由：增长曲线 132M（07-04）→449M 行/264 GB（08-15，6 周 3.4×，周期累积
驱动）+ epic #1336 携带的新剖面；并按 ADR 自设口径引压缩 receipt 实测
压缩比与 #1338 裁剪后的当前索引占比，正面回应 Decision 6 的原始论证
（"压缩已移除 star schema 的大部分收益"只覆盖 terminal chunk——当前 6
中 2，热 chunk 明文与索引不受益）。**Decision 4（segmentby 覆盖现 pkey
的具体列清单）在 cutover 后同被改写，amendment 一并覆盖。**不落
amendment 就动手 = 静默推翻 Accepted ADR，禁止。

## D2 — Schema 形状（迁移 000050，全幂等）

- **不另建维表：给四张既有权威表加整型代理键**（`hydro.hydro_run`、
  `core.basin_version`、`core.river_network_version`、
  `core.river_segment` 各加 `<name>_key INTEGER GENERATED ALWAYS AS
  IDENTITY UNIQUE`——IDENTITY 隐含 NOT NULL，不再重复）。**锁代价如实**：
  IDENTITY 默认值是 volatile `nextval()`，走不了 fast-default → 权威表
  加列是**全表 rewrite + ACCESS EXCLUSIVE**。hydro_run(~704 行)/两张
  version 表(各 18 行)瞬时；`core.river_segment` ~9 万行带 LineString +
  GIST 且在 MVT 生产读路径上（services/tiles/mvt.py 六处 JOIN）——迁移
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
  `quality_flag_e` enum）全 **nullable 无默认值**（metadata-only 加列；
  `quality_flag_e` **绝不镜像**现列 `DEFAULT 'ok'`——带默认值加列在含压
  缩 chunk 的 2.10 上非 metadata-only，迁移注释写明）。**不建 FK 约束、
  不建索引**（YAGNI：无读者；一致性由回填 JOIN + receipt 等值审计 +
  cutover 前 NOT NULL 校验兜底；索引属切换 issue）。
- **实现第一步（写迁移前，tasks 1.0）**：node-27 throwaway 库一次性
  实验，三项**都必须在非空、有代表性的对象上做**（空库出的"通过"是假
  证据）：(a) 建 toy hypertable 灌跨 ≥2 个 chunk interval 的行并
  `compress_chunk` 一个，再加 nullable 无默认列——计时 + 7 列
  `atthasmissing=false` 验证；(b) 灌 ~9 万行的权威表复制品做 IDENTITY
  加列——实测 rewrite 耗时定性 lock_timeout 取值；(c) 复刻事实表文本
  FK（二元组 → toy 权威表）后对新 settings 做 ALTER——证伪"TSDB 2.10
  是否要求 FK 列也被 segmentby∪orderby 覆盖"（唯一能提前证伪 D4 形状
  的实验）。任一结果推翻假设 → 回炉 fixture 而非硬写。

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

## D4 — cutover：verify/prepare/cutover 三段，AEL 压到分钟级，迁移不调用

硬约束 3 使"只换 segmentby"不存在（pkey 与 settings 必须一体切）。但
"一个函数里全扫两遍 449M + 排序建 4.49 亿行索引"会把数小时塞进一把
ACCESS EXCLUSIVE——形状拆三段：

1. **`hydro.verify_river_identity_normalization()`（只读，无锁，窗口前
   跑）**：报告 7 列 NULL 计数、等值审计差异计数（与 runner receipt 同
   口径）、压缩 chunk 计数——全扫成本发生在窗口外。
2. **prepare（runbook 步骤，窗口外）**：**plain `CREATE UNIQUE INDEX`**
   建目标 pkey 形状 `(run_key, river_network_version_key,
   river_segment_key, variable_e, valid_time)` 的唯一索引——**CIC 在本
   hypertable 上不可用**（000049:37-40 记录的 2026-08-14 node-27 实测：
   `ERROR: hypertables do not support concurrent index creation`，TSDB
   2.10.2；plain CREATE INDEX 可用但全程 SHARE 锁，**阻塞 ingest 写、
   不阻塞读，无非阻塞变体**）。目标索引是 4×int4+enum+timestamptz 窄
   形状（远小于 000049:35 那个 ~162 GB 文本索引），但建索引工期与磁盘
   余量仍须进 runbook，且必须排在 ingest 暂停之后（与 D4 前置自洽）；
   `WITH (timescaledb.transaction_per_chunk)` 是否可作较轻变体由
   1.0(d) 实验判定。对 7 列各建 `CHECK (col IS NOT NULL) NOT VALID`
   再 `VALIDATE CONSTRAINT`（SHARE UPDATE EXCLUSIVE，在线）——把 SET
   NOT NULL 的全扫移出 AEL 窗口。
3. **`hydro.cutover_river_identity_normalization()`（窗口内，分钟级）**：
   前置校验全部走 catalog（零压缩 chunk；7 条 validated CHECK 在位；
   目标唯一索引在位且 valid），任一不满足 RAISE（fail-closed）；满足则
   原子执行 7 列 `SET NOT NULL`（借 validated CHECK 免扫）→ drop 旧
   pkey → `ADD CONSTRAINT ... PRIMARY KEY USING INDEX` → ALTER
   `compress_segmentby='run_key, river_network_version_key,
   river_segment_key'`、`compress_orderby='variable_e, valid_time'`。
   （进一步瘦身如借 segment_key 蕴含 network 省列，留切换 issue 评估。）

**既有文本 FK 的去留**（000006:57-58 二元组 → core.river_segment）：
**保留**（文本列本身留到下线 issue，FK 随列走）。风险：TSDB 2.10 是否
要求 FK 列也被 segmentby∪orderby 覆盖，离线不可判（现 FK 恰是现
segmentby 子集，两种实现都解释得通）——tasks 1.0(c) throwaway 实验
证伪；若被纳入校验，cutover 增"drop FK（随文本列退役提前）"步骤并回炉
本节。

**迁移本体不调用任何一段**（空库自动应用会造成 CI/prod 默认 settings
与 pkey 分叉——oracle 完整性红线）。**有序步骤（生产与 toy 测试同一
顺序）**：ingest 暂停 → runner `--final-sweep`（补齐末 chunk）→
verify → prepare（唯一索引 + CHECK VALIDATE）→ cutover →
compress_chunk→decompress_chunk→逐行相等（AC-4 往返证据；integration
在 **node-27 throwaway 库**显式走全序列）。生产执行属切换 issue 运维
窗口，前置清单（写入路径 ON CONFLICT 目标已适配、ingest 暂停、磁盘
余量、低峰）入 runbook 节。000047 文件文本零触碰（字面钉
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
- cutover 窗口前的 prepare 步骤（D4.2：CIC 唯一索引 + CHECK NOT VALID/
  VALIDATE），及 verify（D4.1）→ prepare → cutover 的顺序与回退说明。
- 权威表加列（IDENTITY = rewrite + AEL）挑低峰应用（D2）。

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
  receipt；**不做** enforce 全量回填、不跑 prepare/cutover。

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

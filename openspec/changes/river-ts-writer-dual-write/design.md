# Design: river_timeseries 写路径代理键双写（issue #1340）

## 风险三角与 fixture 级别

- 级别：**expanded**（生产热写路径 + 真实 DB 语义 + 与 #1339 回填车道共存；
  但面窄——两个写者文件 + 测试，无 schema 变更、无窗口操作）。
- issue 无 `Suggested fixture level` 字段（旧格式）；本裁决记录于此，无
  上游分歧可言。
- 必须保持的行为：文本列写入逐字节不变（回滚安全的根基）；DELETE-replace
  语义不变；write-guard 断言不变；`FileOutputParserRepository` JSONL 产物
  不变；parser CLI JSON payload 不变。
- 审查席位（risk-adaptive-cross-review 词表）：数据完整性（双写一致性/
  ON CONFLICT drift）、并发与共存（回填 runner/压缩车道）、测试证据完整性。

## 上游契约偏差（消费不重谈，但记录）

1. issue In-Scope 列了 5 个文件；探索证据（PR 前评审可复核）：
   `forecast_store.py:3452` 是查询索引元数据字符串、`publisher.py:177` 是
   `_has_table` schema probe（其唯一 INSERT 在 :623 目标 `map.tile_layer`）、
   `forcing_copyback_backfill.py:240,265` 是 SELECT-only 列白名单——三者对
   `hydro.river_timeseries` 零写入。真实写者 = `parser.py:773`（生产唯一，
   与 000050:103-104 头注释一致）+ `seed_demo.py:715`（dev）。按证据收窄，
   偏离记录进 PR body。
2. issue 措辞"维表 upsert + 整型外键"承袭 #1339 前的设计假设；#1339 实测
   裁决为权威表代理键、事实列**无 FK**（TSDB 2.10 强制）。本 change 按已
   合并现实交付："维表 upsert"→ 权威表键**只读解析**（写者从不创建权威行
   ——`load_run_context` 缺行即 `HYDRO_RUN_NOT_FOUND`，探索证据 §4 FK 链
   保证正常路径零 miss）。
3. issue 引用 distinct 计数（run_id 704 / segment 90291）过时（live 实测
   3,643 / 209,126）；D1 采纳零额外往返方案后计数量级不再影响设计，
   仅作偏离记录。
4. AC"17 流域"——ADR 0002 amendment 实测口径 18 流域；node-27 leg 按实际
   basin 集合如实记录，不硬凑 17。

## D1 键解析位置：扩展既有 load 查询（两个方法、共三条 SELECT），零额外往返（fixture 评审 round-1 P2-3 采纳）

- **不做批解析、不做跨事务缓存**（初稿的 4 缓存字段被评审证伪两次：
  `transaction()` 走 `dataclasses.replace`，`init=False` 字段不被复制，
  每事务缓存皆空；且 3/4 的键来自单一 `HydroRunContext`，distinct 恒为
  单元素——集合式解析是为不存在的问题设计的机器）。
- 键随**既有 load 查询** 一起取回，额外查询数 = 0：
  - `load_run_context`（parser.py:593-618）的查询追加
    `h.run_key, bv.basin_version_key, rnv.river_network_version_key`。
    **join 谓词钉死（复审迭代1 P2-D）**：`bv` 按
    `bv.basin_version_id = h.basin_version_id`（**hydro_run 侧**——事实行
    文本列取自 hydro_run，`core.model_instance.basin_version_id` 可与之
    不同，join 错源会让等值审计对新行判背离且测试 fixture 盖不住）；
    `rnv` 按 `rnv.river_network_version_id = mi.river_network_version_id`。
    两条 FK 均存在（000006_hydro.sql:6 / 000004_core.sql:74），INNER JOIN
    不改 `HYDRO_RUN_NOT_FOUND` 语义。`HydroRunContext` dataclass 加 3 个
    `int | None = None` 字段（DB-free 路径留 None）。
  - `load_river_segments` 的**主查询（parser.py:638-647）与 fallback
    查询（:649-657）两条 SELECT 都**追加 `river_segment_key`（复审迭代1
    P2-B：只扩 fallback 则生产主路径键恒 None、每次 parse 整批 fail-
    closed）；`RiverSegmentOrder` 加 `river_segment_key: int | None =
    None`。查询按 `river_network_version_id` 限定，pair 粒度天然成立。
- **键传输管道定死（复审迭代1 P1-A）**：走**签名加参**——
  `upsert_river_timeseries(rows, *, run_identity=None, segment_keys=None)`
  keyword-only 带默认值。初稿备选"repository 在 load 时留存映射"被证伪：
  repository dataclass 是 `frozen=True`（parser.py:554），且 `parse_run`
  的 load 跑在 `self.repository`（:183,185，无事务连接）而 upsert 跑在
  `transaction()` 经 `replace` 造出的另一实例上（:585,194-195）——留存
  映射根本到不了 upsert。连带面（全部列入 tasks）：Protocol
  （parser.py:142）、DB-free `FileOutputParserRepository`（:343，接受并
  忽略新参，JSONL 产物逐字不变）、`tests/test_output_parser.py:38` 与
  `tests/test_e2e.py:280` 两个 fake repository 同步扩签名。
  `RiverTimeseriesRow` 与 JSONL 产物零变化：键不进行 dataclass 行。
- **fail-closed**：upsert 侧任一行的键为 None/映射缺失 → 抛结构化错误
  （house 式样 `OUTPUT_PARSE_*` 错误码），整批不写（事务回滚）。既有
  `HYDRO_RUN_NOT_FOUND`/`RIVER_SEGMENTS_MISSING` 已挡上游缺行；000050 的
  `GENERATED ALWAYS AS IDENTITY` 加列重写已给全部既有权威行填键（迁移
  自证 000050:25），新行 IDENTITY 自动填——正常路径零 miss，miss 即脏
  数据，绝不静默写 NULL 键。NULL 键只允许由"旧代码写的行"产生，这是
  回填收敛的语义边界。

## D2 ENUM 列：同一 Python 值重复入参 + 列类型赋值强制（评审 round-1 P1-2 改裁）

初稿"模板内同参数 cast"被证伪：`_execute_values`（parser.py:983-1002）
从不传 `template=`，且位置模板无法复用同一占位符。改裁为最简形态：
- **行元组把 3 个 ENUM 列的值再 append 一次**（同一 Python 字符串对象，
  与文本列取值同源——"构造一致性"来自同一个值，不是同一个占位符），
  行 arity 10→17，`execute_values` 按 arity 自动生成模板，**helper 与
  模板零改动**。
- **不写显式 cast**：客户端插值产出的是无类型字面量，INSERT 落入
  `hydro.river_variable` 等类型列时由 PG 赋值强制完成转换，越界值同样
  报错回滚——fail-closed 属性不依赖 cast（评审核验）。D7.1 断言改为
  "INSERT 列表含 7 新列 + 行元组 arity 17 + ENUM 三值与文本列同源"。
- 越界值（未来写者新字面量未 `ALTER TYPE ADD VALUE`）→ PG 报错 → 事务
  回滚 fail-closed，与 #1266 闭世界文法同哲学。探索 §6 证实当前两写者
  字面量集 ⊆ ENUM 值集（parser 恒 q_down/m3\/s/ok|qc_warning；seed 覆盖
  y_stage/m 分支）。
- 不引入应用侧 enum 映射表的理由：双真值源漂移，与 #1339 拒绝维表的
  论证同构。

## D3 ON CONFLICT 镜像规则

现 DO UPDATE SET（parser.py:786-792）re-set：`basin_version_id`、
`lead_time_hours`、`value`、`unit`、`quality_flag`。规则：**文本列 re-set
谁，其代理对应列同步 re-set**：
```
basin_version_key = EXCLUDED.basin_version_key,
unit_e            = EXCLUDED.unit_e,
quality_flag_e    = EXCLUDED.quality_flag_e
```
冲突键身份五列（run_id/river_network_version_id/river_segment_id/
variable/valid_time）文本侧不 re-set，其对应 `run_key`/
`river_network_version_key`/`river_segment_key`/`variable_e` 同样不 re-set
——身份不变量。该镜像恰好闭合 000050:244-247 预告、
`verify_river_identity_normalization()` 等值审计计数器所监测的 drift 源。
`lead_time_hours`/`value` 无代理列，无镜像项。seed 的
`ON CONFLICT DO NOTHING` 无 SET 列表，无镜像义务。

**可达性如实声明（评审 round-1 P1-1）**：生产重解析路径先执行
DELETE-replace（parser.py:685-755，DELETE 谓词是冲突键的超集），既有行
在 INSERT 前已被删，DO UPDATE 分支对既有行**不触发**——该分支是并发
写者/绕过 DELETE 的重放路径的安全网，近乎死代码但正是等值审计监测的
drift 源。验证因此分两层：unit 断言语句形态（主证据）；integration
**直接重放 INSERT 语句**对预置文本已漂移的既有行触发 DO UPDATE（绕过
DELETE），断言镜像生效。

## D4 双写原子性与回滚安全

- 文本列与代理列在**同一条 INSERT 语句**内写入：不存在任何时刻某行"只有
  文本没有代理"或反之（对新写行）。等值审计对新行零背离由构造成立。
- 回滚安全（issue Description 的硬要求）：文本列写入路径逐字节不变；部署
  回滚到 pre-#1340 代码后新行退化为"只填文本列"——恰好是 #1339 回填
  runner 的哨兵形态，数据零丢失，回填车道自然接管。
- 与回填 runner 共存（评审 round-1 P2-4 改述）：双写行
  `run_key IS NOT NULL` → 不再是哨兵候选，runner 每批候选数自然递减。
  **写者并非只写 active chunk**——analysis/hindcast run（000045）与重
  解析都写终态 chunk，与 runner 的 eligible 集合有交集；安全性不来自
  "零交集"，而来自：guard 先断言未压缩、写者同事务原子、runner 更新集
  =`run_key IS NULL` 哨兵行而双写行不满足谓词——两者的**更新行集不相交**。
  竞争面是行锁等待/statement_timeout（会以 runner 的 duration_wall 口径
  上报，见 #1408），不是数据损坏。`--final-sweep` 静默断言（#1403 修复
  后真生效）仍是终局闸。

## D5 吞吐证据（AC-2）

- 探索 §9：parser 路径无任何计时仪表，`ops.pipeline_job` 计时属 Slurm 子
  系统。**不往生产代码加临时仪表**（用完即拆的仪表是熵）；node-27 证据用
  "同一 run 重解析 wall-clock 对比"：选一个已完成 run，旧代码 `time uv run
  ... parse` 一次、新代码一次（DELETE-replace 语义使重解析幂等），记录两次
  wall-clock 与 rows_written 于 PR 评论。D1 设计下预期增量 = 0 条额外
  查询 + 每行 7 参数序列化，应在噪声级；若对比显示 >10% 退化则回炉 D1。**前提（评审 round-1 P2-5）**：所选 run 的 valid_time 窗口必须
  全落未压缩 chunk（表已开压缩，压缩 chunk 会让 guard 在 DELETE 前抛
  `CompressedChunkWriteError`）——选 run 时先查 chunk 压缩状态并记入
  receipt。
- 偏离记录：AC 字面"记录一个 cycle 的 ingest 耗时对比"按"同 run 重解析
  对比"交付（cycle 级计时不存在，新造 cycle 级仪表超出 PR 边界）。

## D6 测试策略（D7.x = Evidence Floor 映射）

- D7.1 unit（扩展 `tests/test_output_parser.py` 或新模块）：
  - INSERT 列表含 7 新列、行元组 arity 17 且 ENUM 三值与文本列同源、
    ON CONFLICT SET 恰为镜像三列（文本列表逐字不变红证）
  - 键传递：load 查询含键列、键为 None/缺失 → 结构化错误且整批零写入
  - fake cursor 语句序列：guard SELECT → DELETE → INSERT（守卫顺序不变，
    无新增独立解析查询）
- D7.2 integration（真实 DB，`-m integration`）：
  - 双写端到端：parse 后新行 7 列全非 NULL 且等值审计
    `verify_river_identity_normalization()` 三计数零增量
  - ON CONFLICT：直接重放 INSERT 语句对预置漂移行触发 DO UPDATE（绕过
    DELETE-replace，见 D3 可达性声明），断言镜像生效
  - 与回填共存：库中混入旧形态行（手工 NULL 键行）→ runner dry-run 只报
    旧行为候选
  - seed_demo 全量 seed 后 y_stage/m 分支行代理列正确
- 红证配对：新断言在 pre-change 代码上必红（stash 法）。

## 非目标

读路径与 display（#1341）、旧文本列下线、enforce 全量回填与 cutover 窗口
（#1403 已声明归属）、write-guard 行为变更、`RiverTimeseriesRow`/JSONL
产物 schema、OpenAPI。

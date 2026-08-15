# Tasks: river_timeseries 身份列规范化（issue #1339）

## 1. 实现

- [ ] 1.0 **写迁移前**：node-27 throwaway 库一次性实验，三项均在非空
      代表性对象上做（design D2 末段）：（a）toy hypertable 跨 ≥2 chunk
      灌行 + compress_chunk 一个 → 加 nullable 无默认列计时 +
      atthasmissing 验证；（b）~9 万行权威表复制品 IDENTITY 加列 →
      rewrite 耗时 + lock_timeout 取值；（c）复刻文本 FK 后对新
      settings ALTER → 证伪 FK 列是否须被 segmentby∪orderby 覆盖；
      （d）toy hypertable 上验证 `ADD CONSTRAINT ... PRIMARY KEY USING
      INDEX`（约束下推各 chunk）与 `CHECK NOT VALID`+`VALIDATE` 的
      传播/免扫效果，顺带试 `CREATE INDEX ... WITH
      (timescaledb.transaction_per_chunk)` 可否作唯一索引较轻变体
      （CIC 已被 000049:37-40 实测否定，不再试）。结果记录；任一推翻
      假设 → 回炉 fixture 而非硬写
- [ ] 1.1 迁移 `db/migrations/000050_river_identity_normalization.sql`：
      四权威表代理键（ALTER 前 SET lock_timeout，头注释记录锁代价与
      1.0(b) 实测耗时）+ 3 ENUM + 事实表 7 nullable 列（design D2；全
      幂等；quality_flag_e 无 DEFAULT；无 FK 约束、无新索引）
- [ ] 1.2 ENUM 值集核定：parser.py 写入字面量 ∪ db/seeds/seed_demo.py
      （q_down/y_stage/m3/s/m）∪ node-27 实测 distinct，来源逐条注释在
      迁移；tests/test_tile_publisher.py:275 的 `'m3 s-1'` 改 `'m3/s'`
      （孤例测试字面量不焊进生产 ENUM，design D2）
- [ ] 1.3 `packages/common/timescale_write_guard.py` 加法扩展：chunk 级
      `assert_chunk_uncompressed`（既有 3 写路径断言行为逐字不变；
      docstring 与 wired 测试的 "three production write paths" 描述性
      文字同步为四写者口径）
- [ ] 1.4 `scripts/node27_river_identity_backfill.py`：ctid 块区间分批 +
      双保险重入 + 时长墙减半重试 + 压缩/active chunk 双跳过（active
      判定复用 compression runner terminal/lag 口径）+ `--final-sweep`
      （断言 ingest 静默才覆盖末 chunk）+ 每批候选数 vs rowcount 差值
      检测 unmatched/unmappable fail-closed 分流 + 等值审计 + `--probe`
      rollback 采样 + flock（design D3）；receipt schema
      `schemas/river_identity_backfill_receipt.schema.json`
- [ ] 1.5 `hydro.verify_river_identity_normalization()`（只读）+
      `hydro.cutover_river_identity_normalization()`（catalog 前置校验
      + 分钟级 AEL）随 000050 交付，迁移本体不调用；prepare（plain
      CREATE UNIQUE INDEX——CIC 在 hypertable 上不可用，000049:37-40
      ——+ CHECK VALIDATE）为 runbook 步骤（design D4 三段有序）
- [ ] 1.6 runbook 节：压缩 timer mask/stop + timer 状态入 receipt、逐
      chunk VACUUM、磁盘余量前置、压缩 chunk 解压→回填→再压编排、
      verify→prepare→cutover 顺序与前置清单、权威表加列低峰（design D6）
- [ ] 1.7 ADR 0002 amendment：Decision 4 + 6，提前触发重评口径（design
      D1：18 流域如实、压缩 receipt 压缩比 + #1338 后索引占比数字）

## 2. 验证（Evidence Floor）

- [ ] 2.1 unit：批计划/双保险重入/时长墙/压缩跳过/fail-closed 分流/
      receipt schema/flock（design D7.1）
- [ ] 2.2 integration（real-db marker）：000050 双次 replay 幂等 + 7 列
      `pg_attribute.atthasmissing = false` 钉；toy 端到端回填含重入两遍
      零差异；ENUM 值集 ⊇ 字面量断言。**压缩语义子集（D4 三段
      verify→prepare→cutover + 往返、压缩 chunk 跳过）以 node-27
      throwaway 库为 oracle**（CI 是 pg15-latest 非 2.10，不算数）
- [ ] 2.3 `uv run pytest -q tests/test_migrations.py` + 定向 runner/write
      guard 测试全绿（含 tests/test_timescale_write_guard_wired.py）
- [ ] 2.4 `uv run ruff check .` 通过
- [ ] 2.5 `openspec validate river-identity-normalization-backfill --strict
      --no-interactive` 通过
- [ ] 2.6 diff 自证：000047 文件、parser.py、apps/services 查询路径、
      #1069 车道脚本零触碰；write guard 既有断言零改动
- [ ] 2.7 node-27：000050 应用（加列语句计时记录）+ backfill dry-run +
      `--probe` receipt（逐 chunk 行数/字节/耗时/锁等待/膨胀预估与
      `/home` 余量/压缩与 active chunk 跳过清单/unmatched 计数；AC-3）
- [ ] 2.8 node-27：throwaway 库 integration 子集（cutover positive path
      + 压缩往返）通过 + 定向真实 DB pytest

## 3. 交付记录

- [ ] 3.1 PR body：D5 偏离记录（生产 cutover 路由 + "维表"按意图交付）+
      AC 逐条覆盖声明
- [ ] 3.2 PR body：node-27 receipt 摘要 + 生产 enforce 全量回填与 cutover
      的后续窗口声明

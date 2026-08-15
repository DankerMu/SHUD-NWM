# Tasks: river_timeseries 身份列规范化（issue #1339）

## 1. 实现

- [x] 1.0 **写迁移前**：node-27 throwaway 库一次性实验（记录
      `probe-1339-throwaway.md`，2026-08-15 完成）。结果：(a) 确认
      metadata-only（含 a' 理由修正）；(b) 确认（209k 行复制品 ~6.6 s，
      lock_timeout 2 s 快败可用）；(c) **证伪"FK 可保留"**（FK 列必须
      入 segmentby）；(d) **证伪 USING INDEX / transaction_per_chunk /
      窗口外 prepare**（compress=true 拒全部 unique DDL），确认
      CHECK/VALIDATE/SET NOT NULL 免扫与 d-6 完整可行序列 + 45x 往返
      零丢行。fixture 已按"推翻即回炉"重写 D4（第三稿）与硬约束 6-8
- [x] 1.1 迁移 `db/migrations/000050_river_identity_normalization.sql`：
      四权威表代理键（ALTER 前 SET lock_timeout，头注释记录锁代价与
      1.0(b) 实测耗时）+ 3 ENUM + 事实表 7 nullable 列（design D2；全
      幂等；quality_flag_e 无 DEFAULT；无 FK 约束、无新索引）
- [x] 1.2 ENUM 值集核定：parser.py 写入字面量 ∪ db/seeds/seed_demo.py
      （q_down/y_stage/m3/s/m）∪ node-27 实测 distinct，来源逐条注释在
      迁移；tests/test_tile_publisher.py:275 的 `'m3 s-1'` 改 `'m3/s'`
      （孤例测试字面量不焊进生产 ENUM，design D2）
- [x] 1.3 `packages/common/timescale_write_guard.py` 加法扩展：chunk 级
      `assert_chunk_uncompressed`（既有 3 写路径断言行为逐字不变；
      docstring 与 wired 测试的 "three production write paths" 描述性
      文字同步为四写者口径）
- [x] 1.4 `scripts/node27_river_identity_backfill.py`：ctid 块区间分批 +
      双保险重入 + 时长墙减半重试 + 压缩/active chunk 双跳过（active
      判定复用 compression runner terminal/lag 口径）+ `--final-sweep`
      （断言 ingest 静默才覆盖末 chunk）+ 每批候选数 vs rowcount 差值
      检测 unmatched/unmappable fail-closed 分流 + 等值审计 + `--probe`
      rollback 采样 + flock（design D3）；receipt schema
      `schemas/river_identity_backfill_receipt.schema.json`
- [x] 1.5 `hydro.verify_river_identity_normalization()`（只读三计数）+
      `hydro.cutover_river_identity_normalization()`（单事务：零压缩
      chunk 前置 → compress=false → drop 文本 FK → 7× CHECK NOT
      VALID/VALIDATE（即零 NULL 闸）/SET NOT NULL → 换整型 pkey →
      compress=true 新 settings；= probe d-6 实证序列）随 000050 交付，
      迁移本体不调用（design D4 第三稿两段）
- [x] 1.6 runbook 节：压缩 timer mask/stop + timer 状态入 receipt、逐
      chunk VACUUM、磁盘余量前置（回填膨胀 + cutover 全量解压两口径，
      执行时重算）、压缩 chunk 解压→回填→再压编排、ingest 暂停→
      final-sweep→verify→全量解压→cutover 顺序与前置清单、PK 建索引
      期间读阻塞声明、权威表加列低峰（design D6）
- [x] 1.7 ADR 0002 amendment：Decision 4 + 6，提前触发重评口径（design
      D1：18 流域如实、压缩 receipt 压缩比 + #1338 后索引占比数字）

## 2. 验证（Evidence Floor）

- [x] 2.1 unit：批计划/双保险重入/时长墙/压缩跳过/fail-closed 分流/
      receipt schema/flock（design D7.1）
- [x] 2.2 integration（real-db marker）：000050 双次 replay 幂等 + 7 列
      `pg_attribute.atthasmissing = false` 钉；toy 端到端回填含重入两遍
      零差异；ENUM 值集 ⊇ 字面量断言。**压缩语义子集（D4 两段
      verify→cutover 全序列 + 往返、压缩 chunk 跳过、cutover 负路径
      ——留一行 NULL 调 cutover 断言抛错且 compress=true/文本 FK/旧
      pkey 三者原状未变）以 node-27 throwaway 库为 oracle**（CI 是
      pg15-latest 非 2.10，不算数）
- [x] 2.3 `uv run pytest -q tests/test_migrations.py` + 定向 runner/write
      guard 测试全绿（含 tests/test_timescale_write_guard_wired.py）
- [x] 2.4 `uv run ruff check .` 通过
- [x] 2.5 `openspec validate river-identity-normalization-backfill --strict
      --no-interactive` 通过
- [x] 2.6 diff 自证：000047 文件、parser.py、apps/services 查询路径、
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

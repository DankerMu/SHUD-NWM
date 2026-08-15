# Proposal: river_timeseries 身份列规范化——权威表代理键 + 可重入回填（issue #1339）

## Why

`hydro.river_timeseries`（node-27 生产库，2026-08-15 实测 459.9M 行 / 249 GB /
6 个周 chunk，其中 2 个已压缩，probe 复测口径）每行携带约 163 B 重复文本身份列（run_id 56 B
× chunk 级 537 distinct、river_segment_id 37 B × 56k 等，chunk 级 pg_stats
实测与 issue 剖面一致）。规范化为整型代理键后 heap 与含身份列的索引大幅
收缩。ADR 0002 Decision 6 曾 defer 该 star schema；其字面重评条件（national
scale ~100 basins）未满足（现 18 流域），本变更以增长曲线（132M→459.9M 行，
6 周 3.4×）+ epic #1336 新剖面**提前触发重评**并落 amendment（含被 cutover
改写的 Decision 4），非绕过 ADR。

## What Changes

- **迁移 000050**：给四张既有权威表（`hydro.hydro_run`、
  `core.basin_version`、`core.river_network_version`、`core.river_segment`）
  加 `INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE` 代理键（**不另建维表**
  ——避免双身份源、seeding 归零、river_segment 二元组自然键粒度天然正确；
  issue 的"维表"按意图=整型代理键交付，偏离记录。权威表加列是 rewrite +
  ACCESS EXCLUSIVE——lock_timeout + 低峰 + 实测耗时入迁移注释）+ 3 个
  native ENUM（值集 = parser ∪ seeds ∪ 实测 distinct；孤例测试字面量改
  测试不进 ENUM）+ 事实表 7 个 **nullable 无默认值**新列（metadata-only
  加列）。全幂等。
- **回填 runner** `scripts/node27_river_identity_backfill.py`：dry-run 默认
  / `--enforce` / bounded / receipted / flock 互斥（house 式样 #1069）；
  **ctid 块区间**分批（非 LIMIT 子查询——O(n²) 否决）+ NULL 哨兵与块游标
  双保险重入；单批事务时长墙（区间减半重试一次）；压缩 chunk 跳过并
  receipt 报告（TSDB 2.10 无压缩 DML）+ active chunk 跳过（不与 ingest
  抢行锁）；每批候选数 vs rowcount 差值检测 unmatched/unmappable，
  fail-closed 单列计数；`--probe` rollback 采样批（真实 UPDATE 计时 +
  pg_locks 观测后回滚，AC-3 证据零持久化）；等值审计计数（识别 writer
  ON CONFLICT DO UPDATE 造成的文本↔代理背离）。压缩判定走共享写守卫的
  加法扩展（timescale_write_guard 增 chunk 级断言，#851 D5 契约）。
- **verify + cutover 两段**（AC-4；D4 第三稿，按 probe-1339 实测重写）：
  TSDB 2.10 要求 segmentby∪orderby 覆盖 PK 列（000047:11-12）且
  `compress=true` 状态拒绝一切 unique DDL（probe d-2）——压缩设置与
  pkey 切换不可分，且不存在窗口外 prepare。有序步骤 = ingest 暂停 →
  runner `--final-sweep` → 只读 `verify_*`（NULL/等值审计/压缩 chunk
  三计数，人工闸）→ 全量解压 → `cutover_*` 单事务（compress=false →
  drop 文本 FK（实测强制：FK 列必须入 segmentby，文本 FK 不可存活）→
  CHECK NOT VALID/VALIDATE（即零 NULL 闸）/SET NOT NULL 免扫 → 换整型
  pkey（窗口内建索引为主成本）→ compress=true 新 settings；= probe
  d-6 实证序列，45x 往返零丢行）。**迁移本体不调用任何一段**（避免
  CI/prod schema 分叉）；node-27 throwaway 库 integration 自动化全序列
  + 压缩/解压往返。生产执行随切换 issue 运维窗口（runbook 前置清单 +
  磁盘余量执行时重算）。
- **ADR 0002 amendment**（Decision 4 + 6，提前触发重评口径）+ runbook 节
  （回填编排：压缩 timer mask、逐 chunk VACUUM、磁盘余量、压缩 chunk
  解压→回填→再压、cutover 前置清单）。

## Impact

- Affected specs: `river-identity-normalization`（新 capability，ADDED）
- Affected code: `db/migrations/000050_*.sql`、
  `scripts/node27_river_identity_backfill.py`、
  `schemas/river_identity_backfill_receipt.schema.json`、
  `packages/common/timescale_write_guard.py`（加法扩展）、unit +
  integration 测试、`docs/adr/0002-*.md`（amendment）、runbook 节
- Not affected（non-goals）: 写入路径（parser.py）、读取/display 路径、
  旧文本列下线、pkey/segmentby 的**生产**切换执行、000047 文件文本、
  #1069 车道行为、write guard 既有 3 条写路径行为
- node-27 实机：throwaway 库实验（三项前置证伪）+ 000050 应用 + 回填
  dry-run/probe receipt；**不做** enforce 全量回填、不跑 cutover

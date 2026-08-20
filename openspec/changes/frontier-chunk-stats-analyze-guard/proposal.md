# Proposal: frontier-chunk-stats-analyze-guard

## Why

Issue #1378：生产 display 的 `GET /api/v1/layers/discharge/valid-times`
named-identity 分支在 node-27 上从 0.56 ms 退化到 887 ms（1583x）。node-27 只读
诊断轮（2026-08-20 receipt，见 issue 评论）裁定成因为 **(b) ingest 前沿 chunk 的
统计漂移**：整条 ingest 链（`scripts/node27_autopipeline.py`、
`workers/output_parser`、`scripts/node27_timeseries_compression.py`）没有任何
ANALYZE 步骤，前沿 chunk 的 planner 统计完全依赖 autovacuum 的 10% scale factor
——250M 行的 chunk 允许 ~25M 行修改不触发 autoanalyze，而每个新 cycle 的
`run_id`/`run_key` 是统计里不存在的新值，planner 估行 ≈0，在
`DISTINCT … ORDER BY valid_time DESC LIMIT` 下翻转为"顺 valid_time 索引逐行过滤"
计划（实测 Rows Removed by Filter: 2,715,324）。假设 (a)（chunk 索引缺失/失效）被
机械排除（全部索引在场且 `indisvalid=t`）；(c)（SkipScan 成本模型）仅是文本形态下的
次要放大项（38.6→18.7 ms），随 #1342 退场。

漂移会重现：2026-08-20 复采仍见 chunk 58/62 各挂 ~6.8M `n_mod_since_analyze`
（2.7%/5.5%，低于阈值）。#1341 的键形态**并非免疫**——新 `run_key` 同样是统计外新值，
只是索引前缀更挑剔、误选空间更小。

## What Changes

1. **autopipeline tick 末尾统计看护**（主修）：`scripts/node27_autopipeline.py`
   在 phase 3（publish）之后新增 phase 3.5——当本 tick 有 ≥1 run ingested 时，
   对 `hydro.river_timeseries` 与 `met.forcing_station_timeseries` 的**未压缩**
   chunk 中 `n_mod_since_analyze >= 10_000` 者执行 chunk 级 `ANALYZE`（机制匹配
   触发：任何被本 tick 触及的 chunk 必然过槛，下限只为跳过未触及者；每 tick
   上限 3 个、逐条 120 s statement_timeout、裁掉者记 deferred），结果（清单 +
   耗时 + 回读的 `last_analyze` 自检）写入 tick summary JSON。
2. **压缩 runner 顺带 ANALYZE**（ride-along）：`scripts/node27_timeseries_compression.py`
   对本次 run 记账中到达 compressed 状态的每个 chunk 执行 `ANALYZE`，结果记入
   receipt（schema_version 2.1→2.2，closed schema 同步扩展 + example 更新）；
   失败不改变压缩记账、`outcome` 与进程 rc。
3. **runbook 记录**：`docs/runbooks/tier-node27-timeseries-storage.md` 新增
   "ingest 前沿 chunk 统计漂移"小节（成因、机制、看护、复核手段）。
4. **回归看护测试**：单测钉住 (1)(2) 的存在与触发条件（mock DB，不需真实 TimescaleDB）。

## Non-Goals

- 不改 `services/tiles/mvt.py` 查询形状（当前键形态 1.163 ms，已满足 <50 ms 验收）。
- 不做 valid-time discovery 物化表（issue 备选方案；当前无必要，YAGNI）。
- 不调 autovacuum 全局/表级参数（TimescaleDB chunk 不继承 hypertable reloptions，
  逐 chunk 设置是持续追赶游戏；显式 ANALYZE 步骤更可审计）。
- 不处理 ingest 突发窗内的实例级 I/O/锁竞争尖峰（同刻 EXPLAIN 执行仍 ~1-3 ms，
  是载荷耦合非计划退化；按纪律另行报告）。

## Impact

- Affected specs: 新增 capability `frontier-chunk-statistics-freshness`。
- Affected code: `scripts/node27_autopipeline.py`、
  `scripts/node27_timeseries_compression.py`、
  `schemas/timeseries_compression_receipt.schema.json`（2.2 bump）、
  `schemas/examples/timeseries_compression_receipt.example.json`、
  `docs/runbooks/tier-node27-timeseries-storage.md`、新增/扩展对应单测。
- 部署面：node-27 两个 systemd timer（autopipe 10min、compression 每日）无需改动；
  代码随 git pull 生效。

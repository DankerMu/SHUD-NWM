# Tasks: frontier-chunk-stats-analyze-guard (#1378)

## 1. Implementation

- [x] 1.1 `scripts/node27_autopipeline.py`：phase 3.5 stats guard——
      `_analyze_frontier_chunks(database_url)`（`n_mod_since_analyze >= 10_000`
      触发查询 + 每 tick 上限 3 个（降序取前 3、其余记 `deferred`）+ 逐条
      `statement_timeout = 120s` + ANALYZE 后回读 `last_analyze` 自检），
      summary 增加 `stats_guard` 块；`NODE27_AUTOPIPE_STATS_GUARD=off` 跳过；
      失败两级——单 chunk ANALYZE 失败逐 chunk 隔离（条目记 `status:"failed"` +
      `error`，剩余照常尝试），guard 级失败（连接/候选查询）记
      `stats_guard.status:"failed"` + `error`；两级都不改 tick rc。
- [x] 1.2 **撤除压缩侧 ride-along**（终审 P1 否决，见 design D3）：
      `scripts/node27_timeseries_compression.py` 恢复无 ANALYZE 改动；
      `schemas/timeseries_compression_receipt.schema.json` 与
      `schemas/examples/timeseries_compression_receipt.example.json` 回到 2.1
      原状；相关新增用例移除，round-1.5 对既有负向 schema 测试的**加强**
      （error-path 断言）保留且须对 2.1 原状 example 仍成立。
- [x] 1.3 `docs/runbooks/tier-node27-timeseries-storage.md`：新增"ingest 前沿 chunk
      统计漂移"小节（新值不可见机制、看护位置与触发条件、`pg_stat_user_tables`
      复核 SQL、PG15 非 owner ANALYZE 静默跳过陷阱）。

## 2. Tests（requirement-driven，mock DB）

- [x] 2.1 autopipeline 六场景：触发 / 不触发（无 ingest 或低于下限）/ 超上限记
      deferred / 失败不拖垮 tick / last_analyze 未刷新记 warning / 开关 off。
- [x] 2.2 压缩 runner：ride-along 撤除后其新增用例一并移除；既有回归套件
      （含 round-1.5 加强的负向 schema 测试）在 2.1 原状 schema/example 上全绿。

## Evidence Floor

- [x] E1 `uv run pytest -q tests/test_node27_autopipeline_handoff.py
      tests/test_node27_timeseries_compression.py
      tests/test_node27_timeseries_compression_live_evidence.py`（含新增用例）PASS
- [x] E2 `uv run ruff check .` PASS
- [x] E3 `openspec validate frontier-chunk-stats-analyze-guard --strict --no-interactive` PASS
- [ ] E4 **硬门**，node-27 实机，须观测到一个**实际触发** guard 的 tick：
      (i) tick summary JSON 的 `stats_guard.analyzed` 含至少一条
      `status: "ok"` 条目（`analyzed` 记录的是尝试，failed/warning 不算数）；
      (ii) `pg_stat_user_tables.last_analyze` 实刷（SQL 复核，防 PG15 非 owner
      静默跳过）；
      (iii) guard 后重跑 issue 验收项 2 的 Q2（当前键形态）EXPLAIN ANALYZE：
      Execution Time < 50 ms、被执行节点无百万级 `Rows Removed by Filter`、
      计划走 selected-identity 键索引——计划全文进 receipt。
- [ ] E5 issue #1378 验收项 3/4 的诊断 receipt 已发（兄弟面健康、header 旁路
      wall time）；验收项 2 由 E4(iii) 的**修复后**实测覆盖；本 change 落地后在
      issue 记录关闭依据。

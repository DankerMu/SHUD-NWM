# Tasks: frontier-chunk-stats-analyze-guard (#1378)

## 1. Implementation

- [x] 1.1 `scripts/node27_autopipeline.py`：phase 3.5 stats guard——
      `_analyze_frontier_chunks(database_url)`（`n_mod_since_analyze >= 10_000`
      触发查询 + 每 tick 上限 3 个（降序取前 3、其余记 `deferred`）+ 逐条
      `statement_timeout = 120s` + ANALYZE 后回读 `last_analyze` 自检），
      summary 增加 `stats_guard` 块；`NODE27_AUTOPIPE_STATS_GUARD=off` 跳过；
      失败 `status:"failed"` + `error`，不改 tick rc。
- [x] 1.2 `scripts/node27_timeseries_compression.py`：全部压缩完成后、receipt
      发布前，对本次 run 记账中到达 compressed 状态的每个 chunk（正常/测量失败后/
      lost-ack 对账三路径）ANALYZE；每条前检查剩余墙钟（`wrapper_wall_seconds -
      elapsed - 120s` 发布保留段，不足 30s 跳过剩余并记
      `analyze_error:"wall_budget_exhausted"`），`statement_timeout = min(300s, 剩余)`；
      receipt 条目增 `analyze_seconds` / `analyze_error`；失败/跳过不置
      `any_errors`、不改 `outcome` 与进程 rc。
      **无条件**扩 closed schema：`schemas/timeseries_compression_receipt.schema.json`
      `schema_version` 2.1→2.2（per-version 分支，新字段仅 2.2 合法），
      同步更新 `schemas/examples/timeseries_compression_receipt.example.json`。
- [x] 1.3 `docs/runbooks/tier-node27-timeseries-storage.md`：新增"ingest 前沿 chunk
      统计漂移"小节（新值不可见机制、看护位置与触发条件、`pg_stat_user_tables`
      复核 SQL、PG15 非 owner ANALYZE 静默跳过陷阱）。

## 2. Tests（requirement-driven，mock DB）

- [x] 2.1 autopipeline 六场景：触发 / 不触发（无 ingest 或低于下限）/ 超上限记
      deferred / 失败不拖垮 tick / last_analyze 未刷新记 warning / 开关 off。
- [x] 2.2 压缩 runner 四场景：记账到达 compressed 即 ANALYZE / ANALYZE 失败不
      改记账、`outcome` 与 rc / 剩余墙钟不足时跳过并记 wall_budget_exhausted、
      receipt 照常发布 / 2.2 receipt（含新字段）过 schema 校验且 example 有效。

## Evidence Floor

- [x] E1 `uv run pytest -q tests/test_node27_autopipeline_handoff.py
      tests/test_node27_timeseries_compression.py
      tests/test_node27_timeseries_compression_live_evidence.py`（含新增用例）PASS
- [x] E2 `uv run ruff check .` PASS
- [x] E3 `openspec validate frontier-chunk-stats-analyze-guard --strict --no-interactive` PASS
- [ ] E4 **硬门**，node-27 实机，须观测到一个**实际触发** guard 的 tick：
      (i) tick summary JSON 含 `stats_guard.analyzed` 非空；
      (ii) `pg_stat_user_tables.last_analyze` 实刷（SQL 复核，防 PG15 非 owner
      静默跳过）；
      (iii) guard 后重跑 issue 验收项 2 的 Q2（当前键形态）EXPLAIN ANALYZE：
      Execution Time < 50 ms、被执行节点无百万级 `Rows Removed by Filter`、
      计划走 selected-identity 键索引——计划全文进 receipt。
- [ ] E5 issue #1378 验收项 3/4 的诊断 receipt 已发（兄弟面健康、header 旁路
      wall time）；验收项 2 由 E4(iii) 的**修复后**实测覆盖；本 change 落地后在
      issue 记录关闭依据。

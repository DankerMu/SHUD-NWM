# Proposal

## Why

批 DB：收口 `hydro.hydro_run` 的索引面，以及 #2424 D1 latest-cycle 发现语句的两笔成本债，一个 PR 完成。

- **#2634**：#2048 收敛（`000063`）之后，账本里有两组字节相同的 partial index：
  - 3 份 `(LOWER(source_id), run_type, basin_version_id, cycle_time DESC, run_id DESC) WHERE cycle_time IS NOT NULL AND status IN (...)`；
  - 2 份 `(basin_version_id, status) WHERE status IN (...)`。

  每次写 `hydro_run` 都要多维护几份副本，对 planner 没有任何好处。node-27 `pg_indexes`（2026-09-28）确认 9 个索引都在。
- **#2626**：D1 的 `cand AS MATERIALIZED` 候选 CTE 故意不带 status 谓词（用户决策 a），用不上任何 status-partial 索引，只能 Seq Scan `hydro_run`。node-27 实测 10723 行 / 1299 块，约每周 +143 块，预计约 24 周后单项就会越过 D11 的 5000 门。
- **#2630**：D1 探针对每个候选在每个 chunk 上都要做一次 seek，空结果 pin 会探遍所有候选。node-27 最坏 pin（`basins_zhaochen_bst`，GFS+IFS，239 个候选 × 29 个 chunk）为 24497 shared hit，而 `hydro_run` 从不修剪。

## What Changes

- 新增 forward migration `db/migrations/000065_*.sql`：
  - `DROP INDEX CONCURRENTLY IF EXISTS` 删除 `hydro_run_qhh_latest_candidate_parsed_idx`、`hydro_run_display_ready_candidate_idx`、`hydro_run_display_ready_basin_status_idx`。
  - 保留代码、测试和 receipt 引用的 `hydro_run_qhh_latest_candidate_idx` 与 `hydro_run_display_product_basin_status_idx`。
  - 新建无 status 谓词的 forecast 候选 partial index，采用"先 DROP 再 CREATE CONCURRENTLY"，失败后可以安全重跑。
- `packages/common/forecast_store.py::_per_source_latest_cycles`：
  - `cand` 增加携带 `h.end_time`；
  - 事实探针由 `WHERE EXISTS (...)` 改为 `CROSS JOIN LATERAL (... LIMIT 1)`；
  - 探针增加 `rt.valid_time >= o.cycle_time AND rt.valid_time <= o.end_time`，让 TimescaleDB 在运行时排除 chunk。
- 已应用的迁移文件一律不改。

## Triage

```text
Issue type: feature (perf) + bugfix (cost bound)
Fixture level: expanded
Upstream suggested level: absent (三单均未给)；按 migration + 生产 DB + 共享读路径判 expanded
Blast radius: node-27 生产 hydro_run 索引集（写放大/planner 选择）、issue_time=latest 默认路径的 latest-cycle 结果（错 → 前端显示错的 issue cycle 或空）
Selected risk packs: Schema / columns / units / field names; Resource limits / large input / discovery; Error handling / rollback / partial outputs; Legacy compatibility / examples; Documentation / migration notes; Concurrency / shared state / ordering
Evidence floor: 见 tasks.md Evidence Floor
```

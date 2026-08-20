# frontier-chunk-statistics-freshness Specification

## Purpose
TBD - created by archiving change frontier-chunk-stats-analyze-guard. Update Purpose after archive.
## Requirements
### Requirement: Ingest tick MUST refresh planner statistics on frontier chunks it touched

`scripts/node27_autopipeline.py` 的每个 tick MUST 在 publish 阶段之后、summary 之前，当本 tick 有至少一个 run 完成 ingest 时，对 `hydro.river_timeseries` 与 `met.forcing_station_timeseries` 的未压缩 chunk 中满足 `n_mod_since_analyze >= 10_000` 的每个 chunk 执行 chunk 级 `ANALYZE`（下限的作用仅是跳过本 tick 未触及、只有零星迟到写入的 chunk；任何被本 tick ingest 触及的 chunk 必然过槛）。每 tick 至多执行 3 个 chunk（按 `n_mod_since_analyze` 降序），被裁掉者 MUST 记入 summary 的 `stats_guard.deferred`。每条 `ANALYZE` MUST 在 `statement_timeout = 120s` 内执行，执行后 MUST 回读 `pg_stat_user_tables.last_analyze` 并连同耗时写入 summary；若 `last_analyze` 未刷新（PG15 非 owner 的静默跳过），该 chunk 条目 MUST 记 `status: "warning"`。单个 chunk 的 `ANALYZE` 失败 MUST NOT 阻止对剩余已选 chunk 的尝试（逐 chunk 隔离——否则被锁/消失的 chunk 会吞掉整批并每 tick 复发），该 chunk 条目 MUST 以 `status: "failed"` 加 `error` 记录；guard 级失败（连接/候选查询）MUST 以 `stats_guard.status = "failed"` 加 `error` 字符串如实记录。任何失败 MUST NOT 使 tick 返回码变为失败。`NODE27_AUTOPIPE_STATS_GUARD=off` 时 MUST 跳过并在 summary 标注 skipped。

#### Scenario: 新 run ingest 后被触及的 chunk 被 ANALYZE

- **GIVEN** 本 tick ingest 了 ≥1 个 run，且某未压缩 chunk 的
  `n_mod_since_analyze >= 10_000`
- **WHEN** tick 进入 stats guard 阶段
- **THEN** 该 chunk 被执行 `ANALYZE`，summary 的 `stats_guard.analyzed` 含其名字、
  耗时与回读的 `last_analyze`

#### Scenario: 无 ingest 或低于下限时不做无谓工作

- **GIVEN** 本 tick 没有 run 被 ingest，或所有 chunk 的
  `n_mod_since_analyze < 10_000`
- **WHEN** tick 走到 stats guard 阶段
- **THEN** 不执行任何 `ANALYZE`，summary 如实记录空清单或 not_triggered

#### Scenario: 超过每 tick 上限时不静默截断

- **GIVEN** 过槛 chunk 多于 3 个
- **WHEN** stats guard 执行
- **THEN** 按 `n_mod_since_analyze` 降序只执行前 3 个，其余 chunk 名字记入
  `stats_guard.deferred`

#### Scenario: 单 chunk ANALYZE 失败不吞剩余批次

- **GIVEN** 已选 3 个 chunk，第 1 个 `ANALYZE` 报错或超时
- **WHEN** stats guard 继续执行
- **THEN** 第 1 个 chunk 条目记 `status: "failed"` 加 `error`，第 2/3 个 chunk
  仍被尝试并如实记录，tick 返回码不变

#### Scenario: guard 级失败不拖垮 tick

- **GIVEN** stats guard 的连接或候选查询报错
- **WHEN** tick 收尾
- **THEN** tick 返回码不因此变化，summary 的 `stats_guard.status` 为 `"failed"`
  且含 `error` 字符串

#### Scenario: 非 owner 静默跳过被上报

- **GIVEN** `ANALYZE` 正常返回但回读的 `pg_stat_user_tables.last_analyze` 未刷新
- **WHEN** summary 生成
- **THEN** 该 chunk 条目记 `status: "warning"`，tick 返回码不变


# frontier-chunk-statistics-freshness Specification

## Purpose
TBD - created by archiving change frontier-chunk-stats-analyze-guard. Update Purpose after archive.
## Requirements
### Requirement: Ingest tick MUST refresh planner statistics on frontier chunks it touched

`scripts/node27_autopipeline.py` 的每个 tick MUST 在 publish 阶段之后、summary 之前，当本 tick 有至少一个 run 完成 ingest 时，对 `hydro.river_timeseries` 与 `met.forcing_station_timeseries` 的未压缩 chunk 中满足 `n_mod_since_analyze >= 10_000` 的每个 chunk 执行 chunk 级 `ANALYZE`（下限的作用仅是跳过本 tick 未触及、只有零星迟到写入的 chunk；任何被本 tick ingest 触及的 chunk 必然过槛）。每 tick 至多执行 3 个 chunk（按 `n_mod_since_analyze` 降序），被裁掉者 MUST 记入 summary 的 `stats_guard.deferred`。每条 `ANALYZE` MUST 在 `statement_timeout = 120s` 内执行，执行后 MUST 回读 `pg_stat_user_tables.last_analyze` 并连同耗时写入 summary；若 `last_analyze` 未刷新（PG15 非 owner 的静默跳过），该 chunk 条目 MUST 记 `status: "warning"`。单个 chunk 的 `ANALYZE` 失败 MUST NOT 阻止对剩余已选 chunk 的尝试（逐 chunk 隔离——否则被锁/消失的 chunk 会吞掉整批并每 tick 复发），该 chunk 条目 MUST 以 `status: "failed"` 加 `error` 记录；guard 级失败（连接/候选查询）MUST 以 `stats_guard.status = "failed"` 加 `error` 字符串如实记录。任何失败 MUST NOT 使 tick 返回码变为失败。`NODE27_AUTOPIPE_STATS_GUARD=off` 时 MUST 跳过并在 summary 标注 skipped。`ingested_runs < 1` 的门控仅作用于前沿 chunk 腿；同一 guard 的权威表统计清零修复腿（见 `authority-table-planner-hygiene`）MUST 在每个 tick 独立运行，其结果写入 `stats_guard.authority`，两腿共享开关与失败隔离语义。

#### Scenario: 新 run ingest 后被触及的 chunk 被 ANALYZE

- **GIVEN** 本 tick ingest 了 ≥1 个 run，且某未压缩 chunk 的
  `n_mod_since_analyze >= 10_000`
- **WHEN** tick 进入 stats guard 阶段
- **THEN** 该 chunk 被执行 `ANALYZE`，summary 的 `stats_guard.analyzed` 含其名字、
  耗时与回读的 `last_analyze`

#### Scenario: 无 ingest 时前沿腿不做无谓工作、修复腿照常运行

- **GIVEN** 本 tick 没有 run 被 ingest，或所有 chunk 的
  `n_mod_since_analyze < 10_000`
- **WHEN** tick 走到 stats guard 阶段
- **THEN** 不对任何 chunk 执行 `ANALYZE`，summary 如实记录空清单或 not_triggered；
  `stats_guard.authority` 仍含本 tick 修复腿的真实结果

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

### Requirement: Autopipeline connections MUST carry bounded connect and statement timeouts

Every database connection opened by `scripts/node27_autopipeline.py` SHALL be opened through the single `_connect` helper, which SHALL apply a connect timeout of 10 seconds (omitted when the DSN query string carries `connect_timeout`, in which case the DSN value wins) and a statement timeout of 600 000 ms (Python-caller override only; the DSN has no path for it); the frontier statistics guard SHALL keep its own `STATS_GUARD_TIMEOUT_MS` budget.

#### Scenario: Default connection

- **WHEN** a tick opens a connection without explicit timeout arguments
- **THEN** the driver receives `connect_timeout=10` and a statement timeout of 600 000 ms effective before the first business statement

#### Scenario: Operator DSN keeps precedence

- **WHEN** the DSN carries `?connect_timeout=3`
- **THEN** `_connect` passes no `connect_timeout` keyword and the backend uses 3 seconds

#### Scenario: A runaway statement cancels instead of wedging

- **WHEN** a statement on a non-guard `_connect` connection exceeds the budget
- **THEN** the driver raises `QueryCanceled` instead of the tick wedging under its flock, and the outcome follows the call path (nine non-guard `_connect` callers — the two stats-guard legs belong to the two scenarios that follow — grouped into four shapes by the path that reaches them, not by caller identity: `_backfill_output_geometry` and `_activate_model` appear in shapes two and three): on the per-run ingest path (`_process_run` → handoff apply, recompute-decline record) the affected run is marked `failed`, the tick emits its JSON summary and exits non-zero; on the display-ready seed path (`_ensure_seeded_basin_display_ready` → `_activate_model`, `_model_river_network_version_id`, `_backfill_output_geometry`) the basin is recorded `seed_failed`, the tick continues, emits its JSON summary and exits non-zero; on the pre-loop and publish sites (`_basin_seeded`, `_seed_basin`'s direct calls, `_already_ingested_runs`, `_publish_display_runs`) the exception propagates out of `main()` — non-zero exit with a traceback and no JSON summary; and on the informational decline count (`_active_decline_count`, which catches `psycopg2.Error` by design) the summary carries `declines_active: null` and the tick rc is unchanged. In every shape the next scheduled tick runs normally

#### Scenario: Guard-leg cancellation keeps the #1643 semantics

- **WHEN** a per-relation `ANALYZE` on a stats-guard connection exceeds `STATS_GUARD_TIMEOUT_MS`
- **THEN** that relation's entry records `status = failed`, the guard summary stays `completed`, and the tick's exit code is unchanged
- **AND** when the guard's connect or candidate query is the statement cancelled, the guard summary records `status = failed` and the tick's exit code is still unchanged

#### Scenario: Stats-guard connection keeps its budget

- **WHEN** the statistics guard opens its connection
- **THEN** its statement timeout equals `STATS_GUARD_TIMEOUT_MS` and its observation semantics are unchanged

### Requirement: The stats-guard disable flag MUST accept the conventional falsy set

`NODE27_AUTOPIPE_STATS_GUARD` SHALL disable the guard when its value, stripped and lower-cased, is one of `0`, `false`, `no`, `off`; any other value keeps the guard enabled.

#### Scenario: Falsy values disable

- **WHEN** the variable is ` FALSE `, `0`, `no` or `Off`
- **THEN** the guard does not run and the receipt records it as disabled

#### Scenario: Other values enable

- **WHEN** the variable is `1`, `on`, or unset
- **THEN** the guard runs


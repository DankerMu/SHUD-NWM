# authority-table-planner-hygiene

## ADDED Requirements

### Requirement: Authority tables MUST regain planner statistics after a statistics wipe without operator action

node-27 ingest tick（`scripts/node27_autopipeline.py` phase 3.5 stats guard）MUST 在每个 tick——不论本 tick 是否 ingest 了 run——查询 `core`/`met`/`hydro` schema 下非 hypertable 的普通表中 `relpages > 0 AND last_analyze IS NULL AND last_autoanalyze IS NULL` 者，并对其执行表级 `ANALYZE`（每 tick 至多 3 张、每条 `statement_timeout = 120s`、执行后回读 `last_analyze` 自检、单表失败隔离、guard 级失败如实记录、不改 tick 返回码），结果写入 summary `stats_guard.authority`。候选集 MUST 排除 hypertable 及 `_timescaledb_internal` 下的 chunk。`NODE27_AUTOPIPE_STATS_GUARD=off` 时 MUST 一并跳过。四张 core 身份表（`river_segment`、`river_segment_crosswalk`、`river_network_version`、`basin_version`）MUST 携带 per-table autovacuum analyze 参数，使单次新增 network 或单行版本变更即可触发 autoanalyze。

#### Scenario: 崩溃恢复清零统计后下一 tick 自动修复

- **GIVEN** 容器崩溃恢复后 `core.river_segment_crosswalk` 的累计统计被清零（`last_analyze`/`last_autoanalyze` 双 NULL，`relpages > 0`），且本 tick 没有 run 被 ingest
- **WHEN** tick 进入 stats guard 阶段
- **THEN** 该表被 `ANALYZE`，summary `stats_guard.authority.analyzed` 含其名字、耗时与非空 `last_analyze`

#### Scenario: hypertable 与压缩 chunk 绝不被修复腿触碰

- **GIVEN** `hydro.river_timeseries` 是 hypertable 且其某压缩 chunk 的统计为双 NULL
- **WHEN** 修复腿查询候选
- **THEN** 候选集不含该 hypertable 根表与任何 chunk

#### Scenario: 新增 network 触发 autoanalyze

- **GIVEN** `core.river_segment` 携带 `autovacuum_analyze_scale_factor=0.01, autovacuum_analyze_threshold=500`
- **WHEN** 一个 5,000 段的新 network 被 seed
- **THEN** 修改量超过 500 + 209k×1% 阈值，autoanalyze 在下一 naptime 内刷新统计

### Requirement: Identifier columns MUST NOT carry a bare-column trigram GIN that equality lookups can select

`core.river_segment.river_segment_id` 的 trigram 索引 `river_segment_id_trgm_idx` MUST 建在表达式 `lower(river_segment_id)` 上，使 `river_segment_id = $1` 形态的等值查找在结构上不可选该索引（与统计新鲜度、成本估计无关）；search 消费者 MUST 以同一表达式 `lower(rs.river_segment_id) LIKE <小写 pattern> ESCAPE '\'` 使用它，命中集合与原 `ILIKE` 相同。迁移 MUST 幂等可重跑，且重建窗口内 search 不失去索引（旧索引先改名、新索引建成后再并发删除）。

#### Scenario: 等值查找不再选中 trigram 索引

- **GIVEN** 共享 34 字符前缀的 id 家族与新鲜或缺席的统计
- **WHEN** 执行 `rs.river_segment_id = t.river_segment_id AND rs.river_network_version_id = t.river_network_version_id` 等值 join（无任何 session planner 旋钮）
- **THEN** `EXPLAIN` 不含 `river_segment_id_trgm_idx`，走 `river_segment_pkey`

#### Scenario: search 仍由 trigram 索引服务且命中集合不变

- **GIVEN** `?search=riv_0012` 与同一 basin 作用域
- **WHEN** 列表查询执行
- **THEN** 计划含 `river_segment_id_trgm_idx`（`lower(river_segment_id) ~~ '%riv_0012%'`），返回的段集合与迁移前逐条相同

#### Scenario: 迁移重跑幂等

- **GIVEN** 000052 已施加一次
- **WHEN** 再次施加
- **THEN** exit 0，`river_segment_id_trgm_idx` 仍为表达式定义，`river_segment_id_trgm_idx_legacy` 不存在，reloptions 不变

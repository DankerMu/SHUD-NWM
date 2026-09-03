## Context

node-27（PG 15.2 / TimescaleDB 2.10.2）2026-09-03 只读实测：

| 项 | 值 |
|---|---|
| `/home` 1.7 TB | 已用 1.3 TB，剩 289 GB；pgdata 836 GB |
| `hydro.river_timeseries` | 779 GB = 堆 363 + 索引 405 |
| 未压缩 chunk | `_hyper_3_91`（08-27..09-03）508 GB / 9.03 亿行；`_hyper_3_107`（09-03..09-10）239 GB / 4.21 亿行 |
| text 主键索引 | 241 GB + 113 GB = 354 GB（工作集 47%） |
| 已压缩 3 chunk | 671 GB → 31 GB（21.6×） |
| 行宽 | text 身份列 run_id 56 + segment 40 + network 34 + basin 27 + variable/unit/flag 20 ≈ 177 B；键/枚举 7 列 28 B；键列 `null_frac = 0` |
| `river_timeseries_valid_time_idx` | 9 GB，两周内两个活 chunk 的 `pg_stat_user_indexes.idx_scan` 合计 2（父表无计数；取自 chunk 级索引） |
| 写入 | 38 流域 × 2 源 × 2 周期 = 152 run/天；每 run = 段数 × 168 行；analysis run 写 `lead_time_hours = NULL` |
| 车道 | live lag 2 天（172800 s；模板 `infra/env/node27-timeseries-compression.example` 的赋值仍是 604800）；retention 14 天；watermark 落后约 1 天 |
| `met.forcing_station_timeseries` | 55 GB（索引 38 GB），同型 text 身份列 + text segmentby，无代理键；`met.forcing_version` 只有 `source_id`，`basin_version_id` 在 `met.met_station` 上 |

约束（探针 `openspec/changes/archive/2026-08-15-river-identity-normalization-backfill/probe-1339-throwaway.md` 与 `db/migrations/000050_river_identity_normalization.sql:318-360` 实测）：
- 只要 `timescaledb.compress = true` **这个设置**生效——与是否存在压缩 chunk 无关——ADD CONSTRAINT（含 FOREIGN KEY）/ VALIDATE / SET NOT NULL / CREATE UNIQUE INDEX / DROP CONSTRAINT 一律被拒；`SET (timescaledb.compress = false)` 又要求零压缩 chunk。
- hypertable 上 `CREATE INDEX CONCURRENTLY` 被拒；`ADD CONSTRAINT ... PRIMARY KEY USING INDEX` 被拒；主键构建在窗口内持 ACCESS EXCLUSIVE。
- 压缩设置一旦有压缩 chunk 就不能再 ALTER（`000047` 的守卫因此存在）；segmentby ∪ orderby 必须覆盖主键与外键列。
- 写守卫按并集窗口 fail-closed（`packages/common/timescale_write_guard.py`）；parser 的 replace chain 是"同 run + 同 network + 同 variable + 闭区间 `valid_time` 窗"的 DELETE + INSERT，窗界两端字面量在同一语句内（`tests/test_timescale_write_guard_wire_site_invariant.py` 结构化强制）。
- 仓内 26 行 `remove with #1342` 标记（23 行逐字 + mvt 3 行非逐字，展开 1:N 后 31 条被标记 aid（含 parser 写路径 2 条），另有 display_coverage 3 条无标记 aid）的实际布局：标记恒在**自己一行**、aid 谓词在**下一行**（`packages/common/forecast_store.py:101-104`）；`services/tiles/mvt.py:661/793/840` 三处标记措辞非逐字且一个标记管 3 条 aid；`forecast_store.py:1903-1906` 的 aid 与键谓词同处一个括号析取式；五处标记压在 `WHERE` 关键字行上：`apps/api/routes/hydro_display.py:773`、`forecast_store.py:1897`、`services/tiles/mvt.py:511/1496/1524`；`packages/common/display_coverage.py:383-418` 的 aid 只有一段散文注释（`:383-391`）而无逐字标记，`:406` 的 `rt.variable` aid 直接压在 `WHERE` 行，`run_id` 与 `river_network_version_id` 两条 aid 分别嵌在 `:410-411`、`:417-418` 的 `OR (` 析取式里。

因此 in-place cutover（000050 的函数）线上不可执行：decompress 671 GB 装不下，13 亿行主键重建让 display 停读数小时。

已拍板的压测分支见文末 **Grill ledger**。

## Goals / Non-Goals

**Goals:**
- 未压缩工作集与流域数的线性斜率降到现在的约 1/4（行宽 −65%、索引 −80%、窗口从最坏 21 天收到约 lag + horizon ≈ 9–10 天）。
- 零解压、零长锁、expand 后随时可按记录的反向序列回退地完成 #1342，关闭 #1336 epic。
- 治理告警能回答"下一次压缩峰值装不装得下"，整库体量不再制造常驻 critical。
- `met.forcing_station_timeseries` 用同一机制完成同型收口。

**Non-Goals:**
- 不升级 PostgreSQL / TimescaleDB（DML-on-compressed 属于后续独立决策）。
- 不改 lag（2 天）与 retention（14 天）的数值。
- 不承接 #1970（浏览器点击 P95 oracle）与 #1895（冷 tablespace 只搬压缩 chunk；过渡期冷层目录不覆盖 `_legacy` 表，runbook 记一句）。
- 不改 display API 对外响应字段；不改 SHUD 输出格式；不改 node-22 任何东西。

## Decisions

### D1 迁移机制：新表 expand–contract，而不是 in-place cutover
新建窄表并改名旧表，写方按部署时点切换，旧表只读到被 retention 清空后 DROP。备选 in-place（000050 函数）需要零压缩 chunk + 窗口内主键重建，线上不可行；备选"临时把 retention 收到 lag 让压缩 chunk 归零再 cutover"仍要在 13 亿行上重建主键且损失 2–14 天历史。新表在**空表、尚未开启压缩设置**的状态下完成全部约束 DDL（主键与外键内联在 `CREATE TABLE`，二级索引在 `create_hypertable` 之后），压缩设置是最后一条 schema DDL——因为阻塞 ADD CONSTRAINT 的是 `compress = true` 这个设置而不是压缩 chunk 的存在。回退见 D12。

### D2 命名：旧表改名 `_legacy`，新表用正名
`hydro.river_timeseries` → `hydro.river_timeseries_legacy`，新表叫 `hydro.river_timeseries`。改名是元数据操作（毫秒级，短暂 ACCESS EXCLUSIVE），chunk 保持归属、owner（`nhms_ingest_rw`）保持，`db/roles/node27_write_roles.sql` 的 owner 审计按"每张 compression-capable hypertable"枚举，自动覆盖 `_legacy`。代码最终不留 `_v2` 痕迹。备选 UNION ALL 视图被否。

### D3 路由：`hydro_run.timeseries_store`，默认 `narrow`，expand 时按 parse 事实回填 `legacy`
`hydro.hydro_run.timeseries_store TEXT NOT NULL DEFAULT 'narrow' CHECK (timeseries_store IN ('legacy','narrow'))`。expand 迁移 `UPDATE hydro.hydro_run SET timeseries_store = 'legacy' WHERE parsed_at IS NOT NULL OR status IN ('parsed','published')`——只有已经把行写进旧表的 run 才是 legacy；该谓词之外仍残留在旧表里的行（此前中途失败的 parse）是可接受的过渡态——所有按 store 绑定的读分支都够不到它们，retention 会清掉，迁移不做逐行探测（那正是本 change 要避免的 7 天 chunk 扫描）；expand 后新建的 run 与 expand 前已注册但尚未 parse 的 run（在飞 run）默认 `narrow`，首次 parse 直接写窄表。parser 在 replace chain 前读该列：`legacy` → 拒绝（D6）；`narrow` → 写窄表并在同一事务内幂等地保持 `narrow`。备选"run_key 水位"被否（回退窗口内会交叉）。forcing 同型：`met.forcing_version.timeseries_store`，expand 时按"legacy 表中存在该 forcing_version 的行"回填。contract 批次删除该列。

### D4 窄表形状（用户拍板：精简三索引）
列与可空性：`run_key INTEGER NOT NULL`、`basin_version_key INTEGER NOT NULL`、`river_network_version_key INTEGER NOT NULL`、`river_segment_key INTEGER NOT NULL`、`valid_time TIMESTAMPTZ NOT NULL`、`lead_time_hours INTEGER NULL`（analysis run 写 NULL，`openspec/specs/analysis-run-pipeline` 依赖）、`variable_e hydro.river_variable NOT NULL`、`value DOUBLE PRECISION NOT NULL`、`unit_e hydro.river_unit NOT NULL`、`quality_flag_e hydro.river_quality_flag NOT NULL`、`created_at TIMESTAMPTZ NOT NULL DEFAULT now()`（三个枚举类型即 000050 已建类型）。
主键 `(run_key, river_segment_key, variable_e, valid_time)`：`river_segment_key` 在 `core.river_segment` 上是 IDENTITY UNIQUE，全局唯一，主键不需要 network key。
索引：`river_ts_segment_time_key_idx (river_segment_key, variable_e, valid_time DESC)`（逐河段曲线；#1342 硬门要求 `river_segment_key` 进 Index Cond）；`river_ts_run_discovery_key_idx (run_key, basin_version_key, river_network_version_key, variable_e, valid_time DESC)`（MVT/latest 发现与 identity-existence 探针，替代 000051 与 mvt_selected_identity text 索引）。不建 `valid_time` 单列索引：`create_hypertable(..., create_default_indexes => false)`。这两处"索引消失"（`river_timeseries_valid_time_idx`、forcing 的 `qhh_latest_window_idx`）按 `timeseries-index-hygiene` 的证据门处理：rollout receipt 必须含 identity-existence 探针 miss 分支与 QHH latest 回落 CTE 的 before/after `EXPLAIN (ANALYZE, BUFFERS)`，并逐条列出覆盖损失。
压缩：`segmentby (run_key, river_segment_key)`、`orderby (variable_e, valid_time)`，覆盖主键与两个外键列。
外键：`run_key → hydro.hydro_run(run_key)`、`river_segment_key → core.river_segment(river_segment_key)`，内联在 `CREATE TABLE`；`basin_version_key`/`river_network_version_key` 不在 segmentby 内不建 FK（ADR 0002 2026-08-15 修正案的同一规则），由 parser 从 `hydro_run` 与 `core.river_segment` 推导写入，coverage 审计以 join 等价校验。
DDL 顺序（迁移 header 记账项）：`CREATE TABLE`（PK + 两 FK 内联）→ `create_hypertable('hydro.river_timeseries','valid_time', chunk_time_interval => interval '1 day', create_default_indexes => false)` → 两个二级索引 → `ALTER TABLE ... SET (timescaledb.compress, compress_segmentby, compress_orderby)` → `ALTER TABLE ... OWNER TO nhms_ingest_rw`。

### D5 读路径：模板规范化 + 按标记块删除
现状不允许"删标记行"：aid 在标记的下一行、mvt 一标记管三 aid、`forecast_store.py:1903` 的 aid 嵌在括号析取式里。因此分两步：
1. **模板规范化（零行为变化，先合）**：每条 aid 改写成独立合取项一行；每条 aid 上方恰有一行逐字 `-- transitional compressed-chunk pushdown aid, remove with #1342`（mvt 三处非逐字标记归一，1:N 拆成 1:1）；标记只能压在 aid 谓词行的正上方，绝不能压在 `WHERE` 或任何其他关键字行上（`hydro_display.py:773`、`forecast_store.py:1897`、`mvt.py:511/1496/1524` 的布局必须先重排）；`display_coverage.py:383-418` 的散文注释换成逐条逐字标记，其 `:406` WHERE 行 aid 同样下移；`forecast_store.py:1903` 与 `display_coverage.py:410/417` 的 `OR (rt.run_id = %(scan_run_id)s AND rt.run_key = (...))` 改写为 `OR (` + 标记行 + `rt.run_id = %(scan_run_id)s AND` + `rt.run_key = (...))`，即 aid 行以尾随 `AND` 结尾、删除后括号内只剩键谓词。`tests/test_river_ts_text_identity_cleanup.py` 的 adjacency 不变式收紧为"一 aid 一逐字标记、标记在 aid 的上一行"；census 计数与逐块 pin 在同一 PR 重钉。
2. **渲染器** `render_river_ts_sql(template, store)`（`packages/common/`）：`legacy` → 表名替换为 `hydro.river_timeseries_legacy`，其余逐字；`narrow` → 表名为正名，删除每个标记行**及其紧邻的下一行**（该行必须是一条 aid 谓词，否则渲染器 fail-closed），随后断言输出可解析、不含任何 text 身份列、legacy 变体中的每个键/枚举谓词仍在。跨 store 查询用 `render_union_all(template, stores, params)` 组合子：两分支各绑定 `h.timeseries_store = '<store>'`，参数按分支复制，输出形状由 oracle 钉住。contract 时渲染器只接受 `narrow`，模板里的标记行与 aid 行物理删除。
非模板面（同批处理）：`services/tile_publisher/publisher.py` 的 `_has_table` 前置在过渡期接受两个名字；`services/tile_publisher/forcing_copyback_backfill.py:314` 的 `required_columns` 按 store 分支（legacy 保 `variable`，narrow 只留键/枚举）；（`scripts/node27_autopipeline.py:1443-1451` 统计守卫的 IN-list 属 D7/任务 3.1，不在本批非模板面内）；`scripts/reset_qhh_smoke_db.py`、`scripts/summarize_qhh_smoke_results.py` 按 store 渲染（reset 对 legacy run 同时清 legacy 表）；`services/production_closure/scale_validation.py` 与 `scripts/node27_timeseries_compression_live_evidence.py` 的计划形状钉子按 store 分支。

### D6 legacy 重解析：fail-closed，走既有 decline 账本、tick rc=0、永久
parser 抛 `LegacyStoreWriteRefused`（parser CLI 退出码独立命名，与 compressed-chunk-blocked、guard-internal 都不同）。autopipeline 把它记入 `ops.ingest_recompute_decline`（沿 `hypertable-compression` "blocked recompute MUST reach a recorded terminal state"，tick `rc = 0`），但 decline 原因为 `legacy_store_refused` 的记录是 **store 维度的永久终态**：不随 `product_mtime` 重开，只有 `timeseries_store` 变为 `narrow`（即回退或重铸身份）才会重新受理。确需重算的 run 重新铸身份为新 run 写窄表。

### D7 车道过渡期：发现式 hypertable 集合 + per-tick 推导按表分别建模
压缩 runner、retention runner、compression supervisor 的 `validate_current_d3`、capture 的 `HYPERTABLE_KEYS`、autopipeline 统计守卫的 IN-list 统一改为"正名 + 存在即纳入的 `_legacy`"（`timescaledb_information.hypertables` 存在性判定）；supervisor 的期望态按表分别断言（正名表键形态 / legacy 表 text 形态）。receipt schema：`schemas/timeseries_compression_receipt.schema.json` 的 `per_table_totals` 改为 `patternProperties`（正名两键必需，`_legacy` 键可选）；`schemas/timeseries_retention_receipt.schema.json` 新增 `legacy_chunks`（按表）。legacy 表 DROP 后集合自动收敛，不需要第二次代码改动。
per-tick 推导按表分别陈述：正名表 1 天 chunk，每表每天到达 1 个 terminal chunk（两表 2/天）；`_legacy` 表 7 天宽、切换后**零到达**，只剩有限存量（river ≤ 2 个 239–508 GB chunk，forcing ≤ 2 个）。wall 关系按最坏组合算：一个 legacy 7 天 chunk ≈ 6 s/GB × 508 GB ≈ 51 min 已逼近 60 min 单 chunk 超时与 65 min 整 tick wall，因此过渡配方是：**expand 前**先用手动 tick（bound 1）把 legacy 存量 chunk 压完（本 change 之外已对 chunk 91 执行的同一配方），expand 后 legacy 只剩 range_end 在未来的最后一个 chunk；稳态 bound 4 满足 2/天 < 4 与 4 × 7.5 min = 30 min < 65 min。timer 节奏结论重述：1 天 chunk 把到达率从每周 2 个改成每天 2 个，日频 timer 仍充分，无需改频率。模板 `PER_TICK_BOUND=4` 保持并重钉（注释改写推导），`LAG_SECONDS` 赋值改为 172800、`one chunk width` 注释删除。

### D8 治理：工作集 + 峰值预测，空态与 watermark 缺失有定义
采集（只读目录）：`uncompressed_bytes`、`daily_ingest_bytes`（最近 7 天按 chunk `range_start` 分日的未压缩体量均值）、`next_compressible_at`（最老未压缩 chunk 的 `range_end + lag`，lag 读自 compression env 的同一变量）、`home_free_bytes`、`projected_peak_bytes = uncompressed_bytes + daily_ingest_bytes × max(0, days(next_compressible_at − watermark))`（单位：天，可为小数）。critical：`projected_peak_bytes > home_free_bytes − safety_margin_bytes`（默认 100 GiB）；warning：`uncompressed_bytes > working_set_warn_bytes`（默认 400 GiB）。空态：无未压缩 chunk → `next_compressible_at = null`、`projected_peak_bytes = uncompressed_bytes`、`projection_status = "no_uncompressed_chunk"`，不报 critical；watermark 不可用 → 与压缩 runner 同一 fail-closed 语义：`projection_status = "watermark_unavailable"`、发 critical `WATERMARK_UNAVAILABLE`（这是车道自身故障，必须到人）。`DATABASE_SIZE_ABOVE_*` 降为 info。`timeseries-db-retention` 的"critical 即非零退出"契约不变；新 capability 只新增推荐码。

### D9 forcing 批次：同模式，串在 river contract 之后
`met.met_station.station_key`、`met.forcing_version.forcing_version_key`：`INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE`（先在 node-27 只读实测行数/索引体量并在 throwaway 库测 ADD COLUMN 锁时长）。枚举 `met.forcing_variable` / `met.forcing_unit` / `met.forcing_quality_flag`（沿 000050 的 `<domain>_<concept>` 命名），由生产写方 `MVP_STATION_VARIABLES` ∪ 现网 `pg_stats` ∪ seeds 三源并集生成，seed 词表（`t2m` 等）显式排除并记录。窄表 `met.forcing_station_timeseries`：`forcing_version_key, station_key, valid_time, variable_e, value, unit_e, quality_flag_e NOT NULL`，`native_resolution TEXT NULL`；主键 `(forcing_version_key, station_key, variable_e, valid_time)`；二级索引 `forcing_ts_version_variable_time_key_idx (forcing_version_key, variable_e, valid_time DESC)`（`qhh_latest_window_idx` 的键形后继，QHH 回落 CTE 用）；`segmentby (forcing_version_key, station_key)`，`orderby (variable_e, valid_time)`；1 天 chunk；FK 两个键列。`source_id` 由 `met.forcing_version` join 推导，`basin_version_id` 由 `met.met_station`（经 `station_key`）join 推导——语义变更：coverage 与 QHH 回落以站点权威 basin 为准，等价性测试须构造行内值与站点权威值不一致的用例。路由列 `met.forcing_version.timeseries_store`。写方两处走同一写守卫。读方经独立的 `render_forcing_ts_sql(template_pair, store)`：forcing legacy 表**没有键列**（IDENTITY 键只落在 authority 表，legacy 表形状不变），river 的"删标记块"机制对它不适用，legacy/narrow 是两份分别注册的模板而不是一份模板减几行；forcing 侧不引入任何 `#1342` 标记。读方九处（`display_coverage` station 腿；`forecast_store` 的 QHH 回落 CTE、station-series 行 helper、station-forcing membership 校验、forcing-readiness overall 与 per-variable 行；`best_available` 的 forcing-inputs 列表；`qhh_production_bootstrap` 的 forcing-state 计数 join；`reset_qhh_smoke_db` 的 forcing DELETE），外加 live-evidence 的 forcing 计划形状钉子按 store 分支；两处写侧写后校验查询（forcing-producer 的 `verify_forcing_version_children`、domain-handoff apply 的行数校验）随写方在 I12 一起改为按 `forcing_version_key` 读窄表；shape oracle 的 forcing census 把仓内每个 `met.forcing_station_timeseries` SQL 站点钉为"已注册"或"仅名字引用的豁免项"（payload 清单、行数键、目录钉子、生命周期工具），避免改名后漏站点直接报 `column forcing_version_id does not exist`。

### D10 迁移与 000047 守卫的关系
`000047` 的期望值按名字 `river_timeseries` 比对压缩设置；生产 ledger 已应用，不重跑；CI 干跑在空库按序执行 000047（旧表）→ expand（改名 + 新表），不冲突。expand 迁移在 000047 之后、以自己的守卫比对新表设置。contract 迁移不重放 000047。

### D11 硬门与 #1342 验收项的归属
本 change 的性能硬门在 SQL / 本机 API 层：逐河段曲线 SQL 在 narrow 未压缩与压缩 chunk 上 `river_segment_key` 进 Index Cond 或 segmentby 剪枝、`Rows Removed by Filter / returned ≤ 10`、`shared hit ≤ 5000`、SQL warm P95 ≤ 300 ms（≥ 5 个 warm 样本）、本机单源 `forecast-series` warm P95 ≤ 500 ms（SHJ-NJ 大河网 + 一个小河网）；identity-existence 探针 miss 分支与 QHH 回落 CTE 的 before/after EXPLAIN（D4 的索引证据门）。rollout receipt 另含 #1342 的两条验收项：注册表全量口径计数（active/runnable/selected/excluded）；`/` 实机点击覆盖 SHJ-NJ、一个中等河网、一个小河网（GFS/IFS 双曲线成功、身份不串档，agent-browser 截图证据）；以及 display 只读边界 deny-write receipt（`docs/runbooks/node-27-bringup-checklist.md` C1–C4）。浏览器点击 P95 < 2 s 的验收文字在关闭 #1342 前显式迁移到 #1970 的 body。

### D12 回退（expand 后、contract 前可执行的反向序列）
停 timers 与 API/parser → `ALTER TABLE hydro.river_timeseries RENAME TO hydro.river_timeseries_narrow_rollback` → `ALTER TABLE hydro.river_timeseries_legacy RENAME TO hydro.river_timeseries` → 回滚代码到 change 之前版本 → `UPDATE hydro.hydro_run SET timeseries_store = 'legacy'`（列保留，旧代码不读它）→ 启动。窄表中已写入的 run 在回退后对读路径不可见（旧代码只读正名表）；这些 run 按旧 parser 重解析进（现为正名的）旧表；`narrow_rollback` 表在再次尝试 expand 前 DROP。允许的中间态"store 回置 legacy 且窄表残留行存在"写进 spec。contract 之后不可回退。

## Sketch seams under test

1. **渲染 SQL 形状 oracle**（最高、已有）：`tests/test_sql_shape_helpers.py` 机制作用于 `render_river_ts_sql` / `render_union_all` 的输出——legacy 变体与规范化后的模板逐字等价（表名除外）、规范化后的模板与规范化前的 pin 语义等价（census/shape pin 重钉）、narrow 变体不含 text 身份列/标记行且键谓词完整。理由：一个 seam 覆盖 13 处读方 + 2 个 smoke 脚本。
2. **parser replace chain 单测**（已有假 cursor seam）：只写窄表列、`ON CONFLICT` 键主键、DELETE 保持 run + network + variable + 闭区间窗、legacy run 拒绝且不发 DELETE、`timeseries_store` 与 `mark_run_parsed` 同事务。
3. **真实 DB 集成（node-27 marker）**：expand 迁移幂等与改名、混合 store（一 legacy 一 narrow run）的 national-tile / coverage 查询两分支各碰自己的表、回退序列后 legacy run 可读可重解析、contract 迁移对非空 legacy 的拒绝、EXPLAIN 硬门。
4. **治理推荐单测**（已有符号阈值风格）：投影公式、fits / does-not-fit / no-uncompressed / watermark-unavailable 四态、DB-size 降级、receipt schema 校验。

## Risks / Trade-offs

- [expand 前 `/home` 先满] → 保命步骤已在本 change 外执行（chunk 91 守门压缩）；expand 迁移不占空间；expand 前按 D7 配方压完 legacy 存量。
- [渲染器删错行] → 规范化把布局钉成 1:1 且标记在上一行；渲染器对"下一行不是 aid"fail-closed；seam 1 双向断言。
- [跨 store UNION ALL 分支在 legacy 压缩 chunk 上退化] → legacy 分支保留 aid；硬门在 legacy 与 narrow 各测一次。
- [新表无 basin/network FK] → 与 ADR 0002 修正案同一规则；coverage 审计 join 等价校验。
- [1 天 chunk 让 chunk 数 ×7] → 14 天内两表合计 ≈ 40 个；retention/compression 按 `range_end` 工作；autopipeline 前沿 ANALYZE 腿每 tick ≤ 3 个 chunk 的预算在 receipt 中复核（1 天 chunk 下未压缩 chunk 数 ≈ 9–10）。
- [forcing IDENTITY 列 ADD 的锁] → 先实测，receipt 前置。
- [contract 迁移在 legacy 非空时被误跑] → 迁移内 chunk 计数 fail-closed；runbook 前置 `legacy_chunks = 0`；contract 窗口先部署去掉列引用的代码再 DROP 列。
- [维护窗口内旧进程读到空窄表] → 窗口顺序：timers stop → API/parser stop → pull → migrate → start → timers start。
- [治理峰值外推低估] → 7 天日均 + 100 GiB 余量；流域新增在 receipt 中即时反映。

## Migration Plan

按依赖倒序，先合零行为变化的读侧：
1. **I1 渲染器 + 模板规范化 + oracle**（零行为变化，独立合绿）。
2. **I2 forecast_store（9 条）**、**I3 mvt（6 条 + UNION ALL 组合子）**、**I4 hydro_display + display_coverage** 各自走渲染器，store 恒为 legacy 时行为不变。
3. **I5 非模板面**（publisher `_has_table`、copyback `required_columns`、两个 smoke 脚本、计划形状钉子；autopipeline 统计守卫 IN-list 归 I6）。
4. **I6 车道与治理**（发现式集合、supervisor/capture、receipt schema、per-tick 重钉、governance 指标）——legacy 缺席时为 no-op，可独立合绿；部署上先于 I7 落 node-27。
5. **I7 expand 迁移 + parser 窄写 + fixture 重钉**（一次合入；此时读方已按 store 路由，真实 DB pytest 可绿）。
6. **I8 rollout runbook + node-27 receipt**（D11 全部项 + D12 回退演练在 throwaway 库）。
7. **I9 river contract**：开门条件 = 14 天 receipt 归档 + `legacy_chunks = 0`；contract 迁移、删函数/backfill runner/aid、oracle 收敛、ADR/runbook/glossary、关闭 #1342/#1336。
8. **I10 forcing 只读实测 receipt** → **I11 forcing 读方** → **I12 forcing expand + 写方** → **I13 forcing rollout receipt** → **I14 forcing contract**（开门条件同 I9）。

## Open Questions

- forcing 两张 authority 表 IDENTITY 列的 ADD 锁时长（node-27 实测后填入 I12 issue）。
- `native_resolution` 的现网 distinct 值实测（spec 已钉 `TEXT NULL`；实测若显示低基数词表，作为后续 change 的输入而非本 change 的变更；原问句：是否改为枚举，默认保留 TEXT 可空）。

## Known limits

- contract 之后 legacy 数据不可恢复；切换时刻之前 14 天的历史在 contract 时随 retention 自然消失，与今日 retention 语义一致。
- 新表到压缩 chunk 的 DML 仍受 TimescaleDB 2.10 限制；lag 内重解析仍是唯一写窗口。
- 过渡期冷层目录（#1895）不覆盖 `_legacy` 表。

## Grill ledger（2026-09-03，全部由用户拍板）

| 分支 | 结论 |
|---|---|
| 迁移机制 | 新表 expand–contract（D1） |
| 治理量法 | 工作集 + 峰值预测（D8） |
| chunk 粒度 | 1 天（D4） |
| forcing 表 | 纳入，作为同一 change 的后置批次、串在 river contract 之后（D9） |
| 命名/读路径 | 旧表改名 legacy、新表正名、按 run 路由（D2/D3/D5） |
| legacy 重解析 | fail-closed 拒绝（D6） |
| 硬门归属 | 本 change 只持 SQL/API 门，浏览器 P95 留 #1970（D11） |
| 键/索引/segmentby | 精简三索引（D4） |
| 事实假设 ①–④ | lag 2 天 / retention 14 天不变；路由用 hydro_run 新列（Stage 3 修正：默认 `narrow`、expand 按 parse 事实回填 `legacy`）；legacy 变体 = 换表名、narrow 变体 = 剔除标记块（Stage 3 修正：先规范化模板，删"标记行 + 紧邻 aid 行"）；contract 删 000050 函数、backfill runner、全部 aid 并收口 #1336 |

开放项：forcing authority 表 IDENTITY 锁成本（实测后填）；`native_resolution` 现网 distinct 值（spec 已钉 `TEXT NULL`，实测只作后续输入）。

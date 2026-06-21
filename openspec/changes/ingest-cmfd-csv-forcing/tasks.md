## 0. Pre-implementation Introspection (BLOCKS all coding)

> 这一节必须先做完且把结论写回 design.md "Open Questions" + PR-A0 body，**任何一条未 resolve 不得开 PR-A1**。每条 OQ 都有真实 fail 模式，盲跑会浪费大量返工。

- [ ] 0.1 **OQ1: heihe / qhh 既有 station forcing_filename 实际是否与 forcing dir CSV 文件名匹配？**
  - `ssh -p 32099 nwm@210.77.77.27 'psql -h 127.0.0.1 -p 55432 -U nwm_owner -d nhms -c "SELECT station_id, properties_json->>\'forcing_filename\' FROM met.met_station WHERE basin_version_id IN (SELECT bv.basin_version_id FROM core.basin_version bv JOIN core.model_instance mi ON mi.basin_version_id=bv.basin_version_id WHERE mi.model_id IN (\'basins_heihe_shud\', \'basins_qhh_shud\')) LIMIT 10;" 2>&1; ls /home/ghdc/nwm/Basins/heihe/forcing/ | grep -E "^X" | head -5; ls /home/ghdc/nwm/Basins/qhh/forcing/ | grep -E "^X" | head -5'`
  - 比对：properties_json.forcing_filename 是否 == 文件名（含 .csv 后缀）？
  - 若 mismatch：escalate to user，可能需要重新设计 station_seeder 反查逻辑 + 重写 spec scenario "Seeder is idempotent for heihe / qhh existing stations"
  - 结论必须 commit 到 design.md OQ1 + PR-A0 PR body
- [ ] 0.2 **OQ2/R-1: node-27 PG data dir 当前剩余磁盘 + total**
  - `ssh -p 32099 nwm@210.77.77.27 'df -B1 $(psql -h 127.0.0.1 -p 55432 -U nwm_owner -d nhms -t -c "SELECT current_setting(\'data_directory\');" 2>/dev/null | xargs) 2>&1 | tail -2; psql -h 127.0.0.1 -p 55432 -U nwm_owner -d nhms -c "SELECT pg_size_pretty(hypertable_size(\'met.forcing_station_timeseries\'));"'`
  - 若 free < 200GB：escalate（评估 compression / 排除最大 basin / 按 phase 分批）
  - 记录 baseline 到 PR-A0 body
- [ ] 0.3 **OQ3: 单 basin（大体量如 heihe / weiganhe / kashigeer）的预估 ingest 耗时与单事务安全窗口**
  - 检查 node-27 PG `statement_timeout` 配置：`psql ... -c "SHOW statement_timeout;"`
  - 若 statement_timeout != 0（即有限制）：估算最大 basin 的 INSERT 时间（按 ~50k rows/sec batch insert，heihe 18.5 亿行 → 数十小时，明显爆超时）
  - 决策：(a) bump statement_timeout to 0 in ingest session (`SET statement_timeout = 0` per-transaction)，OR (b) 按 variable 分多事务（违反 atomicity scenario，需重设计），OR (c) split by time-window（仍单 forcing_version 但 INSERT 分批 commit，破坏 spec atomicity）
  - 默认选 (a) per-session SET，记录到 design.md AD-5
- [ ] 0.4 **OQ4: `replace_forcing_timeseries` 函数对 CMFD 调用是否 100% 适用？**
  - 读 `workers/forcing_producer/store.py:728-769` 完整实现
  - 确认只做 DELETE + INSERT（无 canonical_product FK 检查 / 无 lineage 注入）
  - 决策：reuse 既有方法 OR 写 CMFD-local helper
  - 结论 commit 到 design.md（新增 AD-11）
- [ ] 0.5 **OQ5: CMFD CSV 时间步是否真的 100% 是 3 小时（跨 basin 一致）？**
  - 对 8 个 eligible basin 各取 1 个 CSV，跑 `head -1` 提取 (row_count, start_date, end_date)，计算 `(end-start)*86400 / (row_count-1)` 是否 == 10800
  - 若任何 basin 不是 10800：abort ingest 该 basin，spec 已要求 parser raise `CMFDCSVFormatError`
  - 结论 commit 到 design.md OQ5
- [ ] 0.6 **测试 layout 决策**：本仓库现有 `tests/` 是 flat 布局（如 `tests/test_forcing_producer.py`），不嵌套 `tests/workers/`。本 change 测试文件按既有约定：
  - `tests/test_cmfd_csv_ingest_parser.py`
  - `tests/test_cmfd_csv_ingest_station_seeder.py`
  - `tests/test_cmfd_csv_ingest_timeseries_ingester_real_db.py`
  - `tests/test_cmfd_csv_ingest_orchestrator_smoke.py`
  - 不创建 `tests/workers/` 子目录
- [ ] 0.7 **Migration 序号 reservation**：当前最大 `000039_crosswalk_external_identity.sql`。本 change 占 `000040_seed_cmfd_data_source.sql`。PR-A0 打开后即在 PR body 显著标 "reserves migration 000040"，并与并行 PR 协调（若发现冲突 bump 至下一可用）

## 1. DB Migration: Seed CMFD Data Source [PR-A0]

- [ ] 1.1 创建 `db/migrations/000040_seed_cmfd_data_source.sql`（按 §0.7 确认编号）
- [ ] 1.2 写 idempotent INSERT 语句（`ON CONFLICT (source_id) DO NOTHING`），含 `source_id='cmfd'`, `source_name='CMFD 0.1° static historical forcing'`, `source_type='archive_static'`, `status='enabled'`, `native_format='csv'`, `adapter_name='cmfd_csv_adapter'`, `config_json='{"grid_resolution_deg": 0.1, "time_step_seconds": 10800, "variables": ["PRCP","TEMP","RH","wind","Rn"], "source_citation": "Yang et al. 2010"}'::jsonb`
- [ ] 1.3 本地 SQL syntax check (`pg_format` 或 manual review)；ruff 不覆盖 SQL
- [ ] 1.4 node-27 apply migration + verify `SELECT * FROM met.data_source WHERE source_id='cmfd'` 返回 1 行，config_json keys 完整匹配 4 个 required key；记录 migration receipt 到 PR-A0
- [ ] 1.5 写一个 smoke test：`curl -s "<api>/met/stations/<any station>/series?source_id=cmfd&cycle_time=2000-01-01T00:00:00Z&model_id=basins_keliya_shud"` 应返 **404 FORCING_VERSION_NOT_FOUND**（不是 422 或 500），证明 `cmfd` 是 valid free-form source_id 且 FK 已生效，但 forcing_version 仍空

## 2. CMFD CSV Parser (`workers/cmfd_csv_ingest/parser.py`) [PR-A1]

- [ ] 2.1 定义 `CMFDCSVFormatError(Exception)` typed error 类
- [ ] 2.2 实现 `parse_cmfd_header(file_path: Path) -> CMFDHeader` —— 解析 2 行 header，返回 `(row_count, n_vars, start_date, end_date, epoch_factor_sec, variable_columns)` dataclass
- [ ] 2.3 在 header parse 中守 `row_count > 1`，否则 raise `CMFDCSVFormatError("insufficient data rows: row_count=<n>")`；然后计算 `step_seconds = round((end_date - start_date).total_seconds() / (row_count - 1))`；若 `step_seconds != 10800` raise `CMFDCSVFormatError`
- [ ] 2.4 实现 `parse_cmfd_data_rows(file_path: Path, header: CMFDHeader) -> Iterator[CMFDDataRow]` —— streaming generator，按行 yield，每行 `CMFDDataRow(valid_time, entries=[(variable_canonical_name, unit, value)])`，只 emit 真实数值（NaN/Inf/空 cell skip 该 entry）
- [ ] 2.5 实现 `_compute_valid_time(time_interval: float, start_date: date, epoch_factor_sec: int) -> datetime` —— 显式 UTC tz；公式 `datetime(start_date, tzinfo=UTC) + timedelta(seconds=time_interval * epoch_factor_sec)`
- [ ] 2.6 实现 variable name + unit mapping `CMFD_VAR_MAP: dict[str, tuple[str, str]]`，单位严格按 spec scenario "Parser maps CMFD variable names": PRCP→mm/day, TEMP→degC, RH→`0-1`, wind→m/s, Rn→W/m2（与 `openspec/specs/canonical-conversion/spec.md:46,61,66,68` 对齐 —— 若 implementer 在 §0 introspect 发现 GFS forcing_station_timeseries 用其他 string，必须 align + 同步更新 spec scenario）
- [ ] 2.7 NaN/Inf/empty cell 处理（per design.md AD-9 + spec scenario "Parser skips missing values"）：parser 维护 `skipped_missing_count` 计数器（每 station per call 累加），返回给 ingester；**不写 NULL，不写 sentinel，不 emit entry**
- [ ] 2.8 unit test `tests/test_cmfd_csv_ingest_parser.py`（flat 布局，无 `tests/workers/`）：
  - 正常 CSV（fixture 仿造 5 行 2 站点小数据）→ 正确 row count + valid_time + variable mapping
  - malformed header（少字段、非数字 row_count、错误 variable column line、header line 2 列顺序与期望不一致）→ raise CMFDCSVFormatError
  - `row_count <= 1` (header 写 0 / 1)→ raise CMFDCSVFormatError
  - 数据 section 为空（header 说有 N 行但实际 0 行）→ raise CMFDCSVFormatError
  - NaN/Inf/空 cell → 对应 entry 不 emit，skipped_missing_count 计数累加
  - 非 10800 step_seconds（fixture 模拟 7200 sec 步长）→ raise CMFDCSVFormatError
  - valid_time 边界：第一行 valid_time = start_date 00:00:00 UTC，末行 = end_date - 3h（最后步）
  - 没有任何 emit 的 entry `variable='Press'`（assert by filtering yielded rows）

## 3. Station Seeder (`workers/cmfd_csv_ingest/station_seeder.py`) [PR-A2]

- [ ] 3.1 定义 `CMFDStationSeederError(Exception)` typed error 类
- [ ] 3.2 实现 `_extract_lon_lat_from_filename(filename: str) -> tuple[float, float] | None` —— 正则 `r'^X(?P<lon>-?\d+(?:\.\d+)?)Y(?P<lat>-?\d+(?:\.\d+)?)\.csv$'`；不匹配返 None（skip）
- [ ] 3.3 实现 `_resolve_basin_version_id(conn, model_id: str) -> str` —— 查 `core.model_instance` JOIN `core.basin_version`；缺失 raise `CMFDStationSeederError('no_active_model')`
- [ ] 3.4 实现 `seed_basin_stations(conn, basins_root: Path, basin_slug: str, model_id: str, dry_run: bool = False) -> StationSeedReceipt` —— 返回 `(existing_stations, new_stations_inserted, updated_with_cmfd_marker, skipped_files)`
- [ ] 3.5 实现"existing station 反查"：`SELECT station_id, properties_json FROM met.met_station WHERE basin_version_id=%s AND properties_json->>'forcing_filename'=%s` 来 join existing 与 forcing dir 文件（前置 §0.1 必须 verify forcing_filename 实际匹配）
- [ ] 3.6 对 existing station（命中反查）：仅 `UPDATE met.met_station SET properties_json = jsonb_set(jsonb_set(properties_json, '{source_id}', '"cmfd"'), '{cmfd_seeded_at}', to_jsonb(NOW())) WHERE station_id=%s`，**不动其他 properties_json keys，不动 station_role / geom / elevation_m / basin_version_id / active_flag 列**
- [ ] 3.7 对 new station（无反查命中）：构造 `station_id='<basin>_cmfd_<filename without .csv>'`，INSERT 含 `geom = ST_SetSRID(ST_MakePoint(lon, lat), 4490)`, `station_role='forcing_grid'`, `active_flag=true`, `properties_json = {"forcing_filename": <fn>, "seed": "cmfd_station_seeder", "grid_resolution_deg": 0.1, "source_id": "cmfd"}`（**4 个 key，NO `forcing_mapping_mode` —— 见 design AD-6**）
- [ ] 3.8 `dry_run=True` mode：在事务内执行 INSERT/UPDATE 但 ROLLBACK，仅返回 receipt（不修改 DB），便于 verify
- [ ] 3.9 unit test `tests/test_cmfd_csv_ingest_station_seeder.py`（flat layout）：
  - 仿造 5 个 CSV 文件名 → 5 个新 station INSERT；properties_json 恰好 4 个 key、无 `forcing_mapping_mode`
  - 非 X<lon>Y<lat>.csv 文件（如 Prcp_Correction.csv、README.md）→ skipped_files 记录，不 INSERT
  - 模拟 existing station（mock cursor 返预存数据）→ 只 UPDATE properties_json 的 source_id + cmfd_seeded_at 两个 key，其他不动
  - 模拟 model_id 不存在 → raise CMFDStationSeederError
- [ ] 3.10 real-DB test `tests/test_cmfd_csv_ingest_station_seeder_real_db.py`（real-db-integration marker）：
  - node-27 跑 keliya basin dry_run，验证 32 个新 station 计数正确 + properties_json 字段完整 + 无 forcing_mapping_mode 键
  - node-27 跑 heihe basin dry_run，验证 existing_stations=<true count>, new_stations_inserted=0, updated_with_cmfd_marker=<true count>
  - 测试结束 ROLLBACK 保留 DB 状态（dry_run 自身就 ROLLBACK，测试 framework 验证 DB 行 count 不变）
- [ ] 3.11 边界 case：basin forcing dir 存在但无任何 X*.csv 文件（如只剩 Prcp_Correction.csv）→ seeder 返 `(0, 0, 0, [list of skipped])`；caller 应基于此 result 决定是否调 ingester（spec 要求 orchestrator skip 该 basin）

## 4. Timeseries Ingester (`workers/cmfd_csv_ingest/timeseries_ingester.py`) [PR-B]

- [ ] 4.1 定义 `CMFDIngesterError(Exception)` typed error 类 + `CMFD_FORCING_VERSION_CHECKSUM_CONFLICT(CMFDIngesterError)` typed sub-error
- [ ] 4.2 实现 `_synth_forcing_version_id(source_id: str, start_date: date, model_id: str) -> str` —— 返回 `forc_cmfd_<YYYYMMDD>_<model_id>`
- [ ] 4.3 实现 `_compute_checksum(rows: Iterable[...]) -> str` —— SHA256 hex over canonically-sorted `(station_id, variable, valid_time, value)` tuples 顺序固定（station_id asc, variable asc, valid_time asc）；deterministic 跨 Python 版本
- [ ] 4.4 实现 `ingest_basin_forcing(conn, basins_root: Path, basin_slug: str, model_id: str, package_uri_template: str, force: bool = False) -> TimeseriesIngestReceipt`：
  - 4.4.1 在事务前 `SET LOCAL statement_timeout = 0`（per §0.3 决策）；BEGIN transaction（pg level）
  - 4.4.2 反查 `met.met_station` 该 basin 全 station 字典 `{forcing_filename → station_id}`
  - 4.4.3 扫 forcing dir，按 station 顺序 ingest（station 串行）
  - 4.4.4 对每 station：parser.parse_cmfd_data_rows → 转 `met.forcing_station_timeseries` rows；累积 `skipped_missing_count`
  - 4.4.5 5 个 variable 共用 station 时间序列，分 variable batch insert（page_size=10000，psycopg2.extras.execute_batch）；**不写 NULL value**
  - 4.4.6 累积 row count + checksum
  - 4.4.7 全 station 完成后，pre-check：`SELECT checksum FROM met.forcing_version WHERE forcing_version_id=%s`：
    - 若不存在：直接进 4.4.8（first ingest）
    - 若存在 + 与 freshly-computed checksum 相等：skip overwrite，仅 UPDATE `lineage_json.last_verified_at`
    - 若存在 + checksum 不同 + `force=False`：raise `CMFD_FORCING_VERSION_CHECKSUM_CONFLICT` 含 (old, new)；ROLLBACK
    - 若存在 + checksum 不同 + `force=True`：保存 old checksum 到 lineage_json.previous_checksum，继续 4.4.8
  - 4.4.8 调 `replace_forcing_timeseries` (DELETE 旧 + INSERT 新) **先**，然后 `upsert_forcing_version`（new station_count + checksum + lineage_json） **后**；ORDER 重要 —— 见 design.md R-7
  - 4.4.9 COMMIT；任何 exception → ROLLBACK + raise CMFDIngesterError 包装原 error
- [ ] 4.5 ROLLBACK 路径必须确保 `met.forcing_version` 该 basin 行不存在 + `met.forcing_station_timeseries` 该 forcing_version_id 行 0 个；atomic 保证（实测：mock 一个 INSERT 中段 raise，assert ROLLBACK 后两表均无该 forcing_version 行）
- [ ] 4.6 unit test `tests/test_cmfd_csv_ingest_timeseries_ingester.py`（flat layout）：mock DB，验证 SQL 构造正确、batch size 正确、checksum 输入排序正确、checksum 冲突 raise 类型正确、`SET LOCAL statement_timeout` 调用顺序正确
- [ ] 4.7 real-DB test `tests/test_cmfd_csv_ingest_timeseries_ingester_real_db.py`（real-db-integration marker）：
  - 4.7.1 node-27 实跑 keliya basin（最小 32 站点）的 ingest（限制 valid_time 范围避免 ~3500 万行慢测试 —— 用 `--max-rows-per-station <N>` 或 fixture 只放 100 个 timestamp）
  - 4.7.2 assert forcing_version 1 行存在 + checksum non-pending + station_count=32 + forcing_package_uri 等于 `file:///home/ghdc/nwm/Basins/keliya/forcing/`
  - 4.7.3 assert timeseries row count = 32 * 5 * (cropped time count) - skipped_missing_count
  - 4.7.4 调 API `/met/stations/keliya_cmfd_X<lon>Y<lat>/series?source_id=cmfd&cycle_time=1951-01-01T00:00:00Z&model_id=basins_keliya_shud&variables=PRCP,TEMP&limit=10` 验证 200 + series.points 非空 + variable 集合包含 PRCP+TEMP 不包含 Press
  - 4.7.5 调 API 同样 URL 但 `source_id=gfs` → 验证 404 FORCING_VERSION_NOT_FOUND（source isolation）
  - 4.7.6 **重复 ingest 相同 input** → assert forcing_version row 仍 1 个、checksum 不变、timeseries row count 不变（idempotency）
  - 4.7.7 修改 fixture CSV → 重复 ingest with `force=False` → assert raise `CMFD_FORCING_VERSION_CHECKSUM_CONFLICT`，DB 状态不变
  - 4.7.8 同上 with `force=True` → assert forcing_version 行更新、新 checksum 写入、`lineage_json.previous_checksum` 等于旧 checksum
  - 4.7.9 post-ingest assert: `SELECT COUNT(*) FROM met.canonical_met_product WHERE source_id='cmfd'` == 0；`SELECT COUNT(*) FROM met.interp_weight WHERE source_id='cmfd'` == 0；`SELECT COUNT(*) FROM met.forcing_version_component WHERE forcing_version_id LIKE 'forc_cmfd_%'` == 0
  - 4.7.10 测试结束：用 `forcing_version_id LIKE 'forc_cmfd_%'` predicate DELETE 自身写入的行（DELETE timeseries 先、DELETE forcing_version 后）；assert post-cleanup row count = pre-test count

## 5. CLI (`workers/cmfd_csv_ingest/cli.py`) [PR-B]

- [ ] 5.1 实现 `main()` argparse：`--basins-root`, `--basin`, `--model-id`, `--database-url`, `--seed-stations-only` 开关, `--dry-run` 开关, `--output <receipt path>`, `--force` 开关（透传到 ingester checksum conflict 路径）
- [ ] 5.2 调 station_seeder + timeseries_ingester；输出单 basin receipt JSON
- [ ] 5.3 docstring + `--help` 说明用法

## 6. Batch Orchestrator (`scripts/ingest_cmfd_forcing_all_basins.py`) [PR-C1]

- [ ] 6.1 实现 `discover_eligible_basins(basins_root: Path) -> tuple[list[BasinSpec], list[BasinSpec], list[BasinSpec]]` —— 返回 (eligible_with_csv_files, no_forcing_dir, forcing_dir_but_no_csv)
- [ ] 6.2 实现 `_collect_baseline_metrics(conn) -> dict` —— met_station / forcing_version / forcing_station_timeseries row_count + hypertable_size 基线快照（任一 query fail → 抛错，orchestrator abort 不写入 DB）
- [ ] 6.2.1 实现 `_assert_primary_db(conn) -> None` —— `SELECT pg_is_in_recovery()`；若 `true` raise `OrchestratorError('not_primary_db', hint='node-27 is primary; check DATABASE_URL')`
- [ ] 6.2.2 实现 `_validate_basin_args(basins_root, basin_filter) -> list[str]` —— 若 `--basin <name>` 中任一 name 不存在于 basins_root 下，raise `OrchestratorError('CMFD_BASIN_NOT_FOUND: <name>')` 在 DB 任何访问前；fail-fast
- [ ] 6.3 实现 `_estimate_new_rows(basin_spec: BasinSpec) -> int` —— 用 CSV header row_count + 站点 count × 5 var 估算
- [ ] 6.4 实现 `_disk_capacity_check(conn, estimated_total_new_bytes: int, threshold: float = 0.7) -> None` —— 通过 `current_setting('data_directory')` + Python `os.statvfs` 或 fallback `df -B1 <path>` subprocess；若不可访问 → fallback `pg_total_relation_size('met.forcing_station_timeseries')` + 系统级 `shutil.disk_usage(...)`；任一估算超阈值 raise `OrchestratorError('disk_capacity_pre_check_failed', baseline_bytes=..., estimated_bytes=...)`；avg_row_bytes 初始 ~70（按 §0 real-DB 校准后回填）
- [ ] 6.4.1 实现 `_disk_capacity_mid_check(conn) -> None` —— 在每 basin commit 后 + 下 basin 开始前调；free_bytes < 10% total 或 < pre-ingest threshold → halt + record `aborted_reason='disk_capacity_mid_run'`
- [ ] 6.5 主流程：`_validate_basin_args` → `_assert_primary_db` → discovery → baseline → `_disk_capacity_check`（pre）→ 按 row_count 升序排序 → 串行 ingest（每 basin 调 cli.main，超时配置）→ 收集 per-basin receipt → mid-check after each commit → 最终写 aggregate receipt
- [ ] 6.6 `--continue-on-error` flag（default off）；失败 basin 在 receipt 标 failed + 继续 / 停
- [ ] 6.7 `--basin` flag（repeatable）：限定 subset（filter 模式不 record skipped_basins，但 §6.2.2 typo 检查仍生效）
- [ ] 6.8 输出 `rollback_sql_per_basin` JSON 块，每 basin 三条 DELETE：
  - `DELETE FROM met.forcing_station_timeseries WHERE forcing_version_id = '<id>'`
  - `DELETE FROM met.forcing_version WHERE forcing_version_id = '<id>'`
  - `DELETE FROM met.met_station WHERE station_id LIKE '<basin>_cmfd_%' AND properties_json->>'seed' = 'cmfd_station_seeder'`（仅删本 change 新增的，不动 heihe/qhh existing）
- [ ] 6.9 smoke test `tests/test_cmfd_csv_ingest_orchestrator_smoke.py`（flat layout）：mock filesystem + DB connection，验证：
  - orchestrator 流程不 crash、receipt 结构合规
  - `--basin nonexistent_typo` → exit code != 0, error message 含 `CMFD_BASIN_NOT_FOUND: nonexistent_typo`
  - `--basin keliya --basin nonexistent_typo` → exit code != 0（fail-fast，不部分 ingest keliya）
  - mock `pg_is_in_recovery()` returns true → exit code != 0, receipt `aborted_reason='not_primary_db'`
  - mock 一个 basin 失败 + 不带 `--continue-on-error` → halt, receipt 含失败那个 basin + 后续 basin 不在 per_basin
  - mock 一个 basin 失败 + `--continue-on-error` → 继续下一 basin, receipt failed_basins 含失败那个
  - mock basin forcing dir 存在但无 X*.csv → skipped_basins 含 `reason='no_eligible_csv_files'`
  - basin with `Prcp_Correction.csv` present → per_basin receipt 含 `prcp_correction_applied: false, prcp_correction_csv_present: true`

## 7. Documentation [PR-C1]

- [ ] 7.1 写 `docs/runbooks/ingest-cmfd-forcing.md`：
  - 前置条件（node-27 PG data dir 磁盘 free 阈值，cmfd migration 已 applied，pg_is_in_recovery 为 false）
  - 单 basin dry_run 步骤（基于 cli.main）
  - 全 8 basin 批跑步骤 + 监控点（log tail / pg_stat_activity / disk free / WAL）
  - 失败 basin 回滚（用 receipt 的 rollback_sql_per_basin 块）
  - **API curl e2e 验证步骤**（含 8 basin × 1 station 的 curl 示例 + 期望 JSON 字段）
  - source_id 命名约定：DB 存 `'cmfd'`（lowercase），API 响应显示 `'CMFD'`（uppercased by `_display_source_id`），查询 lookups 大小写不敏感
  - **前端 UI 暴露 CMFD 是独立后续 issue，本 runbook 不覆盖**；操作员若需浏览器验证需引用 follow-up issue 编号
- [ ] 7.2 写 `docs/architecture/cmfd-csv-ingest.md`：
  - 旁路链路（与 forcing_producer 关系）
  - cycle_time 合成约定
  - station_id 命名约定（`<basin>_cmfd_X<lon>Y<lat>`）
  - 单位换算表（含 RH 单位决策来源）
  - 已知 OUT OF SCOPE（Press、Prcp_Correction、tailanhe、zhaochen、frontend UI）
- [ ] 7.3 更新 `docs/data-pipelines.md`（如存在）补 CMFD 旁路链路一段
- [ ] 7.4 更新 `CLAUDE.md` "技术栈速查" 表加一行 CMFD 数据源（若无现成行就增 "气象数据源" subsection 列 GFS / IFS / CMFD 三行）
- [ ] 7.5 在 `docs/spec/03_database_design.md`（若存在）的 data_source 枚举中补 cmfd 行
- [ ] 7.6 创建 follow-up issue "Frontend: expose CMFD source in /meteorology page"（含明确 scope：扩展 HydroMetSource union、bootstrap 分支跳过 latest-product、UI source selector、e2e 浏览器验证 / Playwright 视觉证据）；在本 change 的 docs 中显式 link 该 issue

## 8. Validation

- [ ] 8.1 本地 `ruff check workers/cmfd_csv_ingest/ scripts/ingest_cmfd_forcing_all_basins.py` pass
- [ ] 8.2 本地 `uv run pytest tests/test_cmfd_csv_ingest_*.py` (unit only，flat layout) pass
- [ ] 8.3 `openspec validate ingest-cmfd-csv-forcing --strict --no-interactive` pass
- [ ] 8.4 node-27 `uv run pytest -m real-db-integration tests/test_cmfd_csv_ingest_*_real_db.py` pass + 留 receipt
- [ ] 8.5 node-27 keliya basin smoke ingest（real，commit DB）+ verify `/api/v1/met/stations/keliya_cmfd_X<lon>Y<lat>/series?source_id=cmfd&cycle_time=1951-01-01T00:00:00Z&model_id=basins_keliya_shud&variables=PRCP,TEMP&limit=10` 返回真数据；同时 verify post-ingest assert：`SELECT COUNT(*) FROM met.canonical_met_product WHERE source_id='cmfd'` == 0
- [ ] 8.6 node-27 full 8-basin batch ingest（生产 commit）—— **独立 PR-C2-OPS**，commit receipt 到 `docs/runbooks/receipts/cmfd-ingest-<UTC date>.md`（含每 basin 耗时、最终 row count、rollback SQL 块、disk pre/post baseline）
- [ ] 8.7 node-27 **API curl e2e**：对 8 个 basin × 1 station 跑 curl 验证 series API 返回 200 + 非空 series + 正确 forcing_version_id；curl 输出附 receipt。**浏览器 /meteorology 不在本 change 验证范围**（descope per proposal OUT OF SCOPE）

## 9. PR Boundary

- [ ] 9.1 本 change **拆 5 个 PR**（避免单 PR > 500 LOC、避免代码与生产数据动作混合）：
  - **PR-A0**: §1 migration only（含 §1.5 smoke test 用既有 keliya station 验 cmfd FK，~50 LOC）
  - **PR-A1**: §2 parser + unit tests（~450 LOC，纯 Python，可独立 CI）
  - **PR-A2**: §3 station_seeder + unit + real-DB tests（~400 LOC + real-DB dry_run）
  - **PR-B**: §4 timeseries_ingester + §5 CLI + real-DB ingest tests（~450 LOC + real-DB commit-then-cleanup test）
  - **PR-C1**: §6 batch orchestrator + §7 docs + §8.5 keliya smoke + §8.7 keliya API curl 一站（~400 LOC + docs，可独立 PR review）
  - **PR-C2-OPS**: 仅 §8.6 + §8.7 完整 8-basin × 1 station curl，**不含代码改动**，仅 commit receipt JSON + markdown wrapper 到 `docs/runbooks/receipts/`；这是 operator action 的 audit trail，与 PR-C1 代码 PR 解耦，bisect / rollback / audit 都更清晰
- [ ] 9.2 PR 标题、commit message 跟既有 `feat/issue-<N>-<desc>` 约定
- [ ] 9.3 PR body 含 Evidence Floor 完整覆盖；CI 全绿 + node-27 live receipt 配对
- [ ] 9.4 每个 PR 走 subagent-workflow Phase 0-8（Phase 4 reviewer + Phase 4.5 verifier + Phase 7 final review + Phase 8 evidence + merge gate）
- [ ] 9.5 PR 序：A0 → A1 + A2 可并行 → B 依赖 A0+A1+A2 → C1 依赖 B → C2-OPS 依赖 C1 合并 + node-27 已 sync

## 10. Post-merge

- [ ] 10.1 `openspec archive ingest-cmfd-csv-forcing` （PR-C 合并后）
- [ ] 10.2 append `docs/review-loop-log.jsonl` 三条（每 PR 一条）
- [ ] 10.3 关闭 stage-change-pipeline 衍生的 Epic + sub-issues
- [ ] 10.4 node-27 服务无需重启（API 代码未改，DB 数据变化自动可见）；但建议 `scripts/ops/start-display-api.sh` 重启确保连接池刷新一次

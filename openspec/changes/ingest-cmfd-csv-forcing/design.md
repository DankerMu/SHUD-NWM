## Architecture Decisions

### AD-1: 新 worker module (`workers/cmfd_csv_ingest/`) vs 扩展 ForcingProducer

**选**：新 worker module，与 `workers/forcing_producer/` 平行。

**理由**：
- `forcing_producer` 主链路是 forecast lifecycle 驱动：`forecast_cycle (status=discovered) → download → canonical_met_product → IDW interp → forcing_station_timeseries`。CMFD 是 archival/static dataset，没有 cycle / no download / no canonical conversion（CSV 本身就是分站点的 final form）。塞进 forecast pipeline 必须加 5+ 处条件分支（skip download / skip canonical / synthesize cycle / bypass IDW / skip lineage tracking），破坏既有抽象。
- CMFD CSV 与 GFS canonical NetCDF 数据形态完全不同：grid 坐标在文件名而非 metadata；时间是 fractional days from epoch 而非 ISO timestamp；变量集与命名不同（`Precip_mm.d` vs `PRECIP_RATE`）。pre-existing converter 完全不复用。
- `workers/forcing_producer/direct_grid_contract.py` 已经有 `DirectGridStationBinding`（含 `forcing_filename`）抽象，但它假设 manifest 已经存在；CMFD 场景下我们要 *生成* manifest（从扫 forcing dir），不是 *读* manifest。语义反过来。
- 隔离的好处：CMFD ingest 失败不影响 GFS/IFS forecast loop；CMFD ingest 的 DB 写入失败时可以独立重试；CMFD 代码可以独立测试（不需要 mock canonical_met_product / mock object store）。

**代价**：少量代码重复（hypertable batch insert、forcing_version upsert 这些 helper）。**缓解**：把通用 helper 抽到 `workers/_forcing_common/` 或直接调用 `workers/forcing_producer/store.py:PsycopgForcingRepository` 的 `upsert_forcing_version` / `replace_forcing_timeseries` 方法（这俩是无业务语义的低层 DB 操作）。**决定**：先复用 `PsycopgForcingRepository` 现有方法，不抽公共包；若后续 CMFD 路径分化太多再重构。

**备选（rejected）**：
- 在 `forcing_producer/producer.py` 加 `forcing_mode='csv_direct'` 分支：会污染主链路，5+ 处 if/else，破坏可读性。
- 写 SQL 脚本直接 COPY：丢弃 schema validation（station FK / variable 单位 / cycle_time 一致性），出错只能事后排查。

### AD-2: cycle_time 合成 = CSV header start_date 转 TIMESTAMPTZ

**选**：每个 basin 的 `forcing_version.cycle_time` = 该 basin CSV header 的 start_date（YYYYMMDD）转为 UTC midnight TIMESTAMPTZ。

例：
- heihe CSV start_date=19510101 → `cycle_time='1951-01-01T00:00:00Z'`
- qhh CSV start_date=19790101 → `cycle_time='1979-01-01T00:00:00Z'`
- xinanjiang_upstream CSV start_date=19580101 → `cycle_time='1958-01-01T00:00:00Z'`

**理由**：
- API 的 `/met/stations/{id}/series` 查询签名是 `model_id + source_id + cycle_time`（必填）—— cycle_time 不能 NULL。
- 每 basin 一个 forcing_version 行（不是全局唯一），cycle_time 用 basin 各自的 start_date 让 forcing_version_id 命名 (`forc_cmfd_19510101_basins_heihe_shud`) 可读、可索引、可调试。
- 若未来 CMFD 上游扩展数据时间范围（新加年份），`replace_forcing_timeseries()` 整体重写该 basin 的 forcing_version 即可；cycle_time 保持等于 CSV start_date 让定位逻辑稳定。
- 文档化在 runbook：用户/前端要调 series 必须知道每 basin 的 cycle_time，可由 `/api/v1/met/stations` 响应的 `properties_json` 或额外辅助 endpoint 暴露（出 scope，本次只 ingest）。

**备选（rejected）**：
- 硬编码统一 `cycle_time='1970-01-01T00:00:00Z'`（"epoch baseline"）：所有 basin 的 forcing_version_id 冲掉差异，前端无法分辨 "我现在拿哪个 basin 的版本"；调试时无 dataset 时间信息。
- 用 `created_at` 当 cycle_time：每次 reingest cycle_time 变，破坏 `(model_id, source_id, cycle_time)` 幂等查询。
- NULL cycle_time：API 端点强制要求 cycle_time 为 datetime，schema 允许 NULL 但 API 路径不允许；会被 422 拒绝。

### AD-3: 8 basin ingest scope（heihe + qhh + 6 missing）

**选**：本次 change ingest 全部 8 个有 CMFD CSV 的 basin —— heihe + qhh + (weiganhe / xinanjiang_upstream / kashigeer / keliya / hetianhe / qinyijiang)。

**理由**：
- heihe + qhh 虽然 `met.met_station` 已有站点（来自 `qhh_production_bootstrap.py`），但**他们的 forcing_station_timeseries 也是空**。若只 ingest 6 个新 basin、不 ingest heihe + qhh 的 timeseries，结果是：API 对 heihe / qhh 还是返回 `FORCING_VERSION_NOT_FOUND`，等于半成品。
- station_seeder 对 heihe + qhh 必须 idempotent：检测到既有 station 行（同 station_id）则 skip，不重复 INSERT。timeseries_ingester 对 heihe + qhh 站点 PK 必须能在 `met.met_station` 查到（FK 约束已经强制）。
- 假设 `qhh_production_bootstrap.py` 产生的 heihe / qhh station_id 与 station_seeder 从 forcing dir 文件名反推的一致 —— **必须在 implementer 阶段 verify**（real-DB 测试：现有 heihe station forcing_filename 必须等于 forcing dir 中的 CSV 文件名）。若不一致是 design 假设破裂，需 escalate。

**rejected**：
- 只 ingest 6 个新 basin、跳过 heihe + qhh：半成品，违背用户 "把实际数据接进来" 的诉求。
- ingest 全 10 basin（包括 tailanhe + zhaochen）：磁盘无 CSV，必失败。orchestrator 用 `os.path.isdir(<basin>/forcing)` 做前置 skip。

### AD-4: 时间换算 — `valid_time = csv_start_date + time_interval × 86400 seconds`

**选**：CSV data row 第一列 `time_interval` 是 fractional days from CSV header start_date；`valid_time` = `start_date + timedelta(seconds=time_interval × 86400)`。

**理由**：
- 解析样本 heihe CSV：header 末尾字段 `86400`（days-to-seconds 转换因子），time_interval 列从 0 步进到 27028.875；time_interval 在小数位 .0, .125, .25, .375, .5, .625, .75, .875 → 每天 8 行 → 3-hour 步长。
- 验证：27028 days × 8 = 216224 ≈ 216224（heihe header 第一字段），匹配。
- 公式给出的最后一行 valid_time：`1951-01-01 + 27028.875 × 86400 sec = 1951-01-01 + 27028 day + 21 hour = 2025-01-01 21:00:00 UTC`，与 header end_date=20250101 + 末步对应。
- header 字段 `86400` **不是** "time step"，而是 days-to-seconds 转换因子。**真正的时间步** = `(end_date - start_date) × 86400 / row_count = 10800 秒 = 3 小时`（按 heihe 验证；其他 basin 可能不同，需要 parser 对每个 basin 单独算）。
- 实际写入 `met.forcing_station_timeseries.valid_time` 为 TIMESTAMPTZ（UTC）。CMFD 是 UTC 时区数据（来源 CMFD doc 明确）。

**单位**（**锁定**，与 `openspec/specs/canonical-conversion/spec.md:61` 的 `RH_frac` 约定一致 → `'0-1'`；其余字段沿用 canonical-conversion 既有约定）：
- `Precip_mm.d` → variable=`PRCP`, unit=`mm/day`（CMFD 是 daily-equivalent rate; 3-hour 步 = 1/8 day; 决定值不除以 8，直接存 mm/day 单位的值；API series 响应的 unit 字段原样回传 `mm/day`）
- `Temp_C` → variable=`TEMP`, unit=`degC`（与 canonical-conversion line 46 一致）
- `RH_1` → variable=`RH`, unit=`0-1`（与 canonical-conversion line 61 `RH_frac` 约定一致 —— 不用 `'fraction'` 或 `'1'`，避免与 GFS/IFS canonical 数据不一致）
- `Wind_m.s` → variable=`wind`, unit=`m/s`（与 canonical-conversion line 66 一致）
- `RN_w.m2` → variable=`Rn`, unit=`W/m2`（与 canonical-conversion line 68 一致）

**rejected**：
- 假设 86400 是 time step：所有时间戳错位 8 倍，全量数据失真。
- 用文件创建时间 / mtime 推断起点：与 CSV header 矛盾，且文件 mtime 受 rsync / cp 影响不稳。

### AD-5: 写入策略 — batch INSERT，按 (variable × time chunk) 分批

**选**：单 basin 单 station 单 variable 一次性 `executemany` insert，page_size=10000；多个 variable 串行；多 station 串行。

**理由**：
- 单 basin 总量：例 heihe 1709 站 × 5 var × 216224 时刻 ≈ 18.5 亿行 ——必须分批，否则单事务超大；TimescaleDB chunk 默认 7-day partition，跨越 74 年 ~ 3850 chunks，逐 chunk insert 利于 chunk-level WAL flush。
- `psycopg2.extras.execute_batch(cursor, sql, rows, page_size=10000)` 提供 batch insert without prepared statement explosion。
- 单 station 单 variable 一次约 216224 行 ≈ 6MB plain text COPY-size，单事务可承受。
- **保守起点**：basin 串行，station 串行，variable 串行。若性能不够（验证 receipt 中记录耗时），后续按 variable 并行（IO/CPU 都不饱和时）。
- **承诺**：每 basin ingest 用独立 DB 事务；事务边界 = 1 basin（forcing_version upsert + 该 basin 全 station 全 variable timeseries insert + finalize checksum），中途 fail 全 ROLLBACK，保证 DB 永远不会有半成品 forcing_version（带 partial timeseries）。

**rejected**：
- 整 basin 一次 COPY FROM：psycopg2.copy_expert 性能更高但难处理 batch 内的 FK violation（单 station_id 出错全 batch 退）。
- 多 basin 并行：node-27 DB 单实例，磁盘 IO 不该并行写入同 hypertable；并行 ingest 易触发 chunk lock contention。
- Bulk DELETE + COPY before each ingest：当 forcing_version 已存在时（reingest 场景）DELETE 整 forcing_version 的 timeseries 行很慢；用 `replace_forcing_timeseries` 现有方法（先 DELETE，后 INSERT），有 idempotent 保证。

### AD-6: station_seeder — 文件名反推 station 元数据

**选**：扫 `/home/ghdc/nwm/Basins/<basin>/forcing/X<lon>Y<lat>.csv` 文件名 → 提取 lon/lat → 生成 `station_id = '<basin>_cmfd_X<lon>Y<lat>'`、`geom = ST_SetSRID(ST_MakePoint(lon, lat), 4490)`、`properties_json = {"forcing_filename": "<filename>", "seed": "cmfd_station_seeder", "grid_resolution_deg": 0.1, "source_id": "cmfd"}`。

**重要：`properties_json` 不设 `forcing_mapping_mode` 键**（不是 `'direct_grid'` 也不是 CMFD 变体）。理由：
- `workers/forcing_producer/store.py:73-94` 的 `load_met_stations()` 排除 `forcing_mapping_mode = 'direct_grid'` 的 station —— 那是为 GFS/IFS forecast pipeline 的 direct-grid lifecycle 设计的，CMFD station 不属于该 lifecycle。
- 若设了 `'direct_grid'`，未来 GFS/IFS forecast 跑这 8 个 basin 时 forcing_producer 看不到 CMFD station（被 exclude），且 `met.interp_weight` 缺 `(method='direct_grid', weight=1.0)` row（migration 000038 CHECK constraint 要求），整个 direct_grid 契约不完整 → 半 broken 状态。
- 不设该键 = CMFD station 是"普通 forcing_grid station"；API 直接读 `met.met_station` 不过滤；future GFS/IFS 若要复用这些站点也无障碍（走标准 IDW interp）。
- `seed='cmfd_station_seeder'` 是 CMFD 来源标记（区别于 `qhh_production_bootstrap` 标记），不强加 lifecycle 语义。

**station_id 命名规则**（决定）：
- 6 个新 basin：`<basin>_cmfd_X<lon>Y<lat>`（例：`weiganhe_cmfd_X80.25Y42.05`）—— 明确标 CMFD 来源，避免与未来其他 source 的站点 ID 冲突。
- heihe + qhh：**不动既有 station_id**（`qhh_forc_001` 等命名）。timeseries_ingester 通过查询 `met.met_station WHERE basin_version_id=<basin> AND active_flag=true AND properties_json->>'forcing_filename'=<csv_filename>` 反查既有 station_id，不重新生成。

**理由**：
- CSV 文件名编码 grid cell 中心点坐标（0.1° 分辨率），lon/lat 精度足以唯一索引 station。
- `basin_version_id` 通过 `core.basin_version` 查询活跃 model_id 的 basin_version_id（每 basin 一个）。
- `station_role='forcing_grid'`（与既有 heihe / qhh 站点保持一致）；`active_flag=true`。
- **station_id 命名要求与 frontend / display 端期望对齐**：现状 frontend 只显示 `station_id` 文本（不解析格式），且 station list 是过滤展示，命名格式不影响功能。

**风险**：CSV 文件名格式不一致（极少数 basin 可能用 `X100.5Y36.0.csv` 而非 `X100.55Y36.05.csv`，零填充差异）。**缓解**：parser 用正则 `r'^X(?P<lon>-?\d+(\.\d+)?)Y(?P<lat>-?\d+(\.\d+)?)\.csv$'` 严格匹配，不匹配 raise warning + skip 该文件，receipt 记录 skipped 文件数。

### AD-7: data_source seed migration 序号

**选**：`db/migrations/000040_seed_cmfd_data_source.sql`（当前最大 000039_crosswalk_external_identity.sql）；若 PR 打开期间有并行 PR 抢占 000040，bump 至下一可用。

**理由**：migration 是 idempotent INSERT (`ON CONFLICT (source_id) DO NOTHING`)，重跑安全。

### AD-8: 必须在 node-27 primary DB 上 ingest（拒绝 replica）

**选**：orchestrator 在写任何数据前 `SELECT pg_is_in_recovery()`，若返回 `true`（standby/replica）则 abort with `aborted_reason='not_primary_db'`。

**理由**：
- node-27 当前是 primary（单 DB 拓扑），但运维若把 DATABASE_URL 误指向 replica（未来若引入 standby），ingest 会失败但失败模式不可预测（read-only error in mid-tx）。
- 提前 check 是廉价（一次 SELECT）+ 防御性 + 错误信息清晰。
- 与现有运维约定吻合：CLAUDE.md 写 "27 是主 DB"，本检查把这个约定 codify 进代码。

### AD-9: NaN / Inf / 缺测值的处理 = skip 该 (station × variable × valid_time) 行

**选**：parser 检测到 NaN/Inf/空 cell → skip 整个 (station, variable, valid_time) 元组的 INSERT，不写 NULL（schema 强约束 `value DOUBLE PRECISION NOT NULL`，写 NULL 会 IntegrityError）；receipt 计 `skipped_missing_count` per basin。

**理由**：
- DDL: `db/migrations/000005_met.sql:106` `value DOUBLE PRECISION NOT NULL`，没法插 NULL。
- 不能用 sentinel（如 NaN float）：Postgres `DOUBLE PRECISION` 接受 `'NaN'::float8` 但下游 API / 前端逻辑期望真实数值；NaN 污染 aggregation。
- skip 行后，API series 查询返回的时间序列在 NaN 时间点会缺值；前端可识别"缺一段"（valid_time gap）而不是错误数据。
- 比 "fail entire basin" 更稳：CMFD 数据集 NaN 比例极低（<<1%，per CMFD doc），整 basin 因为 1 个 NaN abort 太严苛。
- 比 "用 quality_flag='missing' + value=NaN" 更稳：避免下游 NaN 传染。

**门槛**：单 basin parser NaN/Inf 比例 > 1%（按 sample 计算） → ingester abort 该 basin，receipt 标 failed，留 raw error 调查；门槛实现在 timeseries_ingester 进 commit 前算。

### AD-10: forcing_version_id 冲突且 checksum 不同时拒绝 silent overwrite

**选**：若 `forcing_version_id` 已存在但 freshly-computed checksum ≠ DB 中 checksum，ingester 抛 `CMFD_FORCING_VERSION_CHECKSUM_CONFLICT` typed error；操作员需显式 `--force` flag（默认 OFF）才覆盖；覆盖时旧 checksum 写入 `lineage_json.previous_checksum`。

**理由**：
- "Re-ingest replaces prior timeseries atomically" 场景（spec）假设 input CSV 没变，输出 deterministic；若 CSV 被改了（追加年份、修正数据），checksum 必然变 —— 这是 dataset 真的换了，不应 silent overwrite 旧的 forcing_version_id。
- 与 `basins-registry-import` spec "Changed package checksum is not silently overwritten" 模式一致。
- `--force` 保留 escape hatch（运维确认要换 dataset 时用）。
- 默认 abort + 错误信息 = 安全；运维显式 opt-in 才覆盖 = 主动选择。

## Risks / Trade-offs

### R-1: DB 体量与 IO 压力

- **风险**：8 basin 全量 ingest 后，`met.forcing_station_timeseries` 行数从 0 → 数亿。粗估 6.5 亿 ~ 25 亿行（heihe 极端值 18.5 亿是上限，hetianhe 9300 万是低值）；按 8 行/字节 hypertable 平均存储估 ~60GB；TimescaleDB 默认无压缩，chunk 数千。
- **检查**：implementer 阶段须 verify 一个小 basin 的 ingest 后 hypertable 物理大小（`SELECT pg_size_pretty(hypertable_size('met.forcing_station_timeseries'))`），按行数比例 extrapolate 8 basin 总量；若估算超过 node-27 PG 数据目录可用磁盘的 70%，**停止后续 basin ingest**，回到 design 评估压缩 policy 或排除最大 basin。
- **缓解措施**：
  - 单 basin 串行 ingest，每 basin 完成后 commit + checkpoint，避免 WAL 累积撑爆 disk。
  - **基线快照（pre-ingest 备份）**：开始 ingest 前 dump `met.*` schema 的 metadata（station / forcing_version / data_source）+ 物理 size 基线，落 receipt + `docs/runbooks/`，便于回滚比对与磁盘审计。
  - 提前与运维确认 node-27 PG data dir 可用空间（`df -h $PGDATA`），写入 runbook 作为门槛。
  - 预留未来加 TimescaleDB compression policy 的扩展点（不在本 change scope）。
- **回滚**：若 ingest 后系统不可用（OOM / disk full），手工 `DELETE FROM met.forcing_station_timeseries WHERE source_id='cmfd'`（按 source_id 走 index）+ `DELETE FROM met.forcing_version WHERE source_id='cmfd'` + `DELETE FROM met.met_station WHERE properties_json->>'seed'='cmfd_station_seeder'`，可降回到 pre-ingest 状态。orchestrator 必须输出每个 basin 各步骤的 deletion-undo SQL 进 receipt 供运维使用。

### R-2: ingest 耗时 + 阻塞窗口

- **风险**：8 basin 总 ingest 时间数小时到 1 天；期间若有人查询 `/api/v1/met/stations/{id}/series`，由于 ingest 用 `replace_forcing_timeseries`（先 DELETE 后 INSERT），查询可能短暂看到不完整数据。
- **缓解**：
  - 单 basin 事务包裹 forcing_version + timeseries，事务未 commit 前 `met.forcing_version` 该 basin 行不可见，API 仍返 `FORCING_VERSION_NOT_FOUND`（与 ingest 前一致）；commit 后整体可见。
  - basin 间串行，单 basin commit 后立即可查询，不影响其他 basin。
  - **决定（锁定）**：单事务实现。不拆分。理由：拆成 forcing_version + timeseries 两事务时，第一段会留下 `checksum='pending'` 行，API `_ensure_forcing_version_finalized` (`packages/common/forecast_store.py:2642`) 会返 409 `FORCING_VERSION_NOT_FINALIZED`，导致 ingest 中段对客户端 visible（破坏 atomicity 期望）。spec 的 "Single-basin ingest is atomic" 已锁定该决策，未来若要拆分必须先改 API + 改 spec scenario，不容 implementer 阶段单方面切换。
  - 大 basin（heihe / weiganhe / kashigeer 1500+ 站点）ingest 时间长 —— 若单事务超过 1 小时触发 Postgres `statement_timeout` 或 lock 问题，需要 escalate 重设计；不是 implementer 阶段静默改 2-tx 的借口。
  - 在 node-27 低峰窗口（凌晨）执行批 ingest。runbook 写明。

### R-3: station_id 与 heihe / qhh 既有站点冲突

- **风险**：heihe / qhh 既有 station 由 `qhh_production_bootstrap.py` 生成，命名格式可能与 station_seeder 的 `<basin>_cmfd_X<lon>Y<lat>` 不一致。timeseries_ingester 需要确定 station_id 用现有的还是新的。
- **缓解**：implementer 阶段先 introspect node-27 实际 heihe / qhh 站点 `properties_json.forcing_filename` 值，反查 station_id；station_seeder 对 heihe / qhh 走"既有反查"路径（不 INSERT 新 station，只更新 `properties_json.source_id='cmfd'` 标记），对 6 新 basin 走"新 INSERT"路径。
- **测试**：real-DB integration test 必须覆盖：(a) heihe 既有 station 不被 INSERT 复制（重复 PK 触发 conflict）；(b) 6 新 basin 站点 INSERT 后 row count 准确。

### R-4: CMFD CSV 数据质量边界

- **风险**：
  - CSV 中变量值 NaN / Inf / 缺测（CMFD 文档说极少但存在）。
  - 文件名编码 lon/lat 超出 basin geometry 边界（边缘 grid cell）。
  - Prcp_Correction.csv 应用 vs 不应用的差异 —— phase 1 决定**不应用**（保持原始 CMFD 数据，与 SHUD 输入一致），但 receipt 必须显式声明。
- **缓解**：
  - parser 对 NaN/Inf/空 cell **skip 该 (station × variable × valid_time) entry，不入库**（见 AD-9 完整说明；不写 NULL、不写 sentinel；schema `value NOT NULL` 强约束）；维护 `skipped_missing_count` 计数返给 ingester。
  - station_seeder 不校验 geometry-in-basin（CMFD grid cell 中心可能落在 basin 外缘，但 forcing 用于该 basin 是合理的）。
  - Prcp_Correction.csv 显式 OUT OF SCOPE，receipt 中标注 `prcp_correction_applied: false`，phase 2 issue 追踪。
- **门槛**：单 basin 单 station 的 NaN/Inf 比例 > **1%**（与 AD-9 + spec scenario 一致；按 station 内 cell 计算） → ingest 该 basin **abort**，receipt 标 `failed: missing_ratio_exceeded`，留 raw error 文件供调查。

### R-5: 与 `workers/forcing_producer/` 未来冲突

- **风险**：未来若 GFS/IFS forecast 也要为这 8 个 basin 跑（产生 source_id='gfs' 的 forcing_version），CMFD 与 GFS 同 basin 同 model_id 共存。
- **不阻塞当前 change**：`met.forcing_version` (model_id, source_id, cycle_time) 复合查询区分；API `/series?source_id=cmfd` vs `?source_id=gfs` 互不干扰。
- design 假设：CMFD 与 forecast pipeline 在 forcing_version 表内**和平共存**，互不删对方。`replace_forcing_timeseries` 必须按 forcing_version_id 限定 DELETE（既有方法已正确，但 implementer 必须 verify CMFD 调用没 broaden 到 source_id 级 DELETE）。
- 测试：real-DB integration 验证 `forcing_version` 同 basin 同 model_id 下 (source_id='cmfd', cycle_time=X) 与 (source_id='gfs', cycle_time=Y) 两行能共存且 timeseries 不互相删除。

### R-6: 时间步与 native_resolution metadata 字段

- `met.forcing_station_timeseries.native_resolution`（TEXT）：实际 CMFD 时间步 = 3 小时（heihe 验证），但 CSV header 字段 `86400` 不是步长。implementer 必须**程序化计算**步长：`step_sec = (end_date - start_date).total_seconds() / (row_count - 1)`，并以 `'PT3H'` ISO 8601 duration 字符串写入 native_resolution。若不同 basin 步长不同（理论上 CMFD 一致 3h，但若发现异常需要 abort + escalate）。
- **预防**：parser 检测到非 10800 sec 步长 → abort + raise，留给 implementer 阶段 verify。

### R-7: replace_forcing_timeseries 行为 reuse

- **风险**：`PsycopgForcingRepository.replace_forcing_timeseries()` 是 `forcing_producer` 现有方法，可能假设上游来自 canonical product，对 CMFD ingest 调用是否 100% 适用尚未 verify。
- **缓解**：implementer 阶段必须读 method 实现细节（store.py:728-769），确认它**只**做 DELETE FROM ... WHERE forcing_version_id=? + INSERT 行（不附加额外语义如 lineage 注入 / canonical_product FK 检查）。若有耦合，CMFD 自己写 batch insert helper。Task §0 pre-impl introspection 必须 record 决策。
- **测试**：单元 test 直接验证 CMFD 调用产生的 DB 状态 = 预期 row count + correct PK 值；real-DB integration test 包括 re-ingest 同 CSV 应当 row count 不变。
- **事务内 ORDER**：调用顺序 = `replace_forcing_timeseries`（DELETE 旧 + INSERT 新）→ `upsert_forcing_version`（新 station_count + checksum + lineage_json）。这样在事务可见 snapshot 永远不会出现 forcing_version 行的 station_count 与 timeseries 实际 station 数不一致的中间态。

## Migration / Sequencing

1. **DB migration**：seed `met.data_source` 'cmfd' 行（idempotent，可独立 PR）。
2. **station_seeder**：本地 unit test → node-27 6 basin INSERT verify（在新事务内 ROLLBACK 不留痕的 dry-run mode 先验证）。
3. **timeseries_ingester**：单 basin smallest CSV (keliya 32 站 × ~216k 时刻) 真 ingest，验证 round-trip API series 查询成功。
4. **batch orchestrator**：扩展到全 8 basin，按行号从小到大顺序 ingest（hetianhe 32k 行 → xinanjiang_upstream 38k → 其他 216k；先小后大便于早期发现问题、晚期投入大数据量）。
5. **API + frontend**：不动；ingest 完成后 e2e 验证 frontend `/meteorology` 页面能拿到温度/降水曲线。
6. **回滚**：若任何 basin ingest 失败且不能 fix forward，按 R-1 提供的 DELETE 撤销该 basin 行，保留其他 basin。

## Open Questions（在 implementer 阶段必须解决）

1. heihe / qhh 既有 station 的 `properties_json.forcing_filename` 是否一定能与 forcing dir CSV 文件名对应？需要 SSH 到 node-27 实际查询 + 对照。
2. node-27 PG data dir 当前剩余磁盘空间是否能容纳预估 60–200GB 新数据？需要 `df -h $PGDATA` 验证。
3. 单 basin（heihe / weiganhe / kashigeer 大体量）ingest 在 node-27 单事务跑多久？是否会触发 Postgres `statement_timeout` 或类似配置？需要在最大 basin smoke run 验证。
4. CMFD CSV 时间步是否真的 100% 是 3 小时？是否存在跨年份步长变化（不同 basin 不同 step）？parser 必须 detect 并 abort（不 silently 接受非标准步长）。
5. Prcp_Correction.csv 的语义是否真的就是 "per-station 时间点修正乘子"？格式 `<epoch_sec> <factor>` 在 phase 2 应用前必须 confirm（看 SHUD 文档 / 源码引用）。

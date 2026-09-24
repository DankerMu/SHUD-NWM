## Context

行号取自 `fd392615a`。

## Decisions

### D1 — #2491 父表锁序收口

- 收口点：`workers/model_registry/basins_registry_import.py::import_basin_into_registry_core`（通用 import `:318` 与 bootstrap 都委托它）。在其第一条会写父行或插入引用 bv 的子行的语句**之前**执行 `SELECT 1 FROM core.basin_version WHERE basin_version_id = %s FOR NO KEY UPDATE`。
  - 行不存在（首次导入）→ 空结果，无锁、无副作用；首次导入并发的唯一键竞争不在范围。
  - bootstrap 路径已持 `FOR UPDATE`（更强）→ 同事务 no-op。
  - `FOR NO KEY UPDATE` 与 B 侧 `:799` 普通 UPDATE 需要的锁同级，与 A 侧 `FOR UPDATE` 冲突 → B 排在 A 之后，环消失。
- 该语句放在 `_delete_legacy_seg_rows` 之前还是之后：`_delete_legacy_seg_rows` 只 DELETE 子表 `river_segment`（锁 segment 行，不锁父行）。为满足 `bv → rnv → segment`，bv 锁必须在它**之前**。
- 调用方式：新增一个小函数 `_lock_basin_version(cursor, basin_version_id)`（与 `_lock_river_network_version` 并列、同文件），由 `import_basin_into_registry_core` 在最前调用。注意 #2490 seam：`import_basin_into_registry_core` 的 11 个 spy 名调用顺序不能被打乱；新调用是新增的第一步。
- `_lock_river_network_version` docstring 的锁序不变量扩为 `basin_version → river_network_version → river_segment`，并保留 #2157 的边界说明：行级 INSERT/DELETE（如 `_delete_legacy_seg_rows` 在 rnv UPDATE 之前删除 `river_segment` 行，`:416-425`）不在该父表锁序不变量内；spec、docstring 与静态扫描同口径表述。
- 静态扫描：在 `tests/test_river_segment_write_surface_scan.py`（或新 `tests/test_registry_parent_lock_order_scan.py`，优先新文件以免改写扫描常量）用 AST 断言 `import_basin_into_registry_core` 函数体中 `_lock_basin_version(` 调用位于任何其它 `_ensure_*` / `_delete_legacy_seg_rows` / `_refresh_parent_version_materialization` 调用之前。
- 真实 DB 交错测试：新文件 `tests/test_registry_parent_lock_order_integration.py`（`integration` marker，参照 `tests/test_river_segment_lock_order_integration.py` 的两会话 + 线程 + 超时框架）：seed bv + rnv；会话 A 执行 `_lock_qhh_basin_scope` 的 SQL 后暂停；会话 B 调用**真实的** `import_basin_into_registry_core`（red 腿通过临时移除该生产调用得到）；先用一次真实的通用 import seed，使 B 的「已存在 basin 二次导入」在等待后正常完成而不是 `CHECKSUM_CONFLICT`；A 仅在 `pg_stat_activity` / `pg_locks` 显示 B 的 backend 正在等锁后才继续执行 `UPDATE core.river_network_version`（同 `tests/test_river_segment_lock_order_integration.py` 的做法）；测试只 import 修复前已存在的符号。修复前 red（某会话 `40P01`），修复后 green（B 等 A，二者均无 `40P01`，所有 join 有上限，`lock_timeout` ≫ `deadlock_timeout`）。red 证据：临时去掉新锁调用运行一次并记录。
- 同类确认：`packages/common/model_registry_catalog.py::_lock_basin_version_scope`（bv `FOR UPDATE`）所在生命周期事务是否锁 rnv——读代码确认并在报告中记录结论；若确有 rnv 锁且顺序为 bv→rnv 则一致，无需改。

### D2 — #1729 读侧过滤

- `packages/common/model_registry_catalog.py::list_basins`：在 SQL 中加 `WHERE basin_group IS DISTINCT FROM 'evidence-only'`（NULL basin_group 的真实流域保留），与 `has_display_product` 的 `EXISTS` 条件用 `AND` 组合；分页在过滤之后（`LIMIT/OFFSET` 仍在 SQL）。
- `list_basin_versions`：evidence-only basin 视同不存在 → `MissingResourceError`（路由映射 404）。实现为把 `_exists` 检查换成带 `basin_group` 条件的存在性检查（只在本方法内，不改 `_exists` 通用 helper 的其它调用者）。
- 常量：`EVIDENCE_ONLY_BASIN_GROUP = "evidence-only"` 定义在 `model_registry_contracts.py` 并由 facade re-export。
- 其它公开读路径（至少点名处置）：`GET /api/v1/models?active=all|false` 与 `/models/{model_id}` 会带出 evidence model 及其 basin 的 `basin_id`/`basin_name`（`model_registry_catalog.py:454-459, 488-491`）——「inactive model」理由不覆盖 `active=all`，须明确处置（建议同样排除 evidence-only basin 下的 model，或记录不改的理由）；`GET /met/stations?basin_version_id=`（`apps/api/routes/data_sources.py:94`）同类。implementer 另枚举 `apps/api/routes/**` 中所有返回 `core.basin` 行或以 basin_id 为入口的 GET（如 `/basins/{id}`、搜索、models 列表按 basin 过滤、display 端点），逐个说明是否需要同样过滤；只改直接列举 `core.basin` 的公开入口，其它（按 model_id / 已 inactive 的 model）记录理由。
- 测试：新文件或现有 `tests/test_model_registry_list_basins.py` / `tests/test_model_registry_basin_versions.py`（116/55 行，可改）加用例：evidence-only 行被排除（两种 has_display_product）、NULL/其它 basin_group 保留、分页计数正确、versions 404。真实 DB 集成用例（`integration`）至少覆盖 list_basins SQL。

### D3 — #1729 删行脚本（执行前暂停）

- 文件：`scripts/ops/node27_1729_delete_evidence_basin.sql`（delete）、`..._backup.sql`（导出）、`..._rollback.sql`（恢复）。参数化目标 `basin_id = 'basin__evidence_cmfd_p02_synth'`，并**硬性断言** `basin_group = 'evidence-only'` 与依赖计数符合预期（不符 → `RAISE EXCEPTION` 整体回滚）。
- **依赖集以 node-27 live catalog 为准，而非 repo migrations**（`db/roles/node27_write_roles.sql:726-728` 记录 node-27 ledger 中有已不在 `db/migrations` 的表）：任务 1.1 须 live 列出 `pg_constraint` 中 confrelid ∈ {basin, basin_version, river_network_version, mesh_version, model_instance, met_station, forcing_version} 的全部 FK dependents、跨全部 schema 扫描列名（`basin_id`、`basin_version_id`、`model_id`、`station_id`、`river_network_version_id`、`mesh_version_id`、`*_key`）、并查 `pg_trigger`；脚本运行时断言 live FK 集合 = 预期集合，出现意外即 `RAISE`。该断言**排除 TimescaleDB per-chunk 约束**（`conrelid` 属于 `_timescaledb_internal` schema 的行），只比对 `.workplans/k3/k3-catalog.out` 中的 20 条非 chunk FK；否则预期集随每日新 chunk 增长而误 `RAISE`。
- 已知依赖（node-27 只读实测，`fd392615a` 时刻）：`core.basin_version` 1、`core.river_network_version` 1、`core.mesh_version` 1、`core.model_instance` 2、`met.met_station` 6、`hydro.hydro_run` 0。脚本须同时断言以下为 0 或一并删除（先实测）：`core.river_segment` / `core.river_segment_crosswalk`（rnv segment_count=0）、`met.interp_weight`（model_id 与 station_id）、`met.forcing_version`、`hydro.state_snapshot`、`flood.flood_frequency_curve`（均为普通表或已有索引的等值谓词，`.workplans/k3/k3-deps2.out` 实测全 0）。**hypertable 不做显式扫描**（`.workplans/k3/PROBE-NOTE.txt`：对 hypertable station/key 列与 `ops.audit_log` 的 `LIKE` 扫描在活主库上跑 >10 min 被取消）：
  - 有 FK 覆盖的 hypertable（`met.forcing_station_timeseries.station_key`，`_legacy.station_id` / `forcing_version_id`）依赖 NO ACTION FK 强制——若存在引用行，DELETE 失败、整个事务回滚，不另行计数；
  - 无 FK 的 hypertable 列在 receipt 中以**论证代替扫描**：`hydro.river_timeseries.basin_version_key` / `river_network_version_key`——`run_key` 为 FK → `hydro.hydro_run`（`000059:13`），evidence basin 的 `hydro_run` 行数为 0；`met.forcing_station_timeseries_legacy.basin_version_id`——`forcing_version_id` 为 NOT NULL FK → `met.forcing_version`，evidence models 的 `forcing_version` 行数为 0（`k3-deps2.out`）；故均不可能存在引用行；
  - 非 FK 引用（`ops.audit_log`、`met.canonical_grid_*`）只清点不删（审计 append-only，保留），在 receipt 中列出；`ops.audit_log` 只用**等值谓词**计数（实测 13），禁止 `LIKE`。
- 顺序（单事务）：子表 → `met.met_station` → `core.model_instance` → `core.mesh_version` → `core.river_network_version` → `core.basin_version` → `core.basin`；每步断言受影响行数 = 预期。
- 备份与回滚必须**逐字节保真**：`basin_version_key` / `river_network_version_key`（`000050:185,192`）与 `met_station.station_key`（`000061:193`）是 `GENERATED ALWAYS AS IDENTITY`，且 `hydro.river_timeseries` 以无 FK 的方式引用这些 key（`000050:79`）；geometry（`MultiPolygon,4490` / `Point,4490`）经 json 会丢 SRID 与精度。因此：备份用 `COPY (SELECT <显式列，排除 STORED generated 列如 river_segment.stream_type(000048)> FROM t WHERE ...) TO STDOUT`（text 格式，geometry 以 hex EWKB）落盘到 node-27 `/home/nwm/tmp/1729-delete-apply-<ts>/`（dry-run 用独立的 `1729-delete-dry-<ts>/`，可选预检 `1729-precheck-<ts>/`；目录不复用） 并在本地保存副本；回滚按反向 FK 顺序 `INSERT ... OVERRIDING SYSTEM VALUE`（或经验证等价的 COPY FROM）。测试 4.2 须逐列精确比对恢复行，含 identity key 与 `ST_AsEWKB(geom)` / SRID。
- 备份与删除同事务（review round 1）：delete 脚本在 id 集 / FK 断言之后、DELETE 之前，对 6 张表目标行 `SELECT … FOR UPDATE` 并以 `@copy-out` 写出备份（列集守卫同 backup 脚本），故 `--copy-dir` 必填，`--apply` 的备份恰为被删行；独立 `_backup.sql` 仅作可选只读预检。
- 活主库负载：删 6 行 `met_station` 触发对 `met.forcing_station_timeseries`（FK `station_key`，`000061:243`）与 `_legacy`（FK `station_id`，`000005:102`，非 PK 前导列）全部 chunk 的 FK 检查；删 `model_instance` 检查 `forcing_version`/`hydro_run`/`state_snapshot`/`interp_weight`。脚本设 `lock_timeout` 与 `statement_timeout`；`BEGIN…ROLLBACK` dry-run 记录删除耗时；执行时段避开 node-27 retention/compression 作业（chunk 级锁）。`_legacy` 的 FK 检查按非前导列 `station_id` 查找，可能与被取消的探针同样慢：若 dry-run 触发 `statement_timeout`，D5 第 3 步如实报告并停下，不重试、不放宽超时，由用户决定。
- 只清点不删（receipt 列出）：`ops.audit_log`、`met.canonical_grid_*`、`ops.pipeline_job.model_id`（`000011`）、`ops.pipeline_event.entity_id`、`ops.qc_result.target_id`、`hydro.state_snapshot.cloned_from_model_id`（`000046`）、`flood.return_period_result` 的 `model_id` / `basin_version_id` / `river_network_version_id`（无 FK，实测 0）；`hydro.river_timeseries` 无 FK key 列按上文论证处理、不扫描。
- 执行角色：`docker exec -i nhms-db psql -U nhms -d nhms`（owner）；`\set ON_ERROR_STOP on`；先 `BEGIN; ... ROLLBACK;` dry-run 打印计数，确认后再 `COMMIT` 版。
- 真实 DB 测试：`tests/test_node27_1729_evidence_basin_delete_integration.py` 在 disposable DB 上 seed 等价行 → 运行 delete 脚本 → 断言清空 + 计数断言失败路径（改一个依赖计数 → 脚本 RAISE 且零删除）→ 运行 rollback → 断言恢复一致。

### D4 — #1480 回填脚本（执行前暂停）

- 文件：`scripts/ops/node27_1480_backfill_seed_station_provenance.sql` + `..._rollback.sql`。
- 谓词：`properties_json->>'seed' = 'qhh_production_bootstrap' AND properties_json->>'project_name' IS NOT NULL AND (properties_json->>'source' IS DISTINCT FROM (properties_json->>'project_name') || '.tsd.forc' OR (jsonb_typeof(properties_json->'elevation_metadata') = 'object' AND properties_json#>>'{elevation_metadata,source}' IS DISTINCT FROM (properties_json->>'project_name') || '.tsd.forc'))`（`elevation_metadata` 缺失或非 object 的行不因该条件反复命中，保证幂等）；SET 仅 `jsonb_set(jsonb_set(properties_json, '{source}', to_jsonb(project_name||'.tsd.forc')), '{elevation_metadata,source}', ...)`（`elevation_metadata` 缺失或非 object 时不创建——用 `CASE WHEN jsonb_typeof(properties_json->'elevation_metadata') = 'object'`）。值从同行 `project_name` 推导，不硬编码 heihe。
- 回滚：执行前把受影响行的**完整原 `properties_json`**（按 `station_id`）备份；回滚只在当前值仍等于回填后值时还原（避免覆盖之后的合法写入）。回填断言受影响行数 = dry-run 计数（node-27 预期 1709），否则 `RAISE`。
- 幂等：二次执行影响 0 行。前后分组计数 receipt（预期 `heihe|qhh.tsd.forc` 1709 → 0、`heihe|heihe.tsd.forc` 0 → 1709、`qhh|qhh.tsd.forc` 386 不变）。
- 其它字段（`forcing_source_identity`、`source_file`、`project_name`、`source_sha256` 等）字节不变——测试断言整行 `properties_json` 除两键外相等。
- 真实 DB 测试：`tests/test_node27_1480_seed_provenance_backfill_integration.py`（disposable DB：seed heihe 错配行 + qhh 正确行 + 非 seed 行 + `elevation_metadata` 缺失行 → 运行 → 断言 → 二次运行 0 行 → rollback 还原）。
- fixture 同步：执行后以 live API 响应校对 `tests/fixtures/station_series_baseline_heihe_ifs_2026060100.json` 的 `:20/:31`；该文件 2074 行未豁免，编辑方式届时由用户裁定。

### D5 — 执行与部署顺序

1. 代码 + 脚本 + 测试（本 PR），node-27 全量 pytest（disposable DB）→ review → CI → 合并。
2. 合并后 node-27 `/home/nwm/NWM` `git pull --ff-only` + 重启 display API（按 `docs/runbooks/node-27-bringup-checklist.md`；失败回滚到上一 SHA），公网 curl 前后对比——此 receipt 证明**读侧过滤**生效（evidence 行仍在库中）。
3. **暂停**：向用户展示 D3/D4 的 live dry-run（`BEGIN…ROLLBACK`）计数、耗时、live FK/trigger 清点、备份位置、回滚脚本，等确认。
4. 用户确认后执行 D3/D4（node-27 活库），落 receipt（前后计数、二次执行 0 行）；`evidence/restore/README.md` 更新与 fixture 同步随 post-merge archive PR 提交。

## Governing invariant

- 所有调用 `import_basin_into_registry_core` 的事务对父表的加锁顺序为 `basin_version → river_network_version → river_segment`。
- `basin_group = 'evidence-only'` 的 basin 及其 model 不出现在任何公开 basin 列表 / versions / models 列表 / model 详情响应中（`get_model_internal` 不过滤；scheduler 经 `list_models(active=True)` 的发现路径随之过滤，属刻意选择）。
- #1729 删除脚本在**同一事务**内先锁定并备份将被删除的行，再执行 DELETE；无备份目录即拒绝运行。
- 一次性数据脚本只改动其谓词精确选中的行，且可由回滚脚本逐行（含 identity key 与 geometry EWKB）恢复。

## Sibling surfaces

- Lock order：`qhh_bootstrap_registry.py::_lock_qhh_basin_scope`、`_lock_river_network_version`、`model_registry_catalog.py::_lock_basin_version_scope`、`scripts/node27_autopipeline.py` 的 `_ensure_seeded_basin_display_ready`（只锁 rnv，不参与）。
- Read side：`list_basins`（两路）、`list_basin_versions`、其它 `/api/v1/basins*` 读路径、前端 `overviewData.ts` 用 `has_display_product=true`（行为不变）。
- Data：`met.met_station.properties_json` 其它键；`evidence/restore/README.md` 保留声明。

## Seams under test

`import_basin_into_registry_core`（两会话真实 DB）、`list_basins` / `list_basin_versions`（fake cursor + 真实 DB）、SQL 脚本（disposable DB）。

## Required evidence

见 tasks.md。

## Non-goals

见 proposal Out of scope。

## Review focus

1. bv 锁确实在 `import_basin_into_registry_core` 第一条写之前，且 #2490 的 11 个 spy 调用顺序未变。
2. 交错测试真能在修复前红（不是永远绿）且不会挂起。
3. 过滤不误伤 NULL basin_group、分页在过滤后；versions 404 不泄露存在性差异之外的信息。
4. 删除 / 回填脚本的断言、单事务、备份、回滚完整性；谓词精确。

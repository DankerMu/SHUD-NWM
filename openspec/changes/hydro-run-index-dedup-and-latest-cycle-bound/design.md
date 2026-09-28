# Design

Change surface:
- `db/migrations/000065_hydro_run_candidate_index_dedup.sql`（新文件；名称以实现为准，序号 000065）
- `packages/common/forecast_store.py::_per_source_latest_cycles`、`_LATEST_CYCLE_FACT_PROBE_SQL` 及其 river_ts 模板注册条目
- `tests/test_migrations.py`（账本清单、000063 索引副本 pin、新增 000065 pin）
- `tests/test_schema_ledger_convergence_integration.py`（real-DB：`fresh_catalog` 的六索引断言、`test_runner_converges...` 的 `fresh_indexes[name]`、`:418` 的 `broken` 索引名、`test_fresh_database_..._as_no_ops` 的 000063 重放）——见 D1 "测试面"
- 形状 pin：`tests/test_latest_cycle_discovery_shape.py`（`fact_probe()` `:81`、围栏字符串 `:114`、`test_the_fact_probe_is_a_registered_narrow_template` `:203`），以及需核对的 `tests/test_river_ts_template_golden.py:123,179`、`tests/test_river_ts_text_identity_cleanup.py:189`、`tests/test_river_ts_read_path_surrogate_keys_integration.py:1283`。（`tests/test_forecast_store_routing.py` 的 `OUTER_CLAUSES["per_source_latest_cycles"]` 已随 #2424 D1 移除，`:144,263`）
- `scripts/node27_autopipeline.py:1426-1429` docstring（点名了被删的 `hydro_run_display_ready_basin_status_idx`）

## D1 去重（#2634）

保留：`hydro_run_qhh_latest_candidate_idx`（`packages/common/forecast_store.py:4459`、`tests/api_contract_helpers.py:308`、`tests/test_forecast_api.py:2033`、`tests/test_forecast_store_routing.py:87`、`tests/test_migrations.py:375` 与 i13 receipts 引用）、
`hydro_run_display_product_basin_status_idx`（`scripts/node27_autopipeline.py:1426` 说明它是实际路径）。
删除：`hydro_run_qhh_latest_candidate_parsed_idx`、`hydro_run_display_ready_candidate_idx`、`hydro_run_display_ready_basin_status_idx`。
**测试面（real-DB）**：`tests/test_schema_ledger_convergence_integration.py` 按 000065 之后的期望改写——`fresh_catalog` 的 `hydro_run` 索引集合为保留的 4 个 partial（`latest_ready_run`、`qhh_latest_candidate`、`display_product_basin_status`、新候选索引）+ 非 partial 的 3 个；`broken` 改用保留的索引名（如 `hydro_run_display_product_basin_status_idx`）；重放测试改为按序重放 000062→000065 整段并断言终态等于 fresh（000063 单独重放会把三个重复索引建回，单独断言 no-op 已不成立）；新增"新候选索引 INVALID 残留后重跑 000065"用例。既有 000063 INVALID 重跑用例（`:404-432`）必须同时删掉 `000063` 与 `000065` 的账本行，让 runner 按 000063→000065 顺序重放后再与 fresh 比较（只删 000063 的行会让 runner 单独重放 000063，把三个重复索引建回）；规格场景 "A failed index rebuild is safe to rerun" 按同一口径改写。

receipt 2009 记录 planner 当时选的是 `hydro_run_display_ready_candidate_idx`；它与保留的 `hydro_run_qhh_latest_candidate_idx` 定义字节相同（node-27 `pg_indexes` 2026-09-28），删除后 planner 改走保留副本，计划形状不变。

## D2 候选索引（#2626）

`CREATE INDEX CONCURRENTLY <name> ON hydro.hydro_run (basin_version_id, cycle_time DESC ...) [INCLUDE (...)] WHERE run_type = 'forecast' AND cycle_time IS NOT NULL`，无 status 谓词。
- 可 sarg 的只有 `basin_version_id` 等值前缀（scenario filter 是 `LOWER(...)` 析取）。列序与 `INCLUDE` 由 node-27 同集群 scratch DB（拷贝 `hydro_run`）上 `cand` CTE 的 `EXPLAIN (ANALYZE, BUFFERS)` 定稿，取 shared hit 最小者，结果与候选对比表写进本文件；不照 issue 写死。
- 名称须在 node-27 上确认不存在（`pg_indexes`）。
- 重跑安全：与 `000063` 同形，先 `DROP INDEX CONCURRENTLY IF EXISTS <name>` 再 `CREATE INDEX CONCURRENTLY`。失败的 CONCURRENTLY 会留下 INVALID 索引且不写账本行，重跑整文件时先删后建；只写 `IF NOT EXISTS` 会在 INVALID 残留上静默 no-op（#2048 证据 1）。三条 DROP 本身幂等。
- runner 对每条语句 autocommit（`packages/common/migrate.py:393`），CONCURRENTLY 可行。

**列序对比（tasks 1.2，node-27 同集群 scratch DB `nhms_scratch_batchdb_d2`，2026-09-28；`receipts/2026-09-28-branch/d2-compare.*`）：**
scratch DB 由 `db/migrations` 从零建到 000064，`hydro.hydro_run` 经 `nhms_display_ro` 只读 COPY（10723 行，scratch 堆 864 页；生产 1299 页，含 7941 dead tuple）。每个候选单独建（其余全删），对 `cand` 的 SELECT 做 `EXPLAIN (ANALYZE, BUFFERS)`，warm 第 3 次，数字为根节点 shared hit。VM 分两态：`analyze-only` 为 COPY 后不 VACUUM，VM 近空，接近生产（生产 `relallvisible` 47 / `relpages` 1299）；`vacuumed` 为 VACUUM ANALYZE 后全页 all-visible，是 INCLUDE 的最好情况。

| 候选（均 `WHERE run_type = 'forecast' AND cycle_time IS NOT NULL`） | zhaochen_bst GFS+IFS（239 行） | byh GFS+IFS（128） | byh IFS（64） | byh GFS+IFS + model_id（64） |
|---|---|---|---|---|
| 无（现状：Seq Scan；model 行走 `ops_strict_identity`） | 864 / 864 | 864 / 864 | 864 / 864 | 317 / 317 |
| **A `(basin_version_id, cycle_time DESC)`** | **194 / 194** | **125 / 125** | **125 / 125** | **125 / 125** |
| B A + `INCLUDE (run_key, scenario_id, source_id, end_time)` | 197 / 7 | 127 / 5 | 127 / 5 | 127 / 127 |
| C `(basin_version_id, scenario_id, cycle_time DESC)` | 197 / 197 | 127 / 127 | 127 / 127 | 127 / 127 |
| D `(basin_version_id)` | 194 / 194 | 125 / 125 | 125 / 125 | 125 / 125 |

（每格为 `analyze-only / vacuumed`。）四个候选都走 Bitmap Index Scan（`basin_version_id` 等值）+ Bitmap Heap Scan，成本几乎全是候选行所在的堆页。
- B 只在全页 all-visible 时走 Index Only Scan；生产 VM 几乎为空（`hydro_run` 的 status 更新持续弄脏最新的页，而 latest-cycle 发现恰好读这些页），所以 B 的优势在生产上不成立；model_id / run_id 过滤也会让它退回堆。
- C 的 `scenario_id` 列不可 sarg（scenario filter 是 `LOWER(...)` 析取），只增宽度。
- A 与 D 并列最低；取 A：与本节模板一致，且 `cycle_time DESC` 给按时间取序的读者留了有序路径，不多占堆访问。

定稿：`hydro_run_forecast_basin_cycle_idx ON hydro.hydro_run (basin_version_id, cycle_time DESC) WHERE run_type = 'forecast' AND cycle_time IS NOT NULL`。该名在生产 `pg_indexes` 中不存在（`nhms_display_ro`，2026-09-28，`receipts/2026-09-28-branch/prod-hydro-run-indexes.out`）。

## D3 有界探针（#2630）

语义 I1：每个 scenario 取"该 segment 有 `q_down` 行的最大 `cycle_time`"，无 status 过滤。
新探针：

```sql
CROSS JOIN LATERAL (
    SELECT 1 FROM hydro.river_timeseries rt
    WHERE rt.run_key = o.run_key
      AND <原有 segment/basin/network/variable 谓词，#2451 C1 拼写不变>
      AND rt.valid_time >= o.cycle_time
      AND rt.valid_time <= o.end_time
    LIMIT 1
) hit
```

**探针拆分：** 两条时间谓词属于事实侧谓词，放进注册模板 `_LATEST_CYCLE_FACT_PROBE_SQL`（引用外层 `o.cycle_time` / `o.end_time`，与既有 `o.run_key` 同形）；`CROSS JOIN LATERAL (` 包装与 `LIMIT 1` 放在 `_per_source_latest_cycles` 的外层 SQL。模板 golden 与形状 pin 按此更新。

**语义前提与证据（node-27，2026-09-28，`receipts/2026-09-28-phase0-proof/`）：**
- `hydro_run`（`window-shape.*`）：10723 行，全部是 forecast run，`start_time = cycle_time` 全部成立，`end_time - cycle_time ∈ {6 days (64), 7 days (10659)}`，无 NULL，无 `end_time <= cycle_time`。
- 下界：全部 forecast run 的全部事实行（所有变量，不止 `q_down`）中 `valid_time < cycle_time` 为 **0 行**（`proof-valid-ge-cycle.*`，75 s）。
- 上界：`valid_time > end_time` 为 **0 行**（`proof-valid-le-end.*`，73 s）。
- 因此对现存数据，加界后 EXISTS 的真值与不加界相同，I1 结果不变。

**契约归属：**
- `workers/output_parser/parser.py::_parse_time_token` 把行时间写成 `start_time + offset`；
- `scripts/node27_ingest_run.py` 从 manifest 写入 `start_time`/`end_time`（同一 run_id 重写时两者一起更新）；
- SHUD 运行窗口就是 [start_time, end_time]。

DB 层没有跨表 CHECK 能表达这条约束（hydro_run 对 hypertable）。本批不在 parser 加强制检查（范围只含 forecast_store + migration），把"ingest 侧强制 valid_time ∈ [cycle_time, end_time]"作为 follow-up 立单，避免 D1 的依赖无人知晓。
失败模式：只有当某个 run 在该 segment 上**所有** `q_down` 行都落在窗口外时才会漏选。行本来从 `start_time = cycle_time` 起写，这是病态数据，不是 retention 能造成的情况（retention 只按 chunk 砍掉较早的行，剩下的尾部仍在窗口内）。

**为什么必须改成 LATERAL：** 实测对照（`nhms_display_ro`，warm 第 3 次；手写 SQL 与代码渲染的 D1 语句同形，段 id 已按代码映射为 `<basin>_shud_shud_riv_000001`；`d1-explain-<pin>-<variant>.*`）：

| 形态 | 空 pin `basins_zhaochen_bst` GFS+IFS（239 候选） | 非空 pin `basins_byh` GFS+IFS（128 候选，2 行） |
|---|---|---|
| old：EXISTS、无界（= master） | 24497 | 1456 |
| new：EXISTS + 时间界 | 24497（planner 改写为 Nested Loop Semi Join，ChunkAppend 不做 chunk 运行时排除） | 1456 |
| latnb：LATERAL LIMIT 1、无界 | 24497 | 1456 |
| **lat：LATERAL LIMIT 1 + 时间界** | **1330**，`Chunks excluded during runtime: 29`，44 ms | **1345**，`Chunks excluded during runtime: 21` |

非空 pin 上命中来自未压缩 chunk `_hyper_9_206`，走 `narrow_pkey`，`run_key` 在 Index Cond 中。每个 chunk 的索引选择可能因 pin 而异（最新的未压缩 chunk 可能改走 `river_ts_segment_time_key_idx`），这不是单个 pin 能证明的，所以非空 pin 的成本门以 440-pin 回归为准：非空 max/p95 不得高于 3301/2530。若回退，停止并报告，不合并。

两者缺一不可。`LIMIT 1` 的 LATERAL 与 EXISTS 在"是否存在一行"上等价，外层 `ORDER BY o.cycle_time DESC LIMIT 1` 与 `OFFSET 0` 围栏不变。

## 结果（branch，生产 apply 前；`receipts/2026-09-28-branch/`）

- **3.3 440-pin 等价回归**（`equivalence-bounded.*`，`nhms_display_ro`，每 pin 一个 REPEATABLE READ 快照；old = origin/master `aff6201e0` 的 `_per_source_latest_cycles`，new = branch；两侧 `packages/ services/ apps/ workers/` 只差 `forecast_store.py`，harness 开头断言）：64 个网络，440 pin，364 非空 / 76 空，**0 mismatch**。
  | shared hit（EXPLAIN 根节点，shared read 全为 0） | 空 pin max / p95 | 非空 pin max / p95 |
  |---|---|---|
  | master（EXISTS，无界） | 24491 / 24491 | 3287 / 2516 |
  | **branch（LATERAL LIMIT 1 + 时间界）** | **1308 / 1308** | **3155 / 2384** |
  | 门 | ≤ 5000 | ≤ 3301 / 2530 |

  非空 max 来自 `basins_shj`：其中 1842 是 `seg` CTE 解析 `river_segment_id` 时 planner 选了 `river_segment_network_stream_type_idx`（master 同样如此，与本批无关），事实探针本身只有几十。
- **逐 chunk EXPLAIN**（`explain-bounded-{byh,shj,zhaochen_bst}.out`，warm 第 3 次）：非空 pin `byh` GFS+IFS 1318 hit、`Chunks excluded during runtime: 21`，命中在未压缩 chunk `_hyper_9_206` 的 `narrow_pkey`（`run_key` 在 Index Cond 中）；空 pin `zhaochen_bst` GFS+IFS 1308 hit、`Chunks excluded during runtime: 29`。余下约 1299 hit 是 `cand` 在生产上仍然 Seq Scan `hydro_run`（新索引待生产 apply）。
- **3.4 apply 前 sibling 选择器**（`sibling-explain-*.out`，scratch DB 带 `hydro_run`/`core.basin`/`basin_version`/`river_network_version`/`model_instance`/`met.forcing_version`/`run_display_coverage` 的只读拷贝；SQL 从代码取）：000064 态 vs 000065 态，同一 VM 状态下每条语句的 `hydro_run` 访问只是换到保留的同定义副本，buffers 相同——`display_ready_run` 走 `hydro_run_latest_ready_run_idx`（不变）；QHH latest-product candidate CTE 及其 fast path 由 `hydro_run_display_ready_candidate_idx` 换到 `hydro_run_qhh_latest_candidate_idx`；display-coverage candidate CTE 的 Bitmap Index Scan 由 `hydro_run_display_ready_basin_status_idx` 换到 `hydro_run_display_product_basin_status_idx`；`_eligible_run_ids` 由 Index Only Scan `hydro_run_display_ready_candidate_idx` 换到 `hydro_run_qhh_latest_candidate_idx`（191 hit，相同）；autopipeline publish UPDATE 走 `hydro_run_display_product_basin_status_idx`。注：未 VACUUM 的 000065 scratch 上 `_eligible_run_ids` 改走 `hydro_run_latest_ready_run_idx`（VM 为空时 index-only 无利），这是 VM 状态的差别，不是去重造成的。
- **4.3 生产 apply 后**：待用户确认 apply 后补。

Must preserve:
- I1：结果逐 scenario 与 master 相同（440-pin 等价回归 0 mismatch）；无 status 语义变化；#2424 tasks 3.3 等价 pin 不变。
- `#2451 C1`：basin/network key 保持非 sargable 的 `IS NOT NULL AND IS NOT DISTINCT FROM` 拼写，segment key 与 variable 保持 `=`；新增时间谓词不得让 `river_ts_run_discovery_key_idx` 的使用越出 `run_key` 前缀以外的既定形态（以 EXPLAIN 为准）。
- 其它 `_segment_rows_source_sql()` 调用方的 SQL 不变。
- `hydro_run_latest_ready_run_idx`、`hydro_run_ops_strict_identity_candidates_idx`、pkey/unique 不动；已应用迁移文件字节不变。
- `display_ready_run`、QHH latest-product、display-coverage 选择器保持走索引（node-27 EXPLAIN）。

Governing invariant:
- 每个 `hydro_run` 索引定义在账本上只出现一次；latest-cycle 发现的结果只由"有行的最大 cycle_time"决定，成本不随 retention 之外的历史候选数增长。

Sibling surfaces:
- 读 `hydro_run` 候选的其它语句：QHH latest-product / display-coverage / `display_ready_run`（依赖保留的两个索引）；`scripts/node27_autopipeline.py` 的 display-product 查询。
- 探针同形：`_SEGMENT_ROWS_SOURCE_SQL`（事实行读取，本批不改），river_ts 模板注册表（`render_river_ts_sql` entry `forecast_store.latest_cycle_fact_probe`）。
- 账本/目录一致性：`tests/test_migrations.py` 的索引 pin、`schema-ledger-convergence` 规格、catalog diff（fresh vs production）。
- 写入方：`scripts/node27_ingest_run.py`、`services/orchestrator/chain_repository.py`、`workers/shud_runtime/runtime.py`（写 `end_time`，本批不改）。

Seams under test:
- `tests/test_migrations.py`（静态：000065 内容、DROP-before-CREATE、CONCURRENTLY、未改已应用文件）
- real-DB：fresh 数据库从零迁移后 `hydro_run` 索引集合；重跑 000065 安全（含人为 INVALID 残留）
- 形状 pin：`tests/test_latest_cycle_discovery_shape.py`、`tests/test_river_ts_template_golden.py`、`tests/test_river_ts_text_identity_cleanup.py`；`PsycopgForecastStore.forecast_series(issue_time="latest")` 行为（既有测试）

Required evidence:
- 本地：ruff、`tests/test_migrations.py`、`tests/test_latest_cycle_discovery_shape.py`、`tests/test_river_ts_template_golden.py`、`tests/test_river_ts_text_identity_cleanup.py`、forecast store/api 相关测试；新形状测试对 master 源码变红。
- node-27 disposable DB（同集群 scratch DB，非生产 `nhms`）：从零迁移 + 000065 重跑安全（INVALID 残留场景）+ 相关 integration 文件。
- node-27 scratch DB 候选索引列序 EXPLAIN 对比表。
- node-27 生产只读 `nhms_display_ro`：440-pin 等价回归（master 对 branch worktree）→ 0 mismatch；空结果 pin max ≤ 5000；非空 pin max/p95 不高于 3301/2530。harness 与输出放 `receipts/`。
- CI：SQL Migration Dry Run 绿（非 draft PR）。
- **生产 apply（用户确认后）**：`pg_indexes` 实测新索引 `indexdef` 与迁移一致、3 个重复索引消失；D1 语句（IFS pin、GFS+IFS pin）`EXPLAIN (ANALYZE, BUFFERS)` 显示 `cand` 走新索引，shared hit 相对 1386/1464 基线不回退；`display_ready_run` / QHH selector 仍走索引。

Non-goals:
- D1 的 status 语义、`_scenario_filter` 析取形态、D11 常量、#2417 statement 3、#2516 narrow membership 探针。
- parser 侧强制 valid_time 窗口（立 follow-up）。
- 部署 display API 新代码到 node-27（等价回归走 worktree，不重启服务）。

Review focus:
1. LATERAL + 时间界与 EXISTS 语义等价（I1），外层排序/围栏未变。
2. 迁移重跑安全（INVALID 残留）与"不改已应用文件"。
3. 保留/删除的索引名选择与所有引用面一致（代码、测试、docstring、spec）。
4. D11 预算：新形态在空/非空 pin 上的 shared hit 证据真实、可复现。
5. 时间界语义前提的证据与契约归属写清楚。

# Tasks: river-ts-consumer-identity-migration (#1442)

## 1. Implementation（idiom 与逐组辅助裁定见 design D1-D4；锚点 @ 2988fa95）

- [x] 1.1 A `packages/common/forecast_store.py` 谓词面：九处查询块
      （:418-423/:449-454/:488-493/:524-530/:579-587/:623-628/:663-668/
      :702-707/:1717-1737）切键+枚举主谓词；辅助按 D1 表（八块：
      `river_network_version_id`+`variable`；A9：三个 scan 绑定参数；
      `scan_basin_version_id` 消失），逐处带 `remove with #1342`。
- [x] 1.2 A 输出面：`rt.unit`（:524/:578/:622/:701）→ `rt.unit_e::text`；
      `:575/:619/:698` 的 `rt.river_network_version_id` 输出改权威表还原；
      A9 SELECT 链（:1718-1721 四文本列 + :1747-1778 下游 GROUP BY）整体切键、
      rollup 还原（范本 `display_coverage.py:392+` 孪生实现及其配套断言）。
- [x] 1.3 A **四销钉**同批重钉：`_qhh_latest_query_indexes()`（:3554-3596，钉
      000051 `river_ts_selected_identity_key_valid_time_idx` 形状，注释更新
      实测出处指向 E4(ii) receipt）+ `tests/test_forecast_api.py:1766-1793`；
      `scripts/node27_timeseries_compression_live_evidence.py:2788-2798`；
      `tests/test_qhh_latest_fallback_pushdown.py:154-172`；
      `tests/test_migrations.py:626-632`（对函数源切片断言旧索引名与
      covered 状态字符串——复审新发现的第四销钉）。
      实现期由全量 pytest 追加发现**第五/第六销钉**（曲线绑定从 8 位增到 10
      位所致，偏离记录已声明）：`scripts/node27_timeseries_compression_
      benchmark.py::_curve_query_and_binding` 的 `names` 列表（自校验
      `len(names)==len(parameters)==count("%s")`）与
      `tests/test_node27_timeseries_compression_benchmark.py:180-207`
      的 `%s` 计数 / `parameter_names` / `bound_parameters` 三处断言；
      `schemas/examples/timeseries_compression_live_evidence.example.json`
      的样例 binding 同步补两位（合成样例，仅防文档漂移）。
- [x] 1.4 B `services/tile_publisher/publisher.py`：PG 腿聚合切键（辅助仅
      `variable`）、`:349` → `COUNT(DISTINCT (r.river_network_version_key,
      r.river_segment_key))`、STRING_AGG → 枚举 `::text`、`:345/:361` 键分组 +
      还原（layer_id 不变）；sqlite 腿方言等价构造（D3b）。
- [x] 1.5 B 测试骨架：`tests/test_tile_publisher.py` sqlite 表扩
      键/枚举列，种子数据双写（D3b）。
- [x] 1.6 C `forcing_copyback_backfill.py:69-75`（辅助仅 `variable`）+
      D `scripts/node27_autopipeline.py`（:909-921、:1116-1125，纯键无辅助）。
- [x] 1.7 E：`db/seeds/seed_demo.py:1047/:1050`、
      `scripts/summarize_qhh_smoke_results.py:29-40`、
      `scripts/reset_qhh_smoke_db.py:45`、`tests/integration_helpers.py:419`
      切键，无辅助。
- [x] 1.8 F `workers/output_parser/parser.py` **三处**（:840-845 探针、
      :853-859 取窗、:890-897 DELETE）按 D4 键定位，无辅助，窗不动；
      `tests/test_timescale_write_guard_wired.py` :319/:349 重钉。
- [x] 1.9 清零 oracle `tests/test_river_ts_text_identity_cleanup.py`（D5：
      渲染 SQL 断言 + 裸列面定向断言，仅在册文件），含反用例。

## 2. Tests（requirement-driven）

- [x] 2.1 A/B/C/D/F 渲染 SQL 形状断言：键谓词在场、禁列缺席、辅助 ⊆ 该组
      允许集且带标记、join 身份无文本等值（spec Scenario "经 join 到达的身份
      不得携带文本 fact join"）。
- [x] 2.2 oracle 反用例：禁列谓词 / 无标记辅助 / 文本 fact join / 裸列面回退
      各一 → 红。
- [x] 2.3 B 双方言：PG 腿元组计数与 sqlite 腿等价构造各有断言；layer_id 与
      segment_count 对同一夹具数据前后一致。
- [x] 2.4 F：三处语句参数元组断言；同窗重放幂等用例（mock/夹具）。
- [x] 2.5 既有套件全绿：E1 全部文件。

## 3. 交叉审查修复（design D10；PR #1655 Phase 5 清单）

- [x] 3.1 (P1) fast path 内联 CTE 补 `h.run_key` / `bv.basin_version_key` /
      `rnv.river_network_version_key` 三列，恢复 fast/fallback 逐字段等价。
- [x] 3.2 (P2) `_require_backfill_schema` 列集随查询走（rt 补
      `run_key`/`variable_e`，h 补 `run_key`）+ `BACKFILL_SCHEMA_MISSING`
      负路径测试。
- [x] 3.3 (P2) parser `ON CONFLICT DO UPDATE` SET 补四个身份键列。
- [x] 3.4 (P1) `select_ci_tests.py` 九个被守护文件 at-site 追加清零 oracle
      （integration_helpers 属 #1487 切口记录不改）+ pins 更新。
- [x] 3.5 (P2) oracle 邻接不变量（辅助与键对应物同合取式；marker 相邻）
      + 反用例；M10/M30/F6-detach 突变红证。
- [x] 3.6 (P2) oracle 语句普查（在册文件新增 `hydro.river_timeseries` 语句
      必须入册）；N1/N2 突变红证。搭车 F4：parser INSERT 列清单断言收紧、
      F5：scan fold-away 恢复 verbatim 形状钉。

## Evidence Floor

- [x] E1 `uv run pytest -q tests/test_forecast_api.py
      tests/test_qhh_latest_fallback_pushdown.py tests/test_tile_publisher.py
      tests/test_forcing_copyback_backfill.py
      tests/test_node27_autopipeline_preflight.py tests/test_seed.py
      tests/test_output_parser_dual_write.py
      tests/test_timescale_write_guard_wired.py
      tests/test_node27_timeseries_compression_live_evidence.py
      tests/test_river_ts_dual_write_integration.py
      tests/test_river_ts_text_identity_cleanup.py
      tests/test_river_ts_read_path_surrogate_keys.py
      tests/test_display_coverage_refresh.py tests/test_sql_shape_helpers.py
      tests/test_migrations.py` PASS（实现期追加两个受影响文件：
      `tests/test_select_ci_tests.py`、
      `tests/test_node27_timeseries_compression_benchmark.py`；
      1093 passed / 8 skipped）
- [x] E2 `uv run ruff check .` PASS
- [x] E3 `openspec validate river-ts-consumer-identity-migration --strict
      --no-interactive` PASS
- [ ] E4 **硬门**，node-27 实机：
      (0) **preflight**：未压缩 chunk 七键 `IS NULL` 计数为 0 的直连 SQL
      receipt（design D7，F 组合并硬前提；不满足先补 sweep）；
      (i) **before 取样**：pull 新码前对选定键收敛 run 跑曲线端点 +
      latest-product 存档，pull 后同 run 重跑，逐字段 diff 相等（含 unit
      非空）；
      (ii) A 代表性查询与 B 聚合 `EXPLAIN (ANALYZE, BUFFERS)` 前后对比无劣化
      （含压缩腿观察；同批产出 `_qhh_latest_query_indexes()` 重钉的实测依据）；
      (iii) autopipeline 一趟真实 tick 全绿（D 组判据在产）；
      (iv) MVT + `/`+`/ops` e2e 不回归；B 组同批 run 新旧 segment_count 比对
      一致。
- [x] E5 清零判据以 oracle 形式落地并全绿（取代 issue 两条 grep——勘查+首审共
      证六处假阴性，差异在 PR 偏离记录声明）；
      `tests/integration_helpers.py:419` 无 valid_time 界隐患已立单 #1654
      （#1640 同失败类但边界只覆盖同文件 :428 的 met 语句，故另立；本单不修，
      仅把该语句身份谓词切到 run_key）。

# Proposal: river-ts-consumer-identity-migration

## Why

Issue #1442（#1342 硬阻塞，epic #1336）：#1340/#1341 两份 In Scope 清单相加没有
覆盖全部 `hydro.river_timeseries` 消费面，六组 out-of-boundary 消费面仍按旧文本
身份列过滤/连接/聚合/输出。#1342 的删列**不可逆**，落地即断：预报曲线端点、
latest-product、tile publisher 的 q_down 发布发现、copyback run 发现、node-27
autopipeline 每 tick 判据、seed/smoke 校验、以及 ingest 主写路径的 replace 全链
（fixture 首审补充：parser 的存在性探针与取窗 CTE 比 DELETE **更早**崩）。勘查
确认（2026-08-20 @ master `2988fa95`）：A–F 六组全部未被后续 PR 迁移。

## What Changes

统一采用 #1341 idiom；文本过渡辅助按 design D1 的逐组裁定表保留（仅字面量绑定
+ 压缩可达两条件同时成立时），经权威表 join 到达的身份保持 key-join only（基线
spec:195 禁令）：

1. **A `packages/common/forecast_store.py`**：九处查询块切键+枚举主谓词（八块
   辅助仅 `river_network_version_id`+`variable`；A9 辅助为 scan 绑定参数三列，
   `scan_basin_version_id` 消失）；**输出侧同 scope**——`rt.unit` →
   `rt.unit_e::text`，`:575/:619/:698` 的 `rt.river_network_version_id` 输出与
   A9 的 SELECT/GROUP BY 文本身份链整体切键、rollup 处权威表还原（孪生范本
   `packages/common/display_coverage.py:392+`）；同批重钉**四销钉**：
   `_qhh_latest_query_indexes()`（钉到 000051 的
   `river_ts_selected_identity_key_valid_time_idx` 形状，实测依据 = E4(ii)
   EXPLAIN receipt）+ `tests/test_forecast_api.py:1766-1793`、
   `scripts/node27_timeseries_compression_live_evidence.py:2788-2798`、
   `tests/test_qhh_latest_fallback_pushdown.py:154-172`、
   `tests/test_migrations.py:626-632`（函数源切片断言旧索引名——第四销钉）。
2. **B `services/tile_publisher/publisher.py`**：发现聚合切键（辅助仅
   `variable`）；PG 腿 `||` 拼接计数 → 键行元组 DISTINCT，`STRING_AGG` →
   枚举 `::text`；`:345/:361` 的 network 文本 SELECT/GROUP BY 改键分组 + 权威表
   还原（layer_id 拼装值不变）；**sqlite 测试腿**按 design D3b：测试骨架扩键/
   枚举列 + 方言等价构造。
3. **C `services/tile_publisher/forcing_copyback_backfill.py:69-75`**：EXISTS
   探针 `rt.run_key = h.run_key`（辅助仅 `variable`，无 run_id 文本 join）。
4. **D `scripts/node27_autopipeline.py`**（:909-921、:1116-1125）：纯键连接，
   无辅助（最高运维影响面）。
5. **E seed/smoke**：`db/seeds/seed_demo.py:1047/:1050`、
   `scripts/summarize_qhh_smoke_results.py:29-40`（`count(DISTINCT
   river_segment_id)`→`river_segment_key`）、`scripts/reset_qhh_smoke_db.py:45`、
   `tests/integration_helpers.py:419` 切键，无辅助（无压缩库）。
6. **F `workers/output_parser/parser.py` 全部三处**（:840-845 探针、:853-859
   取窗 CTE、:890-897 DELETE）按键定位，无辅助，valid_time 窗不变（design D4）；
   `tests/test_timescale_write_guard_wired.py` :319/:349 断言重钉。合并前提：
   node-27 键收敛 preflight receipt（design D7）。
7. **清零 oracle**：`tests/test_river_ts_text_identity_cleanup.py`——渲染 SQL
   断言（别名限定面）+ 逐调用点定向断言（裸列/片段面），范围仅本单在册文件
   （design D5；取代 issue 两条 grep——勘查+首审共证其六处假阴性）。

## Non-Goals

- 删列迁移本身与索引重建策略（#1342）。
- #1341 已认领的四个 display 文件与 `services/production_closure/`（含
  `scale_validation.py:144` 静态 plan_lines）；其过渡辅助与成组注释风格不动，
  由既有 oracle 看护。
- `hydro.river_timeseries` 之外的表（`tests/integration_helpers.py:428` met 表
  无界 DELETE 属 #1640）。
- `tests/integration_helpers.py:419` hydro DELETE 无 valid_time 界——与 #1640
  同失败类的隐患，**报告立单不在本单修**（压缩落地前 inert）。

## Impact

- Affected specs: `river-identity-normalization`（ADDED 两条 requirement：
  out-of-boundary 消费面键化；parser replace 全链键定位）。
- Affected code: `packages/common/forecast_store.py`、
  `services/tile_publisher/{publisher.py,forcing_copyback_backfill.py}`、
  `scripts/{node27_autopipeline.py,summarize_qhh_smoke_results.py,reset_qhh_smoke_db.py,node27_timeseries_compression_live_evidence.py,node27_timeseries_compression_benchmark.py,select_ci_tests.py}`、
  `db/seeds/seed_demo.py`、`workers/output_parser/parser.py`、
  `schemas/examples/timeseries_compression_live_evidence.example.json`、
  `tests/{integration_helpers.py,test_forecast_api.py,test_qhh_latest_fallback_pushdown.py,test_timescale_write_guard_wired.py,test_migrations.py,test_tile_publisher.py(骨架扩列),test_forcing_copyback_backfill.py(骨架扩列),test_node27_timeseries_compression_benchmark.py,test_select_ci_tests.py,test_sql_shape_helpers.py}`、
  新增 `tests/test_river_ts_text_identity_cleanup.py`。
- 部署面：node-27 git pull 生效；无迁移、无 timer 改动。

# Tasks: national-identity-probe-lateral-pushdown (#1596)

## 1. Implementation（形态与裁定见 design D1-D3；锚点 @ c3027446）

- [x] 1.1 身份发现取**内联 4 列子查询**（design D2 定稿形态：
      `h.run_key, rnv.river_network_version_key, h.run_id,
      mi.river_network_version_id`，与 `latest_runs` 同门控形状）。共享
      CTE 引用不可行——`latest_runs` 嵌在 `source_rows` 内层 WITH
      （mvt.py:597），词法上对本 CTE 不可见；不要做"装配顺序"核验（顶层
      WITH 链顺序核验会假阳性通过，运行期才 `relation does not exist`）。
- [x] 1.2 `services/tiles/mvt.py:592-628` 探针重塑为 D2 形态：
      `latest_runs lr CROSS JOIN LATERAL (SELECT 1 FROM hydro.river_timeseries
      ts WHERE 键对 + 受批辅助(run_id/network，带 remove with #1342 marker 且
      与键同合取式) + variable/variable_e/valid_time LIMIT 1) LIMIT 1`；
      变量解析用 enum_range+unnest 形态（design D2；`(:variable)::…` 直转
      被 :198-231 禁转钉红）；CTE 名 `source_identity_stats` 不变；输出列
      `source_identity_count` 语义 0/1 不变。
- [x] 1.3 承重钉重钉：`tests/test_river_ts_read_path_surrogate_keys.py:432-441`
      `test_national_identity_probe_uses_the_same_key_shape_as_the_data_legs`
      改断言新 LATERAL 形态 + 辅助集 `{run_id, river_network_version_id,
      variable}`（**不含** river_segment_id——比数据腿窄，负断言钉住）；
      同文件 `:438` 的 2 列 SELECT 字符串钉（`SELECT ts.run_id,
      ts.river_network_version_id`）按 4 列新形态重钉；slicer 锚
      （:100-101、`_integration.py:176`）核验存活。
- [x] 1.4 424 语义 oracle：新增
      `tests/test_mvt_national_identity_probe_integration.py`（integration
      marker）三分支——无 coverage → 424；**内部空洞**（窗覆盖、时次无行）
      → 424；有数据 → 200 + 非空 MVT。防真空与造数前提照 design D4 注记：
      `monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")` + 断言
      424 details 含 z/x/y 而非 required_env；run_type='forecast'、
      met.forcing_version 行在场、端点两时刻写满 expected_segment_count
      （display_coverage.py:80-118、:439-456 口径）；三用例不同 valid_time
      避 tile cache。

## 2. Tests（requirement-driven）

- [x] 2.1 形状断言：display 面实际机制是
      `tests/test_river_ts_read_path_surrogate_keys.py:263-273` 的
      NATIONAL_LATERAL_PROBE_PREDICATES **整条合取式子串钉**（辅助与键
      写死在同一钉里）+ `assert_text_fact_columns` 列集普查——为身份探针
      体新增同款合取式钉与列集登记；体外无 ts 引用、EXISTS 包装保留；
      反用例（辅助失键伴随 / 加 segment 辅助 / 体外 text fact join）→ 红。
- [ ] 2.2 D4 三分支 integration oracle 全绿（node-27 real DB）。
- [x] 2.3 既有套件全绿：mvt.py 的 select_ci_tests 选集 +
      `tests/test_sql_shape_helpers.py`。

## Evidence Floor

- [x] E1 `uv run pytest -q tests/test_river_ts_read_path_surrogate_keys.py
      tests/test_sql_shape_helpers.py tests/test_api_contract.py
      tests/test_display_publish_status_only.py tests/test_migrations.py
      tests/test_openapi_drift.py tests/test_hydro_display_mvt_scaling.py
      tests/test_node27_mvt_prewarm.py` PASS
- [x] E2 `uv run ruff check .` PASS
- [x] E3 `openspec validate national-identity-probe-lateral-pushdown --strict
      --no-interactive` PASS
- [ ] E4 **硬门**，node-27 实机（EXPLAIN 协议：安静库、warm 二采取第二采、
      BUFFERS/相关节点 loops/Rows Removed 主证（PG15.2 无 Batches 字段，
      round-3 D2）、**shipped SQL 直采**——不用 issue 提的仓外
      shape_explain.py 脚手架；before/after 同一安静会话内成对重采，不引用
      issue 里的历史数字）：
      (0) preflight receipt：逐 pin 落所触 chunk `is_compressed`、键 NULL
      计数（chunk 32/51 键全 NULL——误选退化成空对空取证）、覆盖该时次的
      run_display_coverage 行在场性、所触 chunk reltuples/last_analyze；
      (i) 压缩有覆盖 pin（**z4 12/6 @ 2026-08-12T12Z**，chunk 55——先回填
      后压缩，字节比对非空有意义；retention 若已推进按同判据另选并记录；
      preflight 附该时次逐身份命中/缺席向量，round-2 K2）：before 整片
      解压 → after 亚秒、tile 字节相同、fact 侧内层相关节点 loops =
      前导 miss 数 + 1（首序候选命中时 loops=1；混合则按 (ii-b) BUFFERS
      口径兜底；round-3 D2 换掉 batches 单位）、且**无条件**落账压缩
      chunk 关系 Shared Hit+Read ≤ 同会话 before（比值进 receipt——
      round-4 rider：闭合 delta THEN covered 分支与 E4 oracle 的错配）；
      (ii) 压缩无覆盖 pin（preflight 实查选定：落压缩 chunk 且无任何
      coverage 窗覆盖的时次，**不得与 (i) 同时次**——探针无 tile 坐标依赖，
      同时次任意 z/x/y 同答案，原 z9 407/200 pin 与 (i) 同为命中，round-1
      审查 C1 更正）：before 数十秒 → after <1s 空响应（零 fact 触达）；
      (ii-b) 压缩内部空洞 miss pin（窗覆盖、该时次零行——取窗内非整点时刻
      如 2026-08-12T12:30Z，preflight 验证零行）：424 语义保持、计划为逐
      身份参数化探针非整片解压、**定量主证 BUFFERS**——after 压缩 chunk
      关系 Shared Hit+Read ≤ 同会话 before，比值落 receipt（round-2 K1：
      PG15.2 无 Batches 字段，wall time 仅记录不判据）；
      (iii) 未压缩 pin（当批时次任一 z4）：毫秒级不退化、tile 字节相同；
      (iii-b) 未压缩内部空洞 miss pin（当前未压缩批内窗覆盖非整点时刻，
      round-2 K3）：计划与耗时落账，索引取舍（文本 PK vs 000051）据实
      记录；
      (iv) z4 national 端到端压缩时次进秒级（#1341 AC-1 口径）；
      (v) integration oracle（D4 三分支）在 node-27 真实 DB 全绿；
      receipt（preflight + before/after 计划 + 计时 + 字节比对）随 PR 附出。
- [ ] E5 特批扩宽随 PR 偏离记录呈报用户复核（D3 协议）。

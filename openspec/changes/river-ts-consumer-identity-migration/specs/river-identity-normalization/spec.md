# river-identity-normalization

## ADDED Requirements

### Requirement: Out-of-boundary river_timeseries consumers SHALL filter and emit identity by surrogate keys with per-group sanctioned transitional aids

`packages/common/forecast_store.py`（九处查询块，含 A9 fallback 的整条 CTE 链）、`services/tile_publisher/publisher.py`、`services/tile_publisher/forcing_copyback_backfill.py`、`scripts/node27_autopipeline.py`（ingest 判据与 publish 回填 EXISTS）、`db/seeds/seed_demo.py`、`scripts/summarize_qhh_smoke_results.py`、`scripts/reset_qhh_smoke_db.py`、`tests/integration_helpers.py` 对 `hydro.river_timeseries` 的过滤、连接、聚合**与身份输出**（SELECT/GROUP BY 中的文本身份列）MUST 以代理键（`run_key`/`basin_version_key`/`river_network_version_key`/`river_segment_key`）与枚举（`variable_e`/`unit_e`/`quality_flag_e`）为主形态；对外仍需文本处 MUST 从权威表 join 还原或枚举 `::text` 还原，payload 对**键收敛行**逐字段等价（NULL-key 遗留行对键过滤不可见——继承本 capability 已记录的有期限排除契约，不视为数据丢失）。

文本谓词 MUST 限于受批过渡下推辅助列（`run_id`/`river_network_version_id`/`variable`），且仅当 (a) 该身份以字面量/绑定参数形态出现（经权威表 join 到达的身份保持 key-join only，文本 fact join 在受批探针体外禁止）且 (b) 查询可达压缩 chunk 时保留，带 `remove with #1342` 标记；`basin_version_id`/`river_segment_id` 文本谓词 MUST 清零。生产（PostgreSQL）路径的 segment 计数 MUST 用键的行元组 DISTINCT；`publisher.py` 的 sqlite 测试路径 MUST 用键基的方言等价构造（整型拼接计数、直取枚举列），语义一致。

清零 MUST 由 `tests/test_river_ts_text_identity_cleanup.py` 看护：别名限定面用渲染 SQL 断言（复用 `tests/test_sql_shape_helpers.py` 机制），裸列/片段面用逐调用点定向断言；范围仅本单在册文件（display 面由既有 oracle 看护；`db/migrations/**` 与 `scripts/node27_river_identity_backfill.py` 按定义读文本列，不在册）。

#### Scenario: 曲线端点响应对键收敛 run 逐字段等价

- **GIVEN** 一个键收敛的 run（未压缩 chunk 七键 NULL 计数为 0 的 preflight 已过）
  在切换前后各取一次预报曲线端点响应
- **WHEN** 逐字段 diff
- **THEN** 全部字段相等，`unit` 字段非空且与切换前相同

#### Scenario: publisher 发布发现计数与 layer_id 不变

- **GIVEN** 同一批键收敛 run 的 q_down 发布发现聚合
- **WHEN** 以键元组计数替代文本拼接计数、以键分组 + 权威表还原替代文本分组
- **THEN** `segment_count` 与切换前一致，`layer_id` 拼装值不变

#### Scenario: 经 join 到达的身份不得携带文本 fact join

- **GIVEN** 任一在册文件把 `rt.run_id = h.run_id` 类文本等值加进事实表 join
- **WHEN** 运行清零 oracle
- **THEN** 测试失败（该形态不属于受批辅助——辅助只允许字面量/绑定参数绑定）

#### Scenario: 禁列谓词与无标记辅助被 oracle 拒绝

- **GIVEN** 在册文件的渲染 SQL 中出现 `basin_version_id`/`river_segment_id`
  文本谓词，或受批辅助行缺 `remove with #1342` 标记
- **WHEN** 运行 `tests/test_river_ts_text_identity_cleanup.py`
- **THEN** 测试失败并指出调用点

#### Scenario: 裸列面同受看护

- **GIVEN** `scripts/reset_qhh_smoke_db.py` 的 `_delete()` WHERE 片段或
  `tests/integration_helpers.py` 的 IN 谓词被改回文本身份列
- **WHEN** 运行清零 oracle 的定向断言
- **THEN** 测试失败

### Requirement: The parser's river_timeseries replace chain SHALL locate rows by surrogate keys end to end

`workers/output_parser/parser.py` 对 `hydro.river_timeseries` 的**全部三处**文本谓词——存在性探针（:840-845）、`WITH existing AS MATERIALIZED` 取窗（:853-859）、replace DELETE（:890-897）——MUST 以 `run_key`/`river_network_version_key`/`variable_e` 定位（无文本辅助：`check_batch_targets_uncompressed` 保证目标未压缩，辅助无下推收益且 DELETE 侧只会收窄漏删面），valid_time 窗谓词不变；replace 语义（同 run + 同 network + 同 variable + 窗内先删后插的幂等重放）与取窗对压缩守卫判据的输入 MUST 保持。合并前 MUST 有 node-27 键收敛 preflight receipt（未压缩 chunk 七键 NULL 计数为 0）。`tests/test_timescale_write_guard_wired.py` 的 DELETE 参数断言 MUST 与新谓词形状一致重钉。

#### Scenario: 同窗重放幂等

- **GIVEN** 同一 run 的同一 valid_time 窗被 parser 重放（preflight 已证键收敛）
- **WHEN** replace 链按键定位执行
- **THEN** 取窗结果与文本定位一致，窗内旧行删净、新行插入，行数与重放前一致

#### Scenario: 三处谓词无一遗漏

- **GIVEN** parser 模块源中的三条 `hydro.river_timeseries` 语句
- **WHEN** 运行清零 oracle 对 parser 的断言
- **THEN** 探针、取窗、DELETE 三处均为键谓词，无文本身份列

#### Scenario: 写守卫参数形状被钉住

- **GIVEN** DELETE 语句的参数元组
- **WHEN** 运行 `tests/test_timescale_write_guard_wired.py`
- **THEN** 断言的参数形状与实现一致（键 + 窗界，无文本列）

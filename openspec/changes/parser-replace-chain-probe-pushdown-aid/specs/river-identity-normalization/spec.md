## MODIFIED Requirements

### Requirement: The parser's river_timeseries replace chain SHALL locate rows by surrogate keys end to end

`workers/output_parser/parser.py` 对 `hydro.river_timeseries` 的**全部三处**谓词——存在性探针、`WITH existing AS MATERIALIZED` 取窗、replace DELETE——MUST 以 `run_key`/`river_network_version_key`/`variable_e` 定位，valid_time 窗谓词不变；replace 语义（同 run + 同 network + 同 variable + 窗内先删后插的幂等重放）与取窗对压缩守卫判据的输入 MUST 保持。

探针与取窗**按设计不带 valid_time 约束**（职责是找出该 key 窗外的既有行以拓宽 DELETE 窗），因此可达压缩 chunk；`run_key` 非 segmentby 列、压缩侧无索引，对新 run 的 key-only 探针会整段解压扫描（node-27 2026-08-21 EXPLAIN receipt：`DecompressChunk → Seq Scan on compress_hyper_*`，60 s `statement_timeout` 失败，新 cycle 零入库）。故这两处 MUST 额外携带受批过渡下推辅助 `run_id = %s`——它满足受批辅助的两个保留条件：身份以绑定参数形态出现（`replacement_key[0]`，非经权威表 join 到达），且查询可达压缩 chunk；辅助 MUST 紧跟其键对应物 `run_key`（二者之间恰一个 `AND`）并紧邻 `remove with #1342` 标记。该辅助不收窄结果集：`run_id` 与 `run_key` 经 `hydro_run`（`run_id` 主键、`run_key` IDENTITY UNIQUE）双射，本仓全部写入方（parser INSERT 同上下文 dual-write、#1339 回填按 `hydro_run` join 赋键）成对写入，故对本仓写入的行辅助是空操作过滤——此为 writer-enforced 不变量而非 schema 约束（fact 表 `run_key` 无 FK），外部写入方的漂移仍由 000050 审计兜底。DELETE MUST 保持纯键（窗界 + `check_batch_targets_uncompressed` 保证目标 chunk 未压缩，000051 键索引可用，辅助无计划收益）——"守卫保证目标未压缩"的论证仅适用于受窗界约束的 DELETE。合并前 MUST 有 node-27 键收敛 preflight receipt（未压缩 chunk 七键 NULL 计数为 0）。`tests/test_timescale_write_guard_wired.py` 的 DELETE 参数断言 MUST 与谓词形状一致。

#### Scenario: 同窗重放幂等

- **GIVEN** 同一 run 的同一 valid_time 窗被 parser 重放（preflight 已证键收敛）
- **WHEN** replace 链按键定位执行
- **THEN** 取窗结果与文本定位一致，窗内旧行删净、新行插入，行数与重放前一致

#### Scenario: 三处谓词无一遗漏

- **GIVEN** parser 模块源中的三条 `hydro.river_timeseries` 读/删语句
- **WHEN** 运行清零 oracle 对 parser 的断言
- **THEN** 探针、取窗、DELETE 三处均为键谓词；探针与取窗各带且仅带 `run_id` 受批辅助（有标记、与 `run_key` 同合取式），DELETE 无任何文本身份列与标记

#### Scenario: 新 run 的探针在压缩 chunk 上走 segmentby 索引

- **GIVEN** 一个含已压缩 chunk 的 `hydro.river_timeseries` 与一个尚无事实行的新 run
- **WHEN** 以生产源抽取的探针 SQL、在 `enable_seqscan = off` 下执行 `EXPLAIN`
- **THEN** 压缩 chunk 上的计划使用 `compress_hyper_*` 的 `run_id` 索引，不出现 `Seq Scan on compress_hyper`，且同一测试内去掉 `run_id` 辅助行的阴性对照出现该 Seq Scan（无可用索引）

#### Scenario: 写守卫参数形状被钉住

- **GIVEN** DELETE 语句的参数元组
- **WHEN** 运行 `tests/test_timescale_write_guard_wired.py`
- **THEN** 断言的参数形状与实现一致（键 + 窗界，无文本列）

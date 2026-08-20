# Design: river-ts-consumer-identity-migration

## 风险三元组

- 级别：**expanded**——触生产写路径（parser replace 全链路）、node-27 cron 判据
  （坏了每 tick 崩）、对外 payload（曲线端点 unit 字段、latest-product 证据面、
  tile layer_id）；无 API 契约变更、无迁移。
- must-preserve：(1) 对外响应逐字段等价（**限键收敛行**——NULL-key 遗留行的排除
  是基线 spec 记过的有期限契约，见 D8）；(2) replace 语义：同 run + 同 network +
  同 variable + valid_time 窗，先删后插的幂等重放不变；(3)
  `timescale_write_guard` 的 fail-closed 行为与
  `check_batch_targets_uncompressed` 判据输入不失真；(4)
  `_qhh_latest_query_indexes()` 对外 metadata 与实际查询形状一致；(5) 执行计划
  不劣化（node-27 EXPLAIN 前后对比，警惕 #1378 统计病与 #1596 压缩腿整片解压）；
  (6) 基线 spec:195 的禁令：**经权威表 join 到达事实表的身份保持 key-join only，
  文本 fact join 在受批探针体外一律禁止**。
- seams under test：A 的九处查询渲染函数；B 的 `_discover_qdown_runs` 双方言
  SQL；F 的探针/取窗/DELETE 语句常量；清零 oracle 的断言函数（复用
  `tests/test_sql_shape_helpers.py` 机制）。

## D1: 全量采用 #1341 idiom；辅助列按"身份如何到达事实表"逐组裁定

主谓词一律代理键 + 枚举。**受批文本过渡辅助**（`SANCTIONED_TEXT_PUSHDOWN_COLUMNS`
= run_id / river_network_version_id / variable，带 `remove with #1342` 标记）
只在两个条件同时成立时保留：(a) 该身份以**字面量/绑定参数**形态出现（不是经
权威表 join 到达——文本 fact join 被基线 spec:195 与 #1341 用户裁定禁止）；
(b) 该查询计划**可达压缩 chunk**（辅助的唯一作用是 segmentby 下推）。逐组裁定：

| 组 | 身份来源 | 保留的文本辅助 |
|---|---|---|
| A :418-707 八块 | basin/segment/network 字面量；run 经 `h` join | `river_network_version_id` + `variable`（run 无字面量可绑，**不加** run_id） |
| A :1717-1737 (A9) | `cr` join + `scan_*` 绑定参数 | `scan_run_id` / `scan_river_network_version_id` / `variable`；`scan_basin_version_id` 消失 |
| B publisher | run 经 `h` join；`'q_down'` 字面量 | 仅 `variable` |
| C copyback EXISTS | run 经 `h` join；`'q_down'` 字面量 | 仅 `variable` |
| D autopipeline 两处 | run 经 `h` join | **无**（纯键连接） |
| E seed/smoke/helpers | 各库无压缩 chunk | **无** |
| F parser 三处（见 D4） | 绑定参数 | **无**（守卫保证目标未压缩） |

`basin_version_id` / `river_segment_id` 文本谓词全部清零（`river_segment_id`
的 LATERAL 特批仅限 #1341 的 mvt.py 两探针体，不外扩）。文本入参需要解键时用
标量子查询（`apps/api/routes/hydro_display.py:749-789`）。

## D2: 输出侧文本列同 scope（不只是谓词）

- `rt.unit` → `rt.unit_e::text AS unit`（quality_flag 同理）。000050 枚举 label
  逐字等于文本值（migration :126-152），且 unit/quality_flag 列 NOT NULL
  （000006 :53-54），cast 后 payload 逐字节等价。
- `forecast_store.py:575/:619/:698` 的 `rt.river_network_version_id` 输出与
  **A9 的 SELECT 链**（:1718-1721 直接 SELECT 四个文本身份列、:1747-1778 下游
  CTE 按其 GROUP BY）：整条 CTE 链切键、在 rollup 处从权威表 join 还原文本。
  **孪生范本**：`packages/common/display_coverage.py:392+` 的同形 `river_sample_rows`
  已完成同样的迁移（配套断言
  `test_coverage_river_scan_groups_by_keys_and_reconstructs_text_at_the_rollup`）。
- `publisher.py:345/:361` 的 `r.river_network_version_id` SELECT/GROUP BY：改按
  `r.river_network_version_key` 分组、从 `core.river_network_version` join 还原
  文本供 `layer_id = q_down_{run_id}_{network}` 拼装，拼装值不变。

## D3: publisher 计数改行元组 DISTINCT（PG 腿）

`COUNT(DISTINCT r.river_network_version_id || '::' || r.river_segment_id)` →
`COUNT(DISTINCT (r.river_network_version_key, r.river_segment_key))`。语义等价
注记：拼接版对任一侧 NULL 的行不计，元组版对 `(NULL,NULL)` 计 1——依赖"七键
同批写、全有或全无"（双写与回填均整行写键），该前提由 D7 preflight 复验。

## D3b: B 组 sqlite 腿的方言对策（publisher 是双方言模块）

`publisher.py:312` 按 `is_sqlite` 分支，测试骨架 `tests/test_tile_publisher.py`
建的是 sqlite 表。sqlite 不支持 `COUNT(DISTINCT (a,b))`（实测 "row value
misused"）也不支持 `::text`。对策：

- 测试骨架扩列：sqlite 的 `river_timeseries`/`hydro_run` 表加
  `run_key`/`river_network_version_key`/`river_segment_key`/`unit_e`/
  `quality_flag_e` 列，种子数据双写（骨架是本单一半工作量，独立 task）。
- sqlite 分支用方言等价构造：计数
  `COUNT(DISTINCT r.river_network_version_key || ':' || r.river_segment_key)`
  （整型拼接无碰撞）；unit/quality_flag 直接聚合 `unit_e` 列（sqlite 测试表里
  即文本存储）。spec delta 的 MUST 措辞相应限定："生产（PostgreSQL）路径 MUST
  行元组计数/枚举 cast；sqlite 测试路径 MUST 键基等价构造"。

## D4: F 组全链路（不只 DELETE）——键定位，无文本辅助

parser 对 `hydro.river_timeseries` 共**三处**文本谓词，同批切键：

1. `:840-845` 存在性探针（`run_id`/`river_network_version_id`/`variable`）；
2. `:853-859` `WITH existing AS MATERIALIZED` 取窗（同三列）——它是
   `check_batch_targets_uncompressed` 的输入，漏行会同时失真守卫判据与 DELETE
   覆盖面；
3. `:890-897` replace DELETE（同三列 + valid_time 窗）。

三处全部改 `run_key = %s AND river_network_version_key = %s AND variable_e = %s`
+ 原 valid_time 窗不动，**不加文本辅助**：`check_batch_targets_uncompressed`
（:876-883）保证目标 chunk 未压缩，辅助无下推收益；DELETE 谓词是收窄方向，
少一个条件少一分漏删面。键值 parser 已解出（value_rows :911-914），零新增解析。
`tests/test_timescale_write_guard_wired.py` :319/:349 参数断言重钉。
**数据前提复验**见 D7——不是"当既成事实"，是合并前的 preflight 硬门。

## D5: 清零 oracle——渲染 SQL 断言 + 裸列面定向断言，不做源文本扫描

初稿的"三引号源扫描"被首审证伪（seed/helpers/reset/publisher :320 都不在三引号
块里，会假阴性；A9 巨型语句块的派生 CTE 裸列会假阳性）。改为：

- **别名限定面**（A 九处、B、C、D、F）：复用既有断言机制——helper
  `_assert_text_fact_columns(sql, alias, expected, label, allowed=...)` 现居
  `tests/test_river_ts_read_path_surrogate_keys.py:104`（私有），实现时提升进
  `tests/test_sql_shape_helpers.py` 共享（该模块已导出 `text_fact_columns`/
  `outer_predicates`/列集常量）——对**渲染后 SQL** 按调用点断言——禁列
  （`basin_version_id`/`river_segment_id`/…）缺席、受批辅助 ⊆ D1 表中该组的
  允许集、辅助行带标记。
- **裸列/片段面**（summarize、seed_demo、reset 的 `_delete()` WHERE 片段、
  helpers 的 IN 谓词、publisher `:320` 的 where_clauses 元素）：逐调用点定向
  断言其字符串常量含键列名、不含文本身份列名。
- **范围**：仅 #1442 在册文件。display 四文件由既有
  `test_river_ts_read_path_surrogate_keys.py`/`test_sql_shape_helpers.py`
  oracle 看护，不重复、不冲突（避免对 #1341 成组注释风格的误报）。
- oracle 自带反用例：注入禁列谓词 → 红。

## D6: 为什么一次切全（对 issue 备选方案的裁定）

采纳 issue 推荐：A–F 一次切完。#1342 是不可逆删列，任何残留文本谓词都把回滚
窗口交给运气；切换机械同构，分批只是把 node-27 EXPLAIN 复核做两遍。备选
（先 A+D+F）被否：迫使 #1342 的"不改应用代码"边界放宽，把删列与代码改造混进
同一个不可逆变更。

## D7: E4 preflight——键收敛复验（F 的合并前硬前提）

基线 spec 明确回填从不触碰活跃 chunk（收口靠 final sweep），而 parser 的
replace 窗恰落在活跃 chunk；写侧回滚还可能产生新 sentinel 行（基线
spec:183-191 容许）。因此 E4 增加 preflight：node-27 上未压缩 chunk
`run_key IS NULL`（及其余六键）计数为 0 的直连 SQL receipt，作为 F 组合并的
硬前提；不满足则先补 sweep 再合并。

## D8: NULL-key 排除契约随 delta 记账

基线 spec:218-250 已为 in-boundary 读者记录"NULL-key 遗留行对键过滤不可见，
是有期限契约不是数据丢失"。本 delta 的等价 Scenario 继承同一限定：快照对比的
run 必须键收敛（E4(i) 前置条件），delta 正文补同款排除条款——否则挑到含
sentinel 行的老 run 会把既定契约误判为回归。

## D9: 测试策略

- mock/单测：A/B/F 渲染 SQL 形状断言（D5 oracle）；B 双方言各自的聚合断言；
  F 三处语句的参数元组；三销钉重钉一致性；oracle 反用例。
- `_qhh_latest_query_indexes()` 重钉到 000051 的
  `river_ts_selected_identity_key_valid_time_idx` 形状，其实测依据由 E4(ii)
  的 EXPLAIN receipt 提供（沿用 #1338 两份 receipt 的注释惯例更新出处）。
- node-27 真实 DB（E4 硬门）：见 tasks E4。

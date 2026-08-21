# Design（#1681）

## 风险分级

expanded：生产 p0（新预报零入库）；改动面小但触及 #1442 规格的逐组裁定与清零
oracle；真库 + 压缩 chunk 才能复现；node-27 live receipt 是唯一终判。

选用风险包：data-correctness（replace 语义必须逐字保持）、spec-conformance（#1442
规格 (a)/(b) 条件与 marker/同合取式/普查三维接线）、live-ops。未选：security。

## 必须保持

- replace 语义：同 run + 同 network + 同 variable，窗 = incoming ∪ existing，
  `check_batch_targets_uncompressed` 输入（窗界）不变，DELETE/INSERT 语句与参数逐字不变。
- `tests/test_timescale_write_guard_wired.py` 的 DELETE 参数断言
  `(7, 8, "q_down", t0, t2)` 不动；`_RecordingCursor` 子串分派仍命中两条读语句。
- `tests/test_output_parser_dual_write.py` 的语句序列分类不变（探针/取窗仍以
  `select 1 from hydro.river_timeseries` / `select min(valid_time)` 前缀识别）。
- 清零 oracle 三维接线：辅助与 `run_key` 同合取式；marker 紧邻；普查 4 不变；
  `scripts/select_ci_tests.py` 对 `workers/output_parser/parser.py` 的规则已选中
  该 oracle（不改 selector）。
- `_replacement_key_bindings` 返回三元组不变（DELETE 以 `*` 展开复用它）。

## 决策

### D1 两条读语句加 `run_id` 绑定辅助，DELETE 不加

```sql
SELECT 1 FROM hydro.river_timeseries
WHERE run_key = %s
  -- transitional compressed-chunk pushdown aid, remove with #1342
  AND run_id = %s
  AND river_network_version_key = %s
  AND variable_e = %s
LIMIT 1
```
取窗的 `existing` CTE 同形。辅助**紧跟其键对应物**（`run_key` 与 `run_id` 之间恰一个
`AND`），与全仓 `assert_aid_is_conjoined_with_its_counterpart` 的机械判据同形。绑定顺序
随之为 `(run_key, run_id, river_network_version_key, variable)`：新增
`_replacement_read_bindings(replacement_key, run_key, river_network_version_key)` 返回该
四元组，仅探针/取窗调用；`_replacement_key_bindings` 三元组不动，DELETE 继续复用。
marker 文本必须逐字等于 `tests/test_river_ts_text_identity_cleanup.py` 的
`PUSHDOWN_AID_MARKER`，置于辅助行正上方（相邻规则：同一行或上一行）。

**辅助不会收窄结果（与 α 的区别）**：`river_timeseries.run_id` NOT NULL 且是 PK 成员
（000006:44,55）；`hydro_run.run_key` 是 `GENERATED ALWAYS AS IDENTITY UNIQUE`
（000050:178），run_id ↔ run_key 双射；dual-write 在同一行同一上下文写两列
（parser.py ~:937-962）；`parse_run` 单 run（~:225-250），batch 内 `replacement_key[0]`
恒等于该 run。因此 `run_key = K AND run_id ≠ text(K)` 的行不可表示，辅助是空操作过滤，
只改变计划不改变结果集；α 收窄的是 valid_time（真实行会落在窗外），二者性质不同。

为什么是 `run_id` 而不是三列文本：segmentby 是 `(run_id, river_network_version_id,
river_segment_id)`，压缩侧索引前导列是 `run_id`；`river_network_version_id` 作为第二
列对单 run 的剪枝收益可忽略（一个 run 只属一个 network），YAGNI。

### D2 规格 delta 形态

MODIFIED 要求 "The parser's river_timeseries replace chain SHALL locate rows by
surrogate keys end to end"：三处键定位不变；新增"探针与取窗 MUST 携带 `run_id` 受批
辅助（条件内联写明：身份以绑定参数形态出现；查询无 valid_time 约束故可达压缩 chunk），
DELETE MUST 无辅助"；保留的"node-27 键收敛 preflight receipt"是 #1442 已满足的继承
条款，E5 落账；
场景"三处谓词无一遗漏"改写为"探针/取窗键谓词 + 受批辅助，DELETE 纯键"；新增场景
"新 run 的探针计划在压缩 chunk 上走 segmentby 索引"。D1 表对组 F 的"守卫保证目标
未压缩"理由在 delta 中明示其适用边界（仅 DELETE）。

### D3 清零 oracle 断言拆分

`test_parser_probe_window_and_delete_all_locate_rows_by_key` 拆成两段：
- 探针/取窗：三键谓词仍在；裸列面逐调用点定向断言（规格 :338 允许）：
  (1) 行级相邻——存在连续三行 `run_key = %s` / `PUSHDOWN_AID_MARKER` / `AND run_id = %s`
  （正则跨行匹配，空白任意），即"恰一个 AND 隔开键与辅助 + marker 紧邻"一次钉死；
  (2) 除 `run_id` 外无其它文本身份谓词（对 `river_network_version_id`/`variable`/
  `basin_version_id`/`river_segment_id` 逐列跑 `_assert_no_text_identity_predicate` 的
  单列形态，或等价正则）；(3) `run_id` 恰出现一次。
- DELETE：原断言原样（零文本、零 marker）。
先改断言、跑红（当前代码无 `run_id`）、再改 SQL 跑绿——anti-vacuity。

### D4 真库测试（`pytest.mark.integration`，node-27 throwaway DB）

在 `tests/test_river_ts_dual_write_integration.py` 内新增（不开新文件，避免 selector
规则与 `test_select_ci_tests.py` 期望元组改动）：
1. 种子 authority + 一批 `normalized=True` 事实行（旧 run），`compress_chunk` 其 chunk；
   再注册一个**新 run**（`hydro_run` 行，`run_key` 由 IDENTITY 生成，0 事实行）。
2. **从生产源抽取**探针 SQL（`from tests.test_river_ts_text_identity_cleanup import
   _parser_river_statements`，取 `[0]`；不得手抄 SQL，否则 oracle 与生产文本脱钩），
   `SET LOCAL enable_seqscan = off`（throwaway 库的压缩 chunk 只有 1 页，成本模型对
   有索引的情形也可能选 Seq Scan；关掉顺扫后"有索引 → 走索引 / 无索引 → 仍 Seq Scan"
   才是稳定判别）后 `EXPLAIN (COSTS OFF)`：断言**含** `Index Scan using compress_hyper_`
   与 `Index Cond: (run_id =`、**不含** `Seq Scan on compress_hyper`。**同一测试内阴性
   对照**：从抽取的 SQL 中删去 marker 行与 `AND run_id = %s` 行、按三元绑定再 EXPLAIN，
   断言出现 `Seq Scan on compress_hyper`（无可用索引）——每次运行都自证判别力，替代
   一次性的手工回滚红证。
3. 端到端：`PsycopgOutputParserRepository(database_url=...)`（`statement_timeout_ms`
   是可注入 dataclass 字段但本 rig 上 1 页 chunk 不会超时，不做超时红腿）对新 run 的
   valid_time 窗（落在未压缩 chunk）`upsert_river_timeseries` 成功、行数正确；同一窗
   重放幂等（行数不变）。
4. 守卫闭合不变（保留：全仓尚无真库级守卫证据，单元级在
   `tests/test_timescale_write_guard_wired.py:287`）：新 run 的窗若落进压缩 chunk，仍以
   守卫异常失败——证明 α 担心的"静默残留"未被引入。

### D5 live receipt（node-27，堆叠部署）

生产树 `/home/nwm/NWM` 检出本分支（含 #1674 修复），下一 tick：34 个
`fcst_{gfs,ifs}_2026082012` run `outcome` 非 failed、`hydro_run` 翻为
`parsed`/`published`，`done rc=0`，elapsed 分钟级；再一 tick rc=0（无新 run 时
~4 min）。同批 tick 作为 PR #1676 的 E3 rc=0 补证。

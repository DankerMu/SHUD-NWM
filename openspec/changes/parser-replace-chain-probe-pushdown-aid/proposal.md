# parser-replace-chain-probe-pushdown-aid（#1681，#1442 回归）

## Why

#1442 把 `workers/output_parser/parser.py::upsert_river_timeseries` 的三处
`hydro.river_timeseries` 谓词切成纯键形（`run_key`/`river_network_version_key`/
`variable_e`），其 D1 对 parser 组（F）的裁定是"无文本辅助：守卫保证目标未压缩"。
该论证只对 **DELETE** 成立（valid_time 窗界 + `check_batch_targets_uncompressed`
保证目标 chunk 未压缩、键索引可用）；**存在性探针**与 **`WITH existing AS
MATERIALIZED` 取窗**按设计不带 valid_time 约束（它们的职责就是找出该 key 在窗外的
既有行以拓宽 DELETE 窗），因此必然触达压缩 chunk。`run_key` 不是 segmentby 列、压
缩侧无索引，node-27 RO EXPLAIN：key-only 探针对三个压缩 chunk 走
`DecompressChunk → Seq Scan on compress_hyper_*`；对一个**新 run** 探针要证明
"不存在"，于是整段解压扫描 → 60 s `statement_timeout` → parse 失败。

生产后果：自 #1442 部署后，新到 cycle `fcst_{gfs,ifs}_2026082012`（17 流域 × 2）
每 tick 34 个 run 全部 parse 超时，`hydro_run` 状态 `failed`，**没有任何新预报入库**。

## What Changes

- 探针与取窗两条语句加入受批过渡下推辅助 `AND run_id = %s`（绑定参数 =
  `replacement_key[0]`，满足规格 (a) 字面量/绑定参数形态、(b) 可达压缩 chunk），
  与 `run_key` 同合取式，紧邻 `remove with #1342` 单行 marker；DELETE / INSERT
  **逐字不动**（DELETE 受窗界 + 守卫约束，键索引足够）。
- `_replacement_key_bindings` 签名不动（DELETE 继续 `*` 展开复用）；两条读语句改用新的
  四元绑定 helper（顺序 `run_key, run_id, river_network_version_key, variable`，与 SQL
  合取式顺序一致——辅助紧跟其键对应物，满足"同合取式"的房规机械化形态）。
- 清零 oracle（`tests/test_river_ts_text_identity_cleanup.py`）parser 断言拆分：
  探针/取窗 = 键谓词 + `run_id` 受批辅助（有 marker、同合取式），DELETE 仍零文本、
  零 marker；普查计数 4 不变。
- 真库测试（`tests/test_river_ts_dual_write_integration.py`）：含压缩 chunk 的 throwaway DB
  上，从生产源 AST 抽取探针 SQL，`SET LOCAL enable_seqscan = off` 后 `EXPLAIN`：含压缩侧
  `run_id` 索引、不含 `Seq Scan on compress_hyper`；**同一测试内**去掉辅助行作为阴性对照
  （无可用索引 → 仍 Seq Scan）。并驱动 `PsycopgOutputParserRepository.upsert_river_timeseries`
  对新 run 在压缩 chunk 存在时成功写入 + 同窗重放幂等 + 守卫闭合不变。
- 规格 delta：MODIFIED parser replace-chain 要求——"全部三处无文本辅助"改为
  "探针/取窗携带 `run_id` 受批辅助，DELETE 纯键"；理由以 EXPLAIN receipt 落账。

## Non-goals

- 以 valid_time 窗界收窄探针/取窗（方案 α）：改变 replace 语义——key 在窗外的既有行
  会从"守卫闭合失败"变成"静默残留"。拒绝。
- 调大 parser `statement_timeout`（方案 γ）：每 run 每 tick 整段解压，不是修复。
- #1342 删列后的键形后继索引/压缩布局：归 #1342（本辅助带 marker，届时随删）。
- 08-16/08-19 的低频 parse 失败（6-52/tick，#1442 之前）：另行表征，不混入。

## Impact

- 代码：`workers/output_parser/parser.py` 两条 SQL + 两处绑定；测试两文件；规格一处。
- 运维：node-27 部署后下一 tick 34 个 `2026082012` run 应 parse 成功并 publish，
  tick 回到 rc=0 / 分钟级。堆叠在 PR #1676（#1674）分支之上，同批 tick 同时为两个
  PR 提供 rc=0 receipt。

# Proposal: 删除 INVOCATION_ARGV 死岛——测试停止盖没人查的 provenance 章（issue #1240）

## Why

`scripts/node27_timeseries_compression_live_evidence.py:377-445` 的三个符号
（`INVOCATION_ARGV`、`_TIMEOUT_PREFIX`、`_invocation_execution_identity`）自
#1069（supervisor-owned execution lane 取代 pgaudit lane）起就是传递性死代码，
#1239 删掉最后的生产消费者 `_validate_invocation_record` 后成为 production-dead
island：生产侧互相引用、对外零入口，唯一外部引用是测试 fixture `_invocation()`
（`tests/...:247-264`）。

两层危害（第二层是重点）：
1. **漂移的启动契约手抄本**：`INVOCATION_ARGV["migration_apply"]` 相对可执行名、
   缺 `--dbname nhms`/`--no-psqlrc`、相对 SQL 路径；`compression_dry_run` 带
   `"<receipt-path>"` 字面占位符 + 相对首元素——接回活契约会被 `_concrete_argv`
   的 placeholder/absolute 检查直接拒绝。漂移已客观发生且无机制发现。
2. **伪 provenance oracle**：fixture 把 argv + 六个 resolved 字段 +
   `artifact_bindings` 写进 `*-invocation.json`，而 verifier 对这些文件只做
   ref 存在性检查、内容从不 parse（`test_legacy_authored_invocations_do_not_
   contribute_to_v3_truth` 自证 exit_code=1/timeout=901 照样 PASS）；生产
   bundle（bundle_author）里这些槽全部由 ledger 再导出，根本不存在带 argv 的
   invocation 文件。产物长得像 oracle、实际只有 `{path,sha256,bytes}` 约束——
   #1069 G6..G14 / #1086 "伪信任边界早晚被人接回主路径" 同族复发源。

## What Changes

- **方案裁决：A（删岛 + 精简 fixture）**；B（把 launcher 身份接回 ledger lane）
  被否——它与 #1261 已记录的裁定直接冲突（launcher/interpreter 身份是
  producer-side hardening、非 verifier gate，见 live_evidence 锚定注释的
  capability consequence 段），且成本 M+（schema+bundle_author+supervisor+
  capture+node-27 实机重跑）只为给死代码续命。理由详 design.md D1。
- `scripts/node27_timeseries_compression_live_evidence.py`：删除 `:377-445`
  三符号（含 `_TIMEOUT_PREFIX` 的 frozen-wall 注释——它只服务死岛自身）。
- `tests/test_node27_timeseries_compression_live_evidence.py`：`_invocation()`
  精简为 verifier 实际约束的最小形状（可算 `{path,sha256,bytes}` ref 的 JSON
  内容），删除 argv/resolved_*/artifact_bindings 字段及两处 receipt 变异测试的
  `artifact_bindings.receipt_sha256` 重盖行（`:2796`、`:3282` 附近）；
  `test_legacy_authored_invocations_do_not_contribute_to_v3_truth` 保留且负向
  语义不变（"内容不是真相"的活文档）。

## Impact

- Affected specs: `hypertable-compression`（1 条 ADDED requirement，与既有
  aace0913 orphan-validator requirement 同式：grep-zero 场景）
- Affected code: 上述两文件；无兄弟副本（符号全仓唯一，2026-08-15 复核仍成立）
- Not affected（non-goals）: #1086/#1239 已删 validator 不回引；
  `database_audit_proof` 两处 `{"const": false}` 钉不动；
  `_validate_exact_command_argv`/`_concrete_argv` 活契约不削弱不放松；
  #1090 capture 侧 RECORD/EXEC docker argv 分裂（另单）；#1351 落的
  `EXPECTED_TIMEOUT_SECONDS=900` 冻结钉（live_evidence 消费契约，非死岛）不动。
- node-27 实机：**零变更**（方案 A 无 bundle 契约变化）。

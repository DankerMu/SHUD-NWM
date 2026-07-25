# Tasks — receipt-cutover-gate-presence (#1144)

Fixture level: compact（S 规模：schema 2 处 required + runtime ~10 行 + 若干测试 + runbook 一段）。
Risk triage：无 DB/远端面；主要风险 (a) 双校验器语料漂移——schema 与
runtime 必须拒绝同一批缺块语料（本文件显式约定）；(b) 误伤合法早失败
receipt——锁竞争 / preimage 早失败路径合法省略该键，既有用例
`tests/test_scheduler_file_provider_refresh.py:5071` 是守卫；(c) 追加的
id-only refused pins 若钉错口径，会重演 #1140 round-1 的"复刻要修的
bug"失败模式——实现前必须先从写入侧推导 legal corpus（writer 在
declaration 失效时必置 `outcome="failed"`，id-only 合成 refusal 至多
1 条 `__declaration__` 且从不截断；legacy 无 mode arm 已拒绝一切
refused，保持不动）；(d) 升级路径——pre-#1132 legacy published
latest.json 在新条件下被 `validate_current_receipt` 拒绝，runbook 必须
写实证结论而非推测。Must-preserve：`RECEIPT_OPTIONAL_KEYS` 键集语义
（key-set 层面仍 optional，presence 由条件校验强制）、example receipt
有效性、所有既有测试零删除零弱化。Seams under test：
`_validate_cutover_gate_field` presence 分支、schema allOf、id-only
reconciliation refused pins、`validate_current_receipt` legacy 行为。
Not-selected packs：concurrency（无并发面）、performance。Migration 面
已选入（runbook note + legacy 行为 pin）。

## 1. Implementation

- [x] 1.1 `schemas/scheduler_file_provider_refresh_receipt.schema.json`：
      两条既有 `allOf` 分支（`outcome ∈ {published, dry_run}` 与三个
      registry-cutover refusal reason）的 `required` 从
      `["registry_classification"]` 改为
      `["registry_classification", "cutover_gate"]`。不新增分支。
- [x] 1.2 `scripts/scheduler_file_provider_refresh.py`
      `_validate_cutover_gate_field`：镜像
      `_validate_registry_classification_field` 的
      `requires_classification` 条件（`outcome in {"dry_run","published"}
      or reason in REGISTRY_CUTOVER_REFUSAL_REASONS`）；缺键且条件成立
      → raise `ValueError("receipt_cutover_gate_required")`（新 reason，
      不复用 `receipt_shape_invalid` / `receipt_cutover_gate_invalid`）；
      其余 outcome 缺键仍放行。更新 docstring（同一批语料约定）。若仓
      内存在校验错误 reason 的枚举/清单（grep 确认），同步登记。
- [x] 1.3 `_enforce_registry_classification_reconciliation` id-only
      **mode-keyed** arm 追加两钉（#1145 评审 DISCARD 残余，issue
      comment 授权入包）：(a) refused group `truncated == false` 且
      `total == len(items)` 且 `total <= 1` 且每条 `model_id ==
      "__declaration__"`（写入侧唯一可产出形态）；(b) `outcome ==
      "dry_run"` ⇒ `refused.total == 0`（declaration 失效必置
      outcome=failed，无合法违反者）。legacy 无-mode arm 逐字不动。
      实现前先核对写入侧构造点确认上述 corpus 推导成立，若发现反例
      （某路径合法产出违反形态）立即停手报偏离，不得硬钉。
- [x] 1.4 `docs/runbooks/current-production-ops.md`：**就地改写**既有
      「升级 pre-#1132 receipt」段（`:705-709`）与 enable checklist 相
      关条目（`:717-721`）——两处目前把缺 `.cutover_gate` 描述为需
      operator 判断的软信号，本 change 后它是 `validate_current_receipt`
      的硬拒绝（`emergency_record_invalid`），留旧文即自相矛盾（评审
      P2-3）。同时建与 #1143 共用的 migration 落点（命名中性如
      「receipt 契约升级/回滚兼容性」，可即为改写后的该段）：升级后
      pre-#1132 legacy published latest.json 不含 `cutover_gate`，
      `--enable` 校验步硬拒——处置为先跑一次**成功**（published）的
      manual refresh 重写 latest.json（refused/failed 的 refresh 也会
      重写但 `outcome != "published"` 仍被拒，评审 P3；refresh 写路径
      本身不受阻：receipt 排序走 lenient reader）；与 pre-#1080 段
      （`:696-703`，评审 P3 重锚定）风格一致并互相引用。结论必须以
      2.5 的 pytest 实证为准。

## 2. Tests (requirement-driven)

- [x] 2.1 presence 双侧红证（published + dry_run）：分别构造合法
      receipt 后整块删除 `cutover_gate` → schema
      `jsonschema.Draft202012Validator` INVALID 且 runtime
      `_validate_receipt` raise `receipt_cutover_gate_required`。红证：
      pre-change 双双通过（issue 隔离探针已复现，测试落地时重跑确认红
      的原因正确）。
- [x] 2.2 三个 registry-cutover refusal reason 各一例缺
      `cutover_gate` → 双侧拒绝（runtime reason 同 2.1）。
- [x] 2.3 合法省略面守卫：锁竞争等早失败 outcome 缺该键仍通过双侧；
      既有 `test_lock_contention_receipt_omits_cutover_gate`（:5071）零
      改动零回归；`schemas/examples/...example.json` 在新 schema 下仍
      VALID（既有 example-validation 用例覆盖则指认，不足则补断言）。
- [x] 2.4 id-only refused pins：(a) `items=[], total=500` /
      `truncated=true` / `total=2` 双条目 / `model_id="mdl-x"` 各一例
      → `receipt_classification_invalid`；(b) `outcome="dry_run"` 且
      refused 带 1 条合法 `__declaration__` 条目 →
      `receipt_classification_invalid`；(c) honest corpus 回归——
      `outcome="failed"` + mode=id_only + 1 条 `__declaration__`
      refusal 仍通过（守住「id-only constraints follow the mode onto
      failure outcomes」场景）。红证或 mutation probe：新增钉在
      pre-change 语料上必须可证非恒真。
- [x] 2.5 legacy 升级行为 pin（runbook oracle）：构造 pre-#1132 形态
      published receipt（无 `cutover_gate`，其余合法）写入
      latest.json → `validate_current_receipt` raise
      `RefreshError("emergency_record_invalid", phase="receipt")`。红证
      为**pre-merge 程序**（与 2.1 同式）：实现前先跑该用例确认
      pre-change 语义下同一 receipt 通过（用例红）、改后转绿；不得写
      成 post-change 自指断言（评审 note）。
- [x] 2.5b 既有测试 fixture 影响面（评审 P2-2）：runtime presence 条
      件将使 9 个既有用例转红——根因是测试 helper
      `_write_current_published_receipt`（`:473`）与
      `_receipt_with_classification`（`:6200`）构造的
      published/dry_run receipt 不带 `cutover_gate`。**唯一授权修法是
      更新这两个 helper（及同类构造点）补上合法 `cutover_gate` 块**
      ——属 fixture 修复，非断言弱化；禁止用收窄新 presence 条件的方
      式让它们转绿。9 个具名用例（
      `test_current_receipt_validation_rejects_untrusted_or_stale_evidence`、
      `..._worker_registry_generation_mismatch`、
      `test_receipt_schema_and_runtime_reject_same_expressible_negative_corpus`、
      `test_publish_primary_receipt_upgrades_over_pre_1080_latest`、
      `test_publish_primary_receipt_replaces_corrupt_latest[json-decode-error|unicode-decode-error]`、
      `test_receipt_schema_and_runtime_admit_the_same_classification_modes[dry_run_id_only|published_full|legacy_without_mode]`
      ）必须全绿，且各自原断言目标逐字保留；schema 侧 `required` 可能
      再多红同批用例，同法处理。
- [x] 2.6 `uv run pytest -q tests/test_scheduler_file_provider_refresh.py`
      全绿，零删除零弱化。

## 3. Verification (issue 验收标准)

- [x] 3.1 `openspec validate receipt-cutover-gate-presence --strict
      --no-interactive` 通过。
- [x] 3.2 `uv run pytest -q tests/test_scheduler_file_provider_refresh.py`
      全绿（附计数）。
- [x] 3.3 `uv run ruff check .` 通过。
- [x] 3.4 `uv run python -c "import json, jsonschema; ..."` 或既有
      schema 校验用例证明 example receipt 仍 VALID；CI
      `json-schema-validate` 门通过（merge 前由 CI 结果确认）。
- [x] 3.5 `npx markdownlint-cli2 docs/runbooks/current-production-ops.md`
      干净。
- [x] 3.6 scope 核查：diff 仅触及 Impact 列出的文件。

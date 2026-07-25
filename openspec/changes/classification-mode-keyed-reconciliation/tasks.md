# Tasks — classification-mode-keyed-reconciliation (#1140)

Fixture level: compact. Risk triage: 单文件校验器分支键切换 + 1 个 optional
字段；无 DB/远端面。主要风险：(a) legacy 回退丢失——`mode` 缺席的旧 receipt
（reconstruct/validate_current 路径读入）必须保持现行为，两个方向（dry_run
outcome 走宽松、其余走严格）都要钉；(b) exact key-set（`:1884`
`set(classification) != required`）漏改 → 所有新 receipt 全拒；(c) 假绿——
复现测试若不断言"receipt 实际落盘 + reason 为注入的真实原因"，退化实现
（吞异常）也能绿。Must-preserve：#1135 五条 dry_run 约束逐条不回退；
full 分支 R2-N1 等式与 bootstrap 对偶不弱化；`_classify_registry` 划分语义
不变。Seams under test：mode 写入点、分支选择器、legacy 回退、schema 契约、
落盘链路。Not-selected packs：concurrency/perf、migration 之外的兼容
（#1143 承接 rollback 方向）。

## 1. Implementation

- [ ] 1.1 写入侧：`_RegistryClassification`（`:2470`）加
      `mode: str = "full"`；`_classify_registry`（`:2560`）dry_run 早退
      路径置 `"id_only"`（在 `:2610` 一带、早退 return 之前）；
      `to_receipt()`（`:2493`）输出 `"mode": self.mode`。
- [ ] 1.2 `_validate_registry_classification_field`（`:1857`）：exact
      key-set（`:1884`）改为 required ∪ optional `{"mode"}` 的精确匹配
      （沿用 #1132 `RECEIPT_KEYS`/`RECEIPT_OPTIONAL_KEYS` 的差集模式）；
      `mode` present 时必须 ∈ `{"id_only", "full"}`，否则
      `receipt_classification_invalid`。
- [ ] 1.3 `_enforce_registry_classification_reconciliation`（`:1957`）：
      分支选择改为——`mode` present 时按 mode（`id_only` → 现 dry_run
      宽松分支全部约束原样；`full` → 现严格分支），absent 时按
      `outcome == "dry_run"` 回退（legacy 行为逐字保留）。交叉钉：
      `outcome == "dry_run"` ∧ `mode == "full"` 拒；
      `outcome == "published"` ∧ `mode == "id_only"` 拒（伪造组合）。
      docstring 的 governing invariants 段同步措辞。
- [ ] 1.4 schema：`registry_classification.properties` 增 `mode` enum
      `["id_only", "full"]`，**不进** `required`（legacy 兼容）；
      `schemas/examples/scheduler_file_provider_refresh_receipt.example.json`
      的 classification 块补 `"mode": "full"`。

## 2. Tests (requirement-driven)

- [ ] 2.1 复现主用例：previous canonical registry 含 1 个 prospective 缺席
      model（待移除）+ dry_run + gate 之后注入失败（monkeypatch
      `validate_catalog_bound_readiness_entries` 抛
      `RefreshError("provider_invalid")`）→ 断言
      `refresh_scheduler_file_providers` 不抛
      `RefreshError("primary_receipt_failed")`，返回 receipt
      `outcome == "failed"` 且 `reason == "provider_invalid"`（真实原因），
      `registry_classification.mode == "id_only"` 保留；**并断言落盘**：
      `receipts/history/<run_id>.json` 与 `latest.json` 存在且与返回值
      一致。红证：pre-change 该用例以 `primary_receipt_failed` 失败。
- [ ] 2.2 dry_run 成功路径回归：既有 dry_run 测试全绿 + 新落盘 receipt 的
      classification 带 `mode == "id_only"`；真实 publish 成功路径带
      `mode == "full"`。
- [ ] 2.3 legacy 回退双向钉：直接调 `_validate_registry_classification_field`
      喂无 `mode` 的 classification——(a) `outcome="dry_run"` + id-only
      形状 → 通过（现行为）；(b) `outcome="failed"` + id-only 形状 +
      previous 有 removal → 仍拒 `receipt_classification_invalid`
      （legacy 行为不悄然放宽——本修复只对带 mode 的新 receipt 生效）。
- [ ] 2.4 伪造组合钉：`outcome="dry_run"` ∧ `mode="full"` 拒；
      `outcome="published"` ∧ `mode="id_only"` 拒；`mode="bogus"` 拒。
      载荷用全合法 classification 块（防上游臂假绿，#1131 教训）。
- [ ] 2.5 防回退钉：(a) `mode="full"` + 篡改 `unchanged`/`removed` 的
      R2-N1 等式 negative 仍拒（真实 publish 失败 receipt 防篡改不弱化）；
      (b) #1135 五条 dry_run 约束在 `mode="id_only"` 分支下逐条仍拒
      （removed≠0、sha/count 配对破坏、bootstrap unchanged≠0、
      unchanged>prev_count、new_registry_sha256 非 null——既有参数化如
      覆盖则引用，不足则补）。
- [ ] 2.6 schema 契约：`mode` 合法值通过、非法值拒、缺席通过（legacy）；
      example 校验绿。
- [ ] 2.7 既有全量：`uv run pytest -q
      tests/test_scheduler_file_provider_refresh.py` 全绿，零删除零弱化。

## 3. Verification (issue Verification 字段)

- [ ] 3.1 `uv run pytest -q tests/test_scheduler_file_provider_refresh.py`
      全绿（附计数）。
- [ ] 3.2 `uv run ruff check .` 通过。
- [ ] 3.3 `openspec validate classification-mode-keyed-reconciliation
      --strict --no-interactive` 通过。
- [ ] 3.4 schema/example 同步（json-schema-validate CI gate 绿）。
- [ ] 3.5 scope 核查：diff 仅触及 Impact 列出的文件。

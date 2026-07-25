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

- [x] 1.1 写入侧：`_RegistryClassification`（`:2470`）加
      `mode: str = "full"`；`_classify_registry`（`:2560`）dry_run 早退
      路径置 `"id_only"`（在 `:2610` 一带、早退 return 之前）；
      `to_receipt()`（`:2493`）输出 `"mode": self.mode`。
- [x] 1.2 `_validate_registry_classification_field`（`:1856`）：exact
      key-set（`:1884`）改为 required ∪ optional `{"mode"}` 的精确匹配
      （沿用 #1132 `RECEIPT_KEYS`/`RECEIPT_OPTIONAL_KEYS` 的差集模式）；
      `mode` present 时必须 ∈ `{"id_only", "full"}`，否则
      `receipt_classification_invalid`。
- [x] 1.3 `_enforce_registry_classification_reconciliation`（`:1957`）：
      分支选择改为——`mode` present 时按 mode（`id_only` → 宽松分支；
      `full` → 现严格分支），absent 时按 `outcome == "dry_run"` 回退
      （legacy 行为逐字保留）。**id_only 分支的 refused 约束（评审 P1-1）**：
      `package_changed_total == 0` 且 `declared_total == 0` 照旧；`refused`
      允许非空，但每条 item 的 `reason` 必须是
      `registry_cutover_declaration_invalid`（写入侧 `:2813-2824` 在
      `_classify_registry` 之后不分 dry_run 地追加合成 `__declaration__`
      refusal——这是 dry_run 下唯一可达的 refusal；其余 reason 一律拒）。
      legacy outcome 回退分支保持现行 `refused_total != 0` 拒（行为逐字
      不变）。交叉钉：`outcome == "dry_run"` ∧ `mode == "full"` 拒；
      `outcome == "published"` ∧ `mode == "id_only"` 拒（伪造组合；
      `restored_previous`/`replace_uncertain`/`failed` + `full` 合法）。
      id_only 分支在 return 之前保留终末钉（评审 P2-1）：`reason ∈
      REGISTRY_CUTOVER_REFUSAL_REASONS` 时 `refused_total >= 1`（写入侧
      `:2813-2824` 置 refusal reason 与追加 refused 行是同一次动作，二者
      必须同在同灭——防"抹平 refused 桶"篡改在新 receipt 上失守）。
      docstring 的 governing invariants 段同步措辞。
- [x] 1.4 schema：`registry_classification.properties` 增 `mode` enum
      `["id_only", "full"]`，**不进** `required`（legacy 兼容）；
      `schemas/examples/scheduler_file_provider_refresh_receipt.example.json`
      的 classification 块补 `"mode": "full"`（example outcome=published）。
- [x] 1.5 `docs/runbooks/current-production-ops.md`：classification 段落
      （`:483-489` 桶清单一带）补 `mode` 语义（`id_only` 仅来自 dry_run、
      `full` 来自真实 publish）+ 一句"pre-#1140 receipt 无 `mode` 字段属
      正常，校验按 outcome 回退"（对齐既有 pre-#1132 过渡段风格）。

## 2. Tests (requirement-driven)

- [x] 2.1 复现主用例：previous canonical registry 含 1 个 prospective 缺席
      model（待移除）+ dry_run + gate 之后注入失败（monkeypatch
      `validate_catalog_bound_readiness_entries` 抛
      `RefreshError("provider_invalid")`；注意 `_stub_provider_pipeline`
      在 `:542` 把该函数打成 no-op——setattr 必须在 stub **之后**，否则
      假绿）→ 断言 `refresh_scheduler_file_providers` 不抛
      `RefreshError("primary_receipt_failed")`，返回 receipt
      `outcome == "failed"`、`reason == "provider_invalid"` 且
      `phase == "precommit"`（`provider_invalid` 是兜底折叠值，phase 双
      断言补判别力），`registry_classification.mode == "id_only"` 保留；
      **并断言落盘**：`receipts/history/<run_id>.json` 与 `latest.json`
      存在且与返回值一致（空 receipt root，无单调序干扰）。红证：
      pre-change 该用例以 `primary_receipt_failed` 失败。
- [x] 2.2 dry_run 成功路径回归：既有 dry_run 测试全绿 + 新落盘 receipt 的
      classification 带 `mode == "id_only"`；真实 publish 成功路径带
      `mode == "full"`。(c) 带 mode 的对照回归（评审 P2-2）：bootstrap
      （previous=None）+ outcome=failed + mode=id_only → 通过；previous
      无 removal + outcome=failed + mode=id_only → 通过；两者各补一个带
      `__declaration__` refusal（reason=registry_cutover_declaration_invalid）
      的变体 → 通过（P1-1 爆点）；id-only refused 带其它 reason → 拒；
      `mode="id_only"` + `reason="registry_cutover_declaration_invalid"`
      + `refused.total==0` → 拒（P2-1 终末钉不回退）。
      (d) 端到端 declaration 变体：dry_run + 坏 declaration 文件 →
      receipt 落盘、reason 保留 `registry_cutover_declaration_invalid`
      （红证：按未含 P1-1 修正的实现会以 `primary_receipt_failed` 失败）。
- [x] 2.3 legacy 回退双向钉：直接调 `_validate_registry_classification_field`
      喂**整份 receipt 字典**（签名 `:1856` 收 receipt 非 classification：
      `{"outcome": ..., "reason": ..., "registry_classification": {...}}`），
      classification 无 `mode`——(a) `outcome="dry_run"` + id-only 形状 →
      通过（现行为）；(b) `outcome="failed"` + id-only 形状 + previous 有
      removal → 仍拒 `receipt_classification_invalid`（legacy 行为不悄然
      放宽）；(b) 的 reason 避开 `REGISTRY_CUTOVER_REFUSAL_REASONS`，防
      `refused_total >= 1` 混淆臂。
- [x] 2.4 伪造组合钉：`outcome="dry_run"` ∧ `mode="full"` 拒；
      `outcome="published"` ∧ `mode="id_only"` 拒；`mode="bogus"` 拒。
      载荷用全合法 classification 块（防上游臂假绿，#1131 教训）。
- [x] 2.5 防回退钉：(a) `mode="full"` + 篡改 `unchanged`/`removed` 的
      R2-N1 等式 negative 仍拒（真实 publish 失败 receipt 防篡改不弱化）；
      (b) #1135 五条 dry_run 约束在 `mode="id_only"` 分支下逐条仍拒
      （removed≠0、sha/count 配对破坏、bootstrap unchanged≠0、
      unchanged>prev_count、new_registry_sha256 非 null——既有参数化如
      覆盖则引用，不足则补）。
- [x] 2.6 schema 契约：`mode` 合法值通过、非法值拒、缺席通过（legacy）；
      example 校验绿。
- [x] 2.7 既有全量：`uv run pytest -q
      tests/test_scheduler_file_provider_refresh.py` 全绿，零删除零弱化。

## 3. Verification (issue Verification 字段)

- [x] 3.1 `uv run pytest -q tests/test_scheduler_file_provider_refresh.py`
      全绿（附计数）。
- [x] 3.2 `uv run ruff check .` 通过。
- [x] 3.3 `openspec validate classification-mode-keyed-reconciliation
      --strict --no-interactive` 通过。
- [ ] 3.4 schema/example 同步（json-schema-validate CI gate 绿）。
- [x] 3.5 scope 核查：diff 仅触及 Impact 列出的文件。

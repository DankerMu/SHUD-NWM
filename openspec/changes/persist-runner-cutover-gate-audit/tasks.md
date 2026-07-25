# Tasks — persist-runner-cutover-gate-audit (#1132)

Fixture level: compact. Risk triage: 单一持久化产物新增 optional key + 3 处 stderr
载荷改走既有 normalizer；无 DB/远端面；主要风险是 (a) schema
`additionalProperties: false` 与 validator 的 exact-key 假设漏改导致新 key 被拒，
(b) 假绿——equality 断言对"合法字面量恰为 normalizer 不动点"不敏感（#1132 评论
里的 mutant 实证）。Must-preserve：既有 receipt 全部字段与失败路径行为；
`_provider_evidence` 白名单投影不变（runner 块不塞进 per-provider 证据）；
3 处 stderr payload 除 cutover_gate 块被 normalize 外其余字段逐字节不变。
Seams under test：receipt builder 的 cutover_gate 参数、schema 契约、CLI 失败
路径的 normalizer 调用点。Not-selected packs：concurrency/perf（纯序列化路径）、
migration（optional key 向后兼容）。

## 1. Implementation

- [ ] 1.1 `scripts/scheduler_file_provider_refresh.py`：receipt builder
      （`:1556-1598` 一带）新增可选参数 `cutover_gate=None`；非 None 时经
      `normalize_cutover_gate_audit`（从 `packages.scheduler.registry_audit`
      import，单一定义点）后作为 top-level optional key 持久化（沿用
      `registry_classification` 的 json deep-copy freeze 惯用法）。None 时
      key 缺席（不写 null）。
- [ ] 1.2 runner direct-grid 路径把已构造的 `runner_cutover_gate_audit`
      （`:807-817`）传给 receipt builder 的所有到达该路径的 receipt 构造点
      （成功与失败 receipt 一致处理：块已构造则携带）。未构造该块的路径
      （非 registry 刷新、gate 之前的早期失败）保持无 key。若 receipt
      validator（`_validate_receipt` 或等价物）持有 exact top-level key 集合，
      将 `cutover_gate` 加入 allowed（不进 required）。
- [ ] 1.3 `schemas/scheduler_file_provider_refresh_receipt.schema.json`：
      top-level `properties` 增 `cutover_gate` object——`mode` enum
      `["enforced","bypassed_allow_uncovered_cutover","not_wired"]`、
      `declaration_env` `["string","null"]`、`declaration_present` boolean；
      三字段 required、`additionalProperties: false`；不加入 top-level
      required。`schemas/examples/scheduler_file_provider_refresh_receipt.example.json`
      同步补一个合法块。
- [ ] 1.4 `scripts/publish_scheduler_file_registry.py`：`:1251/:1255/:1267`
      三处 stderr 失败载荷的 `"cutover_gate": cutover_gate_audit` 改为
      `"cutover_gate": normalize_cutover_gate_audit(cutover_gate_audit)`
      （或等价的单次预归一化局部变量）；载荷其余字段不动。

## 2. Tests (requirement-driven)

- [ ] 2.1 runner 级：`tests/test_scheduler_file_provider_refresh.py` 新增
      落盘 receipt 断言——direct-grid 刷新后读回 receipt JSON，
      `cutover_gate == {"mode": "enforced", "declaration_env":
      NHMS_REGISTRY_CUTOVER_DECLARATION_PATH 常量名, "declaration_present":
      <bool>}`；declaration_present True（env 指向真实文件）与 False（env
      缺席）两个变体都钉。红证：pre-change 断言以 KeyError/缺 key 失败。
- [ ] 2.2 缺席钉：未构造 gate 块的路径（如 gate 前失败或非 registry 刷新）
      receipt 无 `cutover_gate` key（`not in` 断言，防"写 null"回归）。
- [ ] 2.3 schema 契约：新增块通过 schema 校验（既有 schema-validation 测试
      机制内跑通）；恶意第四字段被 `additionalProperties: false` 拒绝的
      负向用例（若既有测试模式支持 per-block 负向，否则由 json-schema-validate
      CI gate 覆盖并在 PR body 说明）。
- [ ] 2.4 CLI wiring 钉（mutant-sensitive，#1132 评论验收建议）：
      `tests/test_publish_scheduler_file_registry.py` 中 monkeypatch CLI
      模块内的 `normalize_cutover_gate_audit` 返回哨兵块，触发三条失败路径
      之一（其余两条至少一条参数化覆盖），断言 stderr JSON 的
      `cutover_gate` 为哨兵——值相等断言对合法字面量不敏感，必须钉调用而
      非钉值。红证：pre-change 哨兵不出现（字面量直出）。
- [ ] 2.5 既有全量：`uv run pytest -q tests/test_scheduler_file_provider_refresh.py
      tests/test_publish_scheduler_file_registry.py tests/test_registry_audit.py`
      全绿，零删除零弱化。

## 3. Verification (Evidence Floor, issue Verification 字段)

- [ ] 3.1 上述三文件 pytest 全绿（附计数）。
- [ ] 3.2 `uv run ruff check .` 通过。
- [ ] 3.3 `openspec validate persist-runner-cutover-gate-audit --strict
      --no-interactive` 通过。
- [ ] 3.4 schema/example 同步（json-schema-validate CI gate 绿）。
- [ ] 3.5 scope 核查：diff 仅触及 Impact 列出的文件。

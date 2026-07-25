# Tasks — persist-runner-cutover-gate-audit (#1132)

Fixture level: compact. Risk triage: 单一持久化产物新增 optional key + 3 处 stderr
载荷改走既有 normalizer；无 DB/远端面；主要风险是 (a) receipt validator 的
exact allowed-set（`RECEIPT_KEYS`/`RECEIPT_OPTIONAL_KEYS`）漏改导致所有 receipt
落盘即 `receipt_shape_invalid`，(b) 假绿——equality 断言对"合法字面量恰为
normalizer 不动点"不敏感（#1132 评论里的 mutant 实证），CLI 与 runner 两侧都要
wiring 钉，(c) 早期失败路径 `runner_cutover_gate_audit` 未绑定 →
UnboundLocalError。Must-preserve：既有 receipt 全部字段与失败路径行为；
`_provider_evidence`（`:1601-1621`）白名单投影不变（runner 块不塞进
per-provider 证据）；3 处 stderr payload 除 cutover_gate 块被 normalize 外其余
字段逐字节不变。Seams under test：`_receipt` 的 cutover_gate 参数、
`_validate_receipt` 运行时校验、schema 契约、CLI 失败路径与 runner 落盘路径的
normalizer 调用点。Not-selected packs：concurrency/perf（纯序列化路径）、
migration（optional key 向后兼容，`_lenient_receipt_order` 宽松读不受影响）。

## 1. Implementation

- [x] 1.1 `scripts/scheduler_file_provider_refresh.py`：receipt builder
      `_receipt`（`:1555-1598`）新增可选参数 `cutover_gate=None`；非 None 时经
      `normalize_cutover_gate_audit`（module-level `from
      packages.scheduler.registry_audit import ...`，单一定义点）后作为
      top-level optional key 持久化（沿用 `registry_classification` 的 json
      deep-copy freeze 惯用法）。None 时 key 缺席（不写 null）。
- [x] 1.2 runner 接线（registry publish 路径——块在 `:811` 无条件构造，覆盖
      `:822` direct-grid 与 `:865` 全流域两个分支）：先在
      `registry_classification` 声明处附近（`:585-586`，try 之前）加
      `runner_cutover_gate_audit: dict[str, Any] | None = None`，`:811` 改为
      对其赋值；六个 `_receipt(...)` 构造点（`:597`、`:610`（闭包
      `rollback_receipt_if_needed`）、`:998`、`:1015`、`:1074`、`:1089`）
      一律透传该变量——None 即无 key，避免早期失败路径（锁竞争
      `already_running`、`provider_preimage_changed` 等）UnboundLocalError。
      receipt validator 的 allowed-set 是精确匹配（`:1645` 比对
      `RECEIPT_KEYS`/`RECEIPT_OPTIONAL_KEYS`）：必须把 `cutover_gate` 加进
      `RECEIPT_OPTIONAL_KEYS`（`:174`，不进 `RECEIPT_KEYS`），否则所有
      receipt 落盘即 `receipt_shape_invalid`。
- [x] 1.3 `schemas/scheduler_file_provider_refresh_receipt.schema.json`：
      top-level `properties` 增 `cutover_gate` object——`mode` enum
      `["enforced","bypassed_allow_uncovered_cutover","not_wired"]`、
      `declaration_env` `["string","null"]` 且 `"maxLength": 512`（对齐
      `MAX_STRING_LENGTH`）、`declaration_present` boolean；三字段 required、
      `additionalProperties: false`；不加入 top-level required。
      `schemas/examples/scheduler_file_provider_refresh_receipt.example.json`
      同步补一个合法块。
- [x] 1.4 `scripts/publish_scheduler_file_registry.py`：在 `:1206`/`:1216-1222`
      构造点之后、`try` 之前一次性 `cutover_gate_audit =
      normalize_cutover_gate_audit({...})`，`:1243` 成功摘要与
      `:1251/:1255/:1267` 三处 stderr 载荷共用该值（不在 except 处理器内调
      可抛异常的 normalizer）；载荷其余字段逐字节不变。
- [x] 1.5 `_validate_receipt`：`cutover_gate` 存在时校验 mode ∈
      `CUTOVER_GATE_MODES`、`declaration_env` str-or-null（≤512）、
      `declaration_present` 严格 bool、无第四字段，违者
      `raise ValueError("receipt_cutover_gate_invalid")`（沿用
      `_validate_registry_classification_field` 的 helper 惯用法），使
      `reconstruct_primary_receipt`（`:1142`）/`validate_current_receipt`
      （`:1182`）读入的不可信 receipt 与 schema 同拒同放。

## 2. Tests (requirement-driven)

- [x] 2.1 runner 级：`tests/test_scheduler_file_provider_refresh.py` 新增
      落盘 receipt 断言——registry publish 刷新（既有 `:3560+` 模式，stub
      `publish_all_basin_scheduler_registry`）后读回 receipt JSON，
      `cutover_gate == {"mode": "enforced", "declaration_env":
      NHMS_REGISTRY_CUTOVER_DECLARATION_PATH 常量名, "declaration_present":
      <bool>}`；declaration_present True（`:3605` setenv 指向真实文件）与
      False（`:3531` delenv）两个变体都钉。红证：pre-change 断言以
      KeyError/缺 key 失败。
- [x] 2.2 缺席钉：gate 构造前的早期失败路径（锁竞争 `already_running` /
      `provider_preimage_changed`）receipt 无 `cutover_gate` key（`not in`
      断言，防"写 null"回归）。
- [x] 2.3 契约负向：恶意第四字段 / 非法 mode 的 `cutover_gate` 块——
      (a) schema 校验拒绝（既有 schema-validation 测试机制内）；
      (b) 直接调 `refresh._validate_receipt(伪造 receipt)` 断言
      `receipt_cutover_gate_invalid`（`:1142-1144` 会把 ValueError 吞成
      `RefreshError("emergency_record_invalid")`，不能在 reconstruct 路径断
      内层字符串；沿用 `:3690+` 既有直调模式），并另钉
      `reconstruct_primary_receipt` 读该载荷时抛 `RefreshError` 且 reason
      为 `emergency_record_invalid`。
- [x] 2.4 CLI wiring 钉（mutant-sensitive，#1132 评论验收建议）：
      `tests/test_publish_scheduler_file_registry.py` 中 monkeypatch CLI
      模块的 `normalize_cutover_gate_audit` 返回**合法三字段哨兵块**
      `{"mode": "not_wired", "declaration_env": "SENTINEL_ENV",
      "declaration_present": False}`（非法块会被 services 侧未打桩的
      normalizer（`scheduler_file_providers.py:600`）拒掉改走另一条失败
      路径，pass-for-wrong-reason），失败触发源用与 cutover 无关的确定性
      错误（如不存在的 basins-root），断言 stderr JSON 的 `cutover_gate`
      为哨兵——值相等断言对合法字面量不敏感，必须钉调用而非钉值。三条
      stderr 路径至少参数化覆盖两条。红证：pre-change 哨兵不出现。
- [x] 2.5 既有全量：`uv run pytest -q tests/test_scheduler_file_provider_refresh.py
      tests/test_publish_scheduler_file_registry.py tests/test_registry_audit.py`
      全绿，零删除零弱化。
- [x] 2.6 runner wiring 钉（mutant-sensitive，与 2.4 对称）：monkeypatch
      `refresh.normalize_cutover_gate_audit` 返回上述合法哨兵块（须
      schema/`_validate_receipt` 合法，否则 `_publish_primary_receipt`
      拒写），跑一次 registry publish 刷新，断言落盘 receipt 的
      `cutover_gate` 等于该哨兵。红证：pre-change 无 key；"拷字面量不调
      normalizer"的变异实现也会红。

## 3. Verification (Evidence Floor, issue 验收标准)

- [x] 3.1 三文件 pytest 全绿（附计数）。
- [x] 3.2 `uv run ruff check .` 通过。
- [x] 3.3 `openspec validate persist-runner-cutover-gate-audit --strict
      --no-interactive` 通过。
- [ ] 3.4 schema/example 同步（json-schema-validate CI gate 绿）。
- [x] 3.5 scope 核查：diff 仅触及 Impact 列出的文件。

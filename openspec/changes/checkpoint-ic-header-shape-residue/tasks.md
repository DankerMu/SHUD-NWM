## 1. Implementation

- [ ] 1.1 A 面 `packages/common/state_cli.py` `_normalized_checkpoint_ic_file`（消费点
      `:262` 之前）：`cfg_ic_header_shape` 前置判据，不合法抛 `StateManagerError`
      （reason token 对齐 `:68-70` #1325 约定，机器可 grep），不产出 `.normalized`
- [ ] 1.2 B 面 `_checkpoint_header_minute`（消费点 `:317` 之前）：同判据，不合法返回
      `None` + logger warning（token 可 grep）；`_checkpoint_with_header_time` 零改动
- [ ] 1.3 `workers/model_registry/basins_discovery.py:438` `except OSError` 臂：
      记 quirk（`unreadable_required_file` 类）+ warning，`status` 落 `"partial"`；
      `missing_required_files` 语义不变。CHECKSUM_LIMIT_BYTES 静默分支按 proposal
      裁量（零成本顺手则同族 quirk，否则报告不改）
- [ ] 1.4 判据单一来源：`state_cli.py` 只 import `cfg_ic_header_shape`，不新增
      token 计数规则

## 2. Tests

- [ ] 2.1 A 面：2-token 拒发（无 `.normalized`、`StateManagerError`、reason token）；
      3-token 归一化行为逐字不变；4-token 兼容
- [ ] 2.2 B 面：2-token → `None`，`valid_time`/`lead_hours` 保持 manifest 声明
      （06:00 不再改写为 00:06）；3-token rekey 与 no-op 语义逐字不变；4-token 兼容
- [ ] 2.3 basins_discovery：unreadable-required-file（monkeypatch stat/sha256 抛
      OSError）→ quirk + warning + `status=="partial"`，且不进 `missing_required_files`
- [ ] 2.4 判据单一来源锁：`state_cli` 模块 AST/import 断言（形状判定只经
      `cfg_ic_header_shape`，无本地 len(tokens) 第二规则）
- [ ] 2.5 零回归：`tests/test_state_manager.py`（含 `:2204` 既有
      `_normalized_checkpoint_ic_file` 直调用例）与 `tests/test_basins_discovery.py`
      现有用例全绿

## 3. Verification

- [ ] 3.1 红证：2.1/2.2/2.3 新用例改动前红（记录改动前行为：A 面写出毒 `.normalized`、
      B 面 valid_time 被反推、checksum 面 status 假 valid）
- [ ] 3.2 uv run pytest -q tests/test_state_manager.py tests/test_basins_discovery.py
- [ ] 3.3 uv run ruff check packages workers tests
- [ ] 3.4 openspec validate checkpoint-ic-header-shape-residue --strict --no-interactive
- [ ] 3.5 merge 后 node-27 oracle receipt：3.2 两套件，记入 #1430

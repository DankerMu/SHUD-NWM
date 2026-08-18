## 1. Implementation

- [x] 1.1 A 面 `packages/common/state_cli.py` `_normalized_checkpoint_ic_file`：
      **仅当 minute_index 非 None** 时以 `cfg_ic_header_shape` 判形状，不合法抛
      `StateManagerError`（reason token 对齐 `:68-70` #1325 约定，机器可 grep），
      不产出 `.normalized`；minute_index None 类保持 `:263-266` 容忍分支逐字不变
- [x] 1.2 B 面 `_checkpoint_header_minute`：同判据同位置，不合法返回 `None` +
      module-level `logging.getLogger` warning（token 可 grep；沿
      `manifest_index.py:13` 包内惯例新增 logger）；`_checkpoint_with_header_time`
      零改动
- [x] 1.3 `workers/model_registry/basins_discovery.py:438` `except OSError` 臂
      三态化——**机制显式**：checksum walk 吐出独立 `unreadable_required_files`
      集合（镜像 `invalid_required_files` 的 `:227` 调用/`:253` quirk/`:280`
      payload key 三件套），`:256` status 表达式直接消费该集合落 `"partial"`，
      payload 带独立 key；另记 quirk + warning。`missing_required_files` 语义不变；
      不与 `_safe_resolve_under_root` None 的 unsafe-symlink 臂（`:431-436`）合并。
      CHECKSUM_LIMIT_BYTES 静默分支按 proposal 裁量
- [x] 1.4 判据单一来源：`state_cli.py` 只 import `cfg_ic_header_shape`，不新增
      token 计数规则

## 2. Tests

- [x] 2.1 A 面：2-token 拒发（无 `.normalized`、`StateManagerError`、reason token）；
      3-token 归一化行为逐字不变；4-token 兼容
- [x] 2.2 B 面：2-token → `None`，`valid_time`/`lead_hours` 保持 manifest 声明
      （06:00 不再改写为 00:06）；3-token rekey 与 no-op 语义逐字不变；4-token 兼容
- [x] 2.2b minute_index None 类（空/单 token/非数字尾）与 ≥5 数字 token 类：
      None 类 A/B 两面保持今日容忍行为逐字不变；≥5 类落拒绝集（A 抛/B None）
- [x] 2.3 basins_discovery：unreadable-required-file（monkeypatch stat/sha256 抛
      OSError）→ quirk + warning + `status=="partial"`，且不进 `missing_required_files`
- [x] 2.4 判据单一来源锁：`state_cli` 模块 AST/import 断言（形状判定只经
      `cfg_ic_header_shape`，无本地 len(tokens) 第二规则）
- [x] 2.5 零回归：`tests/test_state_manager.py`（含 `:2204` 既有
      `_normalized_checkpoint_ic_file` 直调用例）、`tests/test_basins_discovery.py`
      与 `tests/test_warm_start_chaining.py`（rekey oracle：3-token 头部
      `:213`/`:361-362`，断言 `:475`）现有用例全绿

## 3. Verification

- [x] 3.1 红证：2.1/2.2/2.3 新用例改动前红（记录改动前行为：A 面写出毒 `.normalized`、
      B 面 valid_time 被反推、checksum 面 status 假 valid）
- [x] 3.2 uv run pytest -q tests/test_state_manager.py tests/test_basins_discovery.py
      tests/test_warm_start_chaining.py
- [x] 3.3 uv run ruff check packages workers tests
- [x] 3.4 openspec validate checkpoint-ic-header-shape-residue --strict --no-interactive
- [x] 3.5 merge 后 node-27 oracle receipt：3.2 两套件，记入 #1430

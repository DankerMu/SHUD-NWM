# Proposal: checkpoint-ic-header-shape-residue (#1430)

## Why

Issue #1430（PR #1429/#1197 的 out-of-scope 路由残留，pre-existing）：
`cfg_ic_header_minute_index` 的「最后一个数字 token = minute-time」规则在 2-token
头部（`23106\t6`，#1197 事故形态）下退化为 index 1（列数位），而
`packages/common/state_cli.py` 两处消费点无形状前置判据：

- **A 覆写面** `_normalized_checkpoint_ic_file`（`:247-282`，消费点 `:262`，生产路径
  `:203`）：把列数覆写成 epoch-minute，产出 #1197 同机制毒 IC（present/非空/checksum
  干净/列数=epoch 分钟，183 GB OOM 级）——由本仓 state publish 流程自产。
- **B 读出面（危害更大）** `_checkpoint_header_minute`（`:307-323`，消费点 `:317`）：
  把列数读成 minute 喂 `_checkpoint_with_header_time`（`:285`），checkpoint 的
  `valid_time`/`lead_hours` 被列数反推重写（issue 探针：06:00→00:06、lead 6→0），
  而 `valid_time` 是快照键——下一 cycle warm-start 按错误时刻取用，时间语义静默污染。

第二观察（同单并治）：`workers/model_registry/basins_discovery.py`
`_checksums_for_required_files`（`:422-440`，`except OSError: continue` @ `:438`）
checksum fail-open——已 glob 匹配的必需文件 stat/sha256 抛 OSError 时静默跳过，
不进 `missing_required_files`、不记 quirk，`status` 保持 `"valid"`：「必需文件存在
但读不了」被登记成健康模型。

判据缺口而非活故障（node-27/22 实机 116 对 IC 先验全 3-token；checkpoint 头部本仓
自产），但触发后果为 #1197 同级或更隐蔽。#1429 落地的 `cfg_ic_header_shape`
（`state_qc.py:558`）已能识别该形状（`['23106','6']` → `.valid=False`），只是这两处没接。

## What Changes

- **A**：`_normalized_checkpoint_ic_file` 消费 minute_index **之前**用
  `state_qc.cfg_ic_header_shape` 判形状；不合法 → 抛 `StateManagerError`，消息带
  机器可 grep 的 reason token（对齐 `state_cli.py:68-70` #1325 publish-side
  admission reason 约定），**拒绝发布**、不产出 `.normalized` 文件。
- **B**：`_checkpoint_header_minute` 同判据；不合法 → 返回 `None`，
  `_checkpoint_with_header_time` 自然退回 manifest 声明的 `valid_time`（不 rekey），
  留一条可观测记录（logger warning，token 可 grep）。
- **checksum 面**：`except OSError` 臂改为记 quirk（`unreadable_required_file` 类）
  + warning，`status` 落 `"partial"`；`missing_required_files` 语义不变（匹配到的
  文件不塞进去）。同臂顺带给超过 `CHECKSUM_LIMIT_BYTES` 的静默无-checksum 分支
  是否加可观测标记留给实测裁定：若零成本顺手则加同族 quirk，否则报告不改
  （issue 定性为「设计上的界、缺可观测标记」，非本单硬验收）。
- 形状判据**只**来自 `state_qc.cfg_ic_header_shape` 单一 helper，`state_cli.py`
  不新增第二份 token 计数规则（#1429 单一判据原则）。

## Non-Goals

- `cfg_ic_header_minute_index` 自身语义（运行期共享读/移位规则；`cfg_ic_header_shape`
  比它严是 #1429 有意裁定）。
- #1197 上游交付物修复；注册门/直供门/限定门/注入器（#1429 已覆盖）。
- `basins_discovery.py` 其余 `except OSError` 臂（`:399`/`:540`/`:583`/`:636`/`:681`
  等，各有其上下文语义，issue 只主张 checksum 面这一处）。

## Risk triage

- Fixture level: compact（#1429 同族先例，判据 helper 已定稿，纯接线 + 三态化）。
- Repair intensity: medium（state 时间语义 + 注册健康度面；无新抽象，S 规模）。
- Risk packs: fail-closed/state-semantics selected（毒 IC 拒发 + valid_time 不被
  反推 + 三态注册）；test-evidence selected（2/3/4-token × A/B 两面 + unreadable
  用例）；version-divergence not selected（无解释器分岔轴）；其余 not selected。

## Must preserve

- 3-token native（`<mesh> <cols> <minute>`）与 4-token（`<mesh> <river> <lake>
  <minute>`）头部在 A/B 两面行为**零变化**（现有 rekey/归一化用例全绿；issue
  对照 C 的 no-op 语义保持）。
- `_checkpoint_with_header_time` 对「header minute 与 manifest 一致」的既有
  no-op 语义不变。
- `missing_required_files` 判定语义不变；checksum 成功路径的条目形状不变。
- `StateManagerError` 既有 reason token 集合只增不改。

## Evidence mapping

- 验收 1→A 面 fail-closed 用例（2-token 拒发 + reason token grep）；验收 2→B 面
  `None` 回退用例（06:00 保持 06:00）；验收 3→3/4-token 零变化回归；验收 4→AST/
  import 级断言判据单一来源；验收 5→unreadable-required-file 三态用例（quirk +
  partial + missing 不变）；验收 6→2/3/4-token × A/B 全矩阵。
- Verification：`uv run pytest -q tests/test_state_manager.py
  tests/test_basins_discovery.py` + `uv run ruff check .`（本地）；merge 后
  node-27 receipt。

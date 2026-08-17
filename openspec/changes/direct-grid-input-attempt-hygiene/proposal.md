# Proposal: direct-grid-input-attempt-hygiene

## Why

Issue #1355（#1330 attempt-hygiene 家族的 `input` 侧续作；PR #1354 round-2
S2 DEFER 出口）：direct-grid staging 只写 manifest 白名单成员、从不删除
（`runtime.py:1920-1959` 零 delete 调用），`input/<project>` 跨 attempt 复用
（run_id 确定性，`chain_forecast_state.py:85`），文件系统歧义门
（`runtime.py:1061-1073`）fail-closed 且 `DIRECT_GRID_FORCING_INDEX_AMBIGUOUS`
不可重试——三者叠加：同 cycle re-drive 跨越 #1176 身份迁移点后，上一 attempt
的 legacy 成员与本次 canonical 成员并存，该 run_id 的每一次后续 attempt 都抛
歧义错，**自锁死**，只能人工进 node-22 删文件。PR #1354 仅做了消息级缓解。

## What Changes

- **declared-member-anchored staging 卫生（issue 推荐方向，design D1/D2）**：
  `_stage_direct_grid_directory_artifact` 在写入任何成员**之前**，删除staging
  目录 `shud/` 下**不被本次 checksum-verified manifest 声明**的 accepted
  station-index 成员（严格限定 `SHUD_FORCING_INDEX_MEMBERS` 两元素集合的
  补集，不碰任何其他文件）；`unlink_no_follow` + containment（D3）。
- **删除失败 fail-loud（D3，#1164 `_clear_packaged_initial_states` 样板）**：
  重编码为专用 typed 错误码 `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED`
  终止 attempt，无两分支降级；该码不进 `TRANSIENT_ERROR_CODES`（retry.py
  对未知码默认 non-transient，只读确认，不改 retry.py）。
- **歧义门保留为纵深防御（D4）**：文件系统歧义门原位保留；其错误消息中
  「最可能成因是上一 attempt 残留」的假说段随卫生落地改写（残留已在
  staging 前自愈，剩余成因是白名单之外的带外写入/篡改）。
- **规格修订（D5）**：`fixed-station-forcing-production` 的
  `ambiguous index membership fails closed` 场景改为区分**工作区残留**
  （staging 前自愈 + 删除失败 fail-loud）与**真实 package 污染**
  （manifest 声明多成员、或卫生后文件系统仍并存 → 继续 fail-closed）。

## Risk Triage

- Fixture level: **expanded**。Upstream suggested level: 无（issue 早于
  0.16.0 契约，无 `Suggested fixture level` 字段）；issue 预估规模 S、
  implementation-ready，但改动**变更 fail-closed 语义**（规格影响性）且新增
  删除路径（file-IO 安全面），高于 compact；无 attempt 记账/持久证据多面
  联动，不到 high。divergence：无（无上游建议可偏离）。
- Repair intensity: standard。
- Risk packs:
  - file-IO/path-safety: **selected** —— 新增 unlink 路径必须 no-follow +
    containment，删除集合严格限定两元素补集；symlink/目录形成员的失败形。
  - spec-compliance: **selected** —— 残留 vs 污染两分场景与实现逐句对读；
    歧义门消息假说段与新语义一致。
  - compatibility/regression: **selected** —— 非 direct-grid 车道 manifest
    锚定解析（spec `non-direct-grid staging resolves a residual second
    member by manifest`）零回归；direct-grid 既有缺席/错码场景不回归；
    legacy-only 历史包仍可消费。
  - state-machine/attempt-accounting: not selected —— 不改编排器状态机与
    retry 分类逻辑（新码走未知码默认 non-transient 路径，只读确认）。
  - security/auth: not selected —— 无权限/发布面；路径安全归 file-IO pack。
  - performance: not selected —— 每 attempt 至多 2 次 unlink 探测。

## Non-Goals

- 不改 #1176 已定的成员解析/校验和/身份绑定逻辑（`_direct_grid_runtime_
  checksum_entries` 的 manifest 门原样保留）。
- 不改 `output` 侧 #1330 卫生；不做 `input` 整目录 quarantine-and-recreate
  （issue 备选，NFS 全量 re-stage I/O 成本 + 失去复用加速，规模抬到 M+，
  否决——D1）。
- 不把 `DIRECT_GRID_FORCING_INDEX_AMBIGUOUS` 加进 `TRANSIENT_ERROR_CODES`
  （拿重试掩盖脏工作区）。
- 不做全 `input` 无差别清空（破坏 model package / IC staging 假设与排障
  现场）。
- 非 direct-grid 车道的任何行为变化（其残留是合法稳态，由 manifest 锚定
  解析处理）。
- #1318（`WORKSPACE_ROOT/runs` 回收）——同病根不同缺陷，不在本 change。

## Impact

- `workers/shud_runtime/runtime.py`（`_stage_direct_grid_directory_artifact`
  前置卫生 + 新错误码 + 歧义门消息假说段改写）
- `tests/test_shud_runtime.py`（AC 锚 + 删除失败形 + 非 direct-grid 回归 +
  纵深防御门）
- `openspec/specs/fixed-station-forcing-production/spec.md`（merge 后
  archive 回写）
- `services/orchestrator/retry.py`：**零改动**（只读确认未知码默认
  non-transient）

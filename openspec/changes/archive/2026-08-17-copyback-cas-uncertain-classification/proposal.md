# Proposal: copyback-cas-uncertain-classification

## Why

Issue #1364（PR #1363/#1193 范围外发现，pre-existing）：自然 copyback 路径的
分流判据 `isinstance(error, ProviderAtomicError) and error.phase ==
"release_uncertain"`（`run_tree_copyback.py:107-108`）必然 miss destination
CAS 自身的 commit-uncertain 族——`provider_replace_uncertain` /
`provider_postread_failed` 在 `state_manager._write_state_index_bytes` 被重包
成 `StateManagerError`（reason 原样、`evidence={"phase":"replace_uncertain"}`），
永远不是裸 `ProviderAtomicError`，于是"index 可能/确实已提交"的失败全部落
fail-closed 的 `OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED`。同一批 reason 在
replay 工具里被**刻意排除**在 pre-commit allowlist 之外（exit 3
commit-uncertain）——两个 operator surface 对同一失败判读相反，且错在最贵的
方向（把已提交当未提交，反转 #1189 恢复链的核心二分）。#1363 只用 runbook
文字临时兜住（"尚未分流的 replace_uncertain 族"caveat）。

## What Changes

- **判据加宽为 phase 自述 + 双载体（design D1）**：`run_tree_copyback.py`
  except 内改读错误自述的 phase——`ProviderAtomicError.phase` 或
  `StateManagerError.evidence["phase"]`——**phase 在场且 != "precommit" 即
  路由 `..._COMMIT_UNCERTAIN`**。与 replay 的口径哲学完全对齐（"只有经审计
  的 pre-commit raise point 才证明 index 未改"），未来新增 uncertain
  phase/reason 自动落安全侧。
- **`provider_restored_previous`（phase="postcommit"）显式归属（D2）**：
  路由 `..._COMMIT_UNCERTAIN`，与 replay 排除口径一致——回滚虽经校验成功，
  新 entry 曾短暂可见且"什么都没发生"不成立，操作员下一步同样是核
  entry_count。
- **details 补 `error_reason`（D3）**：uncertain 分支与 `..._FAILED` 分支
  details 均含 `error_reason`（`getattr(error, "reason", None)`），与
  lock-release 分支形状一致；uncertain 消息按 phase 泛化（D4）。
- **runbook 收窄（D5）**：`current-production-ops.md` §8.8 判读口径——
  `..._FAILED` 重新等于纯 pre-commit fail-closed；"尚未分流的
  replace_uncertain 族"caveat 删除；`..._COMMIT_UNCERTAIN` 的
  `error_reason` 枚举扩为 release/replace/postcommit 三族。
- **规格（D6）**：`file-state-snapshot-index` ADDED requirement——自然路径
  对 phase 处于 CAS 之后（release_uncertain / replace_uncertain /
  postcommit）的 merge 失败一律分类 commit-uncertain；#1193 既有
  release-uncertain requirement 原文不动。

## Risk Triage

- Fixture level: **expanded**。Upstream suggested level: 无（issue 无
  `Suggested fixture level` 字段）；issue 预估规模 S，但改的是 operator
  恢复链的分类判读位（#1189 二分）+ 双 surface 口径对齐 + spec/runbook
  多载体，高于 compact；无状态机/持久证据结构改动，不到 high。
  divergence：无。
- Repair intensity: standard。
- Risk packs:
  - error-classification/operator-contract（state-machine pack 变体）:
    **selected** —— phase→code 真值表逐格闭合（precommit/替换不确定/释放
    不确定/postcommit/无 phase）；两载体（裸 ProviderAtomicError vs 重包
    StateManagerError）等价。
  - spec-compliance: **selected** —— 新 requirement 场景、runbook 判读
    口径与实现逐句对读；replay 口径引用保持真实。
  - compatibility/regression: **selected** —— #1363 Must-preserve 修订后
    仍成立：lock-release 用例、既有 pre-commit `..._FAILED` 用例
    （tests/test_run_tree_copyback.py:538,598,732 口径）全绿；
    `chain_forecast_execution` 事件写入器零改动自动携带 code。
  - integration/write-read-parity: not selected —— journal event 通道为
    既有 `except RunTreeCopybackError` 透传，无新写读面；details 新键由
    单测锚定即可。
  - file-IO/path-safety: not selected —— 无新文件系统操作；故障为注入式。
  - performance/security: not selected —— 异常路径判据，无热路径/权限面。

## Non-Goals

- #1193/PR #1363 已交付的 lock-release 分流语义（原 requirement 不动，仅
  ADDED 新 requirement 拓宽适用族）。
- `state_manager` 重包策略（透传 `ProviderAtomicError` 的备选方案明确否决
  ——改公共错误契约，`publish_state_snapshot_index` 其他调用点回归面远大
  于本 issue）。
- reason 白名单判据（备选否决——与 replay allowlist 同样有双处同步漂移
  风险；phase 判据单点且 fail-safe）。
- replay 工具分类逻辑（已正确，不动）。
- `chain_forecast_execution.py` event 写入器（零改动）。
- 真实 NFS 故障复现（禁止在 node-22 制造 fd/挂载故障；注入式测试）。

## Impact

- `services/orchestrator/run_tree_copyback.py`（唯一 code-side 判据）
- `tests/test_run_tree_copyback.py`（镜像 B3 的注入用例 + 回归）
- `tests/test_orchestration_chain.py`（stub 内旧消息字面量同步，断言不变）
- `docs/runbooks/current-production-ops.md` §8.8（判读口径收窄）
- `openspec/specs/file-state-snapshot-index/spec.md`（merge 后 archive 回写）

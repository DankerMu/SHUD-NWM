# Proposal: provider 锁释放期异常归类为 commit-uncertain（release_uncertain 语义）

## Why

Issue #1193：`merge_state_snapshot_index_copyback` 的 destination CAS 提交发生在 provider 锁**内部**，而 `provider_atomic._provider_destination_file_lock` 释放段（`fcntl.flock(LOCK_UN)` / `os.close`，NFS 上均可 EIO/ESTALE）抛出的 `OSError` 无任何归类，直接穿透 context manager——"index 已改"被报成"什么都没发生"。

**issue 立案（fecdbd35）后已被 #1189 r3/r4 部分交付的面**（现状核实，非本 change 工作）：

- replay 侧 `scripts/scheduler_state_index_copyback_replay.py:389` 已 `except Exception` 广捕，裸 OSError 走 `merge_commit_uncertain`（rc 3、`merge_committed_incomplete`、合成 `merge_unexpected_exception:OSError`、stdout 摘要 + receipt）；
- runbook 退出码表（`docs/runbooks/current-production-ops.md:1680+`）已覆盖 0/2/3 与全部 exit-3 reason，rc 1 无判读入口的形态已消失。

**仍然开放的缺口**（本 change 范围）：

1. `provider_atomic.py:244-257` 释放段零归类——所有锁使用点（copyback、refresh、raw manifest、`atomic_replace_provider_bytes` 自持锁）都吃这条裸 OSError 路径，语义全靠调用方兜底；
2. 自然路径 `services/orchestrator/run_tree_copyback.py:106` 只捕 `(ProviderAtomicError, StateManagerError)`：裸 OSError 绕过 `chain_forecast_execution:762` 的 `except RunTreeCopybackError`，**零 pipeline event**；即便归类后落入现有 except，也会被贴 `OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED`（"failed closed"）——把"可能已提交"塞回"未提交"桶，正是 #1189 排障二分要禁止的方向反转；
3. 该路径**零 oracle**：三个测试文件无任何 `LOCK_UN`/flock 释放注入用例。

## What Changes

1. **`packages/common/provider_atomic.py` 释放段归类（issue 推荐方案）**：`_provider_destination_file_lock` 的 unlock/close 失败 → `ProviderAtomicError("provider_lock_release_failed", phase="release_uncertain")`（phase 命名对齐既有 `replace_uncertain`）。body 内已有异常在途时释放失败**不得覆盖 body 异常**（静默吞释放错、传播 body 错）。一处归类覆盖全部锁使用点。
2. **自然路径独立 code**：`run_tree_copyback` 对 `phase == "release_uncertain"` 的 `ProviderAtomicError` 改抛 `RunTreeCopybackError("OBJECT_STORE_COPYBACK_STATE_INDEX_COMMIT_UNCERTAIN", …)`（与 `…_FAILED` 区分）；`chain_forecast_execution` 现有 `except RunTreeCopybackError` 事件写入器自动携带新 code 落 `object_store_copyback` event（`status_to="failed"` 不变，code 承担二分）。
3. **replay 侧零代码变更**：`provider_lock_release_failed` 不在 pre-commit allowlist ⇒ 现有机制自动走 `merge_commit_uncertain`（rc 3），`merge_error_reason` 从合成标识升级为真实 reason。仅同步 `:380-388` 注释中"裸 OSError 来自锁 teardown"的举例措辞（该例经 1 变为 typed）。
4. **注入测试**（AC-1/2/3 + 归类正确性）：LOCK_UN/close 注入下——state_manager merge 抛 `release_uncertain` 且 destination 字节确为 merge 后内容（断言"已提交"事实）；replay rc 3 / `merge_committed_incomplete` / stdout 摘要 / receipt 落盘且 `merge_error_reason=provider_lock_release_failed`；run_tree_copyback 新 code + chain 事件断言；body 异常优先级；close（非 flock）失败同归类。
5. **runbook 两处更新**：`merge_commit_uncertain` bullet 将 `provider_lock_release_failed`（提交后锁释放失败）列为 off-allowlist 的具名 reason 例（合成标识例保留给真正未分类异常）；§8.8 journal grep 扩为两码并列并补 `…_COMMIT_UNCERTAIN` 判读 bullet。
6. **兄弟锁点审计（AC-5，判定不改行为）**：`scheduler_file_provider_refresh.py:639/1320/1512`、`source_cycle_raw_manifest.py:432→:495`、`atomic_replace_provider_bytes` 自持锁分支——释放失败从裸 OSError 变为 typed `ProviderAtomicError`，逐点记录新归类落入的现有 except 口径与语义结论，既有测试全绿。

## Non-Goals

- 锁**获取**侧语义（阻塞/不可重入/root 别名自死锁）——#1192；
- `provider_destination_lock` 可重入化或默认 `blocking` 变更；
- 备选方案（merge 返回值降级 warning）——不采纳，理由见 design；
- run_tree_copyback 对**非锁释放期**裸异常的广捕（pre-existing，超 issue 范围，known-residual 登记）。

## Impact

- Affected specs: `file-state-snapshot-index`（ADDED 一条 requirement）。
- Affected code: `packages/common/provider_atomic.py`（释放段）、`services/orchestrator/run_tree_copyback.py`（code 分流）、`scripts/scheduler_state_index_copyback_replay.py` 与 `tests/…replay.py:505-507`（均仅注释）、`docs/runbooks/current-production-ops.md`（bullet 补名 + §8.8 判读入口）、测试三文件新增注入用例。
- 无 DB/display/调度行为面改动；故障注入式测试，**禁止**在 node-22 制造 fd/挂载故障（issue 明示）；无需远端 receipt。

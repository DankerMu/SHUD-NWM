# Tasks: lock-release-uncertain-classification

## 1. 实现

- [x] 1.1 `packages/common/provider_atomic.py`：`_provider_destination_file_lock` 释放段双路径重构——body 异常优先（quiet close 吞释放错）；干净退出时释放失败 → `ProviderAtomicError("provider_lock_release_failed", phase="release_uncertain")`，**fd 关闭是无条件义务**（unlock 失败仍关 lock_fd，parent_fd 恒关，首错为 cause）；获取段逐字节不动（D1）
- [x] 1.2 `services/orchestrator/run_tree_copyback.py`：现有 except 内按 `phase == "release_uncertain"` 分流新 code `OBJECT_STORE_COPYBACK_STATE_INDEX_COMMIT_UNCERTAIN`（含 `error_reason` detail）；`chain_forecast_execution` 零改动（D2）
- [x] 1.3 注释同步（D3）：`scripts/scheduler_state_index_copyback_replay.py:380-388` + `tests/test_scheduler_state_index_copyback_replay.py:505-507` 同源 prose（注释-only，断言零改动）；allowlist/replay 代码零改动
- [x] 1.4 runbook（D4）：(a) `merge_commit_uncertain` bullet 补 `provider_lock_release_failed` 具名例；(b) §8.8 journal grep 扩为两码并列 + `…_COMMIT_UNCERTAIN` 判读 bullet（"可能已提交"处置方向）
- [x] 1.5 D6 novel-phase 残余核查：grep `infra/` + `scripts/` 确认无对 refresh receipt `phase=="precommit"` 的筛选假设，结果记 PR body

## 2. 测试

- [x] 2.1 B1：LOCK_UN 注入 → merge 抛 `release_uncertain` 且 destination index 字节为 merge 后内容（已提交事实断言）
- [x] 2.2 B1b：注入释放失败后，同进程同路径再次 `provider_destination_lock` 成功（fd 不泄漏/不自死锁钉）
- [x] 2.3 B2：replay `--enforce` 同注入 → rc 3、stderr `status=merge_committed_incomplete`+`reason=merge_commit_uncertain`+details `error_reason=provider_lock_release_failed`；stdout/receipt `merge_error_reason=provider_lock_release_failed`、`merge_commit_state=uncertain`；receipt 落盘；反断言无 rc 1/空 stdout/refused
- [x] 2.4 B3：`copyback_run_trees` 真实注入 → 新 code ≠ `…_FAILED`，destination 已提交
- [x] 2.5 B4：`_copyback_stage_run_trees` 以 stub 抛新 code（既有 harness stub `copyback_run_trees` 模式）→ event 存在、details `error_code` 为新 code、`status_to="failed"`
- [x] 2.6 B5：body pre-commit 异常 + 释放同时失败 → 传播 body 异常（provider 层屏蔽方向钉）
- [x] 2.7 B5b：同双故障 replay 层 → rc 2、`status=refused`、`reason=merge_failed`（uncertain→refused 有意重分类钉）
- [x] 2.8 B6：`os.close` 注入（flock 成功）→ 同归类；fake 判别目标 fd 且真关后再抛（seam 纪律）
- [x] 2.9 兄弟点回归：`test_scheduler_file_provider_refresh.py`、`test_source_cycle_raw_manifest.py`、`test_chain_repository_nfs_raw_manifest.py`、`test_state_manager.py` 既有用例零改动（1.3 注释豁免）全绿；D6 判定表逐点核实（refresh 行含 outcome/reason/phase/rollback 四元组）入 PR body

## 3. Evidence Floor

- [x] 3.1 `uv run pytest -q tests/test_state_manager.py tests/test_scheduler_state_index_copyback_replay.py tests/test_run_tree_copyback.py tests/test_scheduler_file_provider_refresh.py tests/test_source_cycle_raw_manifest.py tests/test_chain_repository_nfs_raw_manifest.py` 全绿
- [x] 3.2 `tests/test_orchestration_chain.py` copyback event 定向用例绿
- [x] 3.3 `uv run ruff check .`；runbook markdownlint（仓库既有入口）
- [x] 3.4 `openspec validate lock-release-uncertain-classification --strict --no-interactive`
- [ ] 3.5 PR body：D6 兄弟点判定表（含 novel phase token 与 rollback 事实）+ 记录性偏离（replay 零代码变更、r3/r4 已交付面清单、双故障 uncertain→refused 重分类）

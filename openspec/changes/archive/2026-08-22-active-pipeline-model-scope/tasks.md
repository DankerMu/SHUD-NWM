## 1. Runtime contract

- [x] 1.1 在 `FileOrchestrationJournalRepository.has_active_pipeline` 的本地 terminal-completion 合取中复用 `_is_foreign_model_cycle_scope_job`；不得改 `_job_matches_candidate`、`candidate_jobs` membership 或 active-job 返回臂。
- [x] 1.2 改写邻近身份口径注释：区分宽 row visibility 与候选作用域 suppression authority，并显式对照 `chain_repository.py:57-96` 的纯 union。
- [x] 1.3 保持 journal read failure fail-closed、self/model-less stale-hydro suppression、`has_completed_pipeline` 和 `active_slurm_jobs` 实现逐字不变。

## 2. Requirement-driven tests and red proof

- [x] 2.1 在生产改动前批量运行新行为 tests 并保存 red proof；修复后同一命令全绿，清理任何 `red-proof` stash。
- [x] 2.2 翻转但不删除 #1470 的 `test_foreign_model_completion_row_suppresses_the_hydro_active_arm` 锚；名称/docstring记录 #1470 freeze 与 #1472 unfreeze，且同 fixture 的 `has_completed_pipeline` 仍 False。
- [x] 2.3 参数化 ACTIVE hydro `{created, staged, submitted, running}` × foreign completion stage `{state_save_qc, publish, parse}`，无其他 active job 时 `has_active_pipeline=True`。
- [x] 2.4 设置 `NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast_state_save_qc`，证明 foreign `state_save_qc` completion仍不能抑制 ACTIVE hydro。
- [x] 2.5 证明 candidate-own `fcst_...` completion与 own-named exact cycle-run completion仍抑制 stale `created/staged` hydro，返回 False。
- [x] 2.6 证明 model-less exact cycle-run 与 suffix cohort terminal completion仍 cycle-wide 抑制 stale hydro，返回 False。
- [x] 2.7 保持 adjacent gates：foreign queued exact cycle-run仍令 active gate True并出现在 `active_slurm_jobs`；foreign completion fixture 的 `has_completed_pipeline` 返回不变；共享谓词未被收窄。

## 3. Spec and evidence floor

- [x] 3.1 delta完整复制并修改 `job-retry-mechanism` requirement，保留全部既有 scenarios并新增 foreign completion不得抑制 ACTIVE hydro 场景。
- [x] 3.2 `uv run pytest -q tests/test_file_orchestration_journal.py tests/test_orchestration_chain.py tests/test_production_scheduler.py` 通过。
- [x] 3.3 `uv run ruff check .` 通过。
- [x] 3.4 `openspec validate active-pipeline-model-scope --strict --no-interactive` 与 `git diff --check` 通过。
- [x] 3.5 对照 Invariant Matrix逐项审计；确认没有测试/spec/CI oracle被弱化，且无 node-22 receipt需要（无 sbatch/gateway/resource/runtime变更）。

## 4. Risk-pack evidence mapping

- [x] 4.1 Concurrency/shared state + Slurm lifecycle：foreign completion与foreign ACTIVE两臂的反向 oracle共同证明 suppression收窄不放松 duplicate detection。
- [x] 4.2 Legacy compatibility + error/idempotency：self、model-less、read-failure与相邻 gate controls全绿。
- [x] 4.3 Config + documentation：默认/生产 terminal contract都覆盖，代码注释、测试docstring与spec三处freeze→unfreeze口径一致。
- [x] 4.4 其余not-selected packs保持无新增surface；实现报告逐项列出所查边界和任何偏离（无偏离亦须明确）。

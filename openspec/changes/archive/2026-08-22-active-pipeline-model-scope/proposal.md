## Why

Issue #1472 暴露了 db-free file-journal 的候选作用域缺口：同 cycle 的他模型具名完成行会进入 `has_active_pipeline` 的 terminal-completion 抑制量，把本候选仍为 `created` / `staged` / `submitted` / `running` 的 hydro run 判成不活跃。DB repository 对照是纯 `UNION ALL`，没有该抑制；#1302 修正完成闸后，这个既有分歧会让重复投递防线失明。

## What Changes

- 在 `FileOrchestrationJournalRepository.has_active_pipeline` 的本地 terminal-completion 合取中复用 `_is_foreign_model_cycle_scope_job`，只排除他模型具名且 run id 恰为 cycle run id 的完成行。
- 保留候选自身完成行与 model-less cohort 完成行抑制 stale ACTIVE hydro 占位符的既有语义；保留他模型 ACTIVE cycle-run 行在 `has_active_pipeline` / `active_slurm_jobs` 中的宽可见性。
- 翻转 #1470 为 #1472 留下的行为钉值，并补齐 hydro 状态、完成 stage、生产终态开关、自身完成、model-less cohort 与相邻 gate 的回归矩阵。
- 改写 journal 注释与 `job-retry-mechanism` 契约，显式区分“行可见性保持宽”与“完成证据只能候选作用域地抑制 hydro-active 臂”。

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `job-retry-mechanism`: file-journal active-pipeline gate 不得用他模型具名完成证据抑制本候选的 ACTIVE hydro run，同时保持自身/cohort 抑制和相邻 duplicate-submission 可见性。

## Impact

- Runtime: `services/orchestrator/file_orchestration_journal.py` 的一个候选状态 gate；无 schema、写路径、迁移或配置格式变化。
- Tests: `tests/test_file_orchestration_journal.py` 的要求驱动真值表；既有三套调度器回归。
- Spec: `job-retry-mechanism` 的既有 requirement 完整 MODIFIED delta。
- No node-22 live receipt: 本改动不触及 sbatch、Slurm gateway、资源或 SHUD runtime，只改变可由确定性 file-journal fixture 完整证明的 repository predicate。

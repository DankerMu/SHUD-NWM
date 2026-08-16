# Proposal: completed-pipeline-model-scope

## Why

Issue #1302（#1288 fixture 复审发现，master 既有，同根第二出口）：db-free
（file-journal）模式下 `has_completed_pipeline` 的 job 合取项走共享谓词
`_job_matches_candidate`，其无条件 cycle-run_id 分支让**任一**他模型具名 +
`run_id == cycle_<source>_<stamp>` 的完成阶段成功行，把同 cycle **所有**
候选判成"已完成"——哪怕候选自己的 hydro_run 是 failed/cancelled/created、
自己的 forecast 行是 failed。四个消费面据此静默跳过候选（trigger 面裸
`continue` 无任何 skipped 记录），DB 侧对同一份数据判 False（journal/DB
分歧）。#1288 已交付 candidate-state 投影面的排除并**显式冻结**了本 gate
的行为（负向回归断言 True）；本 issue 裁定该 gate 自身契约并解冻。

## What Changes

- `services/orchestrator/file_orchestration_journal.py`：
  `has_completed_pipeline`（:543-568）的 `has_terminal_completion` 合取项
  （:558-563）追加本地排除 **复用 #1288 交付的
  `_is_foreign_model_cycle_scope_job`**（:8755-8775，= model_id 非空 ∧
  ≠ 候选 ∧ run_id 恰为 cycle run id）。共享谓词 `_job_matches_candidate`
  （:8805-8824）**逐字不动**；`has_active_pipeline` / `active_slurm_jobs`
  的宽口径**逐字不动**。
- 解冻 #1288 的负向回归：
  `test_foreign_model_cycle_run_row_stays_visible_to_the_completion_gate`
  （`tests/test_file_orchestration_journal.py:1589-1610`）断言 True→False，
  docstring 记「该行为由 #1288 冻结、由 #1302 解冻」。
- 同步改写 candidate-state 投影处的口径注释（`file_orchestration_journal.py:721-729`）。
  注意保留仍为真的半句：「completion gate 无 DB job-row 对照」修后依然
  成立（`chain_repository.py:97-111` 只查 `hydro.hydro_run`）；失效的只是
  由它推出的「所以 journal 侧保持宽口径/原答案」——改写时区分事实与推论。
- **AC6 处置声明**：#1288 的 tasks 2.6(b) 已随 change 归档
  （`openspec/changes/archive/2026-08-07-journal-cycle-row-model-scope/tasks.md:72-81`），
  归档是历史账本，**不回改**；freeze→unfreeze 的记录落在三处——本 change
  文档（design D3）、解冻测试的 docstring、spec delta。
- spec：`job-retry-mechanism` :381-780 requirement 的「gates keep wider
  visibility」句改为 completion gate carve-out + 新增 completion-gate
  scenario（真值表：stages × 终态口径开关 × 自身 hydro 失败形 × cohort
  不回归 × 相邻 gate 逐字不变 × DB 同向）；delta 承载，merge 后 archive
  回写。

## Risk Triage

- Fixture level: **expanded**（强制触发词命中：orchestrator gate /
  persisted journal state 读路径 / 完成判定驱动调度决策）。Upstream
  suggested level: 缺省（scribe 手写 issue，Readiness: implementation-ready）。
- Repair intensity: **medium**（单 gate 单合取项收窄 + 冻结断言解冻 +
  spec 契约句改写；真值表小而闭合，落点谓词已在库）。
- Risk packs:
  - state-machine/gates: **selected** —— 完成判定真值表（3 stage × 2 终态
    口径 × 3 自身 hydro 状态），四个消费面的行为传导。
  - compatibility/regression: **selected** —— cohort model-less 完成行
    cycle-wide True 契约（#841）；候选自身完成形 True；
    `has_active_pipeline`/`active_slurm_jobs` 同 fixture 逐字不变。
  - spec-compliance: **selected** —— #1288 冻结句的解冻必须 spec 与测试
    与注释三处同步（tasks 3.x 逐句对读）。
  - integration/consumer-surface: **selected** —— trigger 面
    （`has_completed_forecast`）至少一条真实 journal 仓储判别锚；其余
    消费面枚举记载（design D4）。
  - file IO/path safety、security/auth、performance: not selected ——
    journal 读路径无新 IO；gate 内每行多一次纯函数谓词，非热路径。

## Non-Goals

- **不得整体收窄共享谓词 `_job_matches_candidate`**（#1288 design D1 已
  实测证伪该落点：会放松 `has_active_pipeline`/`active_slurm_jobs` 的
  有意宽口径）。
- model-less cycle-scope cohort 行 cycle-wide 可见的既有契约（#841）。
- #1288 的 candidate_state 投影 seam 与 manual retry 钉值链（已交付）。
- `scheduler_candidates.py:378-386` 的 `completed_duplicate_pipeline`
  分支（journal 仓储下经 :382-384 合取不可达；issue 明示非本 issue 判定
  对象）。
- `has_active_pipeline` 内部同名 `has_terminal_completion` 局部量（:534-536，
  服务 hydro-active 抑制，属 active 面语义）。
- #1205 / #1287 / #1294 / #1179 / #1186 相邻族。

## Impact

- `services/orchestrator/file_orchestration_journal.py`（`has_completed_pipeline`
  一处合取 + :721-729 注释）
- `tests/test_file_orchestration_journal.py`（解冻 + 新真值表锚）
- 只读参照（行为传导，不改码）：`chain_forecast_trigger.py:378-381,247-251` ·
  `scheduler_generation_gate.py:130-160` · `scheduler_core.py:743-751` ·
  `scheduler_discovery.py:234-249` · `scheduler_candidates.py:226, 378-386` ·
  对照 `chain_repository.py:97-111`
- `openspec/specs/job-retry-mechanism/spec.md`（merge 后 archive 回写）

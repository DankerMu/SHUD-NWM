# Proposal: resume-master-slurm-id-explicit (#1410)

## Why

Issue #1410（#1312 task-0 探针范围外发现，pre-existing @09a0cfb8）：
`services/orchestrator/chain_array_accounting.py:362`

```python
master_slurm_job_id = str(terminal.get("job_id") or terminal.get("slurm_job_id") or "")
```

按 **gateway job 形状**嗅探 `terminal`（gateway dict 的 `job_id` 即 Slurm id）。
resume 腿不满足该假定：`chain_stage_execution.py:820` `terminal = dict(job)`
是 **pipeline_job 行**（`job_id` = `job_cycle_<source>_<cycle>_forecast`，
`slurm_job_id` 才是真实 Slurm id），`:896` 用该形状调
`_record_cycle_stage_status_override` → 投影收到的 master id 是 pipeline
job id。下游判据 `file_orchestration_journal.py:2846`
`existing["slurm_job_id"] != master_slurm_job_id` **永不相等** → 恒进
defer（`identity_mismatch_blocked` / `SLURM_MASTER_IDENTITY_MISMATCH`）：

1. resume 前置是行已终态，defer 在 journal `:3302-3303` 短路成
   `idempotent`——整趟静默 `{"total": 0}`，resume 的 cohort 记账哑火，
   per-task hydro_run 与 cohort 终态停在旧值，零 evidence 零日志差异。
2. 锁内读若非终态则写 `reconcile_unverified` + 身份污染伪事件，脏化
   `identity_blocked_streak` 类读数。

对照兄弟腿（当前正确，不得回归）：`chain_stage_execution.py:627` 的
`terminal` 来自 gateway 形状（`:605`）；`reconcile.py:1043` 显式传
`str(record.slurm_job_id)`。issue 探针实测：resume 腿传
`job_cycle_gfs_2026050100_forecast`，gateway 腿传 `2001`。

## What Changes

按 issue 推荐方案，**嗅探改显式入参**：

- `chain_array_accounting.record_cycle_stage_status_override` 新增
  keyword-only 必填参数 `master_slurm_job_id: str`；`:362` 嗅探行删除。
  projector 分支内该值为空串时 **fail-closed**：抛
  `OrchestratorError("SLURM_MASTER_IDENTITY_UNAVAILABLE", ...)`（含
  pipeline_job_id/stage evidence）——不再静默降级为恒 mismatch。
  非 cohort 分支（不走 projector）不消费该参数。
- wrapper `chain_forecast_orchestrator_cycle.py:486`
  `_record_cycle_stage_status_override` 签名同步透传。
- 两个调用方：
  - submit/poll 腿 `chain_stage_execution.py:627`：传作用域内已有的
    `slurm_job_id`（gateway 真实 id）。
  - resume 腿 `:896`：传 `str(job["slurm_job_id"])`（`:844` 聚合闸已保证
    该键非空——本腿唯一进入 override 的路径要求 `job.get("slurm_job_id")`
    truthy）。
- `reconcile.py:1043`（直调 journal projector）不动。
- `chain_compat_static.py:663` 导出名单不动（名字不变，签名扩展）。

## Non-Goals

- `project_forecast_cohort_tasks` / `_defer_forecast_cohort_projection_unlocked`
  语义不动（判据是对的，此前被喂错值）。
- #1312 已交付的 permanently_failed 粘性、`identity_blocked_streak` 门
  （#1180）不动。
- 不采用备选的「嗅探+isdigit 判据」——隐式形状耦合是根因，显式入参消除之。

## Risk triage

- Fixture level: compact（一处签名 + 两调用方 + fail-closed 空值契约）。
  Repair intensity: low。
- Risk packs: state-semantics selected（master id 取值真值表：gateway 腿
  真实 id 不变；resume 腿从 pipeline job id 变真实 id——投影从恒 defer 变
  真实重投影，这是**有意行为修复**；空值从静默 mismatch 变响亮抛出）；
  test-evidence selected（测试盲区实锤：全仓 11 处 `master_slurm_job_id`
  测试引用全部直调 journal API 手写数字 id，chain 层 id 推导零覆盖——红证
  必须在 chain 层伪造 resume 场景取证）；其余 not selected。

## Must preserve

- submit/poll 腿（`:627`）与 `reconcile.py:1043` 的 master id 仍为真实
  Slurm id；`tests/test_gateway_reconcile.py` / `tests/test_orchestration_chain.py`
  既有断言零改动全绿。
- journal projector 与 defer 语义零改动（`tests/test_file_orchestration_journal.py`
  全绿）。
- 非 cohort stage 的 override 路径行为不变。

## Seams under test

- chain 层 resume 场景构造（stub repository 捕获 projector 入参——issue
  探针先例；或 monkeypatch spy 包装 `project_forecast_cohort_tasks`）；
  真实 journal 面用 file journal 构造已终态 cohort master 行走
  `resume_cycle_stage` 断言真实重投影。

## Evidence mapping

- 验收 1（resume 腿 projector 收到真实 Slurm id）→ tasks 2.1 红证。
- 验收 2（真实重投影而非 identity_mismatch_blocked/{"total":0}）→ tasks 2.2。
- 验收 3（空 master id fail-closed 明确契约）→ tasks 2.3。
- 验收 4（兄弟腿不回归）→ tasks 3.1 三套件全绿。
- Verification：`uv run pytest -q tests/test_orchestration_chain.py
  tests/test_gateway_reconcile.py tests/test_file_orchestration_journal.py`
  + ruff + openspec validate；merge 后 node-27 receipt 记 issue。

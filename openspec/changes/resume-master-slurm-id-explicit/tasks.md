## 1. Implementation

- [ ] 1.1 `chain_array_accounting.record_cycle_stage_status_override` 加
      keyword-only 必填 `master_slurm_job_id: str`；删 `:362` 嗅探行；
      projector 分支空串 → `OrchestratorError("SLURM_MASTER_IDENTITY_UNAVAILABLE")`
      （evidence 含 pipeline_job_id/stage）
- [ ] 1.2 wrapper `chain_forecast_orchestrator_cycle.py:486` 签名透传
- [ ] 1.3 调用方：`chain_stage_execution.py:627` 传 `slurm_job_id`；
      resume 腿 `:896` 传 `str(job["slurm_job_id"])`

## 2. Tests

- [ ] 2.1 红证（chain 层 resume 场景，tests/test_orchestration_chain.py）：
      已终态 forecast cohort pipeline 行走 `resume_cycle_stage`，spy/stub
      repository 断言 `project_forecast_cohort_tasks` 收到的
      `master_slurm_job_id` == 行的真实 Slurm id（非 `job_cycle_*`）。
      **改动前红形状：收到 `job_cycle_<source>_<cycle>_forecast`**
- [ ] 2.2 行为断言（真实 journal 面）：同场景对 file journal 已终态 cohort
      master 行，resume 后为真实重投影（`total > 0` / cohort 状态按聚合
      推导），而非 `reconciliation_decision="identity_mismatch_blocked"`
      或 `{"total": 0}` 哑火。**改动前红形状：{"total": 0} 或
      identity_mismatch_blocked**
- [ ] 2.3 负向锁：`master_slurm_job_id=""` 进 projector 分支 →
      `OrchestratorError` code `SLURM_MASTER_IDENTITY_UNAVAILABLE`（直调
      `record_cycle_stage_status_override` 单测）
- [ ] 2.4 兄弟腿不回归：submit/poll 腿传真实 Slurm id 的行为锁（若既有
      测试已覆盖投影入参则引用之；否则补一条 spy 断言）

## 3. Verification

- [ ] 3.1 uv run pytest -q tests/test_orchestration_chain.py
      tests/test_gateway_reconcile.py tests/test_file_orchestration_journal.py
- [ ] 3.2 uv run ruff check services tests
- [ ] 3.3 openspec validate resume-master-slurm-id-explicit --strict --no-interactive
- [ ] 3.4 merge 后 node-27 receipt（3.1 三套件；全量红按 #1513 已知例外
      口径核对）记 #1410

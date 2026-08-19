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
- [ ] 2.2 行为断言（真实 journal 面，复用 :12049/:9007 骨架；注意终态
      master 必已投影完毕，`total > 0` 不可达、不作判据）三件：
      (a) resume 后 master 行 `reconciliation_decision == "matched_bound"`
      且 `matched_slurm_job_id` 写实为真实数字 id；(b) 全程零
      `SLURM_MASTER_IDENTITY_MISMATCH` / `identity_mismatch_blocked`
      事件；(c) 幂等零写入锁：已投影完毕且聚合字段一致时 master 行
      `error_code`/`log_uri`/`finished_at` 字节不变（diff 闸生效）。
      **改动前红形状：(a)/(b) 处 identity_mismatch_blocked defer 短路
      idempotent**
- [ ] 2.2b docstring 同步：`tests/test_orchestration_chain.py:13641-13647`
      「resume defer 分支使 sticky 行永不触达」随修复失效，按新现实改写
      （断言不动）
- [ ] 2.3 负向锁：`master_slurm_job_id=""` 与非数字（如 `"job_cycle_x"`）
      进 projector 分支 → `OrchestratorError` code
      `SLURM_MASTER_IDENTITY_UNAVAILABLE`（直调
      `record_cycle_stage_status_override` 单测，两腿）
- [ ] 2.5 follow-up 立案（实现期间 issue-scribe）：终态 master 行
      `error_code` 无 #1312 粘性——重投影字段不一致时可被新鲜聚合覆写
      （如 OUT_OF_MEMORY→SLURM_ARRAY_TASK_FAILED），链接记入 PR
- [ ] 2.4 兄弟腿不回归：submit/poll 腿传真实 Slurm id 的行为锁（若既有
      测试已覆盖投影入参则引用之；否则补一条 spy 断言）

## 3. Verification

- [ ] 3.1 uv run pytest -q tests/test_orchestration_chain.py
      tests/test_gateway_reconcile.py tests/test_file_orchestration_journal.py
- [ ] 3.2 uv run ruff check services tests
- [ ] 3.3 openspec validate resume-master-slurm-id-explicit --strict --no-interactive
- [ ] 3.4 merge 后 node-27 receipt（3.1 三套件；全量红按 #1513 已知例外
      口径核对）记 #1410

## 1. Implementation

- [x] 1.1 `chain_array_accounting.record_cycle_stage_status_override` 加
      keyword-only 必填 `master_slurm_job_id: str`；删 `:362` 嗅探行；
      projector 分支空串 → `OrchestratorError("SLURM_MASTER_IDENTITY_UNAVAILABLE")`
      （evidence 含 pipeline_job_id/stage）
- [x] 1.2 wrapper `chain_forecast_orchestrator_cycle.py:486` 签名透传
- [x] 1.3 调用方：`chain_stage_execution.py:627` 传 `slurm_job_id`；
      resume 腿 `:896` 传 `str(job["slurm_job_id"])`

## 2. Tests

- [x] 2.1 红证（chain 层 resume 场景，tests/test_orchestration_chain.py）：
      已终态 forecast cohort pipeline 行走 `resume_cycle_stage`，spy/stub
      repository 断言 `project_forecast_cohort_tasks` 收到的
      `master_slurm_job_id` == 行的真实 Slurm id（非 `job_cycle_*`）。
      **改动前红形状：收到 `job_cycle_<source>_<cycle>_forecast`**
- [x] 2.2 journal 面红证（**geometry-2 构造**——durable 非终态 + 未投影
      的 bound master：复用 `tests/test_file_orchestration_journal.py:8819`
      `_bind_cohort_master` 底座（status="submitted"、slurm_job_id="17667"
      数字、无 candidate_projections，**不要**调 `_project_cohort_failure`），
      再把 status 已终态的快照 dict 喂给 `_resume_cycle_stage`；fake slurm
      client 须为 "17667" 返回两条 array task 且 task_slurm_job_id 为
      `17667_0`/`17667_1`（journal:3068 task 身份闸））：
      **改动前红形状：master 行被真实写入 `status="reconcile_unverified"`
      + `error_code="SLURM_MASTER_IDENTITY_MISMATCH"` +
      `reconciliation_decision="identity_mismatch_blocked"`**（:3011 不等
      → :3469-3473 stale 闸不拦）；改动后：完整投影提交——master 落聚合
      推导终态、`candidate_projections` 填实、`total > 0`、
      `reconciliation_decision="matched_bound"` + `matched_slurm_job_id`
      写实、零 mismatch 事件
- [x] 2.2b docstring 同步：`tests/test_orchestration_chain.py:13641-13647`
      「resume defer 分支使 sticky 行永不触达」随修复失效，按新现实改写
      （断言不动）
- [x] 2.2c 幂等零写入锁（独立回归锁，非红证）：已投影完毕的终态 master
      再 resume，聚合字段一致时 `error_code`/`log_uri`/`finished_at` 字节
      不变（diff 闸 :3313-3327 生效）。注意 resume 腿会重算 log_uri
      （chain_stage_execution.py:874-890）——构造需使重算值与存量一致，
      否则 diff 闸开是预期行为而非回归
- [x] 2.3 负向锁：`master_slurm_job_id=""` 与非数字（如 `"job_cycle_x"`）
      进 projector 分支 → `OrchestratorError` code
      `SLURM_MASTER_IDENTITY_UNAVAILABLE`（直调
      `record_cycle_stage_status_override` 单测，两腿）
- [x] 2.4 兄弟腿不回归：submit/poll 腿传真实 Slurm id 的行为锁（若既有
      测试已覆盖投影入参则引用之；否则补一条 spy 断言）
- [ ] 2.5 follow-up 立案（实现期间 issue-scribe）：终态 master 行
      `error_code` 无 #1312 粘性——重投影字段不一致时可被新鲜聚合覆写
      （如 OUT_OF_MEMORY→SLURM_ARRAY_TASK_FAILED），链接记入 PR

## 3. Verification

- [x] 3.1 uv run pytest -q tests/test_orchestration_chain.py
      tests/test_gateway_reconcile.py tests/test_file_orchestration_journal.py
- [x] 3.2 uv run ruff check services tests
- [x] 3.3 openspec validate resume-master-slurm-id-explicit --strict --no-interactive
- [ ] 3.4 merge 后 node-27 receipt（3.1 三套件；全量红按 #1513 已知例外
      口径核对）记 #1410

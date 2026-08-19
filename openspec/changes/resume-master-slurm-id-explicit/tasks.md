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
- [x] 2.5 follow-up 立案（实现期间 issue-scribe）：终态 master 行
      `error_code` 无 #1312 粘性——重投影字段不一致时可被新鲜聚合覆写
      （如 OUT_OF_MEMORY→SLURM_ARRAY_TASK_FAILED），链接记入 PR
      ——已立 #1589（CONFIRMED @master 8a26fe6e，dedup 无命中，p2/合入后升 p1）

## 3. Verification

- [x] 3.1 uv run pytest -q tests/test_orchestration_chain.py
      tests/test_gateway_reconcile.py tests/test_file_orchestration_journal.py
- [x] 3.2 uv run ruff check services tests
- [x] 3.3 openspec validate resume-master-slurm-id-explicit --strict --no-interactive
- [ ] 3.4 merge 后 node-27 receipt（3.1 三套件；全量红按 #1513 已知例外
      口径核对）记 #1410

## 4. Round-1 fix（verifier CONFIRMED×3，全 FIX_NOW）

- [x] 4.1 **F1 status 覆写闸**：durable master 已终态**且**投影完整
      （complete 判据对齐 journal:3085-3111）时，resume 腿不进投影写路径
      （chain 层收口——chain_stage_execution resume 支或
      chain_array_accounting 调用侧，**journal 零改动**）；bound 未投影
      master（geometry-2）仍进投影，2.2 断言不动仍绿
      ——判据 `chain_array_accounting.settled_cohort_master`（cohort_members
      覆盖 + 每项终态 outcome + 终态 status），resume 腿
      `chain_stage_execution._durable_cohort_master` 按 **durable 行**（非入参
      快照）判闸，命中则 status 取自 durable 行、零写入
- [x] 4.2 **F2 占位符 withheld**：净化往返回来的占位 `advertised_uri`
      （`[object-uri]`）进投影前按 None 处理（chain_workspace.py:100-107
      或 chain_stage_execution.py:882-884 单点），杜绝洗进持久 `log_uri`
      ——收在 `chain_workspace.display_log_publication_for_pipeline_job`，
      判据用既有 `EVIDENCE_REDACTION_PLACEHOLDERS`；`log_uri=None` 对两个写
      实现均为"不动存量"（journal:3311 `if log_uri is not None`、
      chain_repository:781 `COALESCE`），已实证不会抹掉真实 URI
- [x] 4.3 **F3 2.2c 锁复明**：`_spy_cohort_projections` 断言 resume 趟
      projector 返回 `{"total": 0, "pipeline_status": 0, "pipeline_event": 0}`
      + `log_uri` 改用未净化 durable 读逐字节比对（净化读比 URI 恒同形，
      是瞎锁）；4.1/4.2 修完后该锁自然转绿，作共同回归证据
      ——4.1 闸使 projector 根本不被调用，故按编排者给的替代口径断言
      `calls == []`；durable 比对走 `_pipeline_job_for_id_unlocked` 未净化读
- [x] 4.4 **红证（F1）**：公开入口两趟 `orchestrate_cycle`，第二趟聚合改
      `["failed","succeeded"]`——修复前红形：succeeded master 被覆写
      `partially_failed`+`NODE_FAILURE` 且 candidate_projections 不动；
      修复后：durable 行零语义字段变化
- [x] 4.5 follow-up 立案：projector 批量写（journal:3357
      `_journal_record_for_write`）与 `_write_pipeline_job_unlocked` 绕过
      `_strip_redaction_placeholders`（:8700-8718 契约）——pre-existing，
      版位含 reconcile.py:1097 腿核查
      ——已立 #1592（pre-existing @707cd338，与 #1187 相关不重复，互链
      #1589/#1591；reconcile.py 腿今天不传 log_uri 记为现状证据）
- [x] 4.6 三套件重跑 + ruff + openspec validate（1154 passed；ruff
      `services tests` 全过；openspec strict 通过）
      ——连带：2.1 `test_resume_of_a_terminal_cohort_projects_with_the_real_master_slurm_id`
      的 geometry 改为"首趟 accounting 不完整→durable `reconcile_unverified`
      未投影 + 终态快照 resume"（原 geometry 已终态且投影完整，被 4.1 闸短路，
      projector 不再被调用，id 断言将永远空转）；身份断言原样保留并已用
      定向 mutant（回退成 `terminal["job_id"]` 嗅探）验证仍会红。
      2.2b docstring（`:13641`）随 4.1 再次失效，已按"settled master 第二趟
      根本不进 projector"改写（断言不动）

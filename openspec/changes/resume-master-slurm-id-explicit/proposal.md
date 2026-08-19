# Proposal: resume-master-slurm-id-explicit (#1410)

## Why

Issue #1410（#1312 task-0 探针范围外发现，pre-existing @09a0cfb8）：
`services/orchestrator/chain_array_accounting.py:362`

```python
master_slurm_job_id = str(terminal.get("job_id") or terminal.get("slurm_job_id") or "")
```

按 **gateway job 形状**嗅探 `terminal`（gateway dict 的 `job_id` 即 Slurm id）。
resume 腿分两支：行**非终态**时先 poll，`terminal` 换成 gateway 形状
（`:827-840`，poll 回来的 `job_id` 就是 Slurm id）——该支今天取值**正确**；
行**已终态**时不 poll，`chain_stage_execution.py:821` `terminal = dict(job)`
是 **pipeline_job 行**（`job_id` = `job_cycle_<source>_<cycle>_forecast`，
`slurm_job_id` 才是真实 Slurm id），`:896` 用该形状调
`_record_cycle_stage_status_override` → 投影收到的 master id 是 pipeline
job id。下游判据 `file_orchestration_journal.py:3011`
`existing["slurm_job_id"] != master_slurm_job_id` **永不相等** → 恒进
defer（`identity_mismatch_blocked` / `SLURM_MASTER_IDENTITY_MISMATCH`）：

1. 已终态支的 defer 在 journal `:3467-3468` 短路成 `idempotent`——整趟
   静默 `{"total": 0}`，resume 的 cohort 记账哑火，零 evidence 零日志差异。
   （注：终态 master 必已投影完毕——`project_forecast_cohort_tasks` 落终态
   要求 complete=True（journal:3085-3111）——故 per-task 行**不**停在旧值；
   本缺陷的实害是哑火不可观测 + 下述第 2 类伪事件，issue 原文对 per-task
   影响的陈述偏高，本 fixture 按实收敛。）
2. 锁内读若非终态则写 `reconcile_unverified` + 身份污染伪事件，脏化
   `identity_blocked_streak` 类读数——该几何由 tasks 2.2 的 geometry-2
   构造钉成真实持久化红证（bound 未投影 master + 终态快照 resume）。

对照兄弟腿（当前正确，不得回归）：`chain_stage_execution.py:627` 的
`terminal` 来自 gateway 形状（`:605`）；`reconcile.py:1097` 显式传
`str(record.slurm_job_id)`。issue 探针实测：resume 腿传
`job_cycle_gfs_2026050100_forecast`，gateway 腿传 `2001`。

## What Changes

按 issue 推荐方案，**嗅探改显式入参**：

- `chain_array_accounting.record_cycle_stage_status_override` 新增
  keyword-only 必填参数 `master_slurm_job_id: str`；`:362` 嗅探行删除。
  projector 分支内该值为**空或非数字**时 **fail-closed**：抛
  `OrchestratorError("SLURM_MASTER_IDENTITY_UNAVAILABLE", ...)`（含
  pipeline_job_id/stage evidence）。归因口径：file journal 契约下存量
  master 的 `slurm_job_id` 恒为纯数字（journal:2033 强制），空/非数字
  入参**永远**在 :3011 判不等而落进 defer 的静默/误标分支——fail-closed
  的价值是把这条静默分支换成可归因错误。
  非 cohort 分支（不走 projector）不消费该参数。
- wrapper `chain_forecast_orchestrator_cycle.py:486`
  `_record_cycle_stage_status_override` 签名同步透传。
- 两个调用方：
  - submit/poll 腿 `chain_stage_execution.py:627`：传作用域内已有的
    `slurm_job_id`（gateway 真实 id）。
  - resume 腿 `:896`：传 `str(job["slurm_job_id"])`（`:845` 聚合闸已保证
    该键非空——本腿唯一进入 override 的路径要求 `job.get("slurm_job_id")`
    truthy）。
- `reconcile.py:1097`（直调 journal projector）不动。
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
- Risk packs: state-semantics selected（master id 取值真值表分三行：
  gateway 腿真实 id 不变；resume 非终态支（poll 后 gateway 形状）取值不变；
  resume 已终态支从 pipeline job id 变真实 id——从恒 defer 变 matched_bound
  对账，这是**有意行为修复**；空/非数字从静默退化变响亮抛出）。
  **申报的连带翻转**：改后 resume 已终态支会真正走进投影写路径
  （journal:3287-3312）——已投影完毕且字段一致时 diff 闸（:3313-3327）保证
  零写入（幂等，测试钉住）；字段不一致时（如晚一趟 sacct 退化）master 行
  `error_code`/`log_uri`/`finished_at` 会被新鲜聚合覆写——`status` 有 #1312
  粘性保护而 `error_code` 没有，该粘性缺口路由 follow-up issue（实现期间
  scribe 立案），本单如实申报不掩盖；
  test-evidence selected（测试盲区实锤：全仓 14 处 `master_slurm_job_id`
  测试引用全部直调 journal API 手写数字 id，chain 层 id 推导零覆盖——红证
  必须在 chain 层伪造 resume 场景取证）；其余 not selected。

## Must preserve

- submit/poll 腿（`:627`）与 `reconcile.py:1097` 的 master id 仍为真实
  Slurm id；`tests/test_gateway_reconcile.py` / `tests/test_orchestration_chain.py`
  既有**断言**零改动全绿；例外申报：`tests/test_orchestration_chain.py:13641-13647`
  的 docstring（"resume defer 分支使 sticky 行永不触达"）随本修复失效，
  **必须同步改写**（断言本身不动仍绿——`_permanently_failed_events` 只数
  `permanently_failed` 事件，投影发的是 status_change）。
- resume 非终态支（poll 路径）取值**结果**与行为不变；取值**源**已与
  终态支统一为显式绑定入参（`str(job["slurm_job_id"])`）。二者等价的依据：
  `get_job_status` 恒回显入参 id（`real_backend.py:1457` 以请求 id 构造
  记录，mock/fake 同理），故旧嗅探在该支取到的 `terminal["job_id"]` 与
  绑定值恒相等，journal `:3011` 的比较在该支 pre/post 均为恒等式，无判别
  力损失（verifier 四格探针实证）。
- journal projector 与 defer 语义零改动（`tests/test_file_orchestration_journal.py`
  全绿）。
- 非 cohort stage 的 override 路径行为不变。

## Seams under test

- chain 层 resume 场景构造（stub repository 捕获 projector 入参——issue
  探针先例；或 monkeypatch spy 包装 `project_forecast_cohort_tasks`）；
  journal 面红证（tasks 2.2）**自建 geometry-2 底座**（`_bind_cohort_master`
  非终态 bound master + 终态快照 resume，不复用既有骨架）；幂等零写入锁
  （tasks 2.2c）复用既有先例（`tests/test_orchestration_chain.py:12049`
  file journal + `_resume_cycle_stage` + 真实 projector；`:9007` crash→
  reconcile→resume replay）。

## Evidence mapping

- 验收 1（resume 已终态支 projector 收到真实 Slurm id）→ tasks 2.1 红证。
- 验收 2a（对账翻转可观测量：matched_bound + matched_slurm_job_id 写实 +
  零 mismatch 事件 + total>0）→ tasks 2.2 红证。
- 验收 2b（幂等零写入锁）→ tasks 2.2c。
- 验收 3（空/非数字 master id fail-closed 明确契约）→ tasks 2.3 两腿。
- 验收 4（兄弟腿不回归 + docstring 例外同步）→ tasks 3.1 三套件全绿 +
  tasks 2.2b。
- 连带翻转的粘性缺口 → tasks 2.5 follow-up 立案。
- Verification：`uv run pytest -q tests/test_orchestration_chain.py
  tests/test_gateway_reconcile.py tests/test_file_orchestration_journal.py`
  + ruff + openspec validate；merge 后 node-27 receipt 记 issue。

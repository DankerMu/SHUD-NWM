# state-attempt-scope-discipline

## Why

PR #1293（issue #1287）评审留下的三个有记录残留（#1298 / #1299 / #1300），全部属于同一
族缺陷：**attempt / stage / live-failure 的派生在某个消费点上没有受候选身份 / 作用域纪律
约束**——与 #1179 Review Failure Retro 的复发不变量同族（"载带的 stage-attempt 真值必须
在每一个消费点上受与行群体相同的候选身份/可见域纪律约束"），只是载体从 floors 换成了三条
更早的派生：

1. **#1298（p2）**：`_state_retry_attempt` 的 stage 轴只认 canonical downstream stage；
   非 canonical stage（典型：单 basin cycle 里 model-scoped 的 `download` 行）短路回
   flat count，PR #1293 round-4 的 family floor 随之失效——durable `_retry_4` 被丢弃、
   `new_attempt` 重铸已消耗的 attempt → reservation `ON CONFLICT` 落败 → **人工重试
   静默跳过、100% 复现死循环**。
2. **#1299（p3，robustness + 文本诚实）**：`_state_jobs` 在无 job 行时把 state 自身合成
   为一行（无 `job_id`），顶层 `pipeline_status` 经 `_job_status_text` 泄漏进候选作用域
   活失败域，运维手工钉的 `retry_count` 被拒。**生产路径当前不可达**（两条读路径已由
   round-5 闭合），但契约靠外部 projection 形状而非本模块保证，且模块 docstring / spec
   （`_state_has_candidate_scope_failed_job` docstring、spec 挂账句"tracked as #1299"）
   已把这一缺口写成挂账——修掉并让文本说实话。
3. **#1300（p2，master 既有）**：`_failed_stage` 的行扫描不看 cycle scope——多流域 cycle
   里 model-less cohort 行的 canonical stage 被当成"本候选的失败 stage"，cohort 的第 7 次
   retry 被记进候选自身预算：manual retry 的 attempt 3→8 跳号、auto-retry 把首次失败的
   候选判 `permanent`（`retry_limit_exhausted`）——静默、无证据、无日志。

三者合批的理由不只是同族：**#1298 与 #1300 强交互**。#1298 的非 canonical 臂若无 scope
纪律，会让 `stage="download"` 的读取吃到 cohort 的 model-less download 行（identity filter
对该行有保留 carve-out），直接回归 PR #1293 刚兑现的 #1287 download AC（8→3 又变回 8）。
只有在同一设计里同时落"非 canonical 臂 + cycle-scope 排除 + `_failed_stage` 的候选作用域
变体"，三条 AC 才能同时成立。

## What Changes

- `services/orchestrator/scheduler_state_rows.py`：
  - `_job_is_cycle_scope_row` 谓词从 `scheduler_state_manual_retry.py` 下沉至本模块
    （rows 是 import 底座；manual_retry re-export 保持兼容），语义逐字节不变。
  - `_state_retry_attempt` / `_state_job_retry_attempt` / `_job_retry_attempt` 增加
    **非 canonical stage 的第三条臂**：`_canonical_downstream_stage(stage)` 为 None 且
    stage 非空时，按 `_job_stage_name(job)` 原始值相等匹配行、仍走
    `max(flat or 0, max(effective_retry_attempt(job_id, recorded)))` 同口径，**且只统计
    非 cycle-scope 行**（scope 纪律从第一天就生效）。`stage=None` 臂与 canonical 臂
    逐字节不变；#1179 floors 只覆盖 canonical stage，非 canonical 臂无 floor 载带
    （窗口敏感，显式边界，见 design D1）。
- `services/orchestrator/scheduler_state_manual_retry.py`：
  - `_job_is_live_candidate_scope_failure` 增加**行身份**前置判据：无
    `job_id`/`pipeline_job_id` 的 `_state_jobs` 合成行不判活失败（合法生产行恒带 id）。
  - `_state_has_candidate_scope_failed_job` docstring 从"由 projection 形状保证 +
    tracked as #1299"改写为模块自身兑现的事实陈述。
- `services/orchestrator/scheduler_state_failure.py` +
  `scheduler_state_manual_retry.py`（gate 一处）：
  - 新增 `_candidate_failed_stage`：与 `_failed_stage` 同的显式键优先，行扫描**跳过
    cycle-scope 行**；**四个** attempt/policy 消费点（policy 分类、cancelled 分支
    `retry_policy.attempt`、manual-retry fallback `previous_attempt`、
    `_fallback_previous_attempt` 内部的 family-floor gate）切换到它——gate 属对
    #1300 声明边界的显式偏离（不切则主形状恶化为重铸 1，见 design D3）。
    `_failed_stage` 本体与其余消费点（重启路由、downstream 证据）逐字节不变。
- `openspec/specs/job-retry-mechanism/spec.md`（经本 change delta）：三处挂账措辞
  （"tracked as #1298/#1299/#1300"）随修复改写为修后事实；round-5 收窄的
  "only raises"/"pre-existing failed-stage cycle-blindness" 措辞还原为真正成立的强命题。
- `tests/test_production_scheduler.py`：三个 issue 的红先行判别腿 + 回归钉（详见 tasks）。

## Non-Goals

- canonical 臂对 model-less cohort 同 stage 行的统计（pre-existing，#1586 的行扫描通道
  族；本批不动 canonical 臂一个字节）。
- `_state_jobs` 本身的 fallback 语义（32 处调用方；#1299 推荐方案只动一个谓词）。
- `_failed_stage` 在重启路由 / downstream 证据等其余消费点的语义。
- #1179 截断几何与 floors 机制（正交；非 canonical 臂无 floor 是记录在案的边界）。
- #1292（marker id-取证）、#1294（marker 目标行状态域）、#1179/#1572/#1577/#1579。

## Closes

Closes #1298, closes #1299, closes #1300.

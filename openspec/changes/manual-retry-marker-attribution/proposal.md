# Manual retry marker 归属裁定：外来 marker 不得被 sibling 候选采信（#1205）

## Why

`services/orchestrator/scheduler_state_manual_retry.py` 的
`_manual_retry_markers`（`:42-80`，扫描环 `:66-79`）是 manual-retry
判定链上接触原始事件的入口，判据只有 `event_type ∈ {retry,
manual_retry}` + `details.trigger == "manual"` /
`details.manual_retry_marker is True`——**不看 entity_type、不看
entity_id 归属**。而事件行的可见性契约是有意 cycle-wide 的：

- `forecast_cycle` 事件按 cycle 匹配到全体 model 候选
  （`file_orchestration_journal.py:8477-8500`，且
  `_pipeline_event_target:3311-3329` 对该类事件 model_id 恒 None）；
- cycle-scope（model_id IS NULL）cohort job 的事件对全体候选可见
  （journal `_job_matches_candidate:8443-8462` + DB
  `chain_repository_state.py:535-559` 的 `model_id IS NULL` 分支）。

于是"针对某一个 model / 某个 cycle-scope job 的人工重试"被同 cycle
**全体** model 当成"我自己被人工重试了"：sibling 被判
`decision=retry / reason=manual_retry_requested`，外来 marker 的
`retry_count` 成为自己的 `new_attempt`，最终以钉死的
`<stage>_retry_<N>` 身份去抢预约——撞上被占槽位即静默
`skipped_duplicate_submission`（#1164 replay 现场实测：event_id 907
的 cycle 粒度 marker materialize 进 IFS/2026070512 全部 6 个 model
快照并被全部匹配；本地确定性复现 `_manual_retry_requested` 从
False 翻 True）。

勘探（HEAD af66f164 复核）发现缺陷面比 issue 记载更宽：

1. 无作用域扫描共 **3 份逐字重复**：`_manual_retry_markers`、
   `_manual_retry_payload`（`:357-380`）、`_manual_retry_new_attempt`
   （`:382-399`）；另有 `_event_is_manual_retry_marker`（`:334-338`）
   以同判据做 blocker 排除——review 证实该排除语义与归属无关且
   必须保持（候选自己的真实 blocker 不是 marker 形、从不被误跳；
   合取归属反而制造 active-blocker 回归，见 design.md）。
2. `state_evidence["manual_retry"]` 对**每个**决策分支无条件填充
   （`scheduler_state_evidence_owner.py:108` → `_manual_retry_payload`），
   而 manifest 侧 `_candidate_manual_retry_attempt`
   （`scheduler_candidate_manifest.py:263-279`）只看该 payload——
   候选自身 decision 非 manual_retry 也会被钉 `retry_attempt`。
3. 放大到执行层：`_retry_attempt_from_basins`
   （`chain_runtime_utils.py:305-326`）对 batch 全体 basin 取 max，
   单个泄漏 marker 钉死整个 batch 的 cycle 级 attempt；
   `chain_forecast_orchestrator_cycle.py:146-148` 的
   `context.retry_attempt` 短路（`:156-166` 为
   `_terminal_stage_needs_manual_retry`）据此强制重投。

## What Changes

读侧行可见性契约**不动**（cycle-wide 事件对诊断/evidence 是相关
事实）；消费侧两刀修复（fixture review round 1 修订：初版全量
fail-closed 谓词被实测证伪——cycle 级 stage 人工重试是被 3 份
spec + 5 个既有用例锁定的产品语义，不得收窄；取舍全文见
design.md）。

1. **刀 1（采信侧，窄）**：仅当 marker 事件
   `entity_type == "forecast_cycle"` 且无显式 model 归属时不被
   采信（issue 验收通道 1，#1164 现场 event 907 即此形）。显式
   归属出口：事件 `details.model_id` 或事件顶层 `model_id` ∈ 候选
   model 集合（从 `_state_jobs` 非空 model_id 派生；生产事件行无
   model 列——journal 不持久化、DB 不 SELECT——故生产
   forecast_cycle marker 现状 100% fail-closed，出口为写入侧未来
   定向重试预留）。其余 marker（entity 是 job、无 entity_id、任何
   非 forecast_cycle 形）采信语义一律不变——cycle 级 stage 人工
   重试保持有效。**Round-3 修订（decision-path 可达性）**：刀 1
   判据字段 `entity_type`/`model_id` 曾被 identity_filter 的
   decision-event 消毒剥离（过滤态恒放行）；修复为消毒白名单放行
   这三个判据键（`entity_type` + 顶层与 details `model_id`），
   它们是 fail-closed 判据的只读输入而非身份声明（design.md
   Round-3 节，378 个 decision-state 差分纯增量实测）。
2. **刀 2（钉值侧，round-1/2/3 修订后形态）**：最新携带
   `retry_count` 的 adopted marker 定权（终止性）；事件正向解析为
   cycle-scope job（`model_id` 空 ∧ `run_id` 前缀 `cycle_` 文法）
   时按 **stage 感知钉值规则**裁定——resolved job 活失败 ∧
   （`failed_stage` == job.stage ∨ 候选无自身 model 域活失败行）
   → 钉 marker `retry_count`，否则终止回落 `previous_attempt + 1`
   （issue 验收通道 2：cycle 计数不计入候选 forecast 级预算，但
   cycle 级 stage 修复目标本身的运维钉值保持有效——round-2 实测
   定标，design.md 五形 A/B/C/D/E）。entity 查不回 job 的事件默认
   钉值行为不变，**除非** entity_id 命中 cycle-scope job-id 文法
   `^job_cycle_<src>_<stamp>_...`（decision state 删除 cohort
   master 行后的 fail-open 洞，round-3 N1′ 窄化）。
   接线点共 3 处：`_manual_retry_markers` 扫描环、
   `_manual_retry_payload`、`_manual_retry_new_attempt`；
   `_event_is_manual_retry_marker` **不接线**（review P1-2 实测：
   合取会把外来 marker 变成 active blocker 反向压制候选自己的
   人工重试；marker 形事件无论归属都不该当 blocker）。
3. 回归测试（判别对形式，见 design.md oracle 节）：
   - 通道 1 负向 + 归属出口正向（details 与顶层 model_id 两变体）；
   - 通道 2 判别族（round-2 修订后）：同 stage cycle-scope marker
     钉 5（master parity）、交叉 stage / stale 形回落 previous+1
     （不越 forecast 预算）+ 本 model job 同构对照（钉住语义
     不变）；round-3 起另须 decision-path（identity_filter 过滤态）
     判别对（见 tasks.md ORACLE ROUTING）；
   - site 4 守卫：外来 marker 形事件（status_to=pending）与本候选
     自身 marker 共存 → requested 仍 True；
   - 既有正向：`tests/test_file_orchestration_journal.py:2473-2513`、
     `tests/test_file_orchestration_migration.py` 与
     `tests/test_production_scheduler.py` 的全部既有用例零改动
     保持通过。

Out of scope: 读侧行可见性契约（`_event_matches_candidate_rows` /
`_job_matches_candidate` / DB SQL——有意且正确，download /
state_save_qc cohort 依赖）；marker 新鲜度语义（#1201）；
`skipped_duplicate_submission` 静默穿透（#1202）；manual retry 写入
侧与 db-free 执行入口（#1186）；`retry_attempt` 在 job_limit 截断下
降 0（#1179）；forecast_cycle marker 写入侧补 `model_id` 的运维
定向出口（本 change 只预留读侧对齐面）；journal 事件行增加 model
列（读侧无 model 维度是既有存储契约）。

## Impact

- Affected code: `services/orchestrator/scheduler_state_manual_retry.py`
  （唯一生产文件：刀 1 + 刀 2 + 三点接线），
  `tests/test_file_orchestration_journal.py` 或
  `tests/test_file_orchestration_migration.py`（新增回归用例，
  按既有 fixture 风格就近放置）。注：
  `chain_runtime_utils.py:152-165` `_manual_retry_scoped_cycle_execution`
  消费的是 `state_evidence["manual_retry"]` payload，随 payload
  修复传递生效，无需直接改动。
- Affected specs: `job-retry-mechanism`（1 ADDED requirement：
  manual retry marker 的候选归属裁定）。
- Frozen surfaces（零 diff）：`file_orchestration_journal.py`、
  `chain_repository_state.py`、`scheduler_state_decision.py`、
  `scheduler_state_failure.py`、`scheduler_state_evidence_owner.py`、
  `scheduler_candidate_manifest.py`、`chain_runtime_utils.py`、
  `chain_forecast_control.py`、`chain_forecast_orchestrator_cycle.py`、
  `scheduler_state_identity_filter.py`、全部既有测试用例。

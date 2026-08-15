# Proposal: master-row-permanent-failure-marking (#1312)

## Why

已批准规格 `openspec/specs/job-retry-mechanism/spec.md:153` 要求非瞬时失败
"SHALL mark the job as permanently failed immediately"。file-journal 平面的
**master-row geometry 两条出口都缺这半边契约**（形状由 `506d99dd` 引入，自
`949697c1`（#1161）把 `OUT_OF_MEMORY` 移入非瞬时集后变 load-bearing）：

1. 生产实际路径——`chain_forecast_orchestrator_cycle.py:190-197`
   `_schedule_cycle_stage_retry` 的调用方短路臂：master 行 +
   `should_auto_retry` False 时直接 `return None`，不落标、不追加事件；
   `chain_forecast_execution.py:225-229` 随后以 `PipelineResult("failed")`
   收场，持久化行停在 `status="failed"`。
2. 休眠副本——`file_orchestration_journal.py:6607-6608`
   `FileJournalRetryService.handle_failed_job` 的 master 分支
   `should_auto_retry` False 臂原样返回 `current`，绕过同函数 `:6621` 的
   `mark_permanently_failed`（非 master 行走 `:6619-6621`，行为正确）。

全仓 `mark_permanently_failed` 仅两个写入点（`retry.py:409`、
`file_orchestration_journal.py:6704`），对 master-row geometry 均不可达。
已验证后果（PR #1311 round-1 verifier 探针）：master 行 `OUT_OF_MEMORY`
持久化为 `"failed"` 而非 `"permanently_failed"`。影响半径 = 仅标签：
`chain_forecast_orchestrator_cycle.py:166` 等全部终态集合两状态同列，无重投
/配额/数据正确性风险；真实代价是 SHALL 条款在主几何上不成立 + 运维拿不到
永久失败信号（无状态、无事件），配置类故障被误当可重试故障排障。

## What Changes

- 裁决方向：**修代码**（issue 推荐路径），不走 spec deviation——master 行
  与非 master 行应同性（永久失败的可观测语义与行几何无关），备选方案会在
  SHALL 条款上留一条需长期维护的几何例外且信号永久丢失；cohort projection
  反论的正面回应见 design D1。
- **新增 typed authority transition**（fixture-review round-1 P1-1：现
  `mark_permanently_failed` 内部走 `update_pipeline_job_status`，对 master
  行在 `:3238-3243`/`:3251-3256` 无条件 raise，全仓无可替代出口）：参照
  `reject_pipeline_job_submit_attempt`（`:2233`）先例新增 master 行合法的
  `permanently_failed` 写入口；`FileJournalRetryService.mark_permanently_failed`
  对 master 行改走它；`:3240/:3251` 通用禁令不放宽（design D5）。
- `services/orchestrator/chain_forecast_orchestrator_cycle.py:190-197`：
  调用方短路臂在 `return None` 前按 **capability 门控**落标（仅
  file-journal 形 service；store-less `RetryService` 形保持现状不抛——
  round-1 P1-2），标落在 `result.pipeline_job_id` 对应 master 行并读回验
  证；短路不取消（`506d99dd` legacy adapter 动机保留），PipelineResult 形
  状不变（design D2）。
- `services/orchestrator/file_orchestration_journal.py:6607-6608`：休眠分支
  经 `self.mark_permanently_failed` 落标后返回；幂等前置以持久行为准、事
  件仅随真实翻转追加（round-1 P2-4，design D3/D4）。两处副本判据一致，单
  臂红证独立钉住（design D8）。
- 覆盖按 `NON_TRANSIENT_ERROR_CODES` 整体收口（测试两臂各锁
  `OUT_OF_MEMORY` + `INVALID_MANIFEST`），不只补 OOM。
- 规格 delta：在 `job-retry-mechanism` ADD Requirement，把 master-row 落标
  /单事件幂等/upstream-refresh 不复活（round-1 P2-3 裁决）/store-less 形
  状豁免显式钉进 spec。

## Impact

- Affected specs: `job-retry-mechanism`（ADDED Requirement
  "Permanent-Failure Marking Covers Master-Row Geometry"，扩展既有
  `Retry Guard — Non-Transient Error Exclusion` 与
  `Max Retries Exhausted — Permanent Failure` 两条 requirement 到 master
  几何；round-2 Note-3 措辞对齐）。
- Affected code: `services/orchestrator/chain_forecast_orchestrator_cycle.py`
  · `services/orchestrator/file_orchestration_journal.py`（两处臂 + 新
  typed transition + `project_forecast_cohort_tasks` 终态粘性，design
  D5/D9）· `services/orchestrator/chain_array_accounting.py`
  （projection-commit 白名单补 `permanently_failed`，D9 实施修订）·
  `tests/test_orchestration_chain.py` ·
  `tests/test_file_orchestration_journal.py`。
- 不动：非 master 行路径（`file_orchestration_journal.py:6619-6621`、
  `retry.py:340-342`）；DB 平面（#1161 已对齐）；`auto_retry_skipped`
  事件 payload（`spec.md:154`，全仓零实现，#1314 另行路由）；db-free 决策
  梯 permanence 语义（#1313）；终态集合与 PipelineResult 形状。
- 爆炸半径（explorer 全仓核查 + fixture-review round-1 P2-3 补项）：翻转
  持久化状态触达 (a) sticky-guard 站点（`persistence.py:173-174`、
  `retry.py:410-411`、`file_orchestration_journal.py:6706-6707`、`:3261`
  write-once）；(b) `scheduler_state_failure.py:212-213`
  `permanent_failure_guard` 归因标签；(c)
  `_terminal_stage_can_retry_after_upstream_refresh`
  （`chain_forecast_orchestrator_cycle.py:169-178`）白名单不含
  `permanently_failed`——落标后 upstream-refresh 重投分支对该行不再触发
  （含耗尽类瞬时码，round-2 P1-C 并入落标域），**显式裁决接受**（design
  D6/D7，测试钉住）；(d) cohort projection 抹回链（round-2 P1-A）：
  `project_forecast_cohort_tasks`（`file_orchestration_journal.py:2993-3021`）
  无终态粘性，二趟 resume 会抹标振荡——本 change 加同 `:3173-3175` 形粘
  性收口（design D9）；`production_contract.py:380` 把
  `permanently_failed` 别名折叠回 `failed`，外部契约不变；其余全部消费面
  两状态同集合。

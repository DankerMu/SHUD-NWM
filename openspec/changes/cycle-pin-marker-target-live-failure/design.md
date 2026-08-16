# Design: cycle-pin-marker-target-live-failure

## Context

`_cycle_scope_marker_pins_attempt` 决定「cycle-scope marker 的 `retry_count`
是否钉住派生 attempt」。#1287（PR #1293）把同一条规则的候选侧
`_state_has_candidate_scope_failed_job` 加宽到 live-failure 域
（blocking ∧ ¬ACTIVE，含 `cancelled`），并抽出了两个共享半谓词
`_job_is_live_candidate_scope_failure` / `_state_hydro_run_is_live_failure`；
marker 目标侧被显式声明为边界外（docstring 现记录该不对称并指向 #1294）。

本 change 关闭该不对称：`cancelled` 的 cycle-scope 行是合法 repair target
（与 `retry.MANUAL_RETRY_SOURCE_STATUSES`、blocker 谓词口径一致）。

## Decisions

### D1: 共享行级谓词，两侧由构造同源（采纳 issue 推荐方案）

新增 `_job_row_is_live_failure(job)`：

```
¬_pipeline_job_is_repaired_stage_evidence(job)
∧ ¬_job_is_unsubmitted_auto_retry_placeholder(job)
∧ _manual_retry_blocking_pipeline_status(status)
∧ status ∉ ACTIVE_PIPELINE_STATUSES
```

- `_job_is_live_candidate_scope_failure` = `¬_job_is_cycle_scope_row(job)`
  ∧ `_job_row_is_live_failure(job)`（cycle-scope 排除**留在候选侧**，不进
  共享谓词——否则 marker 臂恒 False，marker 目标本来就是 cycle-scope 行）。
- `_cycle_scope_marker_pins_attempt` 的状态臂改为
  `not _job_row_is_live_failure(job)`。
- 域等值：`_manual_retry_blocking_pipeline_status ∧ ¬ACTIVE` ≡
  `FAILED_PIPELINE_STATUSES ∪ {"cancelled"}`（两常量集互斥），故 marker 侧
  是严格加宽，`failed`/`submission_failed`/`partially_failed`/
  `permanently_failed` 行为不变。

否决备选（维持不对称、只改 docstring/spec）：`retry.py` 的
`MANUAL_RETRY_SOURCE_STATUSES`、`:505` 的显式 cancelled 处理、blocker 谓词
三处证据都指向 cancelled 是一等 repair target，论证不出实质不对称理由。

### D2: docstring 改写口径

- `_cycle_scope_marker_pins_attempt`：删除「The two sides of this rule do
  NOT share one status domain … tracked separately by #1294」段，改为记录
  两侧同源于 `_job_row_is_live_failure`；row-absent 臂读不到行状态、按
  state-level staleness 证据裁决（证据面更窄 ⇒ 部分形态钉得更宽）的分歧
  事实显式点名，残留由 #1308 跟踪。
- `_unresolvable_marker_entity_pins_attempt` docstring 中「narrower bare
  ``FAILED_PIPELINE_STATUSES`` vocabulary, #1294」句同步修正（该臂自身语义
  不动，只改它对本臂的引用描述）。

### D4: repaired-annotation producer 门与 live-failure 域同源（round-3 F1 闭合）

Round-3 复审证实：`repair_status="repaired"` / `active_blocker=False` /
`repaired_stage_evidence` 的**全部写入方**都把 repair-target 过滤在裸
`FAILED_PIPELINE_STATUSES` 上——

- `chain_source_cycle.py`（`failed_jobs` 过滤 + 空即早退）
- `chain_repository_state.py` `_manual_stage_repair_state`（retry 血缘成员
  过滤 + 同 stage 旁排过滤，两处）

于是 `cancelled` 行被成功 retry 修复后**拿不到任何 repaired 注记**：D1 的
共享谓词只在消费端同源，排除合取项在生产投影上对 cancelled 恒不可满足，
「repaired stage evidence … pins nothing」对 cancelled 是空头支票（HEAD 实测
钉住陈旧 retry_count，master 拒绝）。

裁定：把 producer 门加宽到与消费端同一 repair-target 域
`FAILED_PIPELINE_STATUSES ∪ {"cancelled"}`（与
`retry.MANUAL_RETRY_SOURCE_STATUSES`、blocker 谓词同口径），抽成共享常量
（如 `REPAIRABLE_PIPELINE_STATUSES`）而非再写字面集合。副作用即收益：
#1287 加宽的候选侧同一盲点（repaired 的 cancelled 候选行恒 live、关死臂 2、
污染 `_restarted_stage_family`）由同一构造一并治愈——depth 不变量禁止拆分。
「pending 占位形」旁支（`slurm_job_id` 空 + `retry_count>0`）保持原样不动。

### D3: hydro 语义不进本臂

本臂判的是一行 job；hydro run 不是 job 行。`_state_hydro_run_is_live_failure`
不参与 marker 目标判定（non-goal，与 issue 边界一致）。

## Invariant Matrix

Governing invariant: 共享 live-failure 谓词的每个合取项（status 减法、
placeholder 门、repaired 排除）对 widened 域全体成员（含 cancelled）端到端
可满足且有判别锚；「repaired/superseded 的行不是 repair target」在
producer 与 consumer 两端同域。
Source-of-truth identity/contract: repair-target status 域 =
`FAILED_PIPELINE_STATUSES ∪ {"cancelled"}`（共享常量承载）。
Surfaces:
- Producers: `chain_source_cycle.py` failed_jobs 门；
  `chain_repository_state.py` `_manual_stage_repair_state` 两处过滤
- Validators/preflight: `_job_row_is_live_failure` 及两个消费者（本 change
  已交付）
- Storage/cache/query: journal `_compact_cycle_scope_job` 投影键含
  status/retry_count/slurm_job_id（round-3 已核，两读路径等价，不改）
- Public routes/entrypoints: 无直接路由；`_marker_event_pins_attempt` 唯一
  内部入口（不改）
- Frontend/downstream consumers: `scheduler_state_failure.py` 消费
  new_attempt（不改）；repaired 注记读者 `_pipeline_job_is_repaired_stage_evidence`
  / `_manual_retry_marker_repairs_historical_failure`（不改，行为随注记
  可产出而自然恢复）
- Failure paths/rollback/stale state: 陈旧 marker 钉已消耗 attempt 号 →
  `skipped_duplicate_submission` 静默失效（#1201 家族）——修复的靶点
- Evidence/audit/readiness: `repaired_stage_evidence` state 键随 producer
  加宽对 cancelled 可产出（row-absent 臂受益，语义不改）
Regression rows:
- cancelled cycle-scope 行 + succeeded `_retry_1` 后继 + 指向该行的 marker
  （真实投影产 state）→ 注记产出、`_job_row_is_live_failure=False`、拒钉、
  `new_attempt = previous+1`
- failed 同形 → 行为与 master 一致（既有护栏），拒钉
- cancelled 无修复后继 + marker → 仍钉 marker retry_count（本 change 主判别）
- 候选侧：repaired 的 cancelled 候选行不再算 live failure（臂 2 恢复可开，
  `_restarted_stage_family` 不再计入其 stage）
- 无 cancelled 行的既有形状 → producer 行为逐位不变（1417 例全绿）
- **加宽使 cancelled 可达的非注记输出面（round-4 披露，全部为
  cancelled↔failed 平价规范化，非新语义）**：
  - 未修复的 cancelled-only cycle-download → 产出 `active_failure_job`、
    state `failed_stage` 由 None 变 "download"（与 failed 腿一致；decision
    state 经 aggregate scoping 剥离，无决策 delta 实测）
  - 修复后的 cancelled cycle-download 进入 evidence 选取 if/elif → 与
    failed 腿逐位一致（含 completed_stage_evidence/restart_stage 被
    source-cycle 形 repaired 证据抢占——该抢占是 **pre-existing 缺陷**，
    failed 腿在 master 同样表现，已路由 issue 跟踪）
  - `_candidate_manual_stage_repair_state` 单赢家 `break` 使较新的
    cancelled 修复可与较老的 failed 修复竞争——竞争本身与两条 failed
    血缘时的 master 行为逐位相同，**单赢家挤占是 pre-existing 缺陷**，
    已路由 issue 跟踪

## Risks / Trade-offs

- 触发面今天生产不可达（`record_manual_repair` 零非测试调用方），修复属
  「#1186 接线前的预闭合」；风险集中在回归面——由 tasks 2.3 的护栏矩阵与
  #1287 判别对覆盖。
- `_restarted_stage_family` 消费 `_job_is_live_candidate_scope_failure`：
  抽取重构若误动 cycle-scope 排除层，tasks 2.2（own jobs 全 succeeded 时
  臂 2 仍钉）即红。

## Migration

无数据迁移。主 spec 措辞随 merge 后 `openspec archive` 由 delta 回写。

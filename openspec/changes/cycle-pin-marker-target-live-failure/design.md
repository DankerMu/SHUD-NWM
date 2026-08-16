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
  两侧同源于 `_job_row_is_live_failure`；row-absent 臂（#1292）仍窄域的
  事实显式点名。
- `_unresolvable_marker_entity_pins_attempt` docstring 中「narrower bare
  ``FAILED_PIPELINE_STATUSES`` vocabulary, #1294」句同步修正（该臂自身语义
  不动，只改它对本臂的引用描述）。

### D3: hydro 语义不进本臂

本臂判的是一行 job；hydro run 不是 job 行。`_state_hydro_run_is_live_failure`
不参与 marker 目标判定（non-goal，与 issue 边界一致）。

## Risks / Trade-offs

- 触发面今天生产不可达（`record_manual_repair` 零非测试调用方），修复属
  「#1186 接线前的预闭合」；风险集中在回归面——由 tasks 2.3 的护栏矩阵与
  #1287 判别对覆盖。
- `_restarted_stage_family` 消费 `_job_is_live_candidate_scope_failure`：
  抽取重构若误动 cycle-scope 排除层，tasks 2.2（own jobs 全 succeeded 时
  臂 2 仍钉）即红。

## Migration

无数据迁移。主 spec 措辞随 merge 后 `openspec archive` 由 delta 回写。

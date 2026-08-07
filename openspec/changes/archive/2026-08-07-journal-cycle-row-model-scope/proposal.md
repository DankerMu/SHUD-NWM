# Proposal: journal-cycle-row-model-scope

## Why

Issue #1288（PR #1286 round-3 escalation,issue-scribe 端到端复现并订正机制）:
db-free(file-journal)读路径的 `_job_matches_candidate`
（`services/orchestrator/file_orchestration_journal.py`）cycle-run_id
分支不看 `model_id`——`run_id == cycle_<source>_<stamp>` 且 `model_id`
非空指向**他模型**的 job 行,对本候选判 True。行被收编后,其
`entity_type=pipeline_job` manual retry marker 经成员判定
（`_event_matches_candidate_rows`）随行进入候选事件表;#1205 两把刀均不拦（刀 1 只 gate
`forecast_cycle`,刀 2 的 cycle-scope 判定要求 `model_id` 为空——他模型行
`model_id` 非空,`_marker_event_pins_attempt` 的 `_job_is_cycle_scope_row`
未命中臂直接钉值）。
端到端实测:他模型行 `retry_count=5` 把自身 `retry_count=0` 的候选钉成
`new_attempt=5`。

DB 读路径无此缺陷:`chain_repository_state.py:510-515` 的 cycle-run_id 谓词带
`model_id IS NULL`。同一份数据两条读路径判定相反,是 node-22 db-free 生产形态
独有的越界。master 既有(缺陷点自 `78d54d11` 初版;`22103181` 补 model-less
分支时未回头收窄旧分支),非 #1286 回归。

## What Changes

- 读侧收窄,seam 定在 **candidate_state 投影**（fixture 复审 P1-1 裁定;
  唯一生产文件仍是 `services/orchestrator/file_orchestration_journal.py`）:
  在 `candidate_state` 的 `pipeline_jobs=` 与
  `pipeline_events=` 推导处排除"他模型具名 +
  cycle-run_id"行及其 `pipeline_job` 事件（可复用
  `_is_model_less_cycle_scope_job`）。共享谓词
  `_job_matches_candidate` / `_filter_cycle_rows_for_model` **逐字不动**——
  它同时驱动 `has_active_pipeline`、`has_completed_pipeline`、
  `active_slurm_jobs`、直录读面
  （`_iter_direct_pipeline_job_records_for_cycle`）四个
  gate surface。其中 active-pipeline / active-slurm-jobs 两个 gate 的
  DB 谓词（`chain_repository.py:74-79`、`:177-181`）**有意保留**无条件
  cycle-run 子句;`has_completed_pipeline` 的 DB 侧
  （`chain_repository.py:97-109`）只查 `hydro.hydro_run`,无 job 谓词可
  对照,按 seam 纪律同样不动。改共享谓词会在 db-free 上放松
  `active_duplicate_pipeline` 去重闸（复审实测 `has_active_pipeline`
  True→False、`active_slurm_jobs` 非空→空,且无既有测试守护）。
- **行/事件同批过滤是硬约束**（复审 P1-1b）:只滤行不滤事件时,落单
  marker 的 entity 退化为不可解析的 `job_cycle_*` 语法形,经
  `_unresolvable_marker_entity_pins_attempt` 在 cycle 同、
  `failed_stage="forecast"` 时照钉——缺陷经 #1287 通道原样复活。测试
  必须断言行与事件同时离场。
- journal/DB 谓词口径对齐在代码注释中显式指向
  `chain_repository_state.py:510-515`（issue AC）。
- 正负向回归测试(见 tasks §2)。

## Impact

- Affected specs: `job-retry-mechanism`（:312 requirement——row 归属对齐条款
  + 新 scenario;与 #1287 同穴,原 visibility 句加限定）
- Affected code: `services/orchestrator/file_orchestration_journal.py`
  （唯一实现点,seam = candidate_state 投影,共享谓词不动）;只读对照
  `chain_repository_state.py:510-515`（候选态谓词,不改）与
  `chain_repository.py:74-79`/`:177-181`（gate 谓词,不改且是"不收窄
  共享谓词"的反向依据）;下游 `scheduler_state_manual_retry.py` 两把刀
  不改（本修复在读侧关闭形状,刀谱语义保持 #1205 交付态）
- 附带效应（方向性说明,复审 P1-1 要求）:他模型行离开候选投影后,
  `latest_job` / `pipeline_status` 派生（`chain_repository_state.py:760-767`
  的回退链）在该形状上从"外模型 running 行 → running"变为 `None`——
  这与 DB 候选态口径一致,但方向是**放松**而非收紧;cycle 级去重闸不经
  candidate_state,不受影响（tasks 2.6 负向回归覆盖）

## Non-goals

- model-less cohort 行 cycle-wide 可见性契约（必须原样保留,是负向回归对象）
- 刀 1 扩展到 `entity_type=pipeline_job`（issue 备选方案,不采用——留污染面
  且不消除 journal/DB 分歧）
- `_unresolvable_marker_entity_pins_attempt:173-174` fail-open 次要暴露面
  （截断/identity filter 丢行保事件的推断几何,issue 明记未端到端复现;
  #1292 域——但注意其**可解析臂**正是 P1-1b 事件同批过滤要防的复活通道,
  该防线在本 change 内,fail-open 臂本身不在）
- `job_limit` 截断降 attempt（#1179）、manual retry 运维入口（#1186）、
  marker 回扫（#1289）

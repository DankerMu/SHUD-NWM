# Model-swap forcing witness completion (#1844 + #2254 + #1846; #1845 descoped)

## Why

本批是 #1826 / PR #1843 的收尾。那次修复立下的不变量是：

> 任何 `restart_stage == "forecast"` 的候选决策发出之前，必须已经为该候选自己的
> `(source, cycle, basin_version_id, model_id)` 查过 per-model forcing 见证。

它把「模型换代后自己没有 forcing，forecast 一秒死在 `ARTIFACT_NOT_FOUND`」这个危害类封在了
strict-warm-start 车道。剩下的是一个同类洞、一个 retry 序号缺陷，外加一个悬而未决的设计取舍：

- **#1844（调度决策侧一个未设防发射点）**：同一车道上 `terminal_run_manifest_missing` retry 是另一个既有的未设防发射点，
  不在 #1844 范围，round-1 审查确认后另立 issue 跟踪。`_journal_predecessor_identity_quarantine`
  （`services/orchestrator/scheduler_candidates.py:2304`）在 `strict_warm_start is None` 车道上，
  用硬编码 `restart_stage: "forecast"` 的 retry 替换 skip 决策（`:613-624`）。之后只有
  `strict_warm_start is not None` 才过 `:638-650` 的见证闸。
- **#2254（retry 序号低估）**：`_next_retry_attempt_for_stage`（`services/orchestrator/chain.py:858`）
  对 `base + "_retry_"` 之后的整段 tail 做 `int()`。叠加形 id `B_retry_1_retry_2_retry_3` 抛
  `ValueError` 被跳过，于是与 `P={B_retry_1}` 共存时算出 `2`、铸出 `B_retry_2`；而 `retry_identity`
  以最后一个 suffix 为准，有效序号是 3。**已从公开入口 `orchestrate_cycle` 复现**：`B_retry_2` 与其
  idempotency key `…:forecast:retry_2` 都到达了 reserve 与 gateway。
- **#1846（设计裁决）**：对自己没有 forcing 的重铸身份模型，要不要由无人值守车道自动重入 forcing 阶段？
  #1843 考虑过并否决了，但没有作为独立决策落档。

## What Changes

- **#1844**：在 `:613` 调用点，仅对 `strict_warm_start is None` 车道，把 quarantine 返回的 retry 交给既有的
  `_strict_warm_start_forcing_witness_decision`（完整的上游产物守卫，含 copyback 腿）。forcing 见证缺席时落入 #1843 的具名 `blocked`
  （`missing_forcing_package_uri` / `forcing_version_row_absent`）。breaker 臂本就是 `blocked`、不提交作业，
  不加代码。
- **#2254**：`_next_retry_attempt_for_stage` 改用 `retry_identity.retry_suffix_attempt`（最后一个 suffix）
  解析每行序号。以下行为保持不变：matching-stage 过滤、前缀过滤、malformed tail 跳过、
  `context.retry_attempt` 优先级、PR #2253 的 snapshot-authority。
- **#1846 裁决：保持 `blocked` + 人工排空，不做无人值守自动重入 forcing。** 书面 Decision 见 design.md，
  逐条回应 #1843 的三条否决理由。`docs/runbooks/current-production-ops.md` hop 5 写明两点：回补是模型换代的
  必经步骤；各车道 `blocked` 各自的排空通道。另在 #1826 下留言，把该成本定性为已接受。

## Descoped

- **#1845（unscoped cohort 下 forcing stage 模型盲 resume）**：经用户裁决降为 follow-up，保持 open。
  fixture review 与可行性核查表明三种修法都有真实代价，需另行设计：
  - issue 推荐的对称先例不可达；
  - 复用部分阵列失败机制结构上不可行；
  - 整 stage fail-closed 会让整个 cohort 停摆。

  核查结论写回 #1845（design「Descoped: #1845」）。

## Impact

- Affected specs: `production-scheduler-orchestration`（ADDED 1）、`slurm-job-chain`（ADDED 1）
- Affected code:
  - `services/orchestrator/scheduler_candidates.py` — `:613` 调用点
  - `services/orchestrator/chain.py` — `_next_retry_attempt_for_stage`
- Affected tests: `tests/test_production_scheduler.py`、`tests/test_scheduler_generation.py`、
  `tests/test_orchestration_chain.py`（以最终 diff 为准）
- Affected docs: `docs/runbooks/current-production-ops.md` hop 5
- Non-goals:
  - 不改 `job_matches_stage` 与 `_candidate_scoped_cycle_execution`
  - 不改 `canonical_readiness` 门
  - 不改 reservation / CAS / 锁 / gateway
  - 不修 #1845
  - 不修 #2393（下游 stage 继承上游 `context.retry_attempt`）
  - 不修 `cycle_download_success_missing_raw_manifest` 的不可达
  - 不实现任何自动重入 forcing 的车道

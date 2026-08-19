# chain-manual-retry-marker-freshness

## Why

Issue #1201（p1，#1164 six-basin replay 生产兑现）：chain 侧 `_retry_attempt_from_basins`
把 `state_evidence.manual_retry` 的 `new_attempt`/`attempt`/`retry_count` 裸读进
`context.retry_attempt`，无任何新鲜度/绑定校验；`_retry_cycle_stage_job_id` 的
`context.retry_attempt or _next_retry_attempt_for_stage(...)` 短路使每次强制重投瞄准
同一个 `<stage>_retry_<N>`。当该 id 已被绑定 `slurm_job_id` 的终态行占据时（reclaim
谓词正确地拒绝接管），reserve 必输 → `_skip_duplicate_submission` 静默跳过——**stage
永久无法重投且自锁**（IFS/2026070512 hhe forecast 被卡死至 quarantine）。

根因是两个写点的不对称：`state_evidence.manual_retry` 既由 freshness-gated 的
`_manual_retry_state_evidence` 写（仅当 `_manual_retry_requested(state)` 为真，此时
`decision == "manual_retry"` / `reason == "manual_retry_requested"`），也由
evidence-owner 面的 `_manual_retry_payload(state)` **无条件回显** raw marker（事故里
的陈旧 `{"marker":true,"new_attempt":1,...}` 正是这条通道）。scheduler-state 侧已有
完整的新鲜度语义（`_manual_retry_requested` → repairs-historical-failure /
overrides-blocker 判定）；chain 侧绕过了它。

## What Changes

（rev-1：fixture review P0——生产主通道是 manifest 铸造点，判别器下沉至该处。）

- **`services/orchestrator/scheduler_candidate_manifest.py`（生产主通道 gate）**：
  `_candidate_manual_retry_attempt` 以共享谓词判别——`state_evidence` 决策面无活跃
  manual-retry 决策（`decision == "manual_retry"` / `reason ==
  "manual_retry_requested"` 均未命中）时返回 None，manifest **不铸**
  `manual_retry_attempt`/`retry_attempt` 两键（现仅 `allowed is False` 拦截，陈旧
  raw echo 直接放行成 direct 字段、遮蔽下游一切判别）。
- `services/orchestrator/chain_runtime_utils.py`（纵深 gate）：
  - `_retry_attempt_from_basins` 的 `state_evidence.manual_retry` 分支同谓词判别；
    否则 fall through 到 `_next_retry_attempt_for_stage`。
  - 丢弃 claim 时发**结构化日志**（措辞"无活跃 manual-retry 决策"而非"陈旧"——
    更高优先 lane 可合法抢占活 marker；含 basin/claim/实际 decision），消除静默降级
    （AC-4）。
  - **运维/API 当次显式传入**的直接字段不变；manifest 铸出的同名字段已在铸造点受判
    （两来源区分见 design must-preserve 2）。
  - 判别谓词**单处实现**，两个 gate 同源调用。
- `services/orchestrator/chain_forecast_orchestrator_cycle.py` /
  `chain_manifests.py` / `chain_stage_execution.py`：不改（`or` 短路与
  `context.retry_attempt` 回写继承保留——修在污染源；三个 `context.retry_attempt`
  消费者随净化愈合，审计 + 测试钉）。
- `_manual_retry_scoped_cycle_execution`：现状保持 + 显式边界（design D3 重写后的
  理由：job id 派生 run-id 命名空间化 + 生产单 basin 候选恒带
  `orchestration_run_id`——**该"marker 非 scoping 决定项"只对
  `_candidate_scoped_cycle_execution` 这个消费者成立**；第二个消费者
  `_replacement_retry_scoped_cycle_execution`（首行短路 →
  `_active_orchestration_conflicts`）上 `orchestration_run_id` 不同解，带 marker
  的候选可穿过 markerless 孪生会被拦的重复编排冲突门。该分叉是既有行为、本 issue
  不改，作为显式记录的边界留存）。
- `tests/test_orchestration_chain.py`：陈旧 marker + 被占终态 `retry_1` 行 → 目标
  `retry_2` 且真实提交的回归钉；新鲜 marker 精确身份既有腿保持通过；判别器三态腿。
- spec delta：manual-retry 场景下 chain 侧 attempt 采信的新鲜度要求。

## Non-Goals

- reserve/reclaim 谓词（fail-closed 语义正确，不放宽）。
- marker 写入侧（`_manual_retry_state_evidence` / `_manual_retry_payload` 本体）与
  `state_evidence` 持久化清理策略。
- `skipped_duplicate_submission` 之后 fall-through 到下一 stage 的缺陷（issue 点名的
  兄弟 issue，单独立项）。
- scheduler-state 侧 `_manual_retry_requested` 语义本体（参照实现，不动）。

## Closes

Closes #1201.

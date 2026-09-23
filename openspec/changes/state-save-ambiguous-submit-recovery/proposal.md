# Proposal

## Why

#2584。2026-09-23 12:30:39 CST，`basins_hlj` IFS 12Z（run `fcst_ifs_2026092212_dg_8a34ed2ba8f8dd22f2716405569628a9`）的
`state_save_qc` 提交收到 gateway 502 `SLURM_PARSE_ERROR`（根因是 #2583 的读管道竞态，已在 #2585 修复）。
实际上 Slurm 已接收作业 54918 并 COMPLETED 0:0，checkpoint 也已写入 private state index。两个缺陷叠加，
使一次"提交结果不明确"变成了无法解除的阻塞：

1. **单作业阶段的不明确提交被当成确定失败**。`chain_stage_execution.py:372-375` 只给 forecast cohort 和
   forcing array 开了 `durable_submit_ambiguity`。`state_save_qc` 走普通路径，`_record_submission_failure`
   记为 `submission_failed` / `SLURM_PARSE_ERROR`。该错误码不在任何 transient 表里，`should_auto_retry` 为假，
   于是 `mark_pipeline_job_permanently_failed`（`file_orchestration_journal.py:4249`）把这一行改成
   `permanently_failed`（journal seq 52/53，"automatic retry declined"）。调度器此后每趟都判
   `permanent_failure_guard`。
2. **值守人员拿不到正确的 run id**。runbook 把 `permanent_failure` 指向 `scripts/node22_manual_retry_failed_runs.py`，
   而 blocked candidate 展示的是 hydro run id（`fcst_…`）。用它预览只会得到 `no_retryable_failed_job`：失败行挂在
   cohort master 下（`run_id = cycle_ifs_2026092212_convert_dg_8a34…`），而 `_manual_retry_source_for_run` 在 hydro 已成功时
   直接拒绝（`file_orchestration_journal.py:12441`）。2026-09-23 实测：同一工具传 cohort master id，预览为 `would_mark`，
   执行后下一趟就恢复了。但**整个 cohort** 从 convert 重跑到 `state_save_qc`（Slurm 55151–55154），不只是 `state_save_qc`。
   单模型 cohort 约 19 分钟；47 个模型的 cohort 会把 47 个 forecast 全部重跑。工具和 runbook 都没有说明这两点。

业务后果（node-27 实查）：HLJ 的 run 树只留在 private object-store，没有 copyback 到 `/ghdc/.../runs/`。
node-27 autopipe 只导入了 IFS 12Z 的 47 个 run；`/api/v1/layers/discharge/cycles` 要求**每个活跃河网**都有
display-ready run（`apps/api/routes/hydro_display.py:346`），因此全国 IFS 12Z 整体不显示，display 上 IFS 停在
2026092200。stall 探针每趟报 `submission_stalled`。

## What Changes

- **第 1 部分（写入时判定）**：`state_save_qc` 的提交已进入 gateway 调用边界、且失败不是"已证明的拒绝"
  （`_submit_error_is_ambiguous(...)` 为真）时，记为新的 transient 错误码 `STATE_SAVE_SUBMIT_AMBIGUOUS`，原始错误码
  保留在事件 details 的 `origin_error_code` 中。之后走已有的阶段内自动重试（有退避，受 `retry_limit` 约束），不再
  直接落成 `permanently_failed`。安全依据：checkpoint 对象用 `write_bytes_atomic` 写入，同 checksum 时走幂等路径；
  index 写入有 provider 锁和 preimage CAS（`atomic_replace_provider_bytes`）；copyback 以 run 为权威
  （`state_manager.py:2395-2407`）。原作业可能仍在跑时，多数情况是锁串行或同 checksum 幂等返回；最坏情况是重试这一方失败、落回 `permanently_failed`，
  也就是今天的状态，不会更差。
- **第 2 部分（人工出口的可发现性）**：manual-retry 脚本对 hydro run id 预览被拒（`no_retryable_failed_job`）时，只读地
  列出同一 cycle 中覆盖该模型、`state_save_qc` 行处于失败状态的 cohort master，含 run id、job id、状态、错误码和
  成员数，并提示"标记会让整个 cohort 从 convert 重跑"。脚本不自动替换 id，也不改变选择器的拒绝语义。
- runbook 决策表与 typed-reasons 同步更新。
- 另开 issue 跟踪"cohort 标记只从失败阶段重启"（成本优化，不在本单范围）。

## 现场恢复（已完成，不属于代码改动）

2026-09-23 21:10 CST 用现有脚本对 `cycle_ifs_2026092212_convert_dg_8a34ed2ba8f8dd22f2716405569628a9` 打标记。随后
55151–55154 全部 COMPLETED，run 树于 21:34 copyback 到 `/ghdc`，node-27 于 21:49 列出 IFS 2026092212，stall 探针恢复 `ok`。

## Triage

```text
Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent (issue has none)
Blast radius: 调度器的失败分类与自动重试，属于状态机；改错会把真失败当成可重试，导致重复执行。脚本提示是只读的
Selected risk packs: Concurrency / shared state / ordering; Error handling / rollback / partial outputs; Legacy compatibility; Documentation / migration notes
Evidence floor: 定向 pytest（见 tasks §3）全绿；新行为测试在改动前的代码上变红；以生产 journal 形态（cohort master 行、hydro run 已成功）为样本的脚本提示测试
```

design.md 已提供（expanded）。

# Proposal

## Why

#2605。#2584（PR #2599）上线后，在 node-22 做只读冒烟时，`cohort_candidates` 把一个已经恢复的旧 cohort master 列成了候选：
`cycle_ifs_2026092212_convert_dg_8a34…`，其 `state_save_qc` 行为 `permanently_failed`。

同一模型在 21:10 人工标记后已经重跑，但重跑写在另一个 cohort run id 下：`cycle_ifs_2026092212_full_dg_8a34…`。
其中 `state_save_qc` 55154 为 `succeeded`，updated_at 为 13:34Z，晚于旧行的 04:31Z。提示按 run_id 分组，
每个 cohort 只看自己最新的那一行，不会发现"这个模型已经在更晚的 cohort 里成功了"。值守人员照着提示标记，
会让整个 cohort 白白从 convert 重跑一次。

## What Changes

- `scripts/node22_manual_retry_failed_runs.py` 的 `_cohort_candidates` 增加一条规则：同一 cycle 中若有一行**更晚**的
  `state_save_qc` 同样覆盖该模型并且已经 `succeeded`，失败的 master 就不列出。"更晚"按 `_file_retry_job_truth_sort_key` 判断；取代行必须可证明覆盖该模型（证明不了的不取代），
  覆盖关系沿用原有规则（单模型看 `model_id`，多成员看 `_complete_cohort_members_by_run`）。
- runbook 在 cohort_candidates 的说明里补一句：已被更晚的成功 cohort 覆盖的 master 不列出。

## Triage

```text
Issue type: bugfix
Fixture level: compact
Upstream suggested level: absent
Blast radius: 只读提示；不改选择器、标记与调度器
Selected risk packs: Public API / CLI / script entry; Legacy compatibility; Operator alerting lanes / observer-observed predicate parity; Slurm production lifecycle / mock-vs-real parity
Evidence floor: 以生产形态（旧 _convert_<model> master 失败 + 新 _full_<model> 各行成功，真实时间戳）为样本的脚本测试在改动前变红、改动后变绿；原有提示测试全部保持通过
```

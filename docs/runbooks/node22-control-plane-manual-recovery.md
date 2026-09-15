# node-22 Control-Plane Manual Recovery Runbook

display API 在 `display_readonly` 模式下对控制面动作返回 409，payload 的
`recovery_runbook` 指向本文（slug `node22-control-plane-manual-recovery`，
`apps/api/routes/pipeline.py`）。DB-free scheduler 在四类终态决策上写
`retry_policy.manual_retry_required: true`，本文给出"怎么找到它们"和"每一类走哪个
入口"。

## 执行纪律（node-22）

- 维护窗口前 node-22 活动 checkout 仍是 Python 3.12.7：**禁止** `uv sync` 与裸
  `uv run`（会按 3.11 pin 重建共享 `.venv`），系统 Python 也不是替代。一律用活动
  解释器 + `-m`：

  ```bash
  cd /scratch/frd_muziyao/NWM
  /scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli <subcommand> ...
  ```

- 验证未合入的分支时不要 `git pull` 共享 checkout：`git worktree add --detach` 到
  `/scratch/frd_muziyao/tmp/<wt>`，**先 `cd <wt>`** 再
  `PYTHONPATH=<wt> /scratch/frd_muziyao/NWM/.venv/bin/python -m ...`，并确认
  `services.orchestrator.__file__` 位于 worktree 内；用完 `git worktree remove`。
- 本文所有命令都不连 DB。写 journal 的命令默认 dry run，只有显式 `--attest` /
  `--execute` 才写。

## 第一步：列出等待 operator 的候选

```bash
/scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli \
  list-operator-actions \
  --evidence-root "$NHMS_SCHEDULER_EVIDENCE_ROOT" \
  --passes 6
```

- 只读：扫描 evidence root **顶层**最新 `--passes` 个终态 pass 文件（按 mtime；
  `*.pre_execution.json` 不计），不读 journal——决策只在 evidence 里。
- `--evidence-root` 缺省取 `NHMS_SCHEDULER_EVIDENCE_ROOT`。
- 按 decision 字面识别，不看 `manual_retry_required` 布尔。bounded 摘要（
  `limit.candidate_lists=summarized`）丢了 `state_evidence`，但保留 `decision` 与
  `retry_attempt` / `retry_limit` / `retry_occurrences` / `manual_retry_required`，
  所以摘要 pass 里的条目照样列出。
- breaker 释放了执行槽的 backfill cycle 不构造候选，它的模型从 not-selected
  `source_cycles` 条目（`selection_reason=journal_predecessor_identity_quarantine_breaker_engaged`）
  读出，`candidate_id` 为 `null`。
- 输出一行 JSON（`schema_version=nhms.operator_action_listing.v1`）。同一
  `(source_id, cycle_time, model_id, decision)` 跨 pass 去重，带 `first_seen_pass` /
  `last_seen_pass` / `seen_in_passes`；读不了的 pass 名列在 `unreadable_passes`。

退出码：

| exit | 含义 |
|---|---|
| `1` | 列出了至少一条 operator action |
| `0` | 没有 |
| `3` | 没有，但某个被扫描的 pass 是 `limit.candidate_lists=dropped`，无法判定——去看更早的 pass 或未截断证据 |
| `2` | evidence root 未设置、缺失或不可读；`--passes < 1` |

## 第二步：按 decision 处置

| decision | 入口 |
|---|---|
| `permanent_failure` | `scripts/node22_manual_retry_failed_runs.py`（manual-retry marker） |
| `cancelled_manual_retry_required` | `scripts/node22_manual_retry_failed_runs.py`（manual-retry marker） |
| `blocked_journal_predecessor_identity_quarantine` | `confirm-operator-reentry`（§8.7 断路器） |
| `blocked_strict_warm_start_init_state_mismatch` | `confirm-operator-reentry`（strict warm-start 预算） |

### `permanent_failure` / `cancelled_manual_retry_required`

失败/取消的 run 不会自己重试。先修掉原因（例如 forcing 回补），再打一次性
manual-retry marker（`FileJournalRetryService.record_manual_repair`，取 cycle 写锁，
run 在飞或不存在时拒绝）：

```bash
/scratch/frd_muziyao/NWM/.venv/bin/python scripts/node22_manual_retry_failed_runs.py \
  --journal-root "$NHMS_SCHEDULER_JOURNAL_ROOT" \
  --run-id "<run_id>" \
  --reason "<why>" \
  --requested-by "<operator>"
# 预览无误后追加 --execute
```

### 两类 completed-skip fail-stop：`confirm-operator-reentry`

断路器与预算两类 blocked 的 journal 行是 terminal-success，manual-retry marker
**到不了**它们（`record_manual_repair` 拒绝 terminal-success 行，且 completed skip
在读 marker 之前就返回）。它们的 `retry_policy` 带
`operator_reentry_command: "confirm-operator-reentry"` 与
`recovery_runbook: "node22-control-plane-manual-recovery"`。

```bash
/scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli \
  confirm-operator-reentry \
  --journal-root "$NHMS_SCHEDULER_JOURNAL_ROOT" \
  --source-id gfs --cycle-time 2026-05-21T00:00:00Z --model-id <model_id> \
  --decision blocked_journal_predecessor_identity_quarantine \
  --pin <quarantine_rerun_count> --recorded-init-state-id <recorded_init_state_id> \
  --operator "<operator>" --reason "<why>"
# dry run 默认；核对 receipt 后追加 --attest
```

- 写入一条 `forecast_cycle` pipeline event，`event_type=operator_reentry_confirmation`
  （不是 `retry` / `manual_retry`，不会被当成 manual-retry marker）。
- **一次一授权**：`--pin` 必须等于 rerun 会推动的那个 live 值——
  - 断路器：该模型的 **quarantine rerun 计数**（已完成、provenance 命名该模型的 cohort
    master 数，不分 token）。它**不是** `list-operator-actions` / blocked evidence 里的
    `occurrences`（那是按 token 的带戳计数，只用来判断断路器是否触发）。先不带
    `--attest` 跑一次，从 dry-run receipt 的 `live.quarantine_rerun_count` 读出 pin；
    `--recorded-init-state-id` 取 `recorded_init_state_id`，必须等于 live 记录 token（写侧
    意图前置条件，读侧不再比较 token）。
  - 预算：`attempt`（stage-scoped 尝试次数，取自 `list-operator-actions`），不需要 token。
  scheduler 只在 pin 严格相等时放行一次；rerun 完成后该值 +1（断路器下无论 rerun 记录了
  哪个 token），fail-stop 自行重新接管，不需要撤销。
- 拒绝时不写任何字节、打印 `decision=refused` receipt 并 exit 2。`reason` 取值：
  `required_argument_blank`、`decision_not_reentry_eligible`、`cycle_time_invalid`、
  `pin_invalid`、`completed_identity_absent`、`recorded_init_state_id_mismatch`、
  `breaker_not_engaged`、`pin_mismatch`（后三者只适用于断路器；预算的 pin 不在 CLI
  侧复算，陈旧 pin 在读侧无效而非放行）。断路器的检查顺序为 `breaker_not_engaged` →
  `recorded_init_state_id_mismatch` → `pin_mismatch`，拒绝 receipt 的 `live` 同样带
  `occurrences` 与 `quarantine_rerun_count`。journal root 不可信时 stderr 为
  `FILE_JOURNAL_INVALID_ROOT: ...`，exit 2。
- 不改 `NHMS_SCHEDULER_RETRY_LIMIT`，也不改两处 forced-resubmit 白名单。

预期 evidence：

- 确认后的下一 pass：该候选进 `candidates[]`，decision 回到
  `retry_journal_predecessor_identity_mismatch` /
  `retry_strict_warm_start_terminal_init_state_mismatch`，`state_evidence` 带
  `operator_reentry_confirmation: {request_id, operator, reason, pin, decision}`；
  断路器几何下该 cycle 保留 backfill 执行槽（不再出现 breaker not-selected 条目）。
- rerun 在飞期间：候选是 active，`submitted_count` 不因它增加。
- rerun 完成后：断路器 `quarantine_rerun_count` +1 / 预算 `attempt` +1，pin 不再相等，下一 pass
  回到 blocked。

各决策的判读细节见
[`scheduler-dbfree-typed-reasons.md`](scheduler-dbfree-typed-reasons.md)
（§8.7 断路器）与 [`failed-basin-retry.md`](failed-basin-retry.md)（strict 预算）。

## 已知限制

- **Slurm 层失败的 rerun**：没有 completed master，`quarantine_rerun_count` 不变，确认物依旧
  匹配；但候选此时是 failed 而不是 completed-skip，走普通失败重试预算。只有 rerun
  最终 completed，确认物才被消费。失败路径受 retry 预算约束，不构成无限自旋。
- **forcing 见证闸（#1844）**：确认物匹配但该模型没有自己的 forcing 时，候选落到
  forcing 缺失的具名 blocked，不提交；断路器几何下该 cycle 会持续占用执行槽。确认物
  **没有撤销手段**——先回补 forcing。
- **确认后的 rerun 即使拿到正确 lineage 也仍显示 breaker-blocked**：file journal 的
  `hydro_run` 行在同一 `run_id` 重跑时不更新（#2397），live 记录 token 停在第一次记录的
  值。因此即使确认过的 rerun 拿到了正确 lineage，候选仍会显示为 breaker-blocked，直到该
  缺陷修复。这不是确认物失效：**不要重复确认**。
- **`recover-released-identity-blocked-reservation` 列表模式的逐行隔离**：receipt 的
  `skipped[]` 只覆盖首轮 flat 扫描与逐 cycle 确认中"单行内容校验"类原因（
  `ROW_CONTENT_SKIP_REASONS`）；预算拒绝、不可读、containment 类故障照旧 fail closed。
  没有 cycle scope 时走的 unscoped 全树 fallback **不隔离**，受 `full_tree_replay`
  预算契约约束，生产规模下会先撞预算。
- `list-operator-actions` 只看 evidence，不接 systemd timer；定时运行另议。

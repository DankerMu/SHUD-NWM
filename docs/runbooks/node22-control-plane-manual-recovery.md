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

- **evidence root 取值**（#2399）：`NHMS_SCHEDULER_EVIDENCE_ROOT` 必须取自 systemd unit
  `nhms-compute-scheduler.service` 实际加载的 `infra/env/compute.scheduler-dbfree.env`。
  node-22 checkout 里的 `infra/env/compute.env` 已漂移，**不要**用它。执行后核对 receipt 的
  `evidence_root` 就是该 unit 的目录，且 `passes_scanned > 0`——否则结论无效。
- 只读：扫描 evidence root **顶层**最新 `--passes` 个终态 pass 文件（按 mtime；
  `*.pre_execution.json` 不计），不读 journal——决策只在 evidence 里。窗口固定为最新 N
  个文件，不会越过它们去扫更早的 pass。
- `--evidence-root` 缺省取 `NHMS_SCHEDULER_EVIDENCE_ROOT`。
- **可判定 pass**：只有可读且 status 属于"候选构造已运行后才写出"的封闭集合
  `EVALUATING_PASS_STATUSES`（权威列表见 `services/orchestrator/operator_action_listing.py`，
  含 `planned`、`blocked`、`submitted`、`submission_failed` 等由执行 evidence 透传的状态；集合外
  的候选构造后状态保守计为不可判定），**且不是 size fallback 产物**的 pass 才能回答"没有待办"。
  size fallback 产物（`status=resource_limit_blocked` 且 `limit.candidate_lists` 为
  `summarized`/`dropped`）无论 `limit.pre_limit_status` 是什么都**不可判定**：写入器把
  `source_cycles` 清空了，而 breaker 释放的 cycle 只出现在那里；但它摘要里的
  `blocked_candidates` 照常列出（有就 exit 1）。node-22 的 pass 文件接近 5 MB 上限，这种 pass
  现实中会出现。不可判定的 pass 列在 `non_evaluating_passes:[{pass,status,reason}]`：
  - `reason=status_not_evaluating`：status 不在集合里（例如 `lock_contended`、
    `preflight_blocked`——它在候选构造前后都会写出，单凭 status 分不清——`lease_lost`、异常路径
    的 `resource_limit_blocked`、未知 status）；
  - `reason=size_fallback_source_cycles_absent`：size fallback 产物，`status` 为它保留的
    `limit.pre_limit_status`（没有时为 `resource_limit_blocked`）。
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
| `0` | 没有，窗口内至少有一个可判定 pass，且最新的可判定 pass 比窗口内所有 size fallback 产物都新 |
| `3` | 没有，但无法判定：某个被扫描的 pass 是 `limit.candidate_lists=dropped`，**或**窗口内零个可判定 pass（全部在 `unreadable_passes` / `non_evaluating_passes` 里——包括窗口里全是 size fallback 产物——或 root 为空、`passes_scanned == 0`），**或**有 size fallback 产物比最新的可判定 pass 更新（按 mtime；breaker 可能在那个可判定 pass 之后才触发，而 fallback 清空了唯一能看到它的 `source_cycles`）——加大 `--passes`、等下一个未超限的正常 pass，或先核对 evidence root |
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
# 预算：--decision blocked_strict_warm_start_init_state_mismatch --pin <budget_reentry_count>（不带 token）
```

- 写入一条 `forecast_cycle` pipeline event，`event_type=operator_reentry_confirmation`
  （不是 `retry` / `manual_retry`，不会被当成 manual-retry marker）。
- **一次一授权**：`--pin` 必须等于 rerun 会推动的那个 live 值——
  - 断路器：该模型的 **quarantine rerun 计数**（provenance 命名该模型的 cohort master 数，
    不看终态、不分 token）。它**不是** `list-operator-actions` / blocked evidence 里的
    `occurrences`（那是按 token 的带戳计数，只用来判断断路器是否触发）。先不带
    `--attest` 跑一次，从 dry-run receipt 的 `live.quarantine_rerun_count` 读出 pin；
    `--recorded-init-state-id` 取 `recorded_init_state_id`，必须等于 live 记录 token（写侧
    意图前置条件，读侧不再比较 token）。
  - 预算：该模型的 **预算重入计数**（预算重入 provenance `strict_warm_start_budget_reentry_model_ids`
    命名该模型的 cohort master 数，不看终态、job id、retry 后缀），不需要 token。它**不是**
    `list-operator-actions` / blocked evidence 里的 `attempt`（`attempt` 只决定是否 blocked）。
    先不带 `--attest` 跑一次，从 dry-run receipt 的 `live.budget_reentry_count` 读出 pin。
  scheduler 只在 pin 严格相等时放行一次。确认物在 rerun **被接受提交**时即被消费：
  provenance 戳在 accepted-submit（reservation）时写入 cohort master，对应计数当场 +1
  （无论 rerun 之后成功、失败，断路器也无论记录了哪个 token，预算也无论 rerun 落在哪个 job-id
  前缀下）。fail-stop 自行重新接管，不需要撤销。
- 拒绝时不写任何字节、打印 `decision=refused` receipt 并 exit 2。`reason` 取值：
  `required_argument_blank`、`decision_not_reentry_eligible`、`cycle_time_invalid`、
  `pin_invalid`（pin 为负）、`completed_identity_absent`、`recorded_init_state_id_mismatch`、
  `breaker_not_engaged`、`pin_mismatch`。`recorded_init_state_id_mismatch` 与
  `breaker_not_engaged` 只适用于断路器；`pin_mismatch` 两类都适用（断路器比
  `quarantine_rerun_count`，预算比 `budget_reentry_count`）。断路器的检查顺序为 `breaker_not_engaged` →
  `recorded_init_state_id_mismatch` → `pin_mismatch`，拒绝 receipt 的 `live` 同样带
  `occurrences` 与 `quarantine_rerun_count`；预算拒绝 receipt 的 `live` 带 `budget_reentry_count`。
  journal root 不可信时 stderr 为 `FILE_JOURNAL_INVALID_ROOT: ...`，exit 2。
- 不改 `NHMS_SCHEDULER_RETRY_LIMIT`，也不改两处 forced-resubmit 白名单。

预期 evidence：

- 确认后的下一 pass：该候选进 `candidates[]`，decision 回到
  `retry_journal_predecessor_identity_mismatch` /
  `retry_strict_warm_start_terminal_init_state_mismatch`，`state_evidence` 带
  `operator_reentry_confirmation: {request_id, operator, reason, pin, decision}`；
  断路器几何下该 cycle 保留 backfill 执行槽（不再出现 breaker not-selected 条目）。
- rerun 在飞期间：候选是 active，`submitted_count` 不因它增加。
- rerun 被接受提交后（在飞、完成或失败）：断路器 `quarantine_rerun_count` / 预算
  `budget_reentry_count` 已 +1，pin 不再相等，之后的 pass 不再放行（回到 blocked；Slurm 层失败的
  预算 rerun 可能改落为 `permanent_failure`）。

各决策的判读细节见
[`scheduler-dbfree-typed-reasons.md`](scheduler-dbfree-typed-reasons.md)
（§8.7 断路器）与 [`failed-basin-retry.md`](failed-basin-retry.md)（strict 预算）。

## 已知限制

- **Slurm 层失败的 rerun 不会恢复确认物**：计数在 rerun 被接受提交时已经 +1，失败不回退。
  需要再次重入时，重新跑 dry run，用新的 live 计数再确认一次。
- **forcing 见证闸（#1844）**：确认物匹配但该模型没有自己的 forcing 时，候选落到
  forcing 缺失的具名 blocked，不提交；断路器几何下该 cycle 会持续占用执行槽。确认物
  **没有撤销手段**——先回补 forcing。
- **候选仍显示 blocked ≠ 确认物未生效**：先看 dry-run receipt 的 live 计数是否已 +1（file
  journal 的 `hydro_run` 在同一 `run_id` 重跑时不更新，#2397，即使 rerun 拿到正确 lineage
  候选也仍显示 breaker-blocked）；已 +1 就**不要重复确认**——除非该 rerun 已到失败终态且
  operator 仍要重入，此时重新跑 dry run，用新的 live 计数再确认一次。**rerun 在飞期间不要写
  新的确认物**：写侧不检查在飞状态，按新计数写下的确认物会在该 rerun 结束后放行下一次。
- **`recover-released-identity-blocked-reservation` 列表模式的逐行隔离**：receipt 的
  `skipped[]` 只覆盖 flat `pipeline-jobs/` 行在首轮扫描与逐 cycle 确认 replay 中的"单行内容
  校验"类原因（`ROW_CONTENT_SKIP_REASONS`），逐行跳过、按 `(path, reason)` 去重，同 cycle 的
  wedged 行照常列出；cycle 的 `journal/` 日志或 `latest/` 视图损坏不属于"行"，照旧 raise；预算拒绝、不可读、containment 类故障照旧 fail closed。
  没有 cycle scope 时走的 unscoped 全树 fallback **不隔离**，受 `full_tree_replay`
  预算契约约束，生产规模下会先撞预算。
- `list-operator-actions` 只看 evidence，不接 systemd timer；定时运行另议。

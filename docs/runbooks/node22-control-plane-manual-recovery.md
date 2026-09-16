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

## 第一步：列出等待 operator 的候选（临时口径，直读 pass evidence）

只读的 operator-action 列举子命令由 #1186 在后续 PR 交付；在它合入之前按本节直读最新一份
pass evidence，本节届时由它整段取代。

- **evidence root 取值**（#2399）：`NHMS_SCHEDULER_EVIDENCE_ROOT` 必须取自 systemd unit
  `nhms-compute-scheduler.service` 实际加载的 `infra/env/compute.scheduler-dbfree.env`。
  node-22 checkout 里的 `infra/env/compute.env` 已漂移，**不要**用它。下面第一条命令若选不出
  文件，说明 root 取错或该 root 下没有 pass——结论无效，先核对 root。

```bash
# evidence root 顶层最新的终态 pass 文件（`scheduler_*.json`；`*.pre_execution.json`
# 不是终态 pass，不计）
PASS=$(ls -t "$NHMS_SCHEDULER_EVIDENCE_ROOT"/scheduler_*.json \
  | grep -v '\.pre_execution\.json$' | head -1)
echo "$PASS"

# 1) 四类等待 operator 的决策：按 decision 字面筛 blocked_candidates
jq -c '.blocked_candidates[]?
  | {candidate_id,
     source_id: (.source_id // .source),
     cycle_time: (.cycle_time_utc // .cycle_time),
     model_id,
     decision: (.decision // .state_evidence.decision),
     reason,
     attempt: (.state_evidence.retry_policy.attempt // .retry_attempt),
     retry_limit: (.state_evidence.retry_policy.retry_limit // .retry_limit),
     occurrences: (.state_evidence.retry_policy.occurrences // .retry_occurrences),
     recorded_init_state_id: .state_evidence.journal_predecessor_identity.recorded_init_state_id}
  | select(.decision as $d
           | ["permanent_failure",
              "cancelled_manual_retry_required",
              "blocked_strict_warm_start_init_state_mismatch",
              "blocked_journal_predecessor_identity_quarantine"] | index($d))' "$PASS"

# 2) breaker 释放了执行槽的 backfill cycle：不构造候选，只出现在 not-selected source_cycles
jq -c '.source_cycles[]?
  | select(.selection_status == "not_selected"
           and .selection_reason == "journal_predecessor_identity_quarantine_breaker_engaged")
  | {source_id, cycle_time: .cycle_time_utc,
     decision: "blocked_journal_predecessor_identity_quarantine",
     models: [.journal_predecessor_identity_quarantine.models[]?
              | {model_id, occurrences, recorded_init_state_id}]}' "$PASS"
```

- 只读 evidence，不读 journal——决策只在 evidence 里；本节不连 DB、不写任何字节。
- 按 decision 字面识别，不看 `manual_retry_required` 布尔。bounded 摘要（
  `limit.candidate_lists=summarized`）丢了 `state_evidence`，但保留 `decision`，以及
  `retry_attempt` / `retry_limit` / `retry_occurrences` / `manual_retry_required` 里
  **产出臂实际写过的那几个**（保留判据是 `value is not None`，所以 `0` 和 `false` 也留得住），
  摘要 pass 里的条目照样列出（上面的 jq 两条取值路径都覆盖）。四个键不是每臂都有：
  - 预算臂（`blocked_strict_warm_start_init_state_mismatch`）写 `attempt` / `retry_limit` /
    `manual_retry_required`，**从不写** `occurrences`；
  - 断路器臂（`blocked_journal_predecessor_identity_quarantine`）写 `occurrences` /
    `manual_retry_required`，**从不写** `attempt` / `retry_limit`。

  jq 把缺的那个渲染成 `null`。这里的 `null` 意思是**这一臂从来没写过这个字段**，不是
  「scheduler 把数字弄丢了」——不要据此判断摘要有损、更不要据此去翻非摘要 pass 找那个数。
- breaker 释放了执行槽的 backfill cycle 不构造候选，它的模型从上面第 2 条命令的 not-selected
  `source_cycles` 条目读出，没有 `candidate_id`。
- **可判定性是闭合白名单，正向判据**（过渡期没有 CLI 替你判，规则得手工执行）：只有终态
  `status` 落在下面这 24 个之一的 pass，才算「候选构造真的跑过」，才有资格让空输出读作
  「没有待办」。这就是被删掉的 #1186 模块里那张 `EVALUATING_PASS_STATUSES` 闭合表，原样抄在这里：

  ```
  planned                     blocked                      unavailable
  submitted                   submitted_partial            slurm_status_synced
  slurm_status_sync_failed    slurm_cancelled              slurm_partially_cancelled
  slurm_cancellation_blocked  restart_reconciled           restart_reconcile_unknown
  submission_failed           skipped_duplicate_submission reconciling
  submit_result_ambiguous     reconcile_unverified         cancelled
  complete                    succeeded                    parsed_partial
  forcing_ready_partial       forcing_ready                already_done
  ```

  **凡是不在这张表里的 status，一律按不可判定处理并升级上报**——不是"大概没事"。已知的
  非评估 status 有 `lock_contended`、`preflight_blocked`、`lease_lost`、`resource_limit_blocked`
  （`preflight_blocked` 在候选构造前后都会写，光看 status 分不出是哪一种，所以也不可判定）；
  **未来新增的、本表未列出的任何 status 同样落在升级一侧**。
- **`limit.candidate_lists == "dropped"` 让被检查的那个 pass 不可判定——不限于最新 pass**，
  窗口里任何一个被翻到的 pass 都一样：丢了候选列表，它既不能列出待办，也不能证明没有待办。
  size fallback 产物（`status=resource_limit_blocked` 且 `limit.candidate_lists` 为
  `summarized`/`dropped`）还被写入器清空了 `source_cycles`——breaker 释放的 cycle 看不见。
  node-22 的 pass 文件接近 5 MB 上限，这种 pass 现实中会出现。
- **空输出 ≠ 没有待办**：`status` 在上面的白名单里、且 `limit` 没有丢列表，两条都满足，才能下
  「没有待办」的结论。
- **只能向前等，不能往回翻**：最新 pass 不可判定时，**等下一个未超限的正常 pass**（隔一会儿
  重跑第一步选 `PASS` 的 `ls -t ... | head -1`），不要退回上一个 pass。规则说正面一点：
  **一个更旧的 pass 只能用来「找活儿」，永远不能用来断定「没有待办」**——断路器可能在那个旧
  pass 写完之后才释放了某个 cycle，旧 pass 里的空列表对「现在」没有证明力。真要翻旧 pass 找
  线索时（`sed -n 2p` 取次新，`*.pre_execution.json` 的过滤不能省），找到的目标仍要按第二步
  的规则用**最新** pass 重新核对一遍。#1186 的 CLI 落地后会把这类不可判定的 pass 显式报出来
  并用独立退出码区分。

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
    不看终态、不分 token）。它**不是** blocked evidence 里的
    `occurrences`（那是按 token 的带戳计数，只用来判断断路器是否触发）。先不带
    `--attest` 跑一次，从 dry-run receipt 的 `live.quarantine_rerun_count` 读出 pin；
    `--recorded-init-state-id` 取 `recorded_init_state_id`，必须等于 live 记录 token（写侧
    意图前置条件，读侧不再比较 token）。
  - 预算：该模型的 **预算重入计数**（预算重入 provenance `strict_warm_start_budget_reentry_model_ids`
    命名该模型的 cohort master 数，不看终态、job id、retry 后缀），不需要 token。它**不是**
    blocked evidence 里的 `attempt`（`attempt` 只决定是否 blocked）。
    先不带 `--attest` 跑一次，从 dry-run receipt 的 `live.budget_reentry_count` 读出 pin。
    **写侧看不到预算是否已耗尽**（#2400 残余，本 PR 不关闭：pin 对了、时间错了）：耗尽前写入的
    确认物在 pin 仍等于计数时一直有效，直到被消费——预算一耗尽就会在没有新签字的情况下放行一次。
    所以只确认**最新** pass evidence 文件的 `blocked_candidates` 当前列为
    `blocked_strict_warm_start_init_state_mismatch` 的目标（第一步第 1 条命令），并逐字核对 `source_id` /
    `cycle_time` / `model_id`；该模型的 rerun 仍在飞时不要确认。写错（目标、pin 或时机）时停止并
    上报，**不要**再写一条覆盖（旧确认物仍有效）。
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
- **forcing 见证闸（#1844）**：确认物匹配但该模型没有自己的 forcing 时，**默认**候选落到
  forcing 缺失的具名 blocked（reason `forcing_version_row_absent` /
  `missing_forcing_package_uri`），不提交；断路器几何下该 cycle 会持续占用执行槽。确认物
  **没有撤销手段**——先回补 forcing。
- **同一 cycle 开着 `--repair-missing-forcing` 时不是上面那样：会提交，并且消费掉确认物**
  （r1 c-03）。运维授权的整点修复策略只改判这两个具名 missing-forcing blocked，而确认物正是
  走到这个 blocked 的**必要条件**（没有确认物时决策是
  `blocked_strict_warm_start_init_state_mismatch`，不在修复策略的受理 reason 里，根本到不了）。
  改判后的决策是 `retry_repair_missing_forcing`——该字面量在 forced-resubmit 白名单里，
  **真的提交**；重试仍带着原来的 `operator_reentry_confirmation` 块，provenance 戳照常在
  accepted-submit（reservation）时写入 cohort master，对应计数当场 +1，**确认物被消费掉，
  恰好一次**，下一 pass 回到 blocked。所以：要么先回补 forcing 再确认，要么就接受「这一次修复
  重试 = 那一次重入」。`current-production-ops.md` 里 strict 车道走
  `--repair-missing-forcing` 的处置流程同理。
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

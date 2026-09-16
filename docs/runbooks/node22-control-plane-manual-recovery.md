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
| `blocked_operator_reentry_restart_stage_refused` | **不要再签一次**；带外修好 forecast 之前的输入，见下面「已知限制」的 sink 拒绝那条 |

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
- **forcing 见证闸（#1844）**：确认物匹配但该模型没有自己的 forcing 时，候选落到
  forcing 缺失的具名 blocked（reason `forcing_version_row_absent` /
  `missing_forcing_package_uri`），不提交；断路器几何下该 cycle 会持续占用执行槽。确认物
  **没有撤销手段**——先回补 forcing。两条 fail-stop（断路器、strict 预算）同此。
- **确认物与 `--repair-missing-forcing` 互斥**（r2-01；两条 fail-stop 都适用）：同一 cycle
  开着单 cycle 修复时，带 `operator_reentry_confirmation` 块的候选**不会被改判、不提交、
  不消费**，确认物保持待用。证据按车道分：
  - strict warm-start 车道（预算臂，以及落在该车道的断路器重入）：修复策略被调用并拒绝，
    `state_evidence.missing_forcing_repair = {status: rejected, reason:
    operator_reentry_confirmation_present, confirmation: {decision, request_id}}`，候选留在
    上面那个 missing-forcing blocked 上。
  - 非 strict 车道的 §8.7 断路器重入：修复策略**根本不会被调用**（调用点在
    `strict_warm_start is not None` 之内），候选只带见证闸的 missing-forcing blocked，
    **没有** `missing_forcing_repair` 键——结果相同，别去找那个键。
  理由：被改判的 `retry_repair_missing_forcing` 从 `forcing` 阶段重启，而重入 provenance 只在
  forecast cohort 的 reservation 处写；forcing 跑成功才顺带戳到，跑失败就是「真提交了、计数没动、
  确认物还在，且新 run-id 前缀把 stage 域 attempt 打回 0/2 让预算判定失效」——一次签字放行两次
  forecast 重入。所以这条路被整体拒绝，而不是赌 forcing 会成功。
  **正确顺序：先把该模型自己的 forcing 补回来，再让确认过的重入跑**——它从 `forecast` 重启、在
  reservation 处被戳、计数 +1，**恰好消费一次**，之后的 pass 回到 blocked、旧 pin 返回
  `pin_mismatch`。`current-production-ops.md` 里 strict 车道走 `--repair-missing-forcing`
  的处置流程同理：带确认物的候选不走那条通道。

  **"补回来"有前提，别默认就是回补脚本**：`scripts/node22_backfill_forcing_for_model_ids.py`
  是**纯改名工具**——它要求同时给出改名前后两份 registry manifest
  （`scripts/node22_backfill_forcing_for_model_ids.py:605-606`），待办**完全**由改名集推导
  （`discover_work` 在 `:409`，`for rename in renames` 在 `:420-421`；`main` 的
  `resolve_renames → probe_coverage → discover_work` 流水线在 `:648-651`）。
  **只有"该模型的 forcing 以另一个 model id 存在、即改名集非空"时它才有活儿**。不是改名造成的
  forcing 缺失，它返回 `work_item_count: 0`（receipt 字段在 `:705` / `:715`），而这个形状与
  `--forcing-root` 指错、NFS 没挂、环境不对**在条目数上分不开**——所以先读 receipt 的
  `coverage`，见 `current-production-ops.md` §3.1.1。非改名成因是真实存在的：retention 会删除
  primary root 下的 `forcing/<source>/<cycle>/...`（`services/orchestrator/retention.py:75`、
  `:684-687`），即 fail-stop 生效之后 forcing 仍可能在带内消失。

  **改名集为空时的升级路径**：回补脚本不是通道，`--repair-missing-forcing` 也不是。只能**带外**
  把 forecast 之前的那份输入修好（重新产出该模型的 forcing 包并落进 object store；或修好
  canonical readiness / raw manifest 身份），修好之前确认物保持待用、候选保持 blocked。修好后的
  下一趟自然 pass 让确认过的重入从 `forecast` 重启、被戳、计数 +1，恰好消费一次。
  （retention 的删除前沿是否钉住 blocked / 带确认物的候选**尚未实测**，两个方向都不要断言；
  设计缺口记在 #2412。）

- **sink 拒绝 `blocked_operator_reentry_restart_stage_refused`**（#1555 round 4）：确认物匹配，
  但候选实际会重启的阶段不是 `forecast`（典型是 canonical 不 ready + raw manifest 就绪触发的
  `convert` 改写）。判定在候选清单构建完成后统一做一次，读候选自己的 `state_evidence`，正向比较
  `== "forecast"`，阶段缺失/`null`/空串以及 `fresh_ingestion.mode == "full_chain"`（run manifest
  会被剥掉 `restart_stage`）一并拒。**不提交、不消费，确认物保持待用**，该 decision 不在两处
  forced-resubmit 白名单里。处置：读 `operator_reentry_sink_refusal.refused_restart_stage` 找到改写
  源头，**带外**修好 forecast 之前的那份输入（canonical readiness index 或 raw manifest 身份），
  下一趟 pass 自己从 `forecast` 重启并消费一次签字。**不要重复签一次**——旧确认物仍然有效。
  **evidence 被有界摘要压过时**：`operator_reentry_sink_refusal` 这个块里只有
  `refused_restart_stage` 被保留（摘要里是同名的行级键），块内**其余字段一律消失**——
  普通拒绝里是 `refused_restart_from_stage` / `effective_restart_stage` /
  `fresh_full_chain` / `confirmation`，确认物损坏那类还多一对 `malformed_confirmation` /
  `confirmation_type`。要看这些字段必须回到未摘要的整份 pass evidence。
  字段与逐项处置见
  [`scheduler-dbfree-typed-reasons.md`](scheduler-dbfree-typed-reasons.md)。
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

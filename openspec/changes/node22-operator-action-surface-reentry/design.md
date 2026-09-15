# Design — node-22 operator-action surface + targeted re-entry

## Risk triage

- **Fixture level: `high`**，round 1 设 4 个 reviewer 席位。
  - **与 precedent 的偏离**：#1157 的 `2026-08-18-scheduler-quarantine-residual-hardening` 定为 `expanded`，但它只有一个 seam。本批改动面更宽：
    - 调度决策路径新增一条 operator 授权通道（D）；
    - evidence bounded 契约扩容（A/B）；
    - journal 扫描异常行为改变（C）；
    - 新增两条 CLI 与一份新 runbook。
  - 五个 issue 都没有 `Suggested fixture level` 字段，本次为首次定级。
- **Risk packs selected**：
  - `invariant-state`：
    - breaker / 预算的反复活形状；
    - 一次授权一次重入；
    - §8.7 read-only invariant；
    - #1843/#1844 forcing 见证不变量在重入路径上保持。
  - `correctness`：
    - pin 匹配语义；
    - 列举 CLI 对 summarized pass 的兼容；
    - skip allowlist 的边界。
  - `security-perf`：
    - operator 写侧的前置条件与 dry-run 默认值；
    - 扫描 N 个 evidence 文件的 I/O 上界；
    - 预算拒绝不得被吞。
  - `test-evidence`：每个切片先红后绿；闭环测试走公开入口 `build_candidates` / scheduler pass。
- **Risk packs not selected**：`integration`。gateway、reservation、producer 不改；display API 只新增 slug 文件存在性测试，payload 不变。

## 事实基线（HEAD `49cf31316`，已读码核实）

| 事实 | 位置 |
|---|---|
| breaker 臂返回 `blocked` + `retry_policy{manual_retry_required, occurrences, occurrence_threshold}` | `scheduler_candidates.py:2389-2402`、`:2432-2475` |
| breaker 的计数来自 journal `_cycle_rows`（不读 bounded candidate_state），只统计带 quarantine provenance 的 completed master | `file_orchestration_journal.py:1423` |
| 预算臂在 `attempt >= retry_limit` 时返回 `blocked`；`attempt` 由 journal 行派生 | `scheduler_candidates.py:2504-2554`（调用点 `:580`，其后 `:587` 见证闸） |
| 非 strict 车道 quarantine retry 过 #1844 见证闸 | `scheduler_candidates.py:625-634` |
| `record_manual_repair` 对 terminal-success 返回 `RetryNotFoundError` | `file_orchestration_journal.py:11793-11822`、`:10943` |
| `insert_pipeline_event(entity_type="forecast_cycle")` 取 cycle 写锁，不要求既有行 | `file_orchestration_journal.py:5456-5500`、`:5558-5575` |
| `_manual_retry_marker_shape` 采纳 `event_type ∈ {retry, manual_retry}` 且 `trigger=="manual"` 或 `manual_retry_marker` 的事件 | `scheduler_state_manual_retry.py:151-156` |
| bounded 白名单只有 4 个 pull，无测试钉；summary 丢弃整个 `state_evidence` | `scheduler_evidence_payload.py:33-42`、`:352-390` |
| pass evidence 文件名 `scheduler_<cycle>_<uuid12>.json` / `.pre_execution.json`；谓词 `is_scheduler_pass_evidence_filename` | `scheduler_evidence.py:32-46` |
| node-22 evidence root：`NHMS_SCHEDULER_EVIDENCE_ROOT` | `infra/env/compute.scheduler-dbfree.env.example:69` |
| `recover-released-identity-blocked-reservation` 的 list receipt 为 `{decision:"listed", wedged_count, wedged:[...]}`；错误时 stderr + exit 2 | `operator_released_reservation_recovery.py:92-300` |
| `_iter_flat_direct_pipeline_job_records_for_cycle` 位于 `_cycle_job_records_memoized` 链上，即调度热路径 | `file_orchestration_journal.py:6575`、`:6824-6994` |
| §8.6 emitter 的 cap `break`，截断记录无 successor | `scheduler_backfill_predecessor.py:365-367`、`:681-689` |

## D1 — 列举面 `list-operator-actions`（#1186）

- **位置**：新模块 `services/orchestrator/operator_action_listing.py`，argparse 子命令挂到 `cli.py`，挂载方式照抄 `operator_released_reservation_recovery.add_argparse_recovery_subparser`。
- **输入**：
  - `--evidence-root`，缺省读 `NHMS_SCHEDULER_EVIDENCE_ROOT`，两者都缺则 exit 2；
  - `--passes N`，默认 `6`，下限 1。
- **扫描**：
  - 只取 root 顶层满足 `is_scheduler_pass_evidence_filename` 的 **终态** `scheduler_*.json`，排除 `.pre_execution.json`；
  - 按 mtime 降序取前 N 个（uuid 后缀不按时间排序）；
  - 单文件 JSON 损坏或不是 object 时记入 `unreadable_passes`，并继续。
- **判定**：
  - 另读每个 pass 的 `source_cycles[]`：`selection_status=="not_selected"` 且 `selection_reason=="journal_predecessor_identity_quarantine_breaker_engaged"` 的条目，把 `journal_predecessor_identity_quarantine.models[]` 逐个列为 `blocked_journal_predecessor_identity_quarantine`，并带上 `occurrences` 与 `recorded_init_state_id`。backfill 模式下，breaker 释放的 cycle 不会进入 `build_candidates`，所以 `blocked_candidates` 里没有它（`scheduler_discovery.py:813-845`，fixture review F1）。
  - 读每个 pass 的 `blocked_candidates[]`，**按 `decision` 识别**，不按 `manual_retry_required` 识别，因为该标志在 summarized pass 上不存在；
  - decision 优先取 summary 顶层 `decision`，其次取 `state_evidence.decision`；
  - 族集合为常量 `OPERATOR_ACTION_DECISIONS`，即 proposal 列出的四个字面；
  - fixture review 已核实：`permanent_failure` 与 `cancelled_manual_retry_required` 写在 `state_evidence.decision`，`retry_policy` 下有 `attempt/retry_limit/manual_retry_required`（`scheduler_state_failure.py:1982-1990`、`:2177-2189`）。blocked 条目的 `reason` 与 decision 字面不同，所以只按 decision 识别。
- **输出**：一行 sorted-key JSON。
  ```text
  {schema_version, evidence_root, passes_scanned, unreadable_passes:[...], operator_action_count,
   operator_actions:[{candidate_id, source_id, cycle_time, model_id, decision, reason,
                      attempt, retry_limit, occurrences, recorded_init_state_id, first_seen_pass, last_seen_pass, seen_in_passes}],
   candidate_lists_dropped_passes:[...]}
  ```
  - 按 `candidate_id + decision` 去重；
  - `attempt / retry_limit / occurrences` 从 `state_evidence.retry_policy`（或 summary 新增的 bounded 键）取，缺失时为 `null`。
- **退出码**：
  - 列表非空为 `1`；
  - 列表为空，但有 pass 的 `limit.candidate_lists == "dropped"`（`scheduler_evidence_payload.py:219-222`）时为 `3`（无法判定），否则会在最拥堵的 pass 上假阴性（F7）；
  - 其余空列表为 `0`，但窗口内必须至少有一个可判定 pass（可读且 status 属于「候选构造已运行」的封闭 allowlist），否则为 `3`；不可判定的 pass 分别进 `unreadable_passes` / `non_evaluating_passes:[{pass,status}]`（round 1 cand-03 修订，推翻原「unreadable 只呈现」）。Phase 7 F-1 修订：size-fallback 产物（`resource_limit_blocked` + `limit.pre_limit_status`）的 `source_cycles` 被 `bounded_evidence_payload` 无条件清空（`scheduler_evidence_payload.py:1129`），看不到 breaker 释放的 cycle，因此不计为可判定 pass（进 `non_evaluating_passes`，reason `size_fallback_source_cycles_absent`），但其 summarized `blocked_candidates` 仍照常列出；
  - root 缺失或不可读为 `2`。
- **bounded 白名单扩容**（与 D3 共用同一次编辑）：
  - 在 `_BOUNDED_CANDIDATE_STATE_EVIDENCE_KEYS` 追加 `retry_attempt ← (retry_policy, attempt)`、`retry_limit ← (retry_policy, retry_limit)`、`retry_occurrences ← (retry_policy, occurrences)`、`manual_retry_required ← (retry_policy, manual_retry_required)`；
  - summary 键名不得与 `_BOUNDED_CANDIDATE_SUMMARY_KEYS` 冲突。
  - 新增一条白名单钉测试，逐项断言 summary 后这些键存在，`False`/`0` 值也要保留。
- **runbook**：
  - 新建 `docs/runbooks/node22-control-plane-manual-recovery.md`，对齐 `apps/api/routes/pipeline.py:248` 的 slug。内容：
    - `list-operator-actions` 用法；
    - 四类决策各自的处置入口：
      - permanent_failure / cancelled 走 `scripts/node22_manual_retry_failed_runs.py`（#1825，`record_manual_repair`）；
      - breaker / 预算 走 D4 的 `confirm-operator-reentry`；
    - node-22 执行纪律：维护窗口前禁止裸 `uv run`，用 `.venv/bin/python -m`。
  - `failed-basin-retry.md` 为四类决策各写一段，并链接新 runbook。
  - 新测试断言 `pipeline.py` 引用的 slug 在 `docs/runbooks/<slug>.md` 存在。测试从源码常量读 slug，不硬编码第二份。
- **不做**：
  - 不改 409 payload 的 `suggested_action` 文本（userspace 字符串，已有两处测试钉）；
  - 不接 systemd timer（用法写进 runbook，部署另议）；
  - 不扫 journal，只扫 evidence。issue 提到的「+ journal」属 YAGNI：journal 不带决策，决策只在 evidence 里。

## D2 — node-22 现场收据的执行方式（偏离 issue Verification 文本）

#1186 的 Verification 文本写的是在 node-22 活动 checkout 上 `git pull` + `uv run python -m ...`，两者都违反 CLAUDE.md：共享 checkout 不应被本批 pull，维护窗口前也禁止裸 `uv run`。改为下面的流程：

1. `git -C /scratch/frd_muziyao/NWM fetch origin <branch>`；
2. `git worktree add --detach /scratch/frd_muziyao/tmp/wt-b7 <sha>`；
3. **先 `cd <wt>`**（`python -m` 会把 cwd 放在 `PYTHONPATH` 前面，在共享 checkout 下运行会导入旧代码），再执行 `PYTHONPATH=<wt> /scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli list-operator-actions --evidence-root $NHMS_SCHEDULER_EVIDENCE_ROOT`。root 值先用只读 `ls` 核实，并断言 `services.orchestrator.__file__` 位于 worktree 内；
4. 贴出输出，然后 `git worktree remove`。

只读，不连 DB，不写 evidence root。

## D3 — §8.6 发射可行性标（#1543）

- 在 `attach_emission_summary_to_blocked` / `_attach_summary_to_single_blocked` 里，根据本 successor 自己的 records 写顶层布尔 `state_evidence.predecessor_emission_blocked`。
- **反向定义**（fixture review F8）：只列一份**瞬时** allowlist 常量；本 successor 的任一 `skipped`/`truncated` 记录的 reason 不在 allowlist 内，就为 `True`。瞬时 allowlist：
  - `predecessor_already_present`
  - `predecessor_backfill_active_pipeline`
  - `predecessor_raw_manifest_env_unwired`：配置问题，已有 one-per-pass warning
  - `LINEAGE_SCOPED_OUT_REASON`：**仅当** successor 的 cycle_time 早于记录中的 `cutover_valid_time`，此时 discovery 已把该 cycle 移出评分（`scheduler_lineage.py`）；successor cycle_time 不早于 cutover 时（lead 大于 cadence 的几何），successor 会永久 blocked，因此打 `True`（F9）
- 这样 `predecessor_raw_manifest_not_ready`、`predecessor_model_not_available`、`predecessor_emission_cap_reached`、`predecessor_candidate_construction_failed`（`:517-525`）、`predecessor_gate_failed`（`:544-552`），以及今后新增的任何 skip 臂，默认都为 `True`，方向是 fail toward escalation。
- 本 successor 的记录全部是 emitted 或 allowlist 内的瞬时 skip 时为 `False`；没有任何记录的 successor 不写该键。
- **截断定位**：迭代改为 `enumerate(pending)`，cap `break` 时把 `pending[i:]` 的 successor id 去重排序后，写入截断记录的 `successor_candidate_ids`（列表）。`attach_emission_summary_to_blocked` 对截断记录按该列表逐个归组，归组记录与既有截断记录相同。
  - `status/reason/total_attempted/cap` 不变；
  - pass totals 语义不变，截断记录仍计 1 次；
  - 列表长度以 pending 为上界。pending 本身已受 blocked 列表约束，不另设上限。
- **白名单**：追加 `predecessor_emission_blocked ← (predecessor_emission_blocked,)`，`False` 值保留。
- **gate 侧不动**：`operator_action_required` 的计算与语义不变，两轴正交，并有测试钉住「`operator_action_required=False` 且 `predecessor_emission_blocked=True`」这一组合。
- **runbook**：`scheduler-dbfree-typed-reasons.md` 处置第 1 步改为两个布尔一起读；反例段改为读 `predecessor_emission_blocked`，不再依赖 summarize 后消失的 `predecessor_backfill.summary`。

## D4 — operator 重入确认物（#1555 + #1768）

### 写侧：`confirm-operator-reentry`

- 新模块 `services/orchestrator/operator_reentry_confirmation.py`，argparse 子命令挂到 `cli.py`。
- **参数**：
  - `--journal-root`（经 `verify_journal_root_authority`，同 #1955）；
  - `--source-id`、`--cycle-time`（ISO UTC）、`--model-id`；
  - `--decision ∈ {blocked_journal_predecessor_identity_quarantine, blocked_strict_warm_start_init_state_mismatch}`；
  - `--pin <int>`、`--operator`、`--reason`，均必填且非空；
  - 默认 dry-run，`--attest` 才写入。
- **前置条件**（不满足即 exit 2，不写）：
  1. journal 对 `(source, cycle, model)` 有 completed identity，即 `completed_pipeline_init_state_identity` 非 `None`。
  2. 若 decision 为 breaker：新增必填参数 `--recorded-init-state-id`。用 journal 现值计算 `completed_pipeline_init_state_id` 与 `completed_pipeline_init_state_id_occurrences(init_state_id=recorded)`，要求 token 与参数相等（写侧意图前置条件）、breaker engaged（阈值现为 1，见 `scheduler_generation.py:1487`），并要求 `--pin` 等于**模型级 quarantine rerun 计数**（见下文「实现后修订」）。
  3. 若 decision 为预算：写侧不重算 attempt，只要求 `pin >= 1`。round 1 cand-02 指出，大于现值的 pin 不是惰性的，而是会**预授权**：attempt 走到该值时就会放行。实现者核实，读侧 attempt 先经 `_candidate_authoritative_stage_retry_attempt_state` 按候选身份过滤（`scheduler_state_identity_filter.py:271-310`，身份字段来自 `scheduler_state_evidence_owner.py:64-80`），而 CLI 只有 `(source, cycle, model)`，在写侧复算会形成第二套可能分叉的推导。因此本 PR 不修（DEFER，#2400）。runbook 要求 operator 先 dry-run，并用 `list-operator-actions` 最新 pass 的 `attempt` 核对 pin；写错时停止并上报，不要再写一条覆盖。
- **写入**：
  - `insert_pipeline_event(entity_type="forecast_cycle", entity_id=<cycle_id>, event_type="operator_reentry_confirmation", status_from=None, status_to="confirmed", details={model_id, decision, pin, operator, reason, request_id, recorded_init_state_id}`，其中 `recorded_init_state_id` 仅 breaker 必填)`；
  - `cycle_id` 用 `_cycle_id_for_file_source`（`file_orchestration_journal.py:13945`）构造，不手拼；
  - `request_id` 为 uuid4 hex。
- **禁止**：`event_type` 不得是 `retry` / `manual_retry`；`details` 不得带 `trigger:"manual"` 或 `manual_retry_marker`，否则会被 `_manual_retry_marker_shape` 采纳，引发非预期重投。有测试钉住：写入确认物后 `_manual_retry_requested(state)` 仍为 `False`。
- **receipt**：一行 sorted-key JSON `{decision:"dry_run"|"recorded", target:{...}, pin, live:{occurrences?|null}, request_id?}`。

### 读侧：repository accessor

- `FileOrchestrationJournalRepository.operator_reentry_confirmations(*, source_id, cycle_time, model_id, decision) -> list[dict]`：
  - **必须用 `_cycle_rows(source_id, cycle_time, model_id=None)`** 读取 `pipeline_events`（记忆化视图），再按 `details.model_id` 过滤。按 model 读时，`_filter_cycle_rows_for_model`（`:826-834`）在 forecast_cycle 行为 terminal success 时置 `cycle_terminated`，`_event_matches_candidate_rows`（`:13434-13443`）会丢弃全部 forecast_cycle 事件，而 breaker/预算候选正是这种形状（F3）。不读 bounded `candidate_state`，所以 `event_limit` 截断不会漏读；
  - 过滤条件：`entity_type=="forecast_cycle"`、`event_type=="operator_reentry_confirmation"`、`details.model_id` 与 `details.decision` 精确相等；
  - 返回 `details` 列表；任何读失败返回 `[]`，不抛。
- 调度侧经 `getattr(context.active_repository, "operator_reentry_confirmations", None)` 注入。accessor 缺失时（DB plane、测试替身）一律视为无确认物，行为逐字节不变，与 #1157 D5 同形。

### 读侧消费：共享谓词 + 三个消费点

- 共享谓词放在 `scheduler_generation.py`，与 `journal_identity_quarantine_occurrence_count` 相邻：`operator_reentry_confirmation_match(repository, *, source_id, cycle_time, model_id, decision, pin, recorded_init_state_id=None) -> dict | None`。
  - 同样按 `getattr` 注入 accessor，任何异常都返回 `None`。
  - breaker 与预算决策在读侧都只比较 pin；breaker 的 pin 现值是模型级 quarantine rerun 计数，不再比较 token（见「实现后修订」）。
  - 多条匹配时取最新一条（按 event_id）。
- **discovery（F1）**：`_breaker_engaged_gap_identities`（`scheduler_discovery.py:535-588`）在 `breaker_engaged(occurrences)` 为真后，若该模型的谓词匹配（pin == 模型级 rerun 计数），立即 `return None`，即保留执行槽位。
  - 语义与既有「mixed cycle」规则一致：有 confirmed 模型就是有真实工作，用 any 而不是 every。
  - 仍然只读，cycle 仍为 gap，不会被判 complete。
  - 同一 cycle 里未确认的 breaker 模型在 `build_candidates` 中照常 `blocked`。


- **breaker（#1555）**：在 `_journal_predecessor_identity_quarantine` 中，`breaker_engaged(occurrences)` 为真之后、返回 `blocked` 之前，调用谓词（pin 现值 = 模型级 quarantine rerun 计数）。
  - 存在则返回既有的 `CandidateStateDecision("retry", "journal_predecessor_identity_mismatch", ...)`；
  - evidence 在 `_journal_predecessor_identity_retry_evidence` 基础上追加 `operator_reentry_confirmation: {request_id, operator, reason, pin, decision}`。
  - 该 retry 已在两处白名单内；非 strict 车道仍经 `:625-634` 的 #1844 forcing 见证闸（调用点在本函数之后，无需改动）。
  - **quarantine provenance**：该 retry 与普通 quarantine retry 走同一铸造与提交路径，所以 rerun master 照常戳 `journal_predecessor_quarantine_rerun_model_ids`。实现者须用测试核实这一点，不能只凭推断。
- **预算（#1768）**：`_strict_warm_start_terminal_mismatch_decision` 增加一个可选参数 `reentry_match`（callable 或 `None`，由调用点 `:580` 以 context 与 candidate 绑定谓词后传入；为 `None` 时逐字节不变）。
  - 在 `attempt >= retry_limit` 分支里，`reentry_match(pin=attempt)` 命中时，返回既有的 `retry` / `strict_warm_start_terminal_init_state_mismatch` 决策，evidence 追加同形 `operator_reentry_confirmation`；
  - 随后照常经 `:587` 见证闸。
- **严格相等**：pin 与现值必须严格相等，不接受 `>=` / `<=`。
- **一次授权一次重入（为什么能自动失效）**：
  - breaker：放行后，rerun 无论记录哪个 token，只要以带 provenance 的 master 完成，模型级 rerun 计数就 +1，pin 不再匹配，breaker 重新接管（F2 变体同样收敛）。fixture review 已核实 provenance 戳由 `state_evidence.decision == "retry_journal_predecessor_identity_mismatch"` 触发（`accepted_submit_identity.py:1186-1208`），重入 retry 复用同一 evidence。
  - 预算：放行后，rerun 行的 `_retry_<n>` 使 attempt +1，pin 不再匹配，再次 `attempt >= limit` 即回到 `blocked`。
  - 放行到 rerun 完成之间，候选处于 active/非 completed 状态，活跃判定先于 terminal skip（`scheduler_state_decision.py:193-221`），不会再次进入铸造点。这一点由 D.4 与 D.6 的中间 pass 测试钉住。
- **§8.7 read-only invariant**：三个消费点都只读 accessor，评分/过滤面不写 journal。discovery 侧的 quarantine **filter**（`_journal_predecessor_identity_is_stale`）不读确认物，保持只能 DECLINE；读确认物的只有槽位释放判断，而它的作用只是「不释放」，不会 ADMIT completion。
- **spec 冲突处理（F4）**：`job-retry-mechanism`「Strict-warm-start terminal mismatch retries SHALL respect a stage-scoped budget」与 `file-state-snapshot-index`「A non-convergent quarantine SHALL be broken …」两条 requirement 以 MODIFIED delta 写入确认物例外与槽位保留。
- **白名单**：`blocked_journal_predecessor_identity_quarantine` / `blocked_strict_warm_start_init_state_mismatch` 仍不在两处 forced-resubmit 白名单，成员钉测试不改。
- **已知限制**（写进 runbook）：
  - （round 1 cand-01 修订）确认物在 rerun **被接受提交**时即被消费：provenance 戳在 accepted-submit 时写入 master，rerun 计数统计带戳 master 而不看终态，所以 Slurm 失败不会恢复确认物；需要再次重入时用新的 live 计数重新确认。原文「只有 rerun 最终 completed 才被消费」会在「失败 → 普通重试补成 completed」路径上二次放行，已废弃。
  - 候选仍显示 blocked 不等于确认物未生效（#2397）：先看 dry-run receipt 的 live 计数是否已 +1。
- **实现后修订（Phase 1 发现，非第三轮 fixture review）**：
  - 原设计把 breaker 确认物的消费绑定在 token 维度的 `occurrences(X)` 上。实现时 D.4 第 6 步（rerun 记录另一个 stale token Y）实测失败：file journal 在同 run_id 重跑时不更新已 succeeded 的 `hydro_run` 行（`create_hydro_run_from_basin` → `_write_hydro_run(retriable_only=True)` 抛 `HYDRO_RUN_NOT_RETRIABLE` 后返回旧行，`file_orchestration_journal.py:2499-2510`、`:9015-9026`），而 `completed_pipeline_init_state_identity` 以 `hydro_run` 为第一权威（`:1428-1438`）。于是 live token 冻结在首跑的 X，`occurrences(X)` 不随 Y 变化，确认物被二次消费。
  - 修订：pin 现值改为**模型级 quarantine rerun 计数**——对 `(source, cycle, model)` 统计 provenance 命名该模型的 cohort master 数，不比较 identity。round 1 cand-01 进一步去掉「已完成」限定：不看终态，rerun 被接受提交即 +1。只读 accessor `quarantine_rerun_count`（journal-direct，`_cycle_rows`），读失败返回 `None`（谓词视为不匹配）；`completed_pipeline_init_state_id_occurrences` 行为不变。
  - `--recorded-init-state-id` 保留为写侧意图前置条件；读侧不再比较 token。
  - `hydro_run` 权威冻结本身是既有缺陷（§8.7 在 node-22 上对「rerun 得到正确 lineage」的情形也无法收敛），按越界规则单独立 issue，不在本批修复。

- **evidence 措辞**：两条 blocked evidence 的 `retry_policy` 追加 `operator_reentry_command: "confirm-operator-reentry"` 与 `recovery_runbook: "node22-control-plane-manual-recovery"`，使 `manual_retry_required: true` 指向真实通道。

## D5 — #1820 逐行 / 逐 cycle 隔离（仅 operator 命令）

- **只改** `query_released_identity_blocked_jobs`（`file_orchestration_journal.py:1931`）的两段：
  1. 首轮 flat 扫描（`:1978` 遍历 `_iter_direct_pipeline_job_records`）：生成器在异常处终止，调用方无法「跳过一行后继续」。
     - 给 `_iter_direct_pipeline_job_records` 加 keyword-only 参数 `skip_collector: list | None = None`：
       - 为 `None` 时行为逐字节不变；
       - 非 `None` 时在**生成器内部**对单行的 `_read_optional_json` + `_validated_direct_pipeline_job_record` 做 try/except，allowlist 内的原因 append `{path, reason, field}` 后 `continue`，allowlist 外 re-raise。
     - 另两个调用方 `_cycle_source_discoveries`、`_replay_all_pipeline_job_records` 不传该参数。
     - 路径枚举与 byte/file 预算原语不动。
  2. 逐 cycle 确认循环（`:1998` 的 `_iter_pipeline_job_records_scoped(cycle_scope)`）：（round 1 cand-04 修订）cycle replay 会重读同 cycle 的 flat 行，整 cycle 扣下会让同 cycle 的 wedged 行消失，违反 spec 与 #1820 验收。改为逐行 skip：`skip_collector` 贯穿 `_iter_pipeline_job_records_scoped` → `_replay_pipeline_job_records_for_cycle` → `_iter_flat_direct_pipeline_job_records_for_cycle`（默认 `None` 逐字节不变），skip 模式读取绕过 `_cycle_job_records_memoized`，skip 条目按 `(path, reason)` 去重；删除 cycle 级 except。C.3 继续钉住同一实例的非 skip 读取仍 raise。
  3. **unscoped 全树 fallback**（`:2002-2009`）**不隔离**：它走 `_replay_all_pipeline_job_records`，受 `full_tree_replay` 预算契约约束（`test_file_journal_full_tree_budget_contract.py:307`），生产规模下本来就会先撞预算。在 runbook 中明写。
- **可跳过原因**是「单行内容校验」的封闭 allowlist 常量 `ROW_CONTENT_SKIP_REASONS`（F6）。实现者须 grep `_validated_direct_pipeline_job_record`（`:8825-8851`）、解码路径（`:13672-13730`）与 accepted-submit 校验（`:12371-12377`）的全部 raise 点，补全列表并逐个表驱动测试。已知成员：
  - `file_journal_malformed_json`、`file_journal_expected_object`、`file_journal_record_type_mismatch`、`file_journal_schema_mismatch`
  - `file_journal_missing_identity`、`file_journal_invalid_identity`、`file_journal_invalid_cycle_time`
  - `file_journal_{source,cycle,cycle_id,run,model,job}_mismatch`
  - `file_journal_evidence_invariant_invalid`、`file_journal_evidence_{enum_invalid,type_invalid,required,field_not_allowed,limit_exceeded}`
  - `file_journal_json_node_limit_exceeded`、`file_journal_json_depth_exceeded`（单文档复杂度，不是聚合预算）
  - `file_journal_unsafe_identity`、`file_journal_invalid_field`
  - **不含** `file_journal_unsafe_path_segment`（文件名层面，接近 containment，re-raise）
- 其余 `FileOrchestrationJournalError` 一律 re-raise，包括 `file_journal_file_limit_exceeded`、`file_journal_depth_limit_exceeded`、`file_journal_record_limit_exceeded`、`file_journal_byte_limit_exceeded`、`file_journal_unsafe_scanned_entry`、`file_journal_quiescence_authority_changed`、`file_journal_unreadable`。
- **返回形状**：函数签名不变，仍返回 list，调用方与既有测试依赖它。跳过项经新增的只读属性或 out 参数传出，由实现者择一，要求调用点显式取用，而不是全局状态：
  - 推荐新增 `query_released_identity_blocked_jobs_with_skips() -> tuple[list, list]`，原函数委托并丢弃 skips；
  - `operator_released_reservation_recovery` 的 list 模式改调新函数。
- **receipt**：list 模式追加 `skipped_count` 与 `skipped:[{path, reason, field}]`（round 1 cand-04 后不再有 cycle 级条目），`path` 经与 receipt 其他字段同一套公共脱敏；exit code 语义不变。
- **`--job-id` 模式不改**（F5）：它走 `get_pipeline_job` + `_diagnose_released_reservation_recovery` 的 typed refusal（`operator_released_reservation_recovery.py:161-174`），不经过 listing；改成新函数会把带原因的 refusal 退化为 not-found。
- **热路径行为零改动**：`_cycle_job_records_memoized`、`_cycle_rows`、`_cycle_source_discoveries` 不改；`_iter_flat_direct_pipeline_job_records_for_cycle` 只加默认 `None` 的 `skip_collector`（round 1 cand-04），调度侧调用不传，行为逐字节不变。理由：它们服务 `candidate_state` / `get_pipeline_job`，在那里逐行跳过等于静默窄化候选集，比响亮中止更糟。
- **预算契约**：`tests/test_file_journal_full_tree_budget_contract.py` 原样全绿，并新增断言：一行畸形与预算超限同时存在时，预算拒绝仍 raise。

## 不做（non-goals）

- 不改 breaker 判定、阈值、计数口径；不改预算判定与 `NHMS_SCHEDULER_RETRY_LIMIT` 默认值，#1767 的回收不在本批。
- 不改 completed-skip 与 `manual_retry_requested` 的评估顺序（#1555 已否决，#1201/#1205 风险）。
- 不做失败签名感知的预算重置（用户裁决选确认物）。
- 不做 #1543 的 gate 侧合轴，也不实现 #1118 breaker。
- 不改 DB plane 的 retry 路径；DB plane 的 accessor 缺失即无确认物。
- 不接 systemd timer / 告警投递；不改 409 payload 文本。

## 偏离记录（预置）

1. 本分支带着上一批的 post-merge 提交 `8713b73e9`（归档 + loop-log + ADR 0003 deferral + `.review-gate-issues.json`）。
2. #1186 Verification 文本中 node-22 的 `git pull` + 裸 `uv run`，改为隔离 worktree + `.venv/bin/python -m`（D2）。
3. Phase 0 勘察 explorer 执行过一次只读的裸 `python3` heredoc，违反「Python 只经 uv」纪律；无写入，未重复。
4. fixture level 由 precedent 的 `expanded` 升为 `high`（见 Risk triage）。
5. fixture review 第一轮判 revise（F1–F10），全部按本文修正；第二轮 approve（附 3 条 P3，已并入）。

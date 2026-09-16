# Design — node-22 operator 重入确认物 + 发射可行性标 + 逐行隔离（#1543 + #1555 + #1768 + #1820）

## Risk triage

- **Fixture level: `high`**，round 1 设 4 个 reviewer 席位。
  - **与 precedent 的偏离**：#1157 的 `2026-08-18-scheduler-quarantine-residual-hardening` 定为 `expanded`，但它只有一个 seam。本批改动面更宽：
    - 调度决策路径新增一条 operator 授权通道（D）；
    - evidence bounded 契约扩容（A'/B）；
    - journal 扫描异常行为改变（C）；
    - 新增一条 CLI 与一份新 runbook。
  - 五个 issue 都没有 `Suggested fixture level` 字段，本次为首次定级。
- **Risk packs selected**：
  - `invariant-state`：
    - breaker / 预算的反复活形状；
    - 一次授权一次重入；
    - §8.7 read-only invariant；
    - #1843/#1844 forcing 见证不变量在重入路径上保持。
  - `correctness`：
    - pin 匹配语义；
    - bounded summary 对 `retry_policy` 键的保留；
    - skip allowlist 的边界。
  - `security-perf`：
    - operator 写侧的前置条件与 dry-run 默认值；
    - 预算拒绝不得被吞。
  - `test-evidence`：每个切片先红后绿；闭环测试走公开入口 `build_candidates` / scheduler pass。
- **Risk packs not selected**：`integration`。gateway、reservation、producer 不改；display API 只新增 slug 文件存在性测试，payload 不变。

## 事实基线（merge-base `49cf31316c`，已读码核实）

**本表每一处坐标都以 `49cf31316c:` 为前缀读取**（例：`git show 49cf31316c:services/orchestrator/scheduler_candidates.py`），在本分支 HEAD 上一律**不成立**——本 PR 改动了其中多个文件。这是刻意的：这张表记的是「动手之前长什么样」，重解到 HEAD 会毁掉它的用途。与 D4 里 `53c39b99c:` / `92140f2e1:` 的 SHA 限定引用同一办法。round-3 c-02 曾把本表按 HEAD 判为坐标漂移，是误判。

| 事实 | 位置（均相对 `49cf31316c`） |
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

## 坐标漂移：本 PR 自己造成的，已逐符号重解

round 5 的提交前测量关对本文全部坐标做了读回核实，D3/D5 两块查出一批失效引用。

**第一版把成因写成「随其他 PR 合并而位移、不是本轮引入」，那是假的**，实测推翻：
`git log --merges 49cf31316c..HEAD` 为空，range 内 14 个提交全是本分支的；本 PR 往同几个文件插的行是
`file_orchestration_journal.py` +357、`scheduler_backfill_predecessor.py` +88、`scheduler_candidates.py` +232
（本轮未提交部分再 +47）。顶走这些坐标的就是这个 PR。它们不是既有债，是本 PR 的输出。

更要紧的是**提交时机**：`scheduler_candidates.py` 的那几组在**已提交的** `09b162a91` 上逐条精确正确，
测到的偏移完全来自本轮**未提交**的 hunk。也就是说它们现在不陈，**提交这一刻才变陈**——
「既有漂移、报告不修」的理由在这里不成立，不修就是亲手制造 drift。因此全部就地修完：

- `scheduler_backfill_predecessor.py` `:517-525` / `:544-552` → `:543` / `:573`（后者在 merge-base 上就差一行）
- `file_orchestration_journal.py` `:826-834` → `:870`；`:8825-8851` → `:9069`
- `file_orchestration_journal.py` `:13672-13730` / `:12371-12377` → `:13959`+`:14002` / `:12619`。
  **这两条是最坏的**：它们在 HEAD 上并非解析失败，而是落到了另外两个讲得通的函数
  （`_job_matches_candidate`、`_durable_error_message`）上——一个算出来的坐标撞上别人，读起来完全合理。
- `scheduler_candidates.py` D5 段：`_upgrade_retry_for_strict_warm_start_manifest` def `:2452`→`:2499`、
  改写点 `:2475`→**`:2522`**（不是 `:2525`——那是参数里的调用）、
  `_apply_explicit_missing_forcing_repair_policy` def `:1836`→`:1883`、改写点 `:1970`→`:2017`、
  `_strict_warm_start_forcing_witness_decision` def `:2389`→`:2436`、blocked 构造 `:2435-2439`→`:2482-2486`、
  `_missing_forcing_repair_rejected_decision` def `:1682`→`:1729`

全部**逐符号重解**（`grep -n 'def <symbol>'` 后 `sed -n '<N>p'` 读回），没有一处用 diff 行差算——
本轮两次算错都是这么来的，其中一条 round-3 的 verifier 还曾标成「已核实正确」。
上面的事实基线表（SHA 钉住）与任何带 `49cf31316c:` / `53c39b99c:` / `92140f2e1:` / `493a0d83e:`
前缀的坐标**不在重解范围内**，也正因为同一坐标字符串在本文有多种含义（`:2475` 就有三种），
这次是逐处按上下文改，没有做全局替换。检测工具与后续清理见 #2423。

## D1/D2 — 已移出本 change（#1186 列举面）

原 D1（只读子命令 `list-operator-actions`）与 D2（node-22 现场收据的执行方式）随 #1186 移入子 change
`node22-operator-action-listing`（PR-B）。拆分依据见 `.workplans/pr-2398/review/split-plan.md`。

留在本 change 的只有两项原属 A 切片、与确认物直接相关的部分：

- **bounded 白名单扩容**（与 D3 共用同一次编辑）：在 `_BOUNDED_CANDIDATE_STATE_EVIDENCE_KEYS` 追加
  `retry_attempt ← (retry_policy, attempt)`、`retry_limit ← (retry_policy, retry_limit)`、
  `retry_occurrences ← (retry_policy, occurrences)`、`manual_retry_required ← (retry_policy, manual_retry_required)`；
  summary 键名不得与 `_BOUNDED_CANDIDATE_SUMMARY_KEYS` 冲突；逐项钉测试，`False`/`0` 值也保留。
  理由不依赖列举面：summarized pass 丢弃整个 `state_evidence`，运维按过渡口径直接读 evidence 时同样看不到 attempt / occurrences。
- **runbook**：新建 `docs/runbooks/node22-control-plane-manual-recovery.md`，对齐 `apps/api/routes/pipeline.py:248` 的 slug；
  测试从源码常量读 slug 断言文件存在。四类决策的处置入口（permanent_failure / cancelled 走
  `scripts/node22_manual_retry_failed_runs.py`；breaker / 预算走 D4 的 `confirm-operator-reentry`）与 node-22 执行纪律照旧。
  「怎么找到目标」写过渡口径——直接读 evidence root 下最新的 `scheduler_*.json`，按 decision 字面量筛 `blocked_candidates`，
  并看 not-selected `source_cycles` 的 breaker 释放条目；PR-B 落地 `list-operator-actions` 时替换该段与退出码表。

## D3 — §8.6 发射可行性标（#1543）

- 在 `attach_emission_summary_to_blocked` / `_attach_summary_to_single_blocked` 里，根据本 successor 自己的 records 写顶层布尔 `state_evidence.predecessor_emission_blocked`。
- **反向定义**（fixture review F8）：只列一份**瞬时** allowlist 常量；本 successor 的任一 `skipped`/`truncated` 记录的 reason 不在 allowlist 内，就为 `True`。瞬时 allowlist：
  - `predecessor_already_present`
  - `predecessor_backfill_active_pipeline`
  - `predecessor_raw_manifest_env_unwired`：配置问题，已有 one-per-pass warning
  - `LINEAGE_SCOPED_OUT_REASON`：**仅当** successor 的 cycle_time 早于记录中的 `cutover_valid_time`，此时 discovery 已把该 cycle 移出评分（`scheduler_lineage.py`）；successor cycle_time 不早于 cutover 时（lead 大于 cadence 的几何），successor 会永久 blocked，因此打 `True`（F9）
- 这样 `predecessor_raw_manifest_not_ready`、`predecessor_model_not_available`、`predecessor_emission_cap_reached`、`predecessor_candidate_construction_failed`（`:543`）、`predecessor_gate_failed`（`:573`），以及今后新增的任何 skip 臂，默认都为 `True`，方向是 fail toward escalation。
- 本 successor 的记录全部是 emitted 或 allowlist 内的瞬时 skip 时为 `False`；没有任何记录的 successor 不写该键。
- **截断定位**：迭代改为 `enumerate(pending)`，cap `break` 时把 `pending[i:]` 的 successor id 去重排序后，写入截断记录的 `successor_candidate_ids`（列表）。`attach_emission_summary_to_blocked` 对截断记录按该列表逐个归组，**归组时做 per-successor 投影**：挂到某个 successor 的 `state_evidence` 上的那份记录只带标量 `successor_candidate_id`（即它自己），不带整张 `successor_candidate_ids`；完整列表只留在 `_build_candidates` 的局部 emission 记录里（`scheduler_candidates.py:1235` 局部变量，`:1251-1254` 归组后即丢弃，**从不序列化**）。没有运行时信息损失：被截断的 successor 全集可以由 blocked 条目里 `status == "truncated"` 的投影记录逐个扫回，逐字节等价。
  - `status/reason/total_attempted/cap` 不变；
  - pass totals 语义不变，截断记录仍计 1 次；
  - 列表长度以 pending 为上界。pending 本身受的是 pass 候选上限 `MAX_CANDIDATES = 10000`（`scheduler_candidates.py:64`、`:271`），**不是** `MAX_PREDECESSOR_EMISSIONS = 256`；若把共享的那一份整表记录按引用扇出到每个被截断的 successor 下，序列化时每个 successor 各展开一份，evidence 字节数是 O(N²)（≈ N²×80 B，N≈250 就越过 `MAX_EVIDENCE_BYTES = 5_000_000`，N=744 时约 44 MB），pass 会退化成 size fallback——恰好是本 change 的 runbook 所警告的盲区。per-successor 投影把它压回线性，并由一条 pending 数量高于 cap 的规模测试钉住。
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
  3. 若 decision 为预算：（Round 3 修订，取代下文旧文）要求 `--pin` 等于**模型级预算重入计数**（journal-direct，见「Round 3 修订」），不再与 attempt 比较。Round 4 修订（r4-01）：这只消除了「错 pin」一半；写侧仍看不到预算是否已耗尽（不可 db-free 复算），耗尽前写入的确认物在耗尽后 pin 仍等于计数时会放行（「对 pin、错时间」），#2400 **不在**本 PR 关闭，残余改为 runbook 操作义务：只确认最新 pass 列为预算 blocked 的目标，rerun 飞行中不确认。旧文：写侧不重算 attempt，只要求 `pin >= 1`。round 1 cand-02 指出，大于现值的 pin 不是惰性的，而是会**预授权**：attempt 走到该值时就会放行。实现者核实，读侧 attempt 先经 `_candidate_authoritative_stage_retry_attempt_state` 按候选身份过滤（`scheduler_state_identity_filter.py:271-310`，身份字段来自 `scheduler_state_evidence_owner.py:64-80`），而 CLI 只有 `(source, cycle, model)`，在写侧复算会形成第二套可能分叉的推导。因此本 PR 不修（DEFER，#2400）。runbook 要求 operator 先 dry-run，并用最新 pass evidence 的现值核对 pin；写错时停止并上报，不要再写一条覆盖。
- **写入**：
  - `insert_pipeline_event(entity_type="forecast_cycle", entity_id=<cycle_id>, event_type="operator_reentry_confirmation", status_from=None, status_to="confirmed", details={model_id, decision, pin, operator, reason, request_id, recorded_init_state_id}`，其中 `recorded_init_state_id` 仅 breaker 必填)`；
  - `cycle_id` 用 `_cycle_id_for_file_source`（`file_orchestration_journal.py:14230`）构造，不手拼；
  - `request_id` 为 uuid4 hex。
- **禁止**：`event_type` 不得是 `retry` / `manual_retry`；`details` 不得带 `trigger:"manual"` 或 `manual_retry_marker`，否则会被 `_manual_retry_marker_shape` 采纳，引发非预期重投。有测试钉住：写入确认物后 `_manual_retry_requested(state)` 仍为 `False`。
- **receipt**：一行 sorted-key JSON `{decision:"dry_run"|"recorded", target:{...}, pin, live:{occurrences?|null}, request_id?}`。

### 读侧：repository accessor

- `FileOrchestrationJournalRepository.operator_reentry_confirmations(*, source_id, cycle_time, model_id, decision) -> list[dict]`：
  - **必须用 `_cycle_rows(source_id, cycle_time, model_id=None)`** 读取 `pipeline_events`（记忆化视图），再按 `details.model_id` 过滤。按 model 读时，`_filter_cycle_rows_for_model`（def `:870`）在 forecast_cycle 行为 terminal success 时置 `cycle_terminated`，`_event_matches_candidate_rows`（`:13434-13443`）会丢弃全部 forecast_cycle 事件，而 breaker/预算候选正是这种形状（F3）。不读 bounded `candidate_state`，所以 `event_limit` 截断不会漏读；
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
  - 在 `attempt >= retry_limit` 分支里，`reentry_match(pin=模型级预算重入计数)` 命中时（Round 3 修订；原为 `pin=attempt`），返回既有的 `retry` / `strict_warm_start_terminal_init_state_mismatch` 决策，evidence 追加同形 `operator_reentry_confirmation`；
  - 随后照常经 `:587` 见证闸。
- **严格相等**：pin 与现值必须严格相等，不接受 `>=` / `<=`。
- **一次授权一次重入（为什么能自动失效）**：
  - breaker：放行后，rerun 无论记录哪个 token，只要以带 provenance 的 master 完成，模型级 rerun 计数就 +1，pin 不再匹配，breaker 重新接管（F2 变体同样收敛）。fixture review 已核实 provenance 戳由 `state_evidence.decision == "retry_journal_predecessor_identity_mismatch"` 触发（`accepted_submit_identity.py:1186-1208`），重入 retry 复用同一 evidence。
  - 预算：（Round 3 修订）放行的 retry 在 reservation 时于 cohort master 戳预算重入 provenance，模型级预算重入计数 +1，pin 不再匹配，回到 `blocked`。原文「rerun 行的 `_retry_<n>` 使 attempt +1」不成立：`_next_retry_attempt_for_stage` 只看同 job-id 前缀，重入 mint 落在与耗尽预算的 retry 不同的前缀下时 suffix 从头计，一次确认可重入多次（r3-02）。
  - 放行到 rerun 完成之间，候选处于 active/非 completed 状态，活跃判定先于 terminal skip（`scheduler_state_decision.py:193-221`），不会再次进入铸造点。这一点由 D.4 与 D.6 的中间 pass 测试钉住。
- **§8.7 read-only invariant**：三个消费点都只读 accessor，评分/过滤面不写 journal。discovery 侧的 quarantine **filter**（`_journal_predecessor_identity_is_stale`）不读确认物，保持只能 DECLINE；读确认物的只有槽位释放判断，而它的作用只是「不释放」，不会 ADMIT completion。
- **spec 冲突处理（F4）**：`job-retry-mechanism`「Strict-warm-start terminal mismatch retries SHALL respect a stage-scoped budget」与 `file-state-snapshot-index`「A non-convergent quarantine SHALL be broken …」两条 requirement 以 MODIFIED delta 写入确认物例外与槽位保留。
- **白名单**：`blocked_journal_predecessor_identity_quarantine` / `blocked_strict_warm_start_init_state_mismatch` 仍不在两处 forced-resubmit 白名单，成员钉测试不改。
- **已知限制**（写进 runbook）：
  - （round 1 cand-01 修订）确认物在 rerun **被接受提交**时即被消费：provenance 戳在 accepted-submit 时写入 master，rerun 计数统计带戳 master 而不看终态，所以 Slurm 失败不会恢复确认物；需要再次重入时用新的 live 计数重新确认。原文「只有 rerun 最终 completed 才被消费」会在「失败 → 普通重试补成 completed」路径上二次放行，已废弃。
  - 候选仍显示 blocked 不等于确认物未生效（#2397）：先看 dry-run receipt 的 live 计数是否已 +1。
- **实现后修订（Phase 1 发现，非第三轮 fixture review）**：
  - 原设计把 breaker 确认物的消费绑定在 token 维度的 `occurrences(X)` 上。实现时 D.4 第 6 步（rerun 记录另一个 stale token Y）实测失败：file journal 在同 run_id 重跑时不更新已 succeeded 的 `hydro_run` 行（`create_hydro_run_from_basin` → `_write_hydro_run(retriable_only=True)` 抛 `HYDRO_RUN_NOT_RETRIABLE` 后返回旧行，`file_orchestration_journal.py:2514`/`:2552`、`:9098`/`:9115`），而 `completed_pipeline_init_state_identity` 以 `hydro_run` 为第一权威（`:1385`）。于是 live token 冻结在首跑的 X，`occurrences(X)` 不随 Y 变化，确认物被二次消费。
  - 修订：pin 现值改为**模型级 quarantine rerun 计数**——对 `(source, cycle, model)` 统计 provenance 命名该模型的 cohort master 数，不比较 identity。round 1 cand-01 进一步去掉「已完成」限定：不看终态，rerun 被接受提交即 +1。只读 accessor `quarantine_rerun_count`（journal-direct，`_cycle_rows`），读失败返回 `None`（谓词视为不匹配）；`completed_pipeline_init_state_id_occurrences` 行为不变。
  - `--recorded-init-state-id` 保留为写侧意图前置条件；读侧不再比较 token。
  - `hydro_run` 权威冻结本身是既有缺陷（§8.7 在 node-22 上对「rerun 得到正确 lineage」的情形也无法收敛），按越界规则单独立 issue，不在本批修复。

- **Round 4 修订（第二次 retro，`.workplans/pr-2398/review/round-4/retro.md`）**：
  - 预算确认物的时间绑定残余（#2400）降为 runbook 操作义务，见 D4 前置条件 3。
  - 同一 retro 里 listing 的时序规则一般化随 #1186 移入 PR-B 的 change。

- **Round 3 修订（review failure retro，shape depth，`.workplans/pr-2398/review/round-3/retro.md`）**：
  - 共同不变量：fail-closed 的 operator 契约必须键在「每个应消费/应暴露它的事件都会推动」的量上。
  - 预算臂与 breaker 同形：reservation writer 在 basin 的 retry evidence 带 `operator_reentry_confirmation`（decision 为预算）时，于 cohort MASTER 行戳预算重入 provenance（模型 id 列表）；只读 accessor 统计 provenance 命名该模型的 master 数，不看终态、job id、retry suffix，读失败返回 `None`（谓词视为不匹配）。
  - 写侧与读侧、dry-run receipt 的 `live.*`、runbook 报告同一个量。
  - 不改 `_next_retry_attempt_for_stage`；普通 strict retry 跨前缀 attempt 泄漏属既有缺陷，单独立单。
  - 预算臂的 blocked 判定仍由 stage-scoped attempt 驱动，本修订只改确认物的 pin。

- **PR-A round 1 修订（c-03，one-shot-authorization-leak）**：Round 3 的共同不变量在**写侧**又破了一次，这次是 decision 字面量被改写。
  - 机制：两条确认 retry 都要过 `_strict_warm_start_forcing_witness_decision`；缺 per-model forcing 见证时返回 missing-forcing blocked（确认块经 `**base_evidence` 存活，未被丢弃）。若 operator 同时开了 `--repair-missing-forcing` 且 cycle 精确匹配，`_apply_explicit_missing_forcing_repair_policy` 把 `decision` 改写成 `retry_repair_missing_forcing`（`scheduler_candidates.py:2017`）——它在 forced-resubmit 白名单内，会**真提交**；但它既不等于 `53c39b99c:accepted_submit_identity.py:1219` 的 quarantine 字面量，也不等于同一 blob `:1243` 的预算字面量（两处坐标均为修复前；修复后 quarantine 字面量在 `:1247`，预算字面量已整条删除，只剩 `:1291` 的块比较），于是不戳 provenance，计数不动，确认物下一 pass 继续匹配。一次签名放行了修复提交 + 后续重入。
  - 确认物是该泄漏的**必要条件**：没有它，决策是 `blocked/strict_warm_start_retry_budget_exhausted`，不在 `_MISSING_FORCING_BLOCKER_REASONS` 内，修复路径根本够不到该候选。
  - Round 1 取 A（投影键在确认块上），不取 B（拒绝改写），理由是「B 只躲开一个消费事件，A 才让每个消费事件都推动 pin」。**Round 2 实测推翻了这个理由**，见下一条。既有 decision 字面量触发保留（普通 quarantine rerun 仍须计数）。
  - 同时修文档：`current-production-ops.md:373` 把 strict 车道的 missing-forcing blocked 直接导进 `--repair-missing-forcing`，而 `node22-control-plane-manual-recovery.md:223` 承诺「不提交」——照文档操作就会踩中。
  - **Phase 6.2 键路审计（round 1 完成）**：枚举确认匹配之后所有改写 retry `decision` 的点——`_upgrade_retry_for_strict_warm_start_manifest`（def `:2499`，改写点 `:2522`）、`_apply_explicit_missing_forcing_repair_policy`（def `:1883`，改写点 `:2017`）、forcing 见证 blocked 构造（`_strict_warm_start_forcing_witness_decision` def `:2436`，blocked 构造 `:2482-2486`）——逐个核实 `operator_reentry_confirmation` 键路存活。这是一次**键路**审计（块活没活），它全绿，而 r2-01 就躺在它旁边没被看见：缺的那一维是**事件路径**（改写后这个候选从哪个 stage 重启，那个 stage 到不到得了 stamp 点）。

- **PR-A round 2 修订（r2-01，同一 one-shot-authorization-leak 类第二次复发）**：A 不够，B 现在是必需的。
  - 实测（三个 scratchpad 探针，见 `.workplans/pr-2406/review/round-2/verdicts.md`）：被改判的 repair retry 把 `restart_stage` 设成 `"forcing"`（`scheduler_candidates.py:2020-2021`），链路真的照办（`chain_forecast_execution.py:173`）；而 provenance 只在 forecast cohort reservation 处写（`chain_forecast_orchestrator_cycle.py:666` quarantine 投影 / `:673` 预算投影，字段写入 `:683-684`，`chain_stage_execution.py:37` 的别名集不含 forcing）。round-3 verifier 把此处旧写的 `:633-635` 列为「已核实正确」，实为原生错误——该文件本 PR 从未改动，坐标从一开始就不对，round 4 修正。

  - **一处措辞更正（round 2 fix pass 实测推翻）**：不能说「这个消费者结构上不可能推动 pin」。链路是按 index 起跑并**向前跑完**的，forcing 成功时它仍会到达 forecast cohort reservation 并真的盖戳——`92140f2e1:tests/test_production_scheduler.py:57888` 当时是绿的，就是这个形状。真正的缺陷是更弱但为真的那句：**确认物是否被消费，取决于一个 operator 从未授权的 stage 的成败**。forcing 失败时才出现「真提交了、计数没动、确认物还在」。这恰恰就是 B 的理由，不是脚注。
  - 且 stamp 点不能搬：`accepted_submit_row_kind`（`accepted_submit_identity.py:519-524`）对非 forecast cohort stage 返回 `None`，`_quarantine_rerun_masters`（`file_orchestration_journal.py:13516-13550`）按 `!= "master"` 过滤，forcing 行永远数不进去——搬 stamp 就得同时换计数口径。
  - 更糟的是这次 forcing 失败会**让预算判定失效**：失败的 forcing 跑在新的 run-id 前缀下，stage 域的 `attempt` 从 2/2 掉回 0/2（探针 2），operator 照 runbook 回补 forcing 后，候选会以 `retry_strict_warm_start_retry_run_manifest_mismatch` **自动**重入 forecast，不带任何确认块、不戳 provenance（探针 3）。一次签名 → 两次 forecast 重入，可重复，计数始终不动。
  - 因此不变量收紧为：**每个从确认物派生的事件，要么在 reservation writer 处推动计数，要么在提交之前被拒绝，没有第三种。** A 覆盖所有「仍从 forecast 重启」的改写（manifest 升级、见证标注——均已验证块存活），B 覆盖唯一那条「从 forcing 重启」的改判。

  - **A 在 B 之后没有可达实例，是纯纵深防御**（round 2 fix pass 实测）：`_upgrade_retry_for_strict_warm_start_manifest` 的早退在 `scheduler_candidates.py:2512-2516`（`native_shud_resubmitted is True` 且 `restart_stage == "forecast"`），断路器臂（`:2684` restart_stage / `:2686` native_shud_resubmitted）与预算臂（`:2877` / `:2880`）**都正好命中**——预算臂在 strict 车道上救它的是 `:2512-2516` 那道，而非 `:2506-2511`（forcing_repair authorized + `restart_stage == "forcing"`）那道。佐证：`tests/test_production_scheduler.py:57165`（断路器臂）、`:57629`、`:57833`（预算臂）的 decision 字面量断言在 HEAD 就未被改写。因此规格里不再拿 manifest 升级给 A 举例，A 的理由改成「`decision` 字面量在改写下不稳定、块稳定，投影不该依赖是哪一次改写触发」。`tests/test_warm_start_chaining.py` 里的 `upgraded` 与 `model_budget_repair` 两个 basin 相应都是**合成形状**，已在该测试 docstring 标注。这条与 #2408 的可达性追问同源（#2407 守的正是这道早退所依赖的隐式耦合）。
  - B 的落点：`_apply_explicit_missing_forcing_repair_policy` 在 flag 判定之后的第一个前置条件——`decision.evidence` 带 `operator_reentry_confirmation` 即走既有 reject 构造（`_missing_forcing_repair_rejected_decision`，def `:1729`）并给出具名 reason，候选留在 forcing 见证 blocked 决策上，确认物**保持待用**（没有任何东西被消费，这是正确的）。operator 先回补 forcing，下一 pass 确认 retry 从 `forecast` 起跑、正常戳、计数到 N+1。未确认候选的 #1844/8.5 修复行为完全不变，既有 repair 测试就是这条的回归闸。
  - **Phase 6.2 事件路径审计（round 2 扩展）**：对「确认匹配 → Slurm 提交」的每一条路径，记录 `candidates.append` 时的 `restart_stage`，并判定该 stage 是否到达 `chain_forecast_orchestrator_cycle.py:666`/`:673` 的投影。全表见 `.workplans/pr-2406/review/round-2/invariant-audit.md`。留在仓库里的闸是一条结构测试：任何一 pass 产出的候选，只要 `action == "retry"` 且 `state_evidence` 带 `operator_reentry_confirmation`，就必须 `restart_stage == "forecast"`。

- **evidence 措辞**：两条 blocked evidence 的 `retry_policy` 追加 `operator_reentry_command: "confirm-operator-reentry"` 与 `recovery_runbook: "node22-control-plane-manual-recovery"`，使 `manual_retry_required: true` 指向真实通道。

## D5 — #1820 逐行 / 逐 cycle 隔离（仅 operator 命令）

- **只改** `query_released_identity_blocked_jobs`（`file_orchestration_journal.py:2076`）的两段：
  1. 首轮 flat 扫描（`:1978` 遍历 `_iter_direct_pipeline_job_records`）：生成器在异常处终止，调用方无法「跳过一行后继续」。
     - 给 `_iter_direct_pipeline_job_records` 加 keyword-only 参数 `skip_collector: list | None = None`：
       - 为 `None` 时行为逐字节不变；
       - 非 `None` 时在**生成器内部**对单行的 `_read_optional_json` + `_validated_direct_pipeline_job_record` 做 try/except，allowlist 内的原因 append `{path, reason, field}` 后 `continue`，allowlist 外 re-raise。
     - 另两个调用方 `_cycle_source_discoveries`、`_replay_all_pipeline_job_records` 不传该参数。
     - 路径枚举与 byte/file 预算原语不动。
  2. 逐 cycle 确认循环（`:1998` 的 `_iter_pipeline_job_records_scoped(cycle_scope)`）：（round 1 cand-04 修订）cycle replay 会重读同 cycle 的 flat 行，整 cycle 扣下会让同 cycle 的 wedged 行消失，违反 spec 与 #1820 验收。改为逐行 skip：`skip_collector` 贯穿 `_iter_pipeline_job_records_scoped` → `_replay_pipeline_job_records_for_cycle` → `_iter_flat_direct_pipeline_job_records_for_cycle`（默认 `None` 逐字节不变），skip 模式读取绕过 `_cycle_job_records_memoized`，skip 条目按 `(path, reason)` 去重；删除 cycle 级 except。C.3 继续钉住同一实例的非 skip 读取仍 raise。
  3. **unscoped 全树 fallback**（`:2002-2009`）**不隔离**：它走 `_replay_all_pipeline_job_records`，受 `full_tree_replay` 预算契约约束（`test_file_journal_full_tree_budget_contract.py:307`），生产规模下本来就会先撞预算。在 runbook 中明写。
- **可跳过原因**是「单行内容校验」的封闭 allowlist 常量 `ROW_CONTENT_SKIP_REASONS`（F6）。实现者须 grep `_validated_direct_pipeline_job_record`（def `:9069`）、解码路径（`_decode_mapping` def `:13959`、`_validate_json_complexity` def `:14002`）与 accepted-submit 校验（`_validate_accepted_submit_evidence` def `:12619`）的全部 raise 点，补全列表并逐个表驱动测试。已知成员：
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

## Round 4 — restart_stage writer audit (sink enforcement)

三轮审查各自找到一个**不同**的 `restart_stage` 改写点（r1 c-03 → 决策字面量；r2 r2-01 →
repair 改判；r3 c3-01 → raw-manifest `convert`），每轮只修那一个。Round 4 把执法点从
**源**移到**汇**：候选清单在 `_build_candidates` 返回前统一过一遍
（`services/orchestrator/scheduler_candidates.py:1261`，晚于 §8.6 的头插
`scheduler_backfill_predecessor.py:699`），拒绝任何带 `operator_reentry_confirmation`
块、而**有效重启阶段**不是 `forecast` 的候选。

**有效重启阶段**（`_candidate_effective_restart_stage`，`scheduler_candidates.py:1540-1589`）
不是裸读 evidence 键，而是 run manifest 真正会带的那个：
`scheduler_candidate_manifest.py:238` 在 `fresh_ingestion.mode == "full_chain"` 时**不写**
`restart_stage`，所以 `full_chain` 的有效阶段是 `None`，正向检查随即拒绝。

**这里曾经写着「manifest 不带该键时 `chain_forecast_execution.py:173` 从第 0 个 stage 起跑」，那是假的**
（round 4 的 B-2，测量关复查时发现它还活着）。实测链路：`chain_forecast_control.py:148` 调
`_restart_stage_from_basins`（`chain_runtime_utils.py:319-336`），该 helper **先**读 basin 顶层
`restart_stage`（`:321-322`），缺失才**回退**读内嵌的 `state_evidence["restart_stage"]` 或
`["restart_from_stage"]`（`:325-331`，其中 `:329` 就是 restart-from 的回退读），然后对 cohort 取
`min`（`:336`）；只有全都解析不出时才返回 `None`，此时 `_restart_stage_index` 才给 0
（`chain_runtime_utils.py:418-424`）。manifest 顶层被抹掉**不等于**从第 0 阶段起跑——它只是让链去读
manifest 里同样带着的那份内嵌证据（`scheduler_candidate_manifest.py:234`）。

`restart_from_stage` **不参与**守卫判定（manifest 没有同名顶层键），但链**会**在 `restart_stage`
为假值时读它（`chain_runtime_utils.py:329`，实测 `restart_from_stage: "convert"` → 链解析出
`convert`、守卫算 `None` 并拒绝）。这不对称是**刻意**的，也正是分歧永远指向拒绝的原因：守卫不该
被"教会"这个回退。实测见
`tests/test_production_scheduler.py::test_the_stage_the_sink_guards_is_the_stage_the_run_manifest_carries`。
链侧那个回退本身（含 cohort `min`）是既有行为，不在本 PR 范围，已立 #2416。

### 审计表

方法：对 `services/orchestrator/` **全目录**机械 grep `"restart_stage"] =` / `"restart_stage":` /
`restart_stage=`，命中 **15** 个文件，**每个赋值一行**（读取不算）。判据是「是否在最终会成为
`candidate.state_evidence` 的那个 dict 上写**顶层**键」。在册的是三个真 writer
（`scheduler_candidates.py` 12 处、`scheduler_state_failure.py` 9 处、`chain_repository_state.py` 4 处）
加一个按位置在册的 `scheduler_backfill_predecessor.py`——后者对该键**零命中**，只经 `candidate_factory`
造候选，本身不是 writer，它的头插位置由下表 W4 的放置钉子覆盖。其余 11 个文件逐个的实测排除理由见
`.workplans/pr-2406/review/round-4/writer-scope.md`。

**R4 此处写的是「三文件 grep」，R5 的第一版写的是「12 个文件」，两个都不对**：前者把 scope 缩得比
实际写入面小（漏掉整个 `chain_repository_state.py`），后者的文件数来自一条比自己声明的方法**更窄**的
grep（漏了 `restart_stage=` 那一支，因而漏了 `scheduler.py`、`chain_forecast_control.py`、
`chain_forced_resubmit.py` 三个命中，同时把并非该 grep 命中项的 `scheduler_state_decision.py` 列进了
排除表）。规格那句「未声明的 scope 不得当作空 scope」管的就是这个。

每行的结案口径有三种（规格 `specs/production-scheduler-orchestration/spec.md` 的同一条 SHALL）：
**(i)** 有测试以**带确认物的候选**可测量地走到它（给出测试名与 premise 断言）；
**(ii)** 结构性证明该点跑在任何确认物匹配之前，或其输出根本进不了录取候选清单；
**(iii)** 证明该点的输出只能**以候选自身顶层 restart 键的值**——也就是 sink 读的那个键——
抵达带确认物的候选，路径上每个 helper 都点名，且它们都不改名该键、不把它嵌套、不删掉
`operator_reentry_confirmation` 块；sink 的正向检查对该键的值域是完备的。
能剥掉确认物块的路径**不**由 (iii) 结案——那样 sink 就不再认得这是确认过的候选。
"当前 fixture 覆盖不到"三种都不算。

**搜索范围（实测，不是断言）**：`services/orchestrator/` 全目录 grep `"restart_stage"] =` /
`"restart_stage":` / `restart_stage=`，命中 **15** 个文件；判据是「是否在最终会成为
`candidate.state_evidence` 的那个 dict 上写**顶层**键 `restart_stage`」，因为 sink 读的正是它
（`_candidate_effective_restart_stage` 取 `state_evidence.get("restart_stage")`）。
逐文件的归类与排除理由见 `.workplans/pr-2406/review/round-4/writer-scope.md`。
round 4 的这次实测**推翻了上一版审计声明的三文件范围**：`chain_repository_state.py` 是第四个
写入者文件（W17/W18），上一版把它漏在搜索范围外。守卫本身不受影响——sink 读最终证据、
不问来源——但"已枚举全部 writer"这个 provenance 声明当时是假的。这是连续第四轮源枚举漏掉源、
也是连续第四轮 sink 不受影响，正是把执行点从源移到 sink 的理由。

| # | 赋值坐标（final head 实读） | 产出的阶段 | 结案 |
|---|---|---|---|
| W1 | `scheduler_candidates.py:921-922` | `forecast` | **(ii)** 只构造 `blocked` 条目后 `continue`（`:925-932`），永不产生已录取候选；且值本身就是 `forecast` |
| W2 | `scheduler_candidates.py:2020-2021`（+ `:2038` 的 `missing_forcing_repair.restart_stage`）`_apply_explicit_missing_forcing_repair_policy` | `forcing` | **(ii)** 带确认物的候选**到不了这个写入点**：r2-01 分支 `:1922-1965` 在它之前返回，所以 `:2020` 永不执行。`test_an_explicit_missing_forcing_repair_refuses_a_confirmed_candidate` 是该结构性论证的**判别证据**（断言 `missing_forcing_repair.status == "rejected"`、`reason == "operator_reentry_confirmation_present"`、`restart_stage == "forecast"`——即证明 writer **没有**跑；删掉 r2-01 分支则 `:2020` 触发、`restart_stage` 变 `"forcing"`、该测试转红），但它不是 (i)：(i) 的定义是「可测量地**带着确认物到达**该站点」，而这里证明的恰是到不了。**R5 第一版把本行标成 (i) 是错的**，与 round 4 的 A-2 同类——证据本身有判别力，错在标签 |
| W3 | `scheduler_candidates.py:2188-2189` `_source_raw_manifest_restart_evidence`，经 `:938-951` 落到候选 | `convert` | **(i)** `test_sink_refuses_the_raw_manifest_convert_rewrite_on_the_budget_arm`（premise：`canonical_readiness.status == "canonical_incomplete"`、`candidate_row_count == 0`、`restart_reason == "raw_manifest_ready_without_canonical"`、`raw_manifest_reuse.status == "ready"`，真 `FileCanonicalReadinessProvider`）与 `..._on_the_breaker_arm`（stub provider；断言其中 3 条 premise，不含 `raw_manifest_reuse.status`——**R5 第一版写"同 premise"是夸大**） |
| W4 | §8.6 头插（`scheduler_backfill_predecessor.py:699`，无自有赋值——全文件 grep 该键**零命中**） | 继承 | **(i)** `test_sink_refuses_a_confirmed_predecessor_prepended_after_the_main_loop`（premise：emitter 真的跑过且真的头插了一个候选）。这是**放置**钉子：写在 `candidates.append` 处的 guard 会漏掉它。此行按**位置**在册、不按取值：`:534-538` 用共享 `candidate_factory` 建候选（不传 `state_evidence`），`:620-632` 只加 `predecessor_backfill_marker` / `predecessor_backfill_gate`；它在表里是因为 `:698-699` `candidates[:0] = admitted` 在主循环之后头插同一个 list 对象，sink 必须跑在它之后 |
| W5 | `scheduler_candidates.py:2684-2685` `_journal_predecessor_identity_retry_evidence` | `forecast` | **(i)** `test_every_confirmed_retry_candidate_a_pass_emits_restarts_at_forecast[breaker]` |
| W6 | `scheduler_candidates.py:2720-2721` `_journal_predecessor_identity_blocked_evidence` | `forecast` | **(ii)** `blocked` 决策，不进候选清单；值为 `forecast` |
| W7 | `scheduler_candidates.py:2753-2754` `_terminal_run_manifest_retry_evidence` | `forecast` | **(ii)** 与两个确认物匹配点（`:581` 预算、`:622` 断路器）同属 `:536-652` 的同一条 if/elif 链，互斥；值为 `forecast` |
| W8 | `scheduler_candidates.py:2846-2847` `_strict_warm_start_terminal_blocked_evidence`（`_STRICT_WARM_START_TERMINAL_RESTART_STAGE = "forecast"`，`:78`） | `forecast` | **(ii)** `blocked` 决策；值为 `forecast` |
| W9 | `scheduler_candidates.py:2877-2878` `_strict_warm_start_terminal_retry_evidence` | `forecast` | **(i)** `test_every_confirmed_retry_candidate_a_pass_emits_restarts_at_forecast[budget]` |
| W10 | `scheduler_candidates.py:2900-2901` `_strict_warm_start_run_manifest_retry_evidence` | `forecast` | **(ii)** 同 W7 的互斥链（`:545-552`）；值为 `forecast` |
| W11 | `scheduler_candidates.py:2920-2921` `_strict_warm_start_retry_run_manifest_evidence`（`_upgrade_retry_for_strict_warm_start_manifest`，调用点 `:653`） | `forecast` | **(ii)** 带确认物的候选**结构上到不了这里**：两个确认物块写入者产出的证据都是 `restart_stage: "forecast"` + `native_shud_resubmitted: True`（断路器臂 `scheduler_candidates.py:2684-2686`，预算臂 `:2877-2880`，均实读），正好命中 `_upgrade_retry_for_strict_warm_start_manifest` 的早退分支 `native_shud_resubmitted is True and restart_stage == "forecast"`。**上一版此行记 (i) 是错的**（round 4 的 A-2）：它引的 `..._emits_restarts_at_forecast[budget]` 断言的是候选最终在 `forecast` 重启，而本行无论是否执行、结果都是 `forecast`，该测试对它的执行与否**不敏感**——检测不到的测试不构成 (i)。此更正**不外推到 W9**：W9 的 (i) 成立，"变异 `:2877` 后 `[budget]` 仍绿"是变异不敏感，与可达性是两回事 |
| W12 | `scheduler_candidates.py:2951-2952` `_strict_warm_start_successor_retry_evidence` | **`state_save_qc`** | **(ii)** 唯一两个调用点 `:564` 与 `:610` 与两个确认物匹配点 `:581`、`:622` 同属 `:536-652` 的同一条 if/elif 链，同一趟 pass 内**结构互斥**；`:1066` 的重导出不调用它 |
| W13 | `scheduler_state_failure.py:505-506` `_downstream_retry_evidence` | `failed_stage`（可能早于或晚于 `forecast`） | **(iii)** 上一版记的是"(i) 带保留"，规格不承认这种混合闭合（round 4 的 A-3），本版按 (iii) 结案：writer 的输出只能作为候选顶层 `restart_stage` 的值经 `_merge_state_evidence` 抵达带确认物的候选，键名不改、不嵌套、确认物块不被剥（依据同 W14 的被更正前提），合并后的值一律由 sink 的正向检查裁决。**支撑证据（不是闭合依据）**：`test_a_post_block_state_rederivation_cannot_hide_a_confirmed_candidate_from_the_sink` 用真 decider 产出 `parse`、真 `_candidate_with_state_evidence` 并到真·带确认物候选上，sink 拒。**pass 内几何：round 5 专门去构造，三道实测闸门都不通过，记为未构造**——(1) breaker lane 上结构互斥：造出 §8.7 quarantine 的前提是跑真实完整 chain 重跑，于是 journal 必有 succeeded 的 `state_save_qc`/`publish`，`_has_terminal_completion_stage_success` 为真，而 `chain_repository_state.py:908-922` 的 writer 正挂在它的否定分支下（旁证：单独调 `_best_completed_stage_success_evidence` 确实返回 `{"stage": "parse", "restart_stage": "state_save_qc"}`，只是永远不会被问到）；(2) 种入 succeeded `parse` 行后 decision 仍是 `retry_journal_predecessor_identity_mismatch` / `restart_stage: "forecast"`，`completed_stage_evidence` 为 null；(3) budget lane 的 `_budget_pass` 直接调 `_build_candidates`，`allow_slurm_status_sync` 用默认 `False`，走不到 post-sync。**第 (1) 条是结构性的，(2)(3) 是 fixture 形状的**——所以本行不按 (ii) 结案，(ii) 需要的是"任何确认物匹配都到不了"，而这三条只证明当前两条 lane 上到不了 |
| W14 | `scheduler_state_failure.py:558-559`、`:1723-1724`、`:1948-1949`、`:2033`、`:2251-2252` | `forecast` 或派生下游阶段 | **(iii)** `scheduler_state_failure.py` 全模块**零次**出现 `operator_reentry_confirmation`（grep 计数 0），它是确认物匹配**之前**的 decider；其输出要落到带确认物的候选上，只能作为该候选顶层 `restart_stage` 键的值，经 `_merge_state_evidence`（`scheduler_candidates.py:2264-2276`）→ `_evidence_safe` → `_redact_secret_manifest_for_evidence`（`scheduler_state_common.py:66-77`）。**上一版写的"从不删键"是假的**（round 4 的 C-1，实测）：`:72-74` 确实会 `continue` 丢键。但结论仍成立，理由换成真的——该谓词判的是**键名**，而确认物块的五个字段名来自 `scheduler_generation.py:1615` 的封闭字典推导（`request_id` / `operator` / `reason` / `pin` / `decision`），没有任何 writer 能往里塞一个会被丢掉的名字；键名不改、不嵌套，块也不被剥掉，合并后的值一律由 sink 的正向检查裁决 |
| W15 | `scheduler_state_failure.py:1786-1787`（+ `fresh_ingestion {"required": True, "mode": "full_chain"}`，`:1788`）、`:1859-1860` | `None` / `download` | **(iii)** 同 W14（含其被更正的前提）；并且这正是 Task 3 第 4 问的形状——`full_chain` 会让 manifest **不带** `restart_stage`（实测：`test_the_stage_the_sink_guards_is_the_stage_the_run_manifest_carries`），所以 guard 的有效阶段对它返回 `None` 并拒，`restart_stage: None` 同理（`test_the_sink_is_a_positive_forecast_check_over_the_stage_the_manifest_obeys[absent|null|fresh_full_chain_strips_the_manifest_stage]`） |

| W16 | `scheduler_state_failure.py:932-933` `_artifact_blocker_evidence`（def `:909`），值取自 `planned_retry` | 继承 `planned_retry` 的阶段 | **(ii)** 与 W6/W8 同因：该证据经 `_missing_upstream_forecast_artifact_evidence`（def `scheduler_state_failure.py:589`）流出，**五个**消费点一律包成 `blocked` 决策——`scheduler_state_decision.py:251`（`:258-263` 包装）、`:291`（`:298-303`）、`:312`（`:319-324`）、`:369`（经闭包 `_missing_forcing_block` def `:365`，再由 `_missing_upstream_artifact_decision` def `:419-424` 包装，调用点 `:382`/`:389`/`:409`），以及 `scheduler_candidates.py:2475`（`:2482-2486` 包装）。blocked 决策永不进录取候选清单。**上一版整行漏了**（round 4 的 A-1），**R5 第一版只数了三个消费点且把 `:2428` 写成调用点**（该坐标在 `scheduler_state_failure.py` 越界、在 `scheduler_candidates.py` 落在 `_warm_state_record_matches` 里）——结论不变，枚举与坐标已按实测改正。注意本行**不能**用"跑在确认物匹配之前"结案：`scheduler_candidates.py:2475` 那个调用点上确认物块确实流经，本 commit 自己的一个测试就断言了那个形状 |
| W17 | `chain_repository_state.py:896-897`（repaired-stage 分支）写 `state["restart_stage"]` / `["restart_from_stage"]` 到 `raw_candidate_state` | 取自 `repaired_stage_evidence` | **(iii)** 与 W13 同一条路径：`_bounded_candidate_state`（`scheduler_state_rows.py:89-95`，只重写 `pipeline_events`，顶层键原样保留）→ `scheduler_state_failure.py:1703-1711` 读出、`:1723-1724` 写进返回证据 → `_candidate_with_state_evidence` 装进候选。全程键名不改、不嵌套，不碰确认物块。**上一版整个文件漏在搜索范围外** |
| W18 | `chain_repository_state.py:921-922`（completed-stage 分支）写同两个顶层键，值取自 `_best_completed_stage_success_evidence` | 下游完成阶段（可能晚于 `forecast`） | **(iii)** 同 W17。要抵达**带确认物**的候选只有一条路：`scheduler_candidates.py:1066` 的 post-sync 重导出重取 `raw_candidate_state`（`:1053-1065`）并重跑 decider，而确认物块在更早的 `:975` 已并到候选上——与 W13 是同一块几何，round 5 的三道实测闸门与"为什么仍不按 (ii) 结案"见 W13 行。本行额外的结构性事实：该 writer 本身挂在 `_has_terminal_completion_stage_success(jobs)` 的否定分支下（`chain_repository_state.py:908-922`），而 quarantine 的成因要求 journal 里有终态完成阶段 |

| W19 | `chain_repository_state.py:244-245` `_manual_stage_repaired_evidence` 的 `payload["restart_stage"]` / `["restart_from_stage"]`，值取自 `_stage_after(stage)`（`:229`） | 下一个阶段 | **(iii)** 它是 **W17 所写值的生产者**：该 payload 经 `state["repaired_stage_evidence"]`（`:891`）被 W17 在 `:896-897` 提到顶层，之后与 W17 同路。单独成行是因为审计方法是「每个赋值一行」，而它确实是一处赋值——**R5 第一版只上了 W17/W18 两行，把两个生产者漏了** |
| W20 | `chain_repository_state.py:271-272` `_completed_stage_success_evidence` 返回 dict 里的同两个键，值取自 `_stage_after(stage)`（`:259`） | 下一个阶段 | **(iii)** 它是 **W18 所写值的生产者**，且有**第二条**通向顶层键的路径：该 dict 被整体嵌套存进 `state["completed_stage_evidence"]`（`:920`），而 `scheduler_state_failure.py:1707-1708` 在顶层 `restart_stage`/`restart_from_stage` 都为假值时会从这个子 dict 里回退读出它。两条路径都终止于候选顶层键，因此同受 sink 的正向检查裁决；键名不改、不嵌套、确认物块不被剥 |

正向检查对**值域**的完备性单独钉住（不依赖任一 writer 是否可达）：
`test_the_sink_is_a_positive_forecast_check_over_the_stage_the_manifest_obeys` 枚举
缺失 / `None` / 空串 / `convert` / `forcing` / `state_save_qc` / `parse` / `full_chain` /
`forecast` / `forecast` + 不一致的 `restart_from_stage` 十种；
`test_the_sink_leaves_an_unconfirmed_off_forecast_candidate_alone` 钉住爆炸半径；
`test_sink_refusal_decision_is_absent_from_both_forced_resubmit_whitelists` 钉住
`blocked_operator_reentry_restart_stage_refused` 不在 `chain_forced_resubmit.py:14-28` 与
`chain_runtime_utils.py:200-211` 两处白名单内（两者都是 `retry_*` 的封闭集合）。

**上一版此处写"无未结行"，那是假的**——W16 整行漏了、W17/W18 整个文件漏在搜索范围外、
W11 的 (i) 用了检测不到它的测试、W13 的"hybrid"闭合规格根本不承认（round 4 的 A-1/A-2/A-3）。
这些都不是守卫的缺陷，是**审计本身**的缺陷；守卫读的是最终 `state_evidence`，与谁写的无关。
本版每行的闭合种类逐行重判，搜索范围实测并写明排除理由，未测到的地方写"未测"而不是推断。

## 不做（non-goals）

- 不改 breaker 判定、阈值、计数口径；不改预算判定与 `NHMS_SCHEDULER_RETRY_LIMIT` 默认值，#1767 的回收不在本批。
- 不改 completed-skip 与 `manual_retry_requested` 的评估顺序（#1555 已否决，#1201/#1205 风险）。
- 不做失败签名感知的预算重置（用户裁决选确认物）。
- 不做 #1543 的 gate 侧合轴，也不实现 #1118 breaker。
- 不改 DB plane 的 retry 路径；DB plane 的 accessor 缺失即无确认物。
- 不接 systemd timer / 告警投递；不改 409 payload 文本。

## 偏离记录（预置）

1. 本分支带着上一批的 post-merge 提交 `8713b73e9`（归档 + loop-log + ADR 0003 deferral + `.review-gate-issues.json`）。
2. #1186 Verification 文本中 node-22 的 `git pull` + 裸 `uv run`，改为隔离 worktree + `.venv/bin/python -m`；该 D2 流程随 #1186 移入 PR-B。
3. Phase 0 勘察 explorer 执行过一次只读的裸 `python3` heredoc，违反「Python 只经 uv」纪律；无写入，未重复。
4. fixture level 由 precedent 的 `expanded` 升为 `high`（见 Risk triage）。
5. fixture review 第一轮判 revise（F1–F10），全部按本文修正；第二轮 approve（附 3 条 P3，已并入）。
6. PR #2398 在第 5 轮交叉审查触到轮次上限（`gates.md` round ceiling），用户裁决拆分：本 change 为 PR-A（#1543/#1555/#1768/#1820），#1186 的列举面移入 `node22-operator-action-listing`（PR-B）。原 change 目录 `node22-operator-action-surface-reentry` 更名为本目录，fixture level 仍为 `high`，轮次计数器按 `gates.md` 对子 PR 重置。

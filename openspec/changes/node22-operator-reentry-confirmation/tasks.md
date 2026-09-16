# Tasks — node-22 operator-action surface + targeted re-entry

执行顺序：B → A → C → D，由单个 implementer 串行完成。每个切片先红后绿：红测试须在源码改动前确认为红，并在 PR 中贴出失败断言。

## B. #1543 — §8.6 发射可行性标（design D3）

- [x] B.1 红测试（`tests/test_scheduler_backfill_predecessor.py`，沿用既有 real-gate 用例风格）。三条非瞬时臂各一条，断言对应 successor 的 blocked `state_evidence.predecessor_emission_blocked is True`：
      - `predecessor_raw_manifest_not_ready`
      - `predecessor_model_not_available`
      - cap 截断：把 `MAX_PREDECESSOR_EMISSIONS` monkeypatch 为小值，断言被截断的 successor 被打标
      - `predecessor_candidate_construction_failed`、`predecessor_gate_failed`（F8）
- [x] B.2 正交钉：同一几何下 `operator_action_required is False` 且 `predecessor_emission_blocked is True`。
- [x] B.3 瞬时臂不打标：`predecessor_already_present`、`predecessor_backfill_active_pipeline`、`predecessor_raw_manifest_env_unwired` 各断言值为 `False`。emitted 的 successor 同样为 `False`；无记录的 successor 不带该键。
- [x] B.4 `LINEAGE_SCOPED_OUT_REASON` 两种几何：successor cycle_time 早于 `cutover_valid_time` 时为 `False`；不早于 cutover 时为 `True`（F9）。
- [x] B.5 实现：
      - `scheduler_backfill_predecessor.py` 迭代改为 `enumerate(pending)`，截断记录追加 `successor_candidate_ids`；
      - 瞬时 allowlist 常量采用反向定义（D3）；
      - `attach_emission_summary_to_blocked` 按该列表归组；
      - `_attach_summary_to_single_blocked` 写布尔。
- [x] B.6 截断记录断言：`successor_candidate_ids` 含被截断 successor，`status/reason/total_attempted/cap` 不变。
- [x] B.7 bounded 保留钉：`_bounded_candidate_summary` 后 `predecessor_emission_blocked` 的 `True` 与 `False` 都保留。

## A'. 运维面留在 PR-A 的部分（原 A 切片，#1186 列举面已移出）

- [x] A'.1 bounded 白名单追加 `retry_attempt / retry_limit / retry_occurrences / manual_retry_required` pulls，与 B 的键同文件编辑；新增逐键保留钉（含 `False`/`0`）。
- [x] A'.2 新建 `docs/runbooks/node22-control-plane-manual-recovery.md`；`failed-basin-retry.md` 为四类决策各写一段处置。「怎么找到目标」写过渡口径（直接读最新 pass evidence，按 decision 字面量筛 `blocked_candidates` + not-selected `source_cycles`），PR-B 落地 `list-operator-actions` 时替换。
- [x] A'.3 slug 存在性测试：从 `apps/api/routes/pipeline.py` 的实际 payload 构造路径读 slug（必要时抽模块常量，且不改值），断言 `docs/runbooks/<slug>.md` 存在。
- [—] A'.4 拆分移出：原 A.3/A.4（`tests/test_operator_action_listing.py`、`services/orchestrator/operator_action_listing.py` 与 `list-operator-actions` 子命令）与 A.7（node-22 现场收据）随 #1186 移入 change `node22-operator-action-listing`（PR-B）。

## C. #1820 — operator 命令逐行 / 逐 cycle 隔离（design D5）

- [x] C.1 红测试（放在 `tests/test_file_orchestration_journal.py`；CLI receipt 断言放在既有 recovery 用例所在的 `tests/test_production_scheduler.py`，先 grep 定位）：
      - flat 目录放一行 `accepted_submit_contract_version` 当前但内容畸形的记录，与若干正常 wedged 行同目录；
      - 断言 list receipt 仍列出正常行；
      - `skipped_count >= 1`；
      - `skipped[]` 含 path 与 `reason == "file_journal_evidence_invariant_invalid"`。
- [x] C.2 表驱动：`ROW_CONTENT_SKIP_REASONS` 中每个 reason 构造一条真实触发该 reason 的行（至少覆盖 malformed JSON、`job_mismatch`（文件名与 job_id 不符）、`schema_mismatch`、accepted-submit `evidence_required`、`json_depth_exceeded`），断言被跳过。无法用真实行触发的 reason 在 PR 中列出原因。
- [x] C.3 逐 cycle：确认循环中某 cycle 的记录畸形，但它不是首轮扫描出的候选本身。（原断言「记一条 cycle 级 skip」已由 R1.4 改为逐行 skip。）断言其他 cycle 的候选照常输出；同一 repository 实例随后 `get_pipeline_job` 该 cycle 仍 raise（记忆化不缓存半成品）。
- [x] C.4 allowlist 外不吞：同时构造畸形行与 `record_limit` / `file_limit` 预算超限，断言仍 raise 预算原因；`file_journal_unreadable` 也 re-raise；`tests/test_file_journal_full_tree_budget_contract.py` 原样全绿。`--job-id` 模式既有测试原样全绿（F5，不改该模式）。
- [x] C.5 热路径不变钉：同一畸形行下，`candidate_state` / `get_pipeline_job` 所在 cycle 的读取行为与改动前一致（仍 raise）。
- [x] C.6 实现：
      - `_iter_direct_pipeline_job_records(skip_collector=None)`；
      - `query_released_identity_blocked_jobs_with_skips`；
      - 原函数委托；
      - `operator_released_reservation_recovery` 的 **list 模式**使用新函数，并把 skips 写进 receipt；`--job-id` 模式不改。

## D. #1555 + #1768 — operator 重入确认物（design D4）

- [x] D.1 写侧红测试（新文件 `tests/test_operator_reentry_confirmation.py`）：
      - dry-run 不写任何 journal 字节；
      - `--attest` 写入 `event_type == "operator_reentry_confirmation"`；
      - 无 completed identity 时 exit 2；
      - breaker 决策下 pin 不等于 live occurrences 时 exit 2；
      - breaker 决策下 `--recorded-init-state-id` 与 live token 不符时 exit 2（F2）；
      - 预算决策下 pin < 1 时 exit 2；
      - 必填参数空白时 exit 2；
      - 非法 journal root 输出 typed refusal 且 exit 2。
- [x] D.2 marker 隔离钉：写入确认物后，该候选 `candidate_state` 的 `_manual_retry_requested` 仍为 `False`，调度决策仍为 breaker `blocked`（pin 不匹配时）。
- [x] D.3 accessor 测试：
      - 按 `model_id + decision` 精确过滤；
      - 别的模型或别的 decision 的确认物不返回；
      - 读失败返回 `[]`；
      - forecast_cycle 行状态为 `complete` / `published` 时仍能读到（F3，按 `model_id=None` 读）；
      - 事件数超过 `DEFAULT_CANDIDATE_STATE_EVENT_LIMIT` 时仍能读到（不经 bounded candidate_state）。
- [x] D.4 #1555 闭环（`tests/test_production_scheduler.py`，E2E 形状，走 scheduler pass）：
      **必须在 backfill 模式下跑**（`backfill_enabled=True`），几何为单 cycle 且全部模型 breaker-engaged（F1）。
      1. breaker 已触发（occurrences 等于阈值）：无确认物时 cycle 以 not-selected 呈现，`submitted_count == 0`；
      2. 通过 `confirm-operator-reentry --attest`（CLI 入口）写入 token + `pin == occurrences` 的确认物后，下一 pass 该 cycle 保留执行槽位，并产生**真实** replacement forecast submission，evidence 带 `operator_reentry_confirmation`；
      3. 中间 pass（rerun 已提交、未完成）：`submitted_count == 0`（F10）；
      4. rerun 以同一 stale token completed，且带 quarantine provenance（经真实 reservation 路径断言 master 行 `journal_predecessor_quarantine_rerun_model_ids` 含该模型，不用替身）；
      5. 下一 pass occurrences 加 1，回到 breaker 释放 / `blocked`，`submitted_count == 0`；
      6. 变体：rerun 记录另一个 stale token Y 时，同样回到 blocked，`submitted_count == 0`（F2）。
      另加一条 mixed：同一 cycle 两个 breaker 模型、只确认其中一个，未确认的仍 `blocked`。
- [x] D.5 #1555 × #1844：非 strict 车道，确认物匹配但本模型无 forcing 时，候选落入 #1843 具名 `blocked`，`submitted_count == 0`。
- [x] D.6 #1768 闭环（E2E，strict 车道，在 backfill 模式下跑）：
      1. 用 CONFLICT 几何（记录 init_state_id 与选中 state 不一致，使 cycle 保持 gap 占槽）构造，`attempt >= retry_limit` 时 `blocked`；
      2. `pin == attempt` 确认物使下一 pass 产生真实 retry submission，且不改 `NHMS_SCHEDULER_RETRY_LIMIT`；（Round 3 修订：pin 为预算重入计数，见 R3）
      3. 中间 pass：`submitted_count == 0`；
      4. rerun 行使 attempt 加 1，下一 pass 回到 `blocked`；（Round 3 修订：pin 为预算重入计数，见 R3）
      5. `pin != attempt`（陈旧）的确认物不放行。（Round 3 修订：pin 为预算重入计数，见 R3）
- [x] D.7 DB plane / 无 accessor 替身：行为逐字节不变，breaker 与预算都保持 `blocked`。
- [x] D.8 实现：
      - `operator_reentry_confirmations` accessor（`_cycle_rows(model_id=None)`）；
      - `scheduler_generation.operator_reentry_confirmation_match` 共享谓词；
      - 三个消费点：discovery `_breaker_engaged_gap_identities`、breaker 铸造点、预算铸造点；
      - 两条 blocked evidence 的 `retry_policy` 追加 `operator_reentry_command` / `recovery_runbook`；
      - `operator_reentry_confirmation.py` 与 `confirm-operator-reentry` 子命令。
- [x] D.9 既有钉原样全绿：
      - forced-resubmit 白名单成员钉；
      - §8.7 read-only invariant 测试；
      - breaker 系列（`tests/test_scheduler_generation.py`、`tests/test_warm_start_chaining.py`、`tests/test_scheduler_backfill.py`）。
- [x] D.10 runbook：
      - `scheduler-dbfree-typed-reasons.md` §8.7 breaker 节的处置步骤从「带外新提交身份」改为 `confirm-operator-reentry`，写明命令、预期 evidence 形状、一次一授权与已知限制；
      - `failed-basin-retry.md` 的 strict 预算节改为同一通道，不再以抬全局旋钮为首选；
      - 已知限制写明：已确认模型若一直被 forcing 见证闸挡住，cycle 会持续占槽，确认物无撤销手段（需先回补 forcing）。

## R1 修复（PR #2398 第 1 轮审查）

- [x] R1.1 cand-01：accessor 改名 `quarantine_rerun_count`，统计 provenance 命名该模型的 cohort master，不看终态、不比 identity；谓词、写侧前置条件、receipt `live.quarantine_rerun_count` 同步；`completed_pipeline_init_state_id_occurrences` 经共享 helper 的 `require_completed=True` 保持原行为。新增 D.4 变体：确认 → 提交 → 在飞时 count == N+1 → forecast 失败 → 仅 1 个新带戳 master、之后 pass 不再提交；变异（只数 completed）变红。
- [x] R1.2 cand-02：**DEFER（#2400）**。预算写侧复算 attempt 不可行：读侧 attempt 依赖调度侧候选身份（registry/adapter 构造的 `candidate_identity` 与 `candidate_state` 参数），写侧无法复用同一推导。runbook 写明预授权风险与 dry-run 核对步骤；spec/design 回到「写侧只校验 pin >= 1」。Round 3 由 R3.3 关闭 #2400。（Round 4 撤回：写侧无法判断预算是否耗尽，#2400 残余（对 pin、错时间）保留为 runbook 义务，见 R4.3）
- [—] R1.3 cand-03（列举面可判定 pass 语义）随 #1186 移入 PR-B 的 change。
- [x] R1.4 cand-04：`skip_collector` 贯穿 `_iter_pipeline_job_records_scoped` → `_replay_pipeline_job_records_for_cycle` → `_iter_flat_direct_pipeline_job_records_for_cycle`，skip 模式绕过 `_cycle_job_records_memoized`，按 `(path, reason)` 去重，删除 cycle 级 except；同 cycle 坏行、无法解析文件名的坏行测试；C.3 改为同 cycle 坏行 + 同实例非 skip 读仍 raise（记忆化变异变红）；C.5 补 `jobs` 断言；cycle journal 日志损坏照旧 raise。
- [x] R1.5 cand-05：D.2 的 `_manual_retry_requested` 断言经变异证实无法咬住（marker 读取还要求 `trigger: "manual"`），删除并以注释指向 `test_attest_records_a_dedicated_confirmation_event` 的精确形状断言。
- [x] R1.6 runbook：确认物在 rerun 被接受提交时消费、Slurm 失败不恢复；「候选仍显示 blocked ≠ 确认物未生效」与 #2397 合并；evidence root 取自 `nhms-compute-scheduler.service` 的 `infra/env/compute.scheduler-dbfree.env`（#2399）。预算 pin 取最新 pass 的现值并附 #2400 预授权警告。（Round 3 修订：pin 为预算重入计数，见 R3；拆分修订：退出码表与 `non_evaluating_passes` 随 #1186 移入 PR-B）

## R3 修复（PR #2398 第 3 轮审查）

- [x] R3.1 r3-02 红测试：`test_budget_reentry_confirmation_is_consumed_even_when_the_rerun_mints_under_another_prefix`——预算由 full chain 自动 retry 耗尽（`job_cycle_gfs_2026052100_full_model_a_forecast_retry_1_retry_2`，attempt 2 == limit；此形状由 chain 自动 retry 铸造，`retry.py:458`，strict db-free 夹具跑不了自动 retry 循环，故手种并注释来源），确认后每个 pass 的重入经调度器自己的 handoff basin（`_execute_candidates_async` → `candidate_basin_manifest`）走真实 reservation 路径（`real_rerun`，只裁 Slurm、NFS copyback 与需要 `DATABASE_URL` 的 publish 阶段），写出 `cycle_gfs_2026052100_forecast_model_a` 前缀 master + reconciled child。在 464445d61 源码上以当时的 live 值 pin=2 跑红：确认后第一个 pass 放行一次，其后第一个 pass 仍放行（`assert ['retry_strict_warm_start_terminal_init_state_mismatch'] == []`）。修复后 pin 改从 dry-run receipt 的 `live.budget_reentry_count` 读取（runbook 口径，现值 0）。`real_rerun` helper 追加可选 `terminal_stage`。
      - 既有 `test_budget_reentry_confirmation_pinned_to_the_attempt_runs_once_then_the_budget_reengages`（本轮更名为 `..._pinned_to_the_budget_reentry_count_...`，节注释同步，因旧名描述的 attempt pin 语义已被取代）夹具修正（偏离记录）：手写的模型级 `job_fcst_..._model_a_forecast_retry_3`（running/succeeded）行是生产从不写出的形状，改为真实 reservation 路径（在飞：复制 journal + `never_terminal_stage`；完成：`real_rerun`）；相应地 stale pin 从「写入后读侧不放行」改为「写侧 `pin_mismatch` 拒绝且读侧不放行」，pin 2 改为 live 计数 0，第 4 步 `attempt == 3` 改为 `attempt >= limit` 且 live 计数 +1（跨前缀重入不推动 attempt，属单独立单的既有缺陷，不在本轮修）。
      - 既有 `test_budget_reentry_is_inert_on_a_repository_without_the_accessor` 的 pin 2 改为 0（新语义下 pin 2 会被写侧拒绝，测试意图不变）。
- [x] R3.2 预算重入 provenance：reservation writer 在 basin 的 `state_evidence` 为 `retry_strict_warm_start_terminal_init_state_mismatch` 且带 decision 为 `blocked_strict_warm_start_init_state_mismatch` 的 `operator_reentry_confirmation` 时，于 cohort master 戳 `strict_warm_start_budget_reentry_model_ids`（与 quarantine provenance 同形：capture-once 冻结字段、ordinary-upsert merge 字段、closed constructor 成员、evidence normalizer）；普通 strict retry 与 breaker 重入不戳。只读 accessor `budget_reentry_count`（journal-direct `_cycle_rows`，不看终态/job id/suffix，读失败 None）；共享谓词按 decision 取对应计数（breaker `quarantine_rerun_count`，预算 `budget_reentry_count`），缺失/None/抛错 → 不匹配；blocked 判定仍由 stage-scoped attempt 驱动，`_next_retry_attempt_for_stage` 未改。测试：accessor 表驱动（无戳 0、戳别的模型 0、failed master 1、跨前缀两个 2、master+terminal copy 1、无 journal 0、读失败 None）；reservation 戳往返 + 伪造改写被拒；Slurm 失败的预算重入不恢复确认物；accessor 缺失/None/抛错时惰性。
- [x] R3.3 写侧：预算 decision 要求 pin == live `budget_reentry_count`，否则 `pin_mismatch`（exit 2）；删除 `pin >= 1` 特例（`pin_invalid` 只剩负数）与 #2400 注释/help 文案；receipt `live.budget_reentry_count`。既有 `test_budget_confirmation_is_recorded_without_a_token`（pin 12、`live is None`）改为 pin 0 / `live == {"budget_reentry_count": 0}`，`budget_pin_below_one` 腿改为 `budget_pin_differs_from_live_reentry_count`（pin_mismatch）与 `budget_pin_negative`（pin_invalid）（偏离记录：旧期望正是被本轮 spec 取代的语义）。Round 4 撤回：写侧无法判断预算是否耗尽，#2400 残余（对 pin、错时间）保留为 runbook 义务（见 R4.3）。
- [—] R3.4 r3-01（列举面时序规则）随 #1186 移入 PR-B 的 change。原文：无待办时，若有 size-fallback pass 按 mtime 比最新的可判定 pass 更新 → exit 3；可判定 pass 比所有 fallback 新 → 0。`test_one_clean_evaluating_pass_in_the_window_decides_zero` 拆为两条（偏离记录）：fallback 最旧、`planned` 最新 → 0；新增 `test_a_size_fallback_pass_newer_than_the_newest_decidable_pass_is_undecidable`（fallback 最新 → 3），两者都用真实 `bounded_evidence_payload`。help、模块 docstring 同步。
- [x] R3.5 runbook：`node22-control-plane-manual-recovery.md` 预算 pin 改取 dry-run receipt `live.budget_reentry_count`，删除预授权/#2400 警告与「`_retry_<n>` 使 attempt +1」措辞，写明确认物在 rerun 被接受提交时消费，`pin_mismatch` 适用两类、`pin_invalid` 仅负数，`failed-basin-retry.md` 预算节同步（退出码表随 #1186 移入 PR-B）。`scheduler-dbfree-typed-reasons.md` 未涉及预算 pin 与 exit 3 条件，无需改。

## R4 修复（PR #2398 第 4 轮审查）

- [—] R4.1（列举面一般规则）随 #1186 移入 PR-B 的 change。原文：r4-02 + r4-03 listing 一般规则（偏离 1 已裁定：以封闭「透明」allowlist 取代「非评估 pass 不触发」）：`operator_action_listing.py` 新增模块常量 `TRANSPARENT_PASS_STATUSES = {lock_contended, preflight_blocked}`；按时间旧→新遍历，可判定 pass 清零标志 `hidden_after_decidable`，透明 pass 不变，其余一律置位（不可读、size-fallback——其原始 status 为 `resource_limit_blocked` 故不透明——、`lease_lost`、异常路径 `resource_limit_blocked`、未知或非字符串 status）；`non_evaluating_passes` 条目形状不变。help、模块 docstring、`EVALUATING_PASS_STATUSES` 注释与 runbook 退出码表 0/3 两行同步。测试：`test_pass_kind_orderings_decide_by_the_hidden_pass_recency_rule` 排序表 26 项（decidable(planned)、size_fallback(真实 `bounded_evidence_payload`，未裁剪时会列出 breaker release)、unreadable(截断 JSON / 0 字节)、lock_contended、preflight_blocked、lease_lost、异常路径 resource_limit_blocked（limit 无 `candidate_lists`）、未知 status、非字符串 status；期望由 spec 推导）；`test_transparent_pass_statuses_are_the_closed_hide_nothing_set` 钉成员且与 `EVALUATING_PASS_STATUSES` 不相交。在 0dca0613b 源码上 11 项红。变异：「只看最新 pass 是否 fallback」「不可读不置位」「lock_contended 重置标志」各有表项红；透明集合加入 `lease_lost` 3 项红、删除 `preflight_blocked` 4 项红（含 `[D, preflight_blocked] → 0`）。517/560 两测保留不动。
- [—] R4.2（早退 writer 的透明性核查，服务于列举面）随 #1186 移入 PR-B 的 change。原文：早退 writer 核查（`scheduler_runtime.py`，候选构造 `_build_candidates` 937）：`lock_contended`（716）构造前写、列表与 `source_cycles` 为空，`test_lock_contended_pass_evidence_on_disk_carries_no_candidate_lists_or_source_cycles` 对真实落盘 artifact 钉住；`preflight_blocked` 构造前（594/644/674 prelock，762/799/841/898）列表为空，构造后（`pass_status` 1043/1077/1089/1173/1195/1208 与 `_scheduler_pass_status_from_execution` 1253）经 1328-1343 写出完整 `source_cycles` / `blocked_candidates`，二者都不隐藏 → 透明。`lease_lost`（988，唯一写者，总在 937 之后）与异常路径 `resource_limit_blocked`（1473；由构造后 progress-guard checkpoint 938/1020/1102/1141/1227/1235 → `scheduler_runtime.py:58`，或构造中 `scheduler_candidates.py:272`、`scheduler_backfill_predecessor.py:671` 抛出）清空列表 → 不透明（可能隐藏）；其 `limit` 不带 `candidate_lists`，不会被误判为 size fallback。偏离 1（此前按停止条件上报的分类问题）已由协调者裁定为透明 allowlist，见 R4.1。
- [x] R4.3 r4-01 #2400 descope：R1.2 / R3.3 撤回「关闭 #2400」；`node22-control-plane-manual-recovery.md` 预算 pin 段与 `failed-basin-retry.md` 第 3 步恢复新形式警告（pin 取 dry-run `live.budget_reentry_count`；只确认最新 pass evidence 当前列为 `blocked_strict_warm_start_init_state_mismatch` 的目标并逐字核对 source/cycle/model；rerun 在飞不确认；耗尽前写入的确认物保持有效直到被消费；写错停止上报、不再写一条覆盖）；`operator_reentry_confirmation.py` 只改 docstring 与 help 文案，逻辑未动。R3.5 删除 #2400 警告的部分由本项取代。
- [x] R4.4 预清理：D.6 第 2/4/5 步与 R1.6 加 Round 3 修订标记（原文保留）；预算重入测试第 4 步 `attempt >= _BUDGET_RETRY_LIMIT` 改回 `== _BUDGET_RETRY_LIMIT`（实测等于 limit 2），注释引用 #2404。

## S 拆分（PR #2398 触顶，用户裁决拆 PR）

- [x] S.1 `services/orchestrator/operator_action_listing.py`、`tests/test_operator_action_listing.py`、`cli.py` 的 `list-operator-actions` 挂载从本分支移除，随 #1186 进 PR-B。
- [x] S.2 runbook 的列举面段落与退出码表改为过渡口径（直接读最新 pass evidence 按 decision 字面量筛），并注明 PR-B 落地时替换；预算 pin、#2400 警告、消费时机、`pin_mismatch`/`pin_invalid` 语义不变。
- [x] S.3 openspec change 更名为 `node22-operator-reentry-confirmation`，列举面 requirement 移出，保留 bounded `retry_policy` 保留钉与 slug 存在性两条。

## PR-A R1 修复（PR #2406 第 1 轮审查）

- [x] A1.1 c-03（P1，运行时）：两个 provenance 投影改以 basin evidence 里**幸存的** `operator_reentry_confirmation` 块为键，而不是 `decision` 字面量。预算投影（`canonical_budget_reentry_model_ids`）只看块（块里的 decision 为 `blocked_strict_warm_start_init_state_mismatch` 即戳），不再要求 `retry_strict_warm_start_terminal_init_state_mismatch` 字面量；quarantine 投影（`canonical_quarantine_rerun_model_ids`）保留字面量触发（普通未确认 rerun 必须继续计数）并 OR 上「块的 decision 为 `blocked_journal_predecessor_identity_quarantine`」。新增平行常量 `JOURNAL_PREDECESSOR_QUARANTINE_BLOCKED_DECISION`。缺陷：`_apply_explicit_missing_forcing_repair_policy` 把已确认的 missing-forcing blocker 改判为 `retry_repair_missing_forcing`（该字面量在 forced-resubmit 白名单里，真的提交），旧投影按字面量匹配不上 → 不戳 provenance → `budget_reentry_count` 不动 → 同一张确认物下一 pass 再放行一次。
- [x] A1.2 c-03 测试：单元 `tests/test_warm_start_chaining.py::test_reentry_provenance_stamps_key_on_the_confirmation_block_not_the_decision_literal`（repair 形状 / 兄弟类 run-manifest 改写形状 / 普通未确认 quarantine 不回归 / 两者皆无不戳 / 同模型不重复戳）；E2E `tests/test_production_scheduler.py::test_an_explicit_missing_forcing_repair_consumes_the_budget_confirmation_exactly_once`（`_seed_budget_journal(..., repair_missing_forcing=True)`：无 forcing 见证 + `require_direct_grid` + direct-grid profile + ready raw manifest；断言修复重试提交、live `budget_reentry_count` 到 `live_pin + 1`、之后 pass 回到 blocked 且不再提交）。HEAD 上两条均红。
- [x] A1.3 c-03 Phase 6.2 审计：枚举确认匹配之后所有重写 retry `decision` 的点，逐一核对块的键路径幸存（表见 design.md D4「Phase 6.2 键路审计」）。发现兄弟泄漏类 `retry_strict_warm_start_retry_run_manifest_mismatch`（同在白名单），由本 option-A 修复顺带关闭。
- [x] A1.4 c-03 runbook：`node22-control-plane-manual-recovery.md` 「已知限制」的 forcing 见证闸条目改写（同 cycle 开着 `--repair-missing-forcing` 时会提交并恰好消费一次确认物）；`current-production-ops.md` strict 车道排空通道表下补交互说明与交叉引用。
- [x] A1.5 c-02（P2，运行时）：`attach_emission_summary_to_blocked` 归组时做 per-successor 投影——挂到某 successor 的截断记录只带标量 `successor_candidate_id`，完整 `successor_candidate_ids` 只留顶层；`status/reason/total_attempted/cap` 与 pass totals 语义不变。规模测试 `test_emission_cap_truncation_evidence_stays_linear_in_the_cut_off_successors`（600 pending，344 被截断）：HEAD 上每 successor 11,922 B、合计 4,101,168 B（`MAX_EVIDENCE_BYTES` 的 82%），修复后 257 B / 88,408 B。同时补钉未截断单 successor 摘要形状不变。
- [x] A1.6 c-01（P2，文档）：`node22-control-plane-manual-recovery.md` §1 过渡口径补回被删模块的两条规则——(a) recency 方向改回**向前等**下一个未超限的正常 pass，并正面写明「更旧的 pass 只能用来找活儿，永远不能用来断定没有待办」；(b) 可判定性改为**闭合白名单**（从 `53c39b99c^:services/orchestrator/operator_action_listing.py` 的 `EVALUATING_PASS_STATUSES` 原样抄回 24 个 status），未列出者一律不可判定 → 升级上报。`limit.candidate_lists == "dropped"` 的 caveat 扩到窗口内**任何**被检查的 pass。（c）与 §2 矛盾一说已被 verifier 否证，未改。
- [x] A1.7 c-05（P2，测试+文档）：`tests/test_production_scheduler.py` 的 union row 重新标注为 SYNTHETIC（删除「bounded 契约把四个数折成一个形状所以预算行也带 occurrences」这一被 `:49170` 精确 dict 钉否证的理由）；新增 `test_bounded_candidate_summary_retains_each_arms_own_retry_policy_keys` 逐臂钉真实 producer 字面量（预算臂断言 `retry_occurrences` **缺席**、断路器臂断言 `retry_attempt`/`retry_limit` 缺席）。runbook §1 bounded 摘要段按臂限定，写明 jq 渲染出的 `null` 是「该臂没写过」而非「数字丢了」。

## PR-A R2 修复（PR #2406 第 2 轮审查）

- [x] A2.1 r2-01（P1，运行时，实测）：`_apply_explicit_missing_forcing_repair_policy` 拒绝任何 `decision.evidence` 带 `operator_reentry_confirmation` 块的候选（具名 reason，走既有 reject 构造），候选留在 forcing 见证 blocked 上、不提交、确认物保持待用。理由：被改判的 repair retry 从 `forcing` 重启，而 provenance 只在 forecast cohort reservation 处写，该消费者结构上不可能推动 pin；且 stamp 点不可搬（`accepted_submit_row_kind` 对非 forecast cohort 返回 `None`）。未确认候选的既有 repair 行为不变。
- [x] A2.2 r2-01 测试：把 A1.2 的 E2E 倒转为 `test_an_explicit_missing_forcing_repair_refuses_a_confirmed_candidate`（断言具名拒绝 reason、无提交、计数仍为 N、确认物仍匹配；HEAD 上红——HEAD 会提交）；新增 happy-path 钉 `test_backfilling_the_forcing_lets_the_confirmed_reentry_consume_the_signature_once`（确认 → repair 被拒 → 回补 forcing → 无 flag 的 pass → 确认 retry 从 forecast 提交 → 戳 → N+1 → 下一 pass blocked 且 `confirm-operator-reentry` 返回 `pin_mismatch`）。单元层 `model_budget_repair` 投影用例保留，改标注为「B 使该形状不可达后的纵深防御」。
- [x] A2.3 r2-01 Phase 6.2 事件路径审计：枚举确认匹配 → Slurm 提交的每条路径的 `restart_stage`，回答 `scheduler_candidates.py:762`/`:1066` 的 `candidate_state_decider` 再推导是否丢块，并 `grep` 所有在 retry 决策上把 `restart_stage` 设成非 `forecast` 的点；落到 `.workplans/pr-2406/review/round-2/invariant-audit.md`。留仓结构测试：任何 `action == "retry"` 且带确认块的候选必须 `restart_stage == "forecast"`（预算 / 断路器 / repair-flag-on 三族 fixture）。
- [x] A2.4 r2-01 副作用核查：探针 2 显示失败的 forcing 在新 run-id 前缀下把 stage 域 `attempt` 从 2/2 打回 0/2（#2404 机制）。B 之后该前缀不再产生；须解释 A1.2 成功路径为何当时仍 fail-stop，并核对 `tests/test_production_scheduler.py:57447`（#2404 的钉）仍成立。若确认重入自身的前缀也能让预算失效，立即上报，不留到 round 3。
- [x] A2.5 r2-03/04/05/06（P2，规格+文档）：spec 的四处「on acceptance」断言按 B 改写为一段连贯口径（已由 orchestrator 完成）；两份 runbook 同步为同一口径——confirmed 与 repair 互斥、先回补 forcing 再重入（`node22-control-plane-manual-recovery.md:219-232`、`current-production-ops.md:376-383`），并解决 `:374` 行与新段落的自相矛盾。
- [x] A2.6 r2-07/08（P3，文档）：四处 "serialized once" 措辞（`scheduler_backfill_predecessor.py:734-735`、design D3、`docs/runbooks/scheduler-dbfree-typed-reasons.md:163`、`tests/test_scheduler_backfill_predecessor.py:1924`）改为「局部记录、从不序列化，截断集合可由 blocked 条目逐字节扫回」；**不**持久化 emission 列表（YAGNI）。`canonical_budget_reentry_model_ids` docstring（`accepted_submit_identity.py:1258-1279`）的「confirmed retry 从 forecast 重启」在 B 之后成立，补上「repair 改判已被拒绝」的理由。design D4 坐标与 tasks A1.3 指针已由 orchestrator 修。

## Evidence Floor

- EF-1 本地：`uv run pytest -q tests/test_scheduler_backfill_predecessor.py tests/test_scheduler_generation.py tests/test_warm_start_chaining.py tests/test_scheduler_backfill.py` 全绿。
- EF-2 本地：`uv run pytest -q tests/test_production_scheduler.py tests/test_retry.py tests/test_retry_cancel_consistency.py tests/test_monitoring_api.py` 全绿。
- EF-3 本地：`uv run pytest -q tests/test_file_orchestration_journal.py tests/test_file_journal_full_tree_budget_contract.py tests/test_operator_reentry_confirmation.py` 以及 C 切片所在测试文件全绿。
- EF-4 `uv run ruff check .` 通过；`openspec validate node22-operator-reentry-confirmation --strict --no-interactive` 通过。
- EF-5 反复活 grep：`grep -n "blocked_journal_predecessor_identity_quarantine\|blocked_strict_warm_start_init_state_mismatch" services/orchestrator/chain_forecast_orchestrator_cycle.py services/orchestrator/chain_runtime_utils.py` 只命中注释。
- EF-6 红证据与变异：每个切片贴出源码改动前的失败断言。单独回退以下三处，对应测试变红：
  - D 的 pin 严格相等（改成 `>=`）→ D.4 第 5 步或 D.6 第 4 步变红；
  - C 的 allowlist（改成吞全部）→ C.4 变红；
  - B 的瞬时 allowlist（删掉一项 → B.3 变红；把 `predecessor_gate_failed` 加进 allowlist → B.1 变红）。
- EF-7 node-27 后端 oracle：隔离 worktree `/home/nwm/tmp/wt-b7a`，复用共享 venv，不设 integration 环境变量，`umask 022` + `TMPDIR=/home/nwm/tmp`。跑 `tests/test_production_scheduler.py tests/test_scheduler_backfill_predecessor.py tests/test_file_orchestration_journal.py tests/test_operator_reentry_confirmation.py`。
- [—] EF-8 node-22 现场收据随 #1186 移入 PR-B（它验证的就是列举面本身）。
- EF-9 runbook 与新 slug 文件随 PR 提交；merge 后在 #1767 评论：定向解除通道已存在，`RETRY_LIMIT=12` 的回收可以执行。只评论，不执行。

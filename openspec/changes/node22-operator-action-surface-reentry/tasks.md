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

## A. #1186 — 列举面 + runbook（design D1/D2）

- [x] A.1 （fixture review 已核实字面与嵌套位置，见 design D1）实现时如发现不符，记入偏离记录。
- [x] A.2 bounded 白名单追加 `retry_attempt / retry_limit / retry_occurrences / manual_retry_required` pulls，与 B 的键同文件编辑；新增逐键保留钉（含 `False`/`0`）。
- [x] A.3 红测试（新文件 `tests/test_operator_action_listing.py`），构造真实 `scheduler_*.json` evidence 文件：
      - 四类 decision 各一条「存在 → 列出且 exit 1」；
      - 一条「全部无关 blocked → 空列表 exit 0」；
      - 一条 summarized pass：用真实 `_bounded_candidate_summary` 产出的条目，仍被识别，`attempt` 等字段来自新 bounded 键；
      - 旧格式 summary 缺少新键时字段为 `null`；
      - `.pre_execution.json` 被排除；
      - `--passes` 按 mtime 取最新 N 个；
      - 同一候选跨 pass 去重，并记录 `first_seen_pass/last_seen_pass`；
      - 损坏 JSON 进入 `unreadable_passes` 且不中止；
      - root 缺失时 exit 2；
      - breaker 释放的 not-selected `source_cycles` 条目按模型列出（F1）；
      - 无 action 且某 pass 的 `limit.candidate_lists == "dropped"` 时 exit 3，并列出该 pass（F7）。
- [x] A.4 实现 `services/orchestrator/operator_action_listing.py`，argparse 子命令 `list-operator-actions` 挂到 `cli.py`，通过 `main([...])` 端到端测试一次。
- [x] A.5 新建 `docs/runbooks/node22-control-plane-manual-recovery.md`；`failed-basin-retry.md` 为四类决策各写一段处置。
- [x] A.6 slug 存在性测试：从 `apps/api/routes/pipeline.py` 的实际 payload 构造路径读 slug（必要时抽模块常量，且不改值），断言 `docs/runbooks/<slug>.md` 存在。
- [ ] A.7 node-22 现场收据（D2）：隔离 worktree，只读执行，贴出输出后移除 worktree。

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
      2. `pin == attempt` 确认物使下一 pass 产生真实 retry submission，且不改 `NHMS_SCHEDULER_RETRY_LIMIT`；
      3. 中间 pass：`submitted_count == 0`；
      4. rerun 行使 attempt 加 1，下一 pass 回到 `blocked`；
      5. `pin != attempt`（陈旧）的确认物不放行。
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
- [x] R1.2 cand-02：**DEFER（#2400）**。预算写侧复算 attempt 不可行：读侧 attempt 依赖调度侧候选身份（registry/adapter 构造的 `candidate_identity` 与 `candidate_state` 参数），写侧无法复用同一推导。runbook 写明预授权风险与 dry-run 核对步骤；spec/design 回到「写侧只校验 pin >= 1」。
- [x] R1.3 cand-03：`EVALUATING_PASS_STATUSES` 正向封闭 allowlist（size fallback 按 `limit.pre_limit_status`），receipt `non_evaluating_passes:[{pass,status}]`；零个可判定 pass（含空 root，#2399）→ exit 3；成员钉与 (a)(b)(c)/空目录测试；CLI help 与 runbook 退出码表同步。
- [x] R1.4 cand-04：`skip_collector` 贯穿 `_iter_pipeline_job_records_scoped` → `_replay_pipeline_job_records_for_cycle` → `_iter_flat_direct_pipeline_job_records_for_cycle`，skip 模式绕过 `_cycle_job_records_memoized`，按 `(path, reason)` 去重，删除 cycle 级 except；同 cycle 坏行、无法解析文件名的坏行测试；C.3 改为同 cycle 坏行 + 同实例非 skip 读仍 raise（记忆化变异变红）；C.5 补 `jobs` 断言；cycle journal 日志损坏照旧 raise。
- [x] R1.5 cand-05：D.2 的 `_manual_retry_requested` 断言经变异证实无法咬住（marker 读取还要求 `trigger: "manual"`），删除并以注释指向 `test_attest_records_a_dedicated_confirmation_event` 的精确形状断言。
- [x] R1.6 runbook：确认物在 rerun 被接受提交时消费、Slurm 失败不恢复；「候选仍显示 blocked ≠ 确认物未生效」与 #2397 合并；退出码表加 exit 3 新条件与 `non_evaluating_passes`；evidence root 取自 `nhms-compute-scheduler.service` 的 `infra/env/compute.scheduler-dbfree.env`（#2399）并核对 `evidence_root` 与 `passes_scanned > 0`。预算 pin 仍取 `list-operator-actions` 最新 pass 的 `attempt`，并附 #2400 预授权警告。

## Evidence Floor

- EF-1 本地：`uv run pytest -q tests/test_scheduler_backfill_predecessor.py tests/test_scheduler_generation.py tests/test_warm_start_chaining.py tests/test_scheduler_backfill.py` 全绿。
- EF-2 本地：`uv run pytest -q tests/test_production_scheduler.py tests/test_retry.py tests/test_retry_cancel_consistency.py tests/test_monitoring_api.py` 全绿。
- EF-3 本地：`uv run pytest -q tests/test_file_orchestration_journal.py tests/test_file_journal_full_tree_budget_contract.py tests/test_operator_action_listing.py tests/test_operator_reentry_confirmation.py` 以及 C 切片所在测试文件全绿。
- EF-4 `uv run ruff check .` 通过；`openspec validate node22-operator-action-surface-reentry --strict --no-interactive` 通过。
- EF-5 反复活 grep：`grep -n "blocked_journal_predecessor_identity_quarantine\|blocked_strict_warm_start_init_state_mismatch" services/orchestrator/chain_forecast_orchestrator_cycle.py services/orchestrator/chain_runtime_utils.py` 只命中注释。
- EF-6 红证据与变异：每个切片贴出源码改动前的失败断言。单独回退以下三处，对应测试变红：
  - D 的 pin 严格相等（改成 `>=`）→ D.4 第 5 步或 D.6 第 4 步变红；
  - C 的 allowlist（改成吞全部）→ C.4 变红；
  - B 的瞬时 allowlist（删掉一项 → B.3 变红；把 `predecessor_gate_failed` 加进 allowlist → B.1 变红）。
- EF-7 node-27 后端 oracle：隔离 worktree `/home/nwm/tmp/wt-b7`，复用共享 venv，不设 integration 环境变量。跑 `tests/test_production_scheduler.py tests/test_scheduler_backfill_predecessor.py tests/test_file_orchestration_journal.py tests/test_operator_action_listing.py tests/test_operator_reentry_confirmation.py`。
- EF-8 node-22 现场收据（A.7，design D2）。
- EF-9 runbook 与新 slug 文件随 PR 提交；merge 后在 #1767 评论：定向解除通道已存在，`RETRY_LIMIT=12` 的回收可以执行。只评论，不执行。

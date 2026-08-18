# Tasks: scheduler-quarantine-residual-hardening

Fixture level: expanded（upstream suggested: absent — issue 无 `Suggested fixture level` 字段；mandatory expanded triggers 命中：retry、persisted/shared state transitions、兄弟副本白名单）
Repair intensity: high（production state machine + retry/resume；Invariant Matrix 见 design.md）

## Must preserve

- §8.7 read-only invariant：无 journal 写入/删除；filter 只能 DECLINE（retry）或 fail-stop（blocked），绝不 ADMIT。
- 生产 cadence `0,12` 下逐字节不变：既有 §8.7 测试全绿、无新 quarantine、非 quarantine 选取路径行为不变。
- `completed_pipeline_init_state_id` accessor 契约不变（journal-only、never-raises、alias 集）。
- 两处白名单的既有成员与既有差异（`retry_repair_missing_forcing` 仅在 `_FORCE_TERMINAL_RESUBMIT_DECISIONS`）不变。
- `_strict_warm_start_terminal_mismatch_decision` 等既有 blocked demotion 行为不变。
- PG provider `get_state_snapshot_by_model_time` 行为不变（`del cycle_id, lead_hours` 保持）。
- 共享 alias 表 `_INIT_STATE_FIELD_ALIASES`（`scheduler_init_state_match.py`）一字不改——alias 收敛只经由 Wiring A 换走 accessor 实现，strict warm-start 比对等其它消费者语义不变。

## Seams under test（upstream-declared，implementer 消费不重谈）

- `_journal_predecessor_identity_quarantine`（候选侧决策函数，喂 context/state fixture）
- `cycle_completion_status` + `_select_backfill_source_cycles`（discovery 完成度与槽位）
- `_terminal_stage_needs_forced_resubmit` / `_replacement_retry_scoped_cycle_execution`（白名单消费点）
- `_select_forecast_initial_state`（env=false 选取，file provider fixture）
- FileOrchestrationJournal 新 accessor（journal 文件 fixture 直测）
- 顶层回归：`tests/test_production_scheduler.py` 既有 db-free 全链 fixture

## Risk packs considered（core）

- Public API / CLI / script entry: not selected — 无 API/CLI 面。
- Config / project setup: not selected — 不加配置项（N=2 常量，YAGNI）。
- File IO / path safety / overwrite: not selected — 新 accessor 只读既有 journal 文件，无写入面。
- Schema / columns / units / field names: selected — evidence 新字段（breaker block、not_selected reason）与 decision/reason 字面是下游按键消费的契约，测试钉字面。
- Auth / permissions / secrets: not selected — 无。
- Concurrency / shared state / ordering: selected — retry/resume 状态机 + backfill oldest-first 槽位语义 + memoized `_cycle_rows` 复用。
- Resource limits / large input / discovery: selected — breaker 计数须不受 `candidate_state_job_limit` 截断影响（accessor 直读 journal）；journal 段数有界（3 段）已由既有契约覆盖。
- Legacy compatibility / examples: selected — `0,12` 逐字节不变 + A 侧 identity 来源切换的 parity。
- Error handling / rollback / partial outputs: selected — accessor never-raises、计数不可得 fail toward liveness、blocked evidence 可读性。
- Release / packaging / dependency compatibility: not selected — 无依赖变更。
- Documentation / migration notes: not selected — 无运维流程变化（breaker blocked evidence 自描述；`docs/runbooks/scheduler-dbfree-typed-reasons.md` 若新增 typed reason 需同步一行，归入实现任务 3.6）。

## Domain packs（NWM profile）

- production-state-machine: selected — §8.7 quarantine → retry/blocked 转移 + 白名单驱动的 resubmit 语义。
- evidence contract: selected — blocked/not_selected evidence 字段与 token 字面。
- solver/numerical、geospatial、forcing-data: not selected — 不触及。

## Implementation tasks

- [ ] 1.1 journal accessor：`completed_pipeline_init_state_id_occurrences(source_id, cycle_time, model_id, init_state_id) -> int`（只读、never-raises→0、复用 `_cycle_rows.pipeline_jobs`；**只数 terminal-success cohort master 行**，per-model terminal 复制行不计，口径见 design D3；含 docstring 契约）
- [ ] 1.2 breaker 判定 helper（`scheduler_generation.py` §8.7 区段或 `scheduler_candidates.py` 内部，纯函数 + accessor 注入；`N=2` 常量）
- [ ] 2.1 候选侧：`_journal_predecessor_identity_quarantine` identity 来源切换为 accessor（D1：getattr 约定、无 accessor→弃判、alias 收敛）
- [ ] 2.2 候选侧：positive mismatch 且 breaker engaged → `blocked` decision（typed decision/reason、recorded/expected token、occurrences、`retry_policy.manual_retry_required`、经 `_evidence_safe`）
- [ ] 3.1 白名单：`retry_journal_predecessor_identity_mismatch` 加入 `_FORCE_TERMINAL_RESUBMIT_DECISIONS` 与 `force_replacement_decisions` 两处
- [ ] 3.2 discovery：`_select_backfill_source_cycles` 仅在"gap 成因全部为 breaker-engaged model"时排除该 cycle（evidence-only、含 token 的 not_selected 条目、不占执行槽；cycle 完成度仍为 gap；混合 cycle 仍占槽——聚合规则见 design D4）
- [ ] 4.1 收敛层：quarantine-rerun lineage **偏好**透传（D2：basin evidence → `_select_forecast_initial_state` → `_exact_or_latest_usable_state` → 先带 `cycle_id`/`lead_hours` 查，miss 回退今日不带 lineage 查找；默认 None 不变）
- [ ] 5.1 PG provider `del` 行注释落字（D5，不改行为）
- [ ] 3.6 若 blocked decision 构成新 typed reason 面，`docs/runbooks/scheduler-dbfree-typed-reasons.md` 增补一行分诊条目（沿用该 runbook 既有两步核对结构）
- [ ] 6.x 测试（见 Required evidence 全部行，均须先红后绿：新断言在未改实现上失败的红证输出入 PR evidence）

## Required evidence（每行 = 测试或命令；输入 → 期望）

- [ ] E1 `0,6,12` cadence + wrong-suffix 首轮 fixture（**wrong-lineage entry 的 state_id 按字符串序严格排在 expected-lineage entry 之前**，堵"未实现也因 `min(state_id)` 碰巧选对"的假绿）→ 首轮 quarantine 产出 `retry_journal_predecessor_identity_mismatch`；重跑（basin 带 quarantine evidence）选中 expected-lineage entry → 记录 expected token → 下轮无 quarantine。
- [ ] E2 同 fixture 但 expected-lineage entry 不存在 → 重跑回退**今日的不带 lineage 查找**，选中同一 wrong-lineage entry（断言选中 state_id 与改动前一致，非 cold start）→ 再次 quarantine → 收敛由 E3 breaker 腿接管；**usable_flag 腿**：expected-lineage entry 存在但 `usable_flag=false` → 同样回退不带 lineage 查找并选中 wrong-lineage usable entry（非 cold start）。
- [ ] E3 不收敛构造（2 条 terminal-success cohort **master** 行记录同一 stale token）→ 候选侧 decision 为 blocked（断言 decision/reason 字面、recorded/expected token、occurrences=2、manual_retry_required=True）；仅 1 条 master 时仍为 retry（N 边界双腿）；repository **无 accessor** / 行不可读 → decision 保持 `retry_journal_predecessor_identity_mismatch`（fail-toward-liveness 腿）。
- [ ] E4 discovery：全部 gap 成因均为 breaker-engaged 的 cycle 不占 `available_gaps[:1]`（下一 gap 被选中执行），evidence 含 not_selected 条目与两 token；该 cycle 完成度仍为 gap（非 complete）；**混合腿**：同 cycle 一 model breaker-engaged、另一 model 真实未完成 → cycle 仍占槽正常执行。
- [ ] E5 白名单：`retry_journal_predecessor_identity_mismatch` 下 `_terminal_stage_needs_forced_resubmit` 与 `_replacement_retry_scoped_cycle_execution` 均为 True；集成腿：第二次同 cycle+model quarantine 产生 replacement forecast submission（新 run 身份，非 idle resume 复用 succeeded job）。breaker blocked decision 字面不在两白名单（成员钉测试）。
- [ ] E6 A/B parity："journal 无 id + manifest 有 id" 行 → A 弃判（skip 保持）且 B 返回 None；裸 `state_id` alias 行 → 两侧一致弃判。
- [ ] E7 `terminal_completed_cycle` skip reason + durable hydro success + wrong-suffix id → quarantine retry 触发。
- [ ] E8 accessor 单测：多 master 同/异 token 计数；**同一 submission 的 master 行 + reconcile 复制出的 per-model terminal 行 → 计数恰为 1**（distinctness 键钉）；无行/不可读/占位行 → 0、never-raises；**截断腿**：cycle job 数超过 `candidate_state_job_limit` → accessor 计数不受影响（不从 bounded payload 计数）。
- [ ] E9 `0,12` 回归：`uv run pytest -q tests/test_scheduler_generation.py tests/test_file_orchestration_journal.py tests/test_warm_start_chaining.py tests/test_state_manager.py tests/test_production_scheduler.py` 全绿；既有 §8.7 测试零改动（除非新增断言）。
- [ ] E10 `uv run ruff check .` clean；`openspec validate scheduler-quarantine-residual-hardening --strict --no-interactive` valid；若任务 3.6 触发 runbook 增补，增补行须过 markdownlint 并保持该 runbook 既有分诊表结构（此为 3.6 的 evidence 归属）。

## Evidence Floor（对应 issue 验收标准）

- 验收 1（一次重跑收敛）↔ E1（expected entry 存在的收敛类；E2 钉不收敛类不退化为 cold start 并移交 breaker）· 验收 2 ↔ E3/E4 · 验收 3 ↔ E5 · 验收 4 ↔ E6 · 验收 5 ↔ E7 · 验收 6 ↔ E9 · 验收 7 ↔ E3/E4（gap 保持）+ 全套无 journal 写断言（既有 immutability 测试形状）

# Design: scheduler-quarantine-residual-hardening

Fixture level: expanded · Repair intensity: high · Project profile: NWM（openspec/project-profile.md）

## Change surface

- `services/orchestrator/chain_forecast_state.py` — `_exact_or_latest_usable_state` / `_select_forecast_initial_state`（env=false 选取，quarantine-rerun lineage 透传）
- `services/orchestrator/chain_forecast_orchestrator_cycle.py` — `_FORCE_TERMINAL_RESUBMIT_DECISIONS`
- `services/orchestrator/chain_runtime_utils.py` — `force_replacement_decisions`
- `services/orchestrator/scheduler_candidates.py` — `_journal_predecessor_identity_quarantine`（identity 来源切换 + breaker demotion）
- `services/orchestrator/scheduler_discovery.py` — `_journal_predecessor_identity_is_stale` 保持；`_select_backfill_source_cycles` 槽位排除 breaker-engaged gap
- `services/orchestrator/file_orchestration_journal.py` — 新只读 accessor（同 token completed 记录计数）
- `services/orchestrator/scheduler_generation.py` — §8.7 区段可放共享 breaker 判定 helper（纯函数）

## Decisions

### D1 — A/B identity 口径统一为 journal-only（B 为准）

Wiring A（候选侧 quarantine）不再读 `raw_candidate_state["hydro_run"]`（该行可能被 `chain_repository_state.py:754-762` 用 run manifest 回填），改为经 `context.active_repository` 上 getattr `completed_pipeline_init_state_id`（repo 约定，cf. `scheduler_backfill_predecessor.py:226`）；accessor 缺失或返回 None → 弃判（保持 pre-#1107 行为）。alias 收敛**纯粹是换走 accessor 的自然结果**：共享 alias 表 `_INIT_STATE_FIELD_ALIASES`（`scheduler_init_state_match.py:44-49`，strict warm-start 比对等 §8.7 之外的消费者仍在用）**一字不改**——绝不通过改该表来"收窄 alias"（那会静默改变 legacy 裸 `state_id` 行的 strict 匹配语义，超出本 change 范围）。

- 拒绝的备选（A 为准）：让 accessor 也吃 manifest 回填——违反 accessor docstring 既定契约（"No run-manifest reads"）与 §8.7 "报告 JOURNAL 记录了什么"的语义；且需要在 `chain_repository_state.py` 加回填标记 plumbing，扩面更大。
- 状态词汇对齐：A 现用 `DURABLE_HYDRO_SUCCESS_STATUSES`（scheduler_state_types，={"succeeded","parsed","published","complete"}），accessor 用 `COMPLETED_HYDRO_STATUSES`（chain，同集合）——实测同集合，parity 由测试钉住。
- 行为差点：accessor 额外要求 `_row_matches_candidate`；raw 路径的 hydro_run 已按候选 scope 装配，预期等价——`0,12` 全量回归 + 既有 §8.7 测试全绿作为逐字节不变证据。

### D2 — 收敛层激活范围：仅 quarantine 重跑（evidence 驱动）；lineage 是**偏好不是过滤**

orchestrator 侧不掌握 scheduler cadence；全局改 exact 选取会碰非 quarantine 热路径。quarantine retry evidence（`journal_predecessor_identity`）已随 basin `state_evidence` 到达 orchestrator（`scheduler_candidates.py:2085-2098` → `scheduler_candidate_manifest.py:228-230` → `chain_forecast_cycle.py:239` 处 basin 可见），其中 `required_lead_hours` 足以推出 expected lineage：`cycle_id = cycle_id_for(source_id, cycle_time - required_lead_hours)`、`lead_hours = required_lead_hours`。仅当 basin 携带该 evidence 且 env=false 且 `before_time == cycle_time`（exact 分支）时先做**带 lineage 的查找**；命中 → 选中 expected-lineage entry，一次重跑收敛（验收 1）。

**lineage miss（含"entry 存在但 `usable_flag=false`"——usability 门在 provider 调用之后，`chain_forecast_state.py:662-666`）时回退到不带 lineage 的今日查找（偏好语义），不是硬过滤**：miss 判定 = 带 lineage 的查找**未产出 usable snapshot**。db-free file 模式下 `get_latest_usable_state` 无条件 raise（`state_manager.py:1116-1118`），`_exact_or_latest_usable_state` 捕获后返回 None → `cold_start_no_state`（`chain_forecast_state.py:196-199`）——若把 lineage 当过滤，expected entry 缺失的重跑会变成**归零 cold start**（#1164 缺陷类，物理上远差于 wrong-lineage warm start，且是否能过 §8 gate 亦未决）。因此 miss 时重跑保持今日的 wrong-lineage 选取（逐字节同前），该不收敛类由 D3 breaker 在 N=2 处 fail-stop——这正是"收敛层 + 兜底层"的分工。首轮（无 evidence）行为不变。

- 实现 seam：selection 调用链（`chain_forecast_cycle.py:239` → `_select_forecast_initial_state` → `_exact_or_latest_usable_state`）需把 basin 的 quarantine lineage 透传下去（新增可选参数，默认 None = 现行为）。implementer 须实测 basin dict 在该调用点可见 `state_evidence`；不可见时通过与 `_basin_max_lead_hours` 同层的既有 basin 通道传递，并在偏离记录中报告实际采用的通道。

### D3 — breaker 载体：journal 历史只读计数（新 accessor），不用 retry-attempt 计数、不加 sidecar 状态文件

判定："当前 stale token 已被 ≥N=2 个**不同 completed forecast submission** 记录"。**distinctness 键 = cohort master 行**：一次 submission 恰好写一条 cohort master 行（reservation 时把 `init_state_identities` 落在 master 上且此后不再改写，`chain_forecast_orchestrator_cycle.py:592-601` + `accepted_submit_identity.py:148-151` #1183 注释——该字段属于 `ACCEPTED_SUBMIT_MASTER_ORDINARY_UPSERT_FIELDS`，**不在** `ACCEPTED_SUBMIT_MASTER_IMMUTABLE_FIELDS`，勿以为要补进 immutable 表），reconcile 会把各 model 的条目**复制**到 per-model terminal 行（`tests/test_file_orchestration_journal.py:7981-7998` 钉住，job_id 不同）——因此计数**只数 terminal-success 的 cohort master 行**（master kind 判定走 `accepted_submit_row_kind` 等既有 helper），per-model terminal 行一律排除；同一 submission 的 master+terminal 两行 = 1（E8 钉）。数据源 = journal `_cycle_rows.pipeline_jobs`（按 job_id 保留、collapse-free，`file_orchestration_journal.py:3774-3776`），新增 FileOrchestrationJournal 只读 accessor（形如 `completed_pipeline_init_state_id_occurrences(source_id, cycle_time, model_id, init_state_id) -> int`，复用 memoized `_cycle_rows`，never-raises 返回 0 语义与 `completed_pipeline_init_state_id` 一致）。

- 拒绝 `_state_retry_attempt`：replacement cohort 拿全新 run_id、无 `_retry_` 后缀 → 系统性 undercount。
- 拒绝 scheduler sidecar 状态文件：新增持久面，违反 KISS 与 §8.7 只读精神。
- 拒绝从 bounded `raw_candidate_state` 计数：`candidate_state_job_limit` 截断会 undercount；accessor 直读 journal 不受 candidate payload 截断影响（E8 有截断腿钉此点）。
- 拒绝"数 journal 历史段里的 distinct completed hydro_run 行"作 fallback：`_CycleRows` 只有单一 last-wins 的 `hydro_run` 槽（`file_orchestration_journal.py:390-397, 4177-4181`），历史 hydro_run 行经 `_cycle_rows` 不可数——那需要新开 raw 段读取面，超出本 change。master-row 计数即唯一口径，无 fallback。
- 计数失败/无 accessor/行不可读 → 0 → breaker 不触发且 decision 保持 quarantine retry（fail toward liveness：宁可多重跑一轮，不可误 fail-stop；E3 有 accessor 缺失腿钉此点）。

### D4 — 槽位释放：discovery 槽位选择把 breaker-engaged gap 当 evidence-only，cycle 仍报 gap

`cycle_completion_status` 的 §8.7 choke point 不变（stale → gap，**绝不 ADMIT**）。`_select_backfill_source_cycles` 在 available_gaps 中排除 breaker-engaged 的 cycle（判定复用 D3 accessor + `_journal_predecessor_identity_is_stale` 同口径），比照 unavailable gap 的处理：产出 evidence 条目（`selection_status="not_selected"`、reason=breaker、含 recorded/expected token）且不占 `available_gaps[:1]` 执行槽。后续 cycle 有该 T 处 completed run 的 state 可作合法 fallback warm start，不会因跳过而断链。

**per-cycle 聚合规则**（breaker 按 cycle+model，槽位按 source-cycle）：仅当"该 cycle 若无 §8.7 stale 判定即为 complete，且所有把它钉成 gap 的 model 均 breaker-engaged"时才排除出执行槽——即 gap 的**唯一**成因是 breaker-engaged 的 quarantine。混合 cycle（某 model breaker-engaged、另一 model 真实未完成）**仍占槽正常执行**：排除它会饿死其余 model 的真实工作，违背 fail-toward-liveness（E4 有混合腿钉此点）。

- 拒绝"breaker 时 `_journal_predecessor_identity_is_stale` 返回 False（cycle 转 complete）"：那是 ADMIT，违反 §8.7 不变量，且 token 证据无处落地（cycle complete 后不再产出任何 evidence）。
- 候选侧 blocked demotion（`_strict_warm_start_terminal_mismatch_decision` :2135-2167 先例形状）覆盖非 backfill 入口（当前 cycle 路径）的同一回路；blocked decision 字符串刻意不进两处白名单（先例同款防复活语义）。

### D5 — PG provider 不动（`del cycle_id, lead_hours` 保持）

§8.7 quarantine filter 与 breaker 都是 file-journal 面（db-free scheduler）；DB 模式无此 quarantine 回路，不存在不收敛类。给 PG 加 lineage 过滤需要真实 DB oracle 验证（node-27），把本 change 拖出"纯文件态 + 本地 pytest"边界，且无消费者。在 PG provider 的 `del` 行注释与 spec Non-goal 中显式落字。

### 常量

- `N=2` 硬编码为模块常量（如 `_JOURNAL_IDENTITY_QUARANTINE_BREAKER_THRESHOLD = 2`），不加配置项（YAGNI）。
- breaker blocked decision/reason 命名建议：decision `blocked_journal_predecessor_identity_quarantine`、reason `journal_predecessor_identity_quarantine_breaker_engaged`（与 `blocked_strict_warm_start_init_state_mismatch` 先例命名法一致；最终字面以实现为准并测试钉住）。

## Invariant Matrix

- Governing invariant: §8.7 identity 面在任何输入下只能对 completed-skip 作 DECLINE（retry）或 fail-stop（blocked），绝不 ADMIT 一个否则不会成立的 skip，绝不写 journal；生产 cadence `0,12` 下整个调度面逐字节不变。
- Source-of-truth identity/contract: journal 记录的 `init_state_id`（journal-only，alias `init_state_id`/`initial_state_id`）对照 `expected_journal_init_state_tokens` 生成的 expected token。
- Producers: `chain_forecast_state.py` 选取 + run 记录路径（写 journal 的既有路径，不改）；quarantine retry evidence 由 `_journal_predecessor_identity_retry_evidence` 产出（不改字段，新增消费者）。
- Validators/preflight: `journal_init_state_lineage_matches_expected`（不改）；新 breaker 判定 helper（纯函数 + accessor）。
- Storage/cache/query: `file_orchestration_journal.py` `_cycle_rows` memoized view；新只读 accessor；`state_manager.py` file provider 既有 `cycle_id`/`lead_hours` 过滤（不改实现，仅新调用形态）。
- Public routes/entrypoints: 无 API/CLI 面。
- Frontend/downstream consumers: 无；evidence 消费者按键读值（additive）。
- Failure paths/rollback/stale state: breaker blocked evidence（recorded/expected token、occurrences、`retry_policy.manual_retry_required`）；accessor never-raises；计数不可得 → breaker 不触发。
- Evidence/audit/readiness: blocked/not_selected evidence 走既有 `_evidence_safe` 净化；discovery evidence 条目按 `_source_cycle_evidence` 形状。
- Regression rows:
  - `0,12` cadence 全量既有 §8.7 测试 → 全绿无新 quarantine（逐字节不变）。
  - `0,6,12` fixture、wrong-suffix 首轮、expected-lineage entry **存在**（且 wrong-lineage 的 state_id 按字符串序严格排在 expected 之前，堵 `min(state_id)` 假绿）→ 一次 quarantine → 重跑（带 evidence）选中 expected lineage → 不再产出 `retry_journal_predecessor_identity_mismatch`。
  - expected-lineage entry **不存在** → 重跑回退今日选取（同一 wrong-lineage state，逐字节同前）→ 再 quarantine → 第 2 条 terminal-success master 同 token 后候选侧 blocked + discovery 槽位放行下一 gap；cycle 仍报 gap。
  - 混合 cycle（一 model breaker-engaged、另一 model 真实未完成）→ 该 cycle 仍占执行槽正常执行。
  - accessor 缺失 / 行不可读 → 计数不可得 → decision 保持 quarantine retry（breaker 不触发）。
  - "journal 无 id + manifest 有 id" 行 → A/B 两侧一致弃判（skip 保持）。
  - 裸 `state_id` alias 行 → 两侧一致弃判。
  - 白名单：decision `retry_journal_predecessor_identity_mismatch` 在两副本中 → 第二次同 cycle+model quarantine 产生 replacement submission（非 idle resume）；breaker blocked decision 不在白名单 → 不可复活。
  - `terminal_completed_cycle` skip + durable hydro success + wrong-suffix id → quarantine 触发。

## Review focus

1. §8.7 不变量：任何新分支都不得 ADMIT skip、不得写 journal（D4 的拒绝备选是关键审点）。
2. `0,12` 逐字节不变：identity 来源切换（D1）与 exact 查找签名变化（D2）在非 quarantine 路径上零行为差。
3. breaker 计数口径（D3）：undercount → 多重跑（可接受）；**overcount → 误 fail-stop（不可接受）**——重点审 token 比对与 distinct-submission 判定。
4. 白名单兄弟副本同步 + breaker blocked decision 防复活。
5. evidence 字段经 `_evidence_safe`，token 字面不被 redaction 破坏（cf. #1152 runbook 路径重写教训）。

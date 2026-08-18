# Tasks: scheduler-predecessor-pending-operator-signal

## Risk triage

- Fixture level: **compact**（additive evidence 字段 + 测试 + runbook；不改 gate
  判定；无 DB、无前端）。Issue 为手写 legacy issue，无上游 Suggested fixture level。
- Repair intensity: **medium**（公开 evidence shape 的 additive 变更，第二个同类
  发现触发 pattern escalation）。
- Risk packs:
  - `production-state-machine / evidence contract`: **selected** —— blocked
    evidence 是运维消费的公开形状，additive 字段需测试锁 + spec delta。
  - `Slurm production lifecycle`: not selected —— 不触提交/调度路径。
  - `Run manifest / QC provenance`: not selected —— 不触 manifest。
  - `Geospatial / forcing / SHUD numerical / PostGIS / provider / display`:
    not selected —— 均不在改动面。

## Seams under test

- `scheduler_generation_gate.strict_warm_start_evidence` §8 路径的
  prior-checkpoint-missing blocked evidence 构造点（唯一 `history_exists=False`
  可达 blocked evidence 的发射面）。
- `scheduler_backfill_predecessor.emit_predecessor_candidates` + 真实
  `strict_warm_start_for_candidate`（非桩 gate）。

## Tasks

- [x] 1.1 在 §8 blocked evidence（`state_snapshot_index_prior_checkpoint_missing_after_history`）
      附加 `self_heal_expected` / `operator_action_required` / `self_heal_probe` /
      （条件性）`operator_action` + `runbook` 字段；不改 `failure` 块与 gate 判定。
      **Round-1 修订**：判据从 `history_exists` 恒等收紧为
      `latest_usable_state.valid_time == required_prior_cycle_time`
      （datetime 比较；malformed/缺失 → False，fail toward escalation）。
      **Round-2 修订（终态）**：valid_time 判据仍给两类假阴性
      （`usable_state_history_evidence` 既 generation-blind 又 object-blind，
      `state_manager.py` :1297-1317），判据改为直接跑 predecessor 自己那道门的
      同一次验证——`strict_warm_start_evidence(valid_time=required_prior_cycle_time,
      model_package_checksum=候选 checksum, required_lead_hours=…)` 要求
      `ready=True`；另附 `self_heal_probe={ready, reason}`。`_evidence_time`
      helper 随判据下线一并删除（无其他消费者）。
- [x] 1.2 `tests/test_scheduler_generation.py`：五个几何的字段断言——
      精确 predecessor state 存在（entry @ T−lead，当代 + 对象在位）→
      `self_heal_expected=True, operator_action_required=False,
      self_heal_probe.ready=True`
      （底座：`test_env_override_does_not_admit_missing_predecessor`）；
      无更早历史 → `operator_action_required=True` 且 `runbook` 字段
      字面值 == `docs/runbooks/scheduler-dbfree-typed-reasons.md`
      （底座：`test_env_override_blocks_predecessor_pending_without_earlier_history`）；
      **≥2 格缺口**（唯一 entry @ T−2·lead，history_exists=True）→
      `operator_action_required=True`
      （`test_multi_cycle_gap_flags_operator_action_despite_earlier_history`）；
      **round-2 新增两个假阴性几何**——错代条目坐在 T−lead 上
      （`test_wrong_generation_state_at_predecessor_slot_flags_operator_action`，
      `self_heal_probe.reason == state_snapshot_index_model_package_checksum_mismatch`）、
      当代条目在 T−lead 但 state 对象已删
      （`test_missing_state_object_at_predecessor_slot_flags_operator_action`，
      `self_heal_probe.reason == state_snapshot_index_object_missing`）；两者的
      `latest_usable_state.valid_time` 都等于 `required_prior_cycle_time`，
      round-1 判据在此读作 `self_heal_expected=True`（已 red-proof）。
- [x] 2.1 `tests/test_scheduler_backfill_predecessor.py`：env-wired
      （`NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST=true`）真实 §8 gate 集成测试，
      钉住 no-earlier-history 几何下被发出的 predecessor 自身评估为同
      typed reason blocked、携带 `operator_action_required=True`，且 gap 不自愈
      （emitter 对该 predecessor 产生 blocked 记录）。
      **前置条件（缺一测试会静默跳过真实 gate）**：predecessor cycle 的 raw
      manifest 必须存在于 `OBJECT_STORE_ROOT`——**不得**把 predecessor cycle
      注入 `cycles`（主循环会先 block 它，emitter 走
      `predecessor_already_present` dedup 跳过，§8 gate 永不执行、测试假绿）。
      否则 `_predecessor_raw_manifest_ready` 返回 `predecessor_raw_manifest_not_ready`
      → emitter `continue`，§8 gate 不执行。断言必须钉 emitter 返回的
      emission-evidence 记录 `status == "blocked"`（且 `predecessor_cycle_time`
      匹配）——这一钉同时封死全部 silent-skip 变体
      （`predecessor_already_present` / `predecessor_backfill_active_pipeline` /
      `predecessor_raw_manifest_env_unwired` / `predecessor_candidate_construction_failed` /
      `predecessor_gate_failed`）伪装成功的可能。若 env=true 使 successor 的
      candidate-state provider 需要 successor 侧 raw manifest 才能维持
      `block_predecessor_pending`，则同时 stage successor 的 raw manifest。
      state-index 几何 = 唯一当代条目
      `valid_time` 严格晚于候选 cycle + in-window declaration +
      `NHMS_REQUIRE_FORECAST_WARM_START=false`（复用
      `tests/test_scheduler_generation.py::test_env_override_blocks_predecessor_pending_without_earlier_history`
      的 fixture 底座）。
- [x] 2.2（round-3 retro 纠正）正向腿真实门端到端钉：
      `tests/test_scheduler_backfill_predecessor.py::test_emitted_predecessor_admitted_when_self_heal_expected`
      —— self-heal 几何下被发出的 predecessor 被真实 §8 门 admit（进
      `candidates`、不进 `blocked`），successor 发射记录 `status=="emitted"`，
      successor 字段 `self_heal_expected=True / self_heal_probe.ready=True`；
      红演示 = 撤 predecessor manifest staging → `'skipped' == 'emitted'` 断言红。
- [x] 3.1 新建 `docs/runbooks/scheduler-dbfree-typed-reasons.md`：该 typed
      reason 的含义、两类群体区分（判据 = predecessor 自己那道门的全量
      warm-start 验证 `ready`，**不是** `history_exists`，**也不是**
      `latest_usable_state.valid_time` 相等）、处置（按 `self_heal_probe.reason`
      分流：缺格 → 回填 state；错代 / unusable / 对象丢失 → 先修条目或对象）、
      §8.6 stall 识别特征（每 pass 追加 blocked predecessor、successor 持续
      defer、multi-gap 下 history_exists 恒 True）。
      **Round-2 修订**：补单级语义边界（分诊只读被发现 successor 的记录；
      emitted-predecessor 记录同带该字段但单级语义，≥2 格链里可能读到
      `self_heal_expected=true`，不构成链收敛证据；predecessor 若撞
      declaration 级 / wrong-generation block 则该组字段完全缺席）；修正
      `predecessor_cycle_time` 与 `required_prior_cycle_time` 的"恒等于"
      （同一时刻、不同序列化：`+00:00` vs `Z`，按时间戳比对）、
      `submitted_count` 为 pass 级聚合、strict 短路 reason 视具体失败而定、
      以及"错代条目会被改判成 wrong-generation reason"这一错误说法。
- [x] 3.2（round-2 C1）`services/orchestrator/scheduler_evidence_payload.py`：
      `_BOUNDED_CANDIDATE_STATE_EVIDENCE_KEYS` 保留 `operator_action_required`，
      使 runbook 的单布尔分诊在 summarized pass 上仍可执行；
      `tests/test_production_scheduler.py::test_bounded_candidate_summary_retains_predecessor_pending_operator_signal`
      钉住（含二次 summary 幂等）。
- [x] 4.1 验证：`uv run pytest -q tests/test_scheduler_backfill_predecessor.py
      tests/test_scheduler_generation.py` + `uv run pytest -q
      tests/test_production_scheduler.py` + `uv run ruff check .` +
      `openspec validate scheduler-predecessor-pending-operator-signal --strict --no-interactive` +
      `npx --yes markdownlint-cli2 "docs/runbooks/scheduler-dbfree-typed-reasons.md"`
      （CI Markdown Lint 门对 `docs/**` 必跑）。

## Evidence mapping

- selected pack `evidence contract` → tasks 1.2 / 2.1 / 2.2 / 3.2（五个几何 +
  真实门双腿（blocked + emitted）+ bounded 摘要层）+ spec delta scenario。
- 非目标（不改判定行为）→ 既有 `test_env_override_blocks_predecessor_pending_*`
  回归绿证明 reason/failure 块不变。

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
      附加 `self_heal_expected` / `operator_action_required` /（条件性）
      `operator_action` + `runbook` 字段；不改 `failure` 块与 gate 判定。
- [x] 1.2 `tests/test_scheduler_generation.py`：两个几何的字段断言——
      history_exists=True → `self_heal_expected=True, operator_action_required=False`
      （底座：`test_env_override_does_not_admit_missing_predecessor`）；
      history_exists=False → `operator_action_required=True` 且 `runbook` 字段
      字面值 == `docs/runbooks/scheduler-dbfree-typed-reasons.md`
      （底座：`test_env_override_blocks_predecessor_pending_without_earlier_history`）。
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
- [x] 3.1 新建 `docs/runbooks/scheduler-dbfree-typed-reasons.md`：该 typed
      reason 的含义、`history_exists` 两类群体区分、处置（发布/回填缺失
      predecessor state）、§8.6 stall 识别特征（每 pass 追加 blocked
      predecessor、successor 持续 defer）。
- [x] 4.1 验证：`uv run pytest -q tests/test_scheduler_backfill_predecessor.py
      tests/test_scheduler_generation.py` + `uv run ruff check .` +
      `openspec validate scheduler-predecessor-pending-operator-signal --strict --no-interactive` +
      `npx --yes markdownlint-cli2 "docs/runbooks/scheduler-dbfree-typed-reasons.md"`
      （CI Markdown Lint 门对 `docs/**` 必跑）。

## Evidence mapping

- selected pack `evidence contract` → tasks 1.2 / 2.1（正负两几何 + 真实门）
  + spec delta scenario。
- 非目标（不改判定行为）→ 既有 `test_env_override_blocks_predecessor_pending_*`
  回归绿证明 reason/failure 块不变。

## 1. Zero-row readiness reason (#2042)

Risk triage（Phase 0.5）:
- Issue type: bugfix · Blast radius: low · Fixture level: **compact**（issue 无上游 `Suggested fixture level`；单谓词收窄，无 schema/路径/状态迁移；`design.md` 豁免见 proposal）· Repair intensity: low
- Change surface: `workers/canonical_converter/converter.py::evaluate_canonical_readiness`（`identity_mismatch` 谓词，`:518-521`）；runbook 一节
- Must preserve: 有行 mismatch / lineage missing 的既有 reason（`tests/test_canonical_converter.py:512,542,570,599`、`tests/test_gfs_adapter.py:951`、`tests/test_ifs_adapter.py:1186`、`tests/test_orchestration_chain.py::test_trigger_ready_forecasts_rejects_stale_canonical_lineage_before_submission` / `::test_trigger_ready_forecasts_rejects_missing_required_lineage_before_submission`）；`_canonical_evidence_is_fresh_zero_row` 判定与 fresh-zero-row 准入（`scheduler_candidates.py:914-932`）；`status`/`ready`/各计数/`missing_*` 字段
- Must add/change: `candidate_row_count == 0` 时 `identity_mismatch` 恒 False → reason 落到 `missing_canonical_variables`
- Seams under test: `evaluate_canonical_readiness` 公开函数；scheduler `_fresh_zero_row_readiness_provider` 驱动的候选路径
- Risk packs: Schema / field names: selected - typed reason 字符串是分诊契约，零行值改变，需证明零消费者（生产代码无按该字符串分支，仅 `scheduler_candidates.py` 在 `candidate_row_count>0` 的 blocked 分支透传 reason）；Documentation: selected - runbook 新增一节；Legacy compatibility: selected - 有行 mismatch 断言逐条保持；Public API / CLI、File IO、Auth、Concurrency、Resource limits、Error handling、Config、Release、Domain packs: not selected - 纯函数谓词，无 IO/状态/依赖变化

- [x] 1.1 `converter.py`：`identity_mismatch` 在零候选行时恒 False（有行时谓词不变）
- [x] 1.2 `tests/test_canonical_converter.py` 新增：`products=[]` + policy/object identity + `forecast_hours=(0,3)` → `ready False`、`status canonical_incomplete`、`candidate_row_count 0`、`identity_rejected_row_count 0`、`reason == "missing_canonical_variables"`；改前源码上跑红（红证据随报告）
- [x] 1.3 同一 evidence 经 `_canonical_evidence_is_fresh_zero_row` 判 True（在 `tests/test_production_scheduler.py` 或 converter 测试内直接断言）；`_fresh_zero_row_readiness_provider` 驱动的既有用例补 reason 断言
- [x] 1.4 `docs/runbooks/scheduler-dbfree-typed-reasons.md` 新增 `canonical_identity_mismatch` 一节：含义（有行且 identity 对不上）、零行新鲜周期报 `missing_canonical_variables`、现场区分（看 `candidate_row_count` / `identity_rejected_row_count`）、2026090312 旧 index 的冻结假信号不回写

Required evidence:
- `uv run pytest -q tests/test_canonical_converter.py tests/test_gfs_adapter.py tests/test_ifs_adapter.py tests/test_orchestration_chain.py tests/test_production_scheduler.py -k "canonical_readiness or identity_mismatch or fresh_zero or zero_canonical"`
- issue Verification 片段 1（空产品 reason != `canonical_identity_mismatch`）
- `uv run ruff check .`；`openspec validate fix-zero-row-canonical-readiness-reason --strict --no-interactive`
- node-27 一次性 worktree 跑同一 pytest 集

Non-goals: 改 fresh-zero-row 准入；改有行 mismatch；改 `canonical_identity_mismatch_cache_miss`；改写 node-22 已冻结 forecast_index；新增 reason 字符串。

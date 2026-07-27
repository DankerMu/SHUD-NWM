# Tasks: scheduler-identity-blocked-convergence

Issue: #1173

## 1. Implementation

- [x] 1.1 **L1 streak 字段与递增**:`accepted_submit_identity.py` 新增 master 字段 `identity_blocked_streak`(加入 `ACCEPTED_SUBMIT_MASTER_ORDINARY_UPSERT_FIELDS` `:98-119`;`AcceptedSubmitTransition` dataclass + `__post_init__` `:172-182` 贯通;`normalize_accepted_submit_evidence` `:507-545` 补 `int ≥ 0` 类型校验与 `identity_mismatch_released` decision 不变量)。`file_orchestration_journal.py` 的 `changed_fields` 幂等闭集(`:1926-1941`)同步扩——否则 streak 递增被静默判 idempotent(design F5)。递增在 `_record_file_reconciliation`(`reconcile.py:1992-2007`)choke point:decision `identity_mismatch_blocked` 时 +1,**达 limit 或出口禁用时饱和停增**(F6)。清零语义 = streak 随 accounting tuple 原子替换:非 blocked 的 accounting transition 与 `begin_attempt`(reclaim/新 attempt,`file_orchestration_journal.py:1616-1619`)默认写 0(F3)。
- [x] 1.2 **L1 release 出口**:`file_orchestration_journal.py` 新增专用 typed API `release_identity_blocked_reservation`(镜像 `permit_pipeline_job_retry` `:2356-2513` CAS:expected `submission_attempt` + attempt anchor + expected status `reserved` + `require_unbound`),写 `status="reservation_lost"`、`reconciliation_decision="identity_mismatch_released"`(token 加入 `accepted_submit_identity.py:22-31` 词表)、`submit_outcome` 保持 `submit_result_ambiguous`、streak 保留终值。`reconcile_reserved_unbound_jobs` 增关键字参数 `identity_blocked_streak_limit: int | None = None`(None/≤0 = 禁用):本 pass 递增后 streak ≥ limit 且过 grace → release。**grace 锚点固定 `submission_attempt_started_at`(fallback `created_at`,绝不用 `updated_at`——streak 写每 pass 刷新 `updated_at`,沿用即出口永不触发,F2)**。CAS 失败 → 维持既有 blocked outcome。三个 writer site 的递增+阈值+outcome 产出收敛到共享 helper(判定条件与顺序不变,F4);`ReservationReconcileOutcome` 增 `identity_blocked_streak: int | None = None`。released row 为**不可 reclaim 终态**(刻意,F1;活性由"每 attempt 铸新 key"保障)。
- [x] 1.3 **L2 预算降级**:`scheduler_candidates.py:446-451` else 分支前置预算检查(`_state_retry_attempt(raw_candidate_state, stage=<restart stage>)` vs `_state_retry_limit`,后者永不 None);耗尽 → **`CandidateStateDecision("blocked", "strict_warm_start_retry_budget_exhausted", evidence)`**(F8:action 必须 `"blocked"`,否则照常提交),evidence 形状按 design D2(`decision="blocked_strict_warm_start_init_state_mismatch"` + `retry_policy` 块);未耗尽 → 现 decision 与 evidence **零字节改动**。`_terminal_decision_matches_strict_warm_start`、`_STRICT_WARM_START_TERMINAL_SKIP_REASONS`、两处 force 白名单(`chain_forecast_orchestrator_cycle.py:17-24`、`chain_runtime_utils.py:171-177`)零编辑。
- [x] 1.4 **证据面**:`scheduler_runtime.py:1505-1539` outcome 行加 `identity_blocked_streak` + 新 action;`scheduler_evidence_payload.py` `_BOUNDED_RESTART_RECONCILE_OUTCOME_KEYS`(`:36-43`)增 `identity_blocked_streak`;`scheduler_evidence_proofs.py` `restart_reconcile_proof`(`:426-500`)按显式 `pipeline_status_write_count` 口径计 release 状态写(legacy fallback 分支仅无显式写计数形状兜底,F9)。
- [x] 1.5 **config + runbook**:`scheduler_config.py` 增 `identity_blocked_streak_limit`(env `NHMS_SCHEDULER_IDENTITY_BLOCKED_STREAK_LIMIT`,默认 3,≤0 禁用),`scheduler_runtime` 传入 reconcile。`docs/runbooks/failed-basin-retry.md` 新增节:`identity_mismatch_released` row(检索方式;**不可 reclaim 终态**)与 `blocked_strict_warm_start_init_state_mismatch` decision 的人工处置、重复提交残量风险,含本次 `2026072000` 的处置结论(修复部署后自动收敛,无手工干预)。**人工重入步骤必须是实现期验证过的具体动作**(复审 P2:`chain_forecast_orchestrator_cycle.py:157-158` 对 reservation_lost 无条件早退,`job_needs_submission` 只认 `pending`——"走 manual retry API"按现状不可执行;可选落点如经 auto-retry 预算放行铸新 `*_retry_N` key、或 journal 侧显式作废/重建,由实现给出并用测试或 4.1 实机 receipt 走通,不得写未经验证的操作步骤)。

## 2. Tests(requirement-driven)

- [x] 2.0 **既有断言迁移清单:空(零迁移)**。特别地:
  - `tests/test_gateway_reconcile.py:870-921`(直调 `transition_pipeline_job_submit_evidence` 零写钉子)**原样保持绿**——它不携带 streak,幂等判定不变;作为"直调 API 仍零写"的对照钉子保留。
  - `tests/test_gateway_reconcile.py:1859-1869`(upsert 白名单子集断言)白名单扩张后自然保持绿,零修改。
  - `tests/test_production_scheduler.py:23975-23984`、`:24327-24340`(attempt < limit 时 retry decision 零改动的看门人)**必须保持绿**——fixture attempt 与默认 retry_limit 的关系如实现中变化导致其变红,视为语义弱化,须偏离记录举证。
  - 任何此外的迁移 = 语义弱化,需偏离记录单独举证。
- [x] 2.1 L1 red-before 主锚点(`tests/test_gateway_reconcile.py`):经 `reconcile_reserved_unbound_jobs` 的 reserved-unbound row 连续 3 pass `identity_mismatch_blocked`(streak 1→2→3 落盘),第 3 pass 迁出 `reserved` → `reservation_lost`,decision `identity_mismatch_released`,streak 终值 3 保留;第 4 pass 该 row 不在 reserved-unbound 查询集。
- [x] 2.2 L1 清零语义:(a) blocked ×2 → 非 blocked 结局(如 `accounting_unavailable`)→ streak 归零 → 再 blocked ×2 不触发出口("连续"非"累计");(b) **击穿序列反证(F3)**:blocked ×2 → absence 路径放行(`absence_retry_permitted`)→ reclaim 回 `reserved`(新 attempt)→ 首次 blocked **不**放行(streak 从 0 重计)。
- [x] 2.3 L1 保护边界:(a) streak 达限但未过 grace → 不放行;**且 grace 判定不被 streak 写刷新的 `updated_at` 推迟**(时钟前进跨过 `submission_attempt_started_at`+grace 后出口触发,F2);(b) limit 禁用(None/0/负)→ 永不放行,且**禁用稳态下 repeat blocked pass 恢复零写**(饱和不增,F6);(c) release CAS 失败(attempt 并发推进)→ 维持 blocked outcome、无状态迁移。
- [x] 2.4 L1 楔死解除端到端(`tests/test_production_scheduler.py`,复用 `:27859-27874` live-pass seam):楔死几何(reserved row + 同 run_id cohort)下 release 后下一 pass 不再 `PIPELINE_ALREADY_ACTIVE`,`timing.pass.status ≠ submission_failed`。
- [x] 2.5 L2 red-before 主锚点(`tests/test_production_scheduler.py`,复用 `:23902-23984` seam):attempt ≥ limit → decision `blocked_strict_warm_start_init_state_mismatch` + `retry_policy` 全形状,**候选落入 blocked 列表而非提交候选**(F8);attempt < limit → 现 decision 与 evidence 零改动(双向)。
- [x] 2.6 L2 真实几何绑定(flag-6/F10 oracle):journal fixture 镜像生产 payload(master `reserved`、`retry_count=0`、`*_forecast_retry_87` 后缀 job row、stage `forecast`),且 **job 行数超过 `candidate_state_job_limit`** → `_state_retry_attempt(state, stage="forecast")` ≥ limit 仍成立 → 降级触发。若需修推导/截断,#1160 attempt 既有测试零迁移。
- [x] 2.7 证据双档:全保真 outcome 行含 `identity_blocked_streak` 与 released action;bounded 压缩档保留该 key;proof 断言按**显式 `pipeline_status_write_count` 口径**(legacy 形状断言需构造无 `durable_write_count` 的 outcome,不得写空转断言,F9);L2 降级 decision 出现在 bounded candidate summary 的 `decision` key。
- [x] 2.8 白名单不动钉子:两处 force 白名单成员集与 master 相同(字面断言);`blocked_strict_warm_start_init_state_mismatch` 不触发强制替换路径(`tests/test_warm_start_chaining.py` 或 orchestration chain 测试)。
- [x] 2.9 红前证据:2.1/2.4/2.5(新行为类)实现前必须能红,失败输出留档 `.workplans/pr-<N>/red-before.log`。

## 3. Verification(合并门)

- [ ] 3.1 `uv run pytest -q tests/test_gateway_reconcile.py tests/test_production_scheduler.py tests/test_warm_start_chaining.py`
- [ ] 3.2 `uv run pytest -q`(全量,防跨面回归)
- [ ] 3.3 `uv run ruff check .`
- [ ] 3.4 `openspec validate scheduler-identity-blocked-convergence --strict --no-interactive`

## 4. Ops oracle(node-22 实机,合并部署后)

- [ ] 4.1 node-22 同步 master 部署后,观察连续 ≥3 个自然 pass:`timing.pass.status` 不再是 `submission_failed`;`restart_reconcile.reserved_unbound` 先出现一次 `identity_mismatch_released`(streak=limit;receipt 记录该 row 的 `submission_attempt_started_at` 与 streak 轨迹——这是 F2/F3 唯一的真实 oracle)后,该对 job_id 不再逐 pass 复现;cycle `2026072000` 的 36 行 candidates 变为 `blocked_strict_warm_start_init_state_mismatch` 且不再重选(或 candidates 收敛为空);**并记录 released row 之后该 (run_id, forecast) 的走向——是否出现新 `*_retry_N` 提交、还是稳定停在 resume(复审 P2/P3 的实机 oracle)**。receipt(artifact 路径 + 关键字段)贴 PR/issue。若观测偏离预期,按"先读数再分支"处置并如实记录,严禁放宽判定。

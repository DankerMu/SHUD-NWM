# Tasks — chain-manual-retry-marker-freshness

## 1. Implementation

- [x] 1.1 新鲜性判别谓词单处实现（design D1："本 evidence 上存在活跃 manual-retry
      决策"——`decision == "manual_retry"` 或 `reason == "manual_retry_requested"`；
      判别不出 fail-safe 不采信）
- [x] 1.2 **铸造点 gate（生产主通道）**：`scheduler_candidate_manifest.
      _candidate_manual_retry_attempt` 以同一谓词判别——无活跃 manual-retry 决策时
      返回 None，manifest 不铸 `manual_retry_attempt`/`retry_attempt` 两键；manifest
      其余键逐字节不变
- [x] 1.3 **evidence 分支 gate（纵深）**：`_retry_attempt_from_basins` 的
      `state_evidence.manual_retry` 分支同谓词判别；direct 字段两分支逐字节不变
- [x] 1.4 丢弃 claim 时结构化 warning（design D4 措辞："no active manual-retry
      decision"，两写点同 schema）；措辞只说写点可知内容并**就此打住**——"the
      marker-claimed attempt is not used"，对 attempt targeting/下游结果一字不提
      （out-of-force resume 几何下什么都没 target、零提交；chain 写点在 direct
      字段被采信时同样为假；round-1 D1 + 终评 Note-1）
- [x] 1.5 `_manual_retry_scoped_cycle_execution` 现状保持 + 注释挂账（design D3
      重写后的理由口径）
- [x] 1.6 spec delta 场景与实现一致（含 D5 的 :467 交叉引用句）

## 2. Evidence Floor（红先行或变异咬红）

- [x] E1 (主判别，红先行，**双格**): 候选带无活跃决策的 marker claim
      （`new_attempt=1`）且 `<stage>_retry_1` 是绑定 `slurm_job_id` 的终态行——
      (a) decision **在** `_FORCE_TERMINAL_RESUBMIT_DECISIONS` 内（#1201 live 几何，
      如 `retry_missing_forecast_output`）→ 强制重投目标 `<stage>_retry_2` 且真实
      提交（无 `skipped_duplicate_submission`）；(b) decision **不在**集合内 →
      走 `_resume_cycle_stage`，与 markerless 孪生几何（同形去 marker，**孪生带
      `orchestration_run_id` 贴生产形状**）在**三项等价面**上同行为——重投决策
      （resubmit vs resume）/ attempt 派生值 / 是否真实提交；scoping 谓词不在等价
      面内（must-preserve 6 限定，防实现者为拉平去 gate scoping 的 evidence 臂）；
      多 stage 断言把 `chain_stage_execution` 的 `context.retry_attempt` 回写继承
      链算进预期；(b) 另加 caplog 钉——该格零提交，丢弃记录**不得**出现
      "falling through"，须含 1.4 的新措辞（对旧字符串红先行，round-1 D1）
- [x] E2 (must-preserve 1): 既有 `test_manual_retry_terminal_stage_submits_new_attempt_identity`
      原样通过；**另加判别力腿**：只经 evidence（不设 direct 字段）的 fresh marker
      （decision=="manual_retry"、new_attempt=4）→ 目标 `retry_4`——删 evidence 分支
      该腿必红（修复 fixture review 指出的既有腿双通道遮蔽问题）
- [x] E3 (must-preserve 2): 运维 API 显式 `basin.retry_attempt`/`manual_retry_attempt`
      仍被采信（不经判别器）两格
- [x] E4 (消费者审计钉): 陈旧 marker 几何下 `:581` `submission_attempt` 与
      `chain_manifests` 写进 runtime manifest 的 `submission_attempt` 都不再是
      marker 值（两处各一断言）
- [x] E5 (AC-4 非静默): 丢弃路径 caplog 断言（措辞 "no active manual-retry
      decision"、token `MANUAL_RETRY_ATTEMPT_CLAIM_IGNORED`，含 claim 值与实际
      decision；不出现 "stale"。措辞里"下游会怎样"的部分由 E1(b) 钉）
- [x] E6 (D3 边界钉): scoped 谓词对陈旧 marker 的现状行为钉 + scoped 谓词选择的
      job 集合喂 `_next_retry_attempt_for_stage` 的耦合现状钉
- [x] E7 (判别器三态参数化): fresh（采信）/ 无活跃决策（丢弃）/ 判别键缺失
      （fail-safe 丢弃）；丢弃格变异（删判别器）咬红
- [x] E8 (reason-only 形状): evidence 只带 `reason=="manual_retry_requested"` 无
      decision 键 → 采信
- [x] E9 (**scheduler→chain 接缝腿**，P0 修正核心): 生产投影路径（
      `_candidate_basin_manifest` 或 FakeProductionOrchestrator 级别）断言——陈旧
      marker 候选的 basin manifest **不带** `retry_attempt`/`manual_retry_attempt`
      两键；fresh marker 候选带且值正确；红先行（gate 前该腿红）
- [x] E10 (P1 可达性核查): `resume_after_completed_stage` 等更高优先 lane 抢占 +
      marker 并存的组合——探针核查可达性并写入测试注释；可达则以一格钉住其丢弃
      行为与日志措辞（"安全方向"限定：对非终态行是降级仍提交；对终态失败行收敛到
      markerless 等价——见 E1(b)，不得声称无害）
- [x] E11 (round-1 E1：在途变换抢占决策面): fresh manual-retry evidence
      （`allowed=True`、`new_attempt=4`）经 `_upgrade_retry_for_strict_warm_start_manifest`
      重写 decision/reason 后，`_candidate_manual_retry_attempt` 返回 None、manifest
      不铸两键——**钉现状为 intended**（markerless 等价优先于 attempt pin，must-preserve
      1 已限定）；docstring 记录审计面残余（evidence 仍带 `retry_policy.attempt=4`
      与 `manual_retry.new_attempt=4`，无非测试消费者）

## 3. Verification & Delivery

- [x] 3.1 命令全绿: `uv run pytest -q tests/test_orchestration_chain.py` ·
      `uv run pytest -q tests/test_production_scheduler.py` · `uv run ruff check .` ·
      `openspec validate chain-manual-retry-marker-freshness --strict --no-interactive`
      （round-1 修复后复跑：317 passed / 1648 passed / ruff 全绿 / spec valid）
- [x] 3.2 偏离记录 + AC-6 "live 复现不可得说明"（D0 口径：接缝以 E9 闭合，node-22
      非本家族 oracle）写入 PR body；PR `Closes #1201`

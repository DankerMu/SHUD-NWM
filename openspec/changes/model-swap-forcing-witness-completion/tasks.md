# Tasks — model-swap forcing witness completion

## 1. #1844 — quarantine retry 过见证闸（design D1/D2）

- [x] 1.1 `services/orchestrator/scheduler_candidates.py:613-624`：在 `state_decision = identity_quarantine`
      之后，仅当 `strict_warm_start is None` 时执行
      `state_decision = _strict_warm_start_forcing_witness_decision(candidate, raw_candidate_state, state_decision)`。
      补一句 why 注释，说明另一条车道已由 `:638-650` 覆盖。helper 本体与 breaker 臂不改。
- [x] 1.2 红测试（`tests/test_production_scheduler.py`，E2E 形状）：stale journal lineage + 本模型无 forcing 见证，
      断言以下三点：
      - 候选为 `blocked`，reason ∈ {`missing_forcing_package_uri`, `forcing_version_row_absent`}；
      - evidence 含 `forcing_provenance`；
      - `submitted_count == 0`。

      实现前先确认它在未改源码时为红。
- [x] 1.3 修改钉住测试 `test_completed_forecast_cycle_stale_journal_identity_is_quarantined_end_to_end`（`:11125`），
      按 design D2 补前提：
      - 用 `_seed_recorded_forcing_packages` 写出本模型 forcing 包；
      - **并**在 state 中加入绑定本模型身份的 `forcing_package_uri`（参照 `:46970-47000`）；
      - 从 `copyback_evidence` 删掉 `copyback_source_uri`（design D2 第 3 步）；
      - 测试签名加 `monkeypatch` 设 `OBJECT_STORE_ROOT`，种包先于构造 `ProductionScheduler`；
      - 保留 `submitted_count == 1` 与 quarantine retry 形状断言；
      - docstring 写明原 fixture 没有 forcing，恰是 #1844 的 bug。

      **不得删除或放宽任何原断言。**
- [x] 1.4 可排空性（D1）：断言 1.2 的 `blocked` 满足 `_decision_is_stable_missing_forcing_blocker`。
      **不**补修复策略调用。
- [x] 1.5 负向测试：`manual_retry_requested` 形状的候选不会被 quarantine 见证闸改写（钉住结构排斥）。
- [x] 1.6 （无实现工作；fixture review 第二轮删除：「strict 车道只探一次」没有行为可观察量，只是性能细节，不入 spec；
      D1 的车道限定由代码注释说明。）
- [x] 1.7 既有 breaker 与白名单测试原样全绿，包括：
      - `tests/test_warm_start_chaining.py` 的 `test_quarantine_rerun_decision_forces_resubmission_on_both_whitelists` 等；
      - `tests/test_scheduler_generation.py` 的 quarantine 系列；
      - `tests/test_scheduler_backfill.py` 的 breaker 系列。

## 2. #2254 — retry 序号以最后一个 suffix 为准（design D4）

- [x] 2.1 公开入口红测试（`tests/test_orchestration_chain.py`），按 D4 复现配方构造：
      - 用真实 `RetryService.handle_failed_job` 写出叠加行（`RetryConfig(max_retries>=3)`；每次调用后把新建的
        `pending` 行更新为终态 failed 并绑定 slurm id，否则 `find_existing_stage_job` 会优先选非终态行走 resume）；
      - 构造 `retry_missing_forecast_output` 证据，`context.retry_attempt=None`；
      - 走 `orchestrate_cycle`。

      断言三项可观察值：
      - forecast `pipeline_job_id` 为 `..._forecast_retry_4`；
      - reserve idempotency key 以 `:forecast:retry_4` 结尾；
      - gateway comment 含 `:forecast:retry_4`。

      **不 spy `_next_retry_attempt_for_stage`**。docstring 标注证据是构造的。
      实现前先确认为红（得到 `retry_2`）。
- [x] 2.2 `services/orchestrator/chain.py` `_next_retry_attempt_for_stage`：保留前缀过滤；逐行序号改为
      `retry_suffix_attempt(job_id)`（从 `services.orchestrator.retry_identity` 导入）；序号为 0 的行不计入。
- [x] 2.3 保留性测试，覆盖以下四点：
      - 扁平 `B_retry_1` + `B_retry_2` 得到 3；
      - malformed `B_retry_garbage`（带非零 `retry_count`）被跳过；
      - `context.retry_attempt` 显式值优先；
      - 他 stage 行不计入。

      既有 `tests/test_orchestration_chain.py:15920-15977` 的 snapshot-authority 测试原样绿。
- [x] 2.4 DB 与 FileJournal producer 回归：确认既有测试覆盖以下两处，并在 PR 中列出测试名；缺失时补。
      - `retry.py:465` 的叠加 id；
      - `file_orchestration_journal.py:10708-10730` 的 accepted-submit / 非 accepted-submit 两形。

## 3. #1846 — 裁决落档（design D3）

- [x] 3.1 `docs/runbooks/current-production-ops.md` hop 5（`:350` 起）补充：
      - (a) 依 #1846 裁决，回补是模型换代的**必经步骤**；调度器**不会**自动重入 forcing；这是已接受的成本。
      - (b) 它排空的 `blocked` 来源：#1843 strict-warm-start 见证，以及 #1844 quarantine 见证（本批）。
      - (c) 排空通道按车道写：strict warm-start 车道可用 `node22_backfill_forcing_for_model_ids.py`，
        也可用运维授权单 cycle 修复；非 strict 车道（#1844 quarantine）**只能**用回补脚本，
        原因是精确修复通道会以 `warm_state_missing` 拒绝。
      - (d) 裁决出处链接到本 change 归档后的 design.md 路径。
- [ ] 3.2 在 #1826（已关闭）下发评论：引用本 PR 与 D3，定性「每次换代须人工回补」为已接受成本。merge 后发。

## 4. 收尾

- [x] 4.1 在 #1845 下发评论：写入 design「Descoped: #1845」的核查结论，注明经用户裁决降为 follow-up，保持 open。
- [x] 4.2 越界 follow-up：`cycle_download_success_missing_raw_manifest` 不可达，经 issue-scribe 立单。
- [ ] 4.3 PR 偏离记录逐条交代：D2 测试修改（含删 `copyback_source_uri`）、#1845 降范围、D4 证据是构造的、
      fixture level 由 high 降为 expanded、breaker 臂不加代码（#1844 in-scope 写了两条出口）、fixture review 两轮 revise。

## Evidence Floor

- **EF-1** `uv run pytest -q tests/test_production_scheduler.py tests/test_scheduler_generation.py tests/test_warm_start_chaining.py tests/test_scheduler_backfill.py` 全绿（含 1.2–1.5）。
- **EF-2** `uv run pytest -q tests/test_orchestration_chain.py tests/test_file_orchestration_journal.py tests/test_retry.py tests/test_gateway_reconcile_master_transitions.py` 全绿（含 2.1、2.3）；integration marker 用例本地干净 SKIP。
- **EF-3** 红证据：1.2 与 2.1 在源码改动前为红，PR 贴出失败断言原文。
- **EF-4** 变异证据：分别回退 1.1、2.2 后，对应新测试各至少一条变红。在 scratch 副本执行并贴结果。
- **EF-5** `uv run ruff check .` 清洁。
- **EF-6** `openspec validate model-swap-forcing-witness-completion --strict --no-interactive` 通过。
- **EF-7** node-27 后端 oracle：
  - 先执行 `mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp`；
  - 在**不设** `NHMS_RUN_INTEGRATION` / `NHMS_INTEGRATION_DATABASE_URL` 的环境里跑
    `uv run pytest -q tests/test_production_scheduler.py tests/test_warm_start_chaining.py tests/test_orchestration_chain.py`；
  - 贴出通过数与 skip 数。

  **禁止**把 integration 用例指向 node-27 活主库。
- **EF-8** runbook 3.1 已提交；#1826 与 #1845 的评论 URL 贴在工作总结评论中。

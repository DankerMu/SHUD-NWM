# Tasks: repair-evidence-projection-composition

## 1. 红先行测试（pre-change source 上必红的腿先写先跑）

- [x] 1.1 **E1（#1460 主腿，红先行）**：同一候选两条不同 stage 失败血缘（forcing 失败→人工重试成功；随后 forecast 失败→人工重试成功），断言**两条**失败行都带 `repair_status="repaired"` / `repaired_by_job_id` / `active_blocker=False`；**并断言只有被修复 stage 的行被标注**（同 `job_type` 无关行不被过度认领，P2-3）；记录 pre-change 红证据。
- [x] 1.2 **E5（#1461 主腿，红先行）**：cycle-scope `download_source_cycle` 失败 + 成功 manual retry +
  候选自身成功 forecast，**且显式给出 `forecast_cycle`（status ∈ RAW_MANIFEST_READY_CYCLE_STATUSES ∪
  {"failed_download"}）+ 与 source/cycle 匹配的 `manifest_uri`**（否则 `_source_cycle_raw_manifest_binding`
  为 None、不产 repaired 证据——复用 `tests/test_orchestration_chain.py:6800-6900` 的
  `_source_cycle_retry_state`（`:6800-6833`）/`_failed_source_cycle_download_job`（`:6836`）/
  `_successful_source_cycle_retry_job`（`:6859`）/`_manual_retry_event`（`:6879-6900`）一族；并钉 `NHMS_ORCHESTRATOR_TERMINAL_STAGE` 未设，
  防 `restart_stage` 期望值漂移）。断言 `candidate_state_from_rows` **同时**给出 `repaired_stage_evidence`
  与 `completed_stage_evidence`/`restart_stage="parse"`；记录 pre-change 红证据。

## 2. 其余证据腿

- [x] 2.1 **E2**：E1 几何下 `repaired_stage_evidence` 仍由最新修复命名（`original_failed_job_id`/`repairing_retry_job_id` 指向 forecast 对），`restart_stage` 不回退。
- [x] 2.2 **E3**：E1 几何下游收敛——blocker 扫描活失败为空、`_restarted_stage_family` 覆盖两个被修复 stage、未被修复 stage 的行不被标注（P2-3 断言复用）。
- [x] 2.3 **E4**：单次修复既有场景零行为变化（现有 `_candidate_manual_stage_repair_state` 用例全绿，`tests/test_production_scheduler.py:6800,7021` 一带）。
- [x] 2.4 **E6**：E5 几何下 `_failed_stage(state)` 非 None，`_manual_retry_new_attempt(state, previous_attempt=0)` 与无 source-cycle 修复行对照组同值（5→1 收敛）。
- [x] 2.5 **E7**：被修复行 `failed` 与 `cancelled` 两状态在 E5/E6 断言上同值（REPAIRABLE parity）。
- [x] 2.6 **E8**：manual-stage 变体带 restart_stage 时既有语义回归——投影 + 清空五键（`pipeline_status/stage/failed_stage/error_code/error_message`）不变，completed-stage scan 被跳过（`completed_stage_evidence` 等于 repaired 证据 dict 而非 scan 产物）。
- [x] 2.7 **E9**：`_has_terminal_completion_stage_success` 守卫回归——已终态完成候选叠加 gap 形状 repaired 证据时不重新武装 restart marker。
- [x] 2.8 **E10**：manual-stage 终段 stage gap 形状（`_stage_after` 为 None）——`repaired_stage_evidence` 保留且 completed-stage 投影照常参与（受 E9 守卫约束）。
- [x] 2.9 **E11（must-preserve 8，#1308 pin gate 对照）**：gap 形状 state（扫描产 completed 证据带 `job_id`）上 `_state_completed_stage_evidence_names_job` 的判定与对照组（无 source-cycle 修复行、其余同几何）同值。
- [x] 2.10 **E12（must-preserve 8，决策通道对照）**：gap 形状下 `_completed_upstream_stage_retry_evidence` 的 `retry_after_completed_stage` 判定与对照组同判（含 `native_shud_resubmitted` 面）。
- [x] 2.11 **E13（must-preserve 9，`:822` 新几何钉，绝对期望值）**：`:822` 分支（候选零行、仅 repaired
  证据）+ gap + cycle 内 succeeded cohort 行（**必须落在 convert/forcing/forecast**——落 publish/
  state_save_qc 会触发 `_has_terminal_completion_stage_success` 抑制扫描，腿空跑）。断言：
  (a) 候选拿到 cohort 派生的 `restart_stage`/`completed_stage_evidence`；
  (b) `_state_retry_attempt(state, stage=<该 cohort stage>)` 等于文档化的 cohort 派生值。
  **不做**「与无 repaired 证据对照组同值」断言——两组 flat `retry_count` 天然不等
  （`chain_repository_state.py:829-831` 的 `retry_count_jobs` fallback 回填 source-cycle 行
  `retry_count`，对照组无 source-cycle 行为 0；非本 change 引入），腿内注释记录此口径。

## 3. 实现

- [x] 3.1 D1：`_candidate_manual_stage_repair_state` 去 break、first-write-wins 累积 `repaired_by_failed_job_id`/`repair_events`；`latest_repair`/`restart_stage` 语义不动。
- [x] 3.2 D2：`candidate_state_from_rows` completed-stage 分支独立化（`repaired_restart_projected` flag），既有两形状 byte-identical。
- [x] 3.3 复核并更新：`tests/test_production_scheduler.py:45492-45523` 可达性枚举注释（其第 3 条逐字引用 `chain_repository_state.py:822-827`/`:875-885`，D2 后行号与语义陈述均漂）+ `services/orchestrator/scheduler_state_manual_retry.py:226-251,363-399` 两处 docstring（「repaired-copy 无 `job_id`」陈述在 D2 后不再穷尽）。
- [x] 3.4 D1a：source-cycle 兄弟腿无同形 break 的核对结论落 PR body（AC-5 显式记录，不改 `chain_source_cycle.py`）。
- [x] 3.5 核对 `_terminal_evidence_matches_candidate` 对 scan 证据 vs job 行两来源的字段判等（design must-preserve 8 第三条）；同判则记录结论，不同判则在 PR body 显式记录处置。

## 4. 验证与证据

- [x] 4.1 `uv run pytest -q tests/test_production_scheduler.py tests/test_orchestration_chain.py` 全绿。
- [x] 4.2 `uv run ruff check .` 干净。
- [x] 4.3 `openspec validate repair-evidence-projection-composition --strict --no-interactive` 通过。
- [x] 4.4 变异证伪：分别恢复 break（E1 红）、恢复 elif（E5 红），各自应用→红→回退→绿。

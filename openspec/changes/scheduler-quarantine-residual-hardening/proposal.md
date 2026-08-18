# Proposal: scheduler-quarantine-residual-hardening

Issue: #1157 · Fixture level: expanded · Repair intensity: high（production state machine + retry/resume 语义 + 兄弟副本白名单）

## Why

§8.7 journal-side predecessor-identity quarantine filter（#1107 / PR #1154）已落地，但 quarantine **之后的收敛路径**留有一个已知不收敛类和三处配套缺口，四项共享同一根因语境（journal-recorded predecessor identity 的选取与重跑语义）：

1. **不收敛类（主项，accepted residual）**：env=false（`NHMS_REQUIRE_FORECAST_WARM_START` 非 true）时 `_exact_or_latest_usable_state`（`chain_forecast_state.py:662-665`）不向 provider 传 `cycle_id`/`lead_hours`，file provider 在 base key 上退化为 `min(state_id)` 确定性选择（`state_manager.py:1004-1008`）。multi-interval cadence（如 `0,6,12`）下，当 nominal predecessor cycle 未跑过而同 `valid_time=T` 存在更早 cycle 的 entry 时，每轮重跑合法地重选同一 wrong-suffix state → 永久 quarantine + 永久占用该 source 唯一的 oldest-first backfill 槽（`scheduler_discovery.py` `available_gaps[:1]`）。当前生产 cadence `0,12` 下该类**不可达**（每 base key 至多一条 entry）——这是改 cadence 前必须关掉的债，不是当下 outage。
2. **`retry_journal_predecessor_identity_mismatch` 不在两处 forced-resubmit 白名单**（`chain_forecast_orchestrator_cycle.py` `_FORCE_TERMINAL_RESUBMIT_DECISIONS` 与 `chain_runtime_utils.py` `force_replacement_decisions`，兄弟副本）：第二次及以后对同一 cycle+model 的 quarantine 重跑会退化为 idle resume（复用已 succeeded 的 forecast job），不是真实 resubmission。
3. **A/B 两面 recorded-id 取值口径不对称**：Wiring A（`scheduler_candidates.py` `_journal_predecessor_identity_quarantine`）读 `raw_candidate_state["hydro_run"]`，该行可能被 `chain_repository_state.py:754-762` 用 run manifest 回填 `init_state_id`，且 alias 集含裸 `state_id`；Wiring B 的 accessor `completed_pipeline_init_state_id`（`file_orchestration_journal.py:582`）是 journal-only、alias 仅 `init_state_id`/`initial_state_id`。同一行"journal 无 id、manifest 有 id"——A 判、B 弃判。**本 change 裁决为 journal-only（B 为准）**，与 §8.7 "报告 JOURNAL 记录了什么" 的既定语义一致（issue 推荐方向，实现时一次性拍板）。
4. **`terminal_completed_cycle` 无 quarantine 测试腿**：`_JOURNAL_IDENTITY_QUARANTINE_SKIP_REASONS` 三个 reason 中它是 env=true 下唯一可达且历来无 identity gate 的一个，形状可构造但未被 exercise。

## What Changes

分两层（收敛层 + 兜底层）+ 两项口径修复：

- **收敛层**：quarantine 重跑（basin `state_evidence.journal_predecessor_identity` 存在）时，env=false 的 exact 查找**先带 expected lineage 查询**（`cycle_id` = `cycle_id_for(source_id, T - required_lead_hours)`、`lead_hours` = evidence 中的 `required_lead_hours`），命中即选 expected-lineage entry；lineage 是**偏好不是过滤**——miss 时回退今日不带 lineage 的查找（db-free file 模式无 earlier-valid-time fallback，硬过滤会把重跑打成归零 cold start；该不收敛类交给 breaker，见 design D2）。非 quarantine 选取路径不传 lineage，逐字节不变。PG provider 保持 `del cycle_id, lead_hours` 不变（DB 模式无 §8.7 quarantine 回路，见 design D5）。
- **兜底层（breaker，R1 修订为 provenance 语义）**：当前 stale `recorded_init_state_id` 已被**至少一条带 §8.7 quarantine-rerun provenance 戳**（`journal_predecessor_quarantine_rerun_model_ids`，reservation 写侧落戳）的 terminal-success cohort **master** 行重复记录（原始缺陷 run 由 positive mismatch 本身见证；无戳的白名单 replacement 不计；per-model terminal 复制行不计；只读自 journal `_cycle_rows.pipeline_jobs`，新增只读 accessor，两侧共用）时：候选侧 quarantine 从 `retry` 降为 `blocked`（typed reason + recorded/expected token + 出现次数 + `retry_policy.manual_retry_required`）；discovery 侧 backfill 槽位仅当"该 cycle 的 gap 成因全部是 breaker-engaged 的 model"时才把它当 evidence-only（不占 `available_gaps[:1]`，cycle 仍报 gap——绝不 ADMIT；混合 cycle 仍占槽执行）。
- **白名单**：`retry_journal_predecessor_identity_mismatch` 加入两处 forced-resubmit 白名单（兄弟副本同步改，成员差异保持现状并测试锁定）。
- **A/B 统一（journal-only）**：Wiring A 的 identity 来源从 raw `hydro_run` 切换为 journal accessor `completed_pipeline_init_state_id`（经 `context.active_repository` getattr 约定；无 accessor → 弃判），manifest 回填值与裸 `state_id` alias 两侧一致弃判；breaker 计数同口径。
- **测试**：`terminal_completed_cycle` quarantine 腿；multi-interval cadence 一次重跑收敛；breaker 触发与槽位释放；第二次 quarantine 真实 resubmission；A/B parity；`0,12` cadence 逐字节不变回归。

## Impact

- Specs: `file-state-snapshot-index`（1 MODIFIED + 3 ADDED requirements）
- Code: `services/orchestrator/chain_forecast_state.py`、`chain_forecast_orchestrator_cycle.py`、`chain_runtime_utils.py`、`scheduler_candidates.py`、`scheduler_discovery.py`、`file_orchestration_journal.py`；`packages/common/state_manager.py` 仅在需要时微调（file provider 已支持过滤，预期零改动或注释级）
- Tests: `tests/test_scheduler_generation.py`、`tests/test_file_orchestration_journal.py`、`tests/test_warm_start_chaining.py`、`tests/test_production_scheduler.py`（+ 触及处的 `tests/test_state_manager.py`）
- 无 DB/display/Slurm 面变更 → 本地 pytest 即正确 oracle（CLAUDE.md oracle 路由）；node-27/22 无需 live receipt

## Non-goals

- env=true strict comparator 逻辑；`active_duplicate_pipeline`；§8/§8.6 predecessor-backfill emitter（#1152 已交付）；journal 写入/删除（§8.7 read-only invariant 必须保持）；cadence 配置本身的变更；PG provider 的 lineage 过滤（design D5 记录理由）；breaker 阈值的可配置化（YAGNI，常量 threshold=1 条带戳 rerun master——连同 mismatch 本身即"原始缺陷 + 1 次失败收敛"共 2 次记录）。

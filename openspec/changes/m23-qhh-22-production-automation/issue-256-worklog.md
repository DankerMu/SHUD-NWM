# Issue #256 动态动作流 + 进度工作日志

> 定制自 `.agents/skills/codex-codeagent-workflow`，按 #256 实际状态裁剪。
> 单一事实来源：本文件记录动作流定义 + 实时进度。每完成一步立即更新。

## 1. 目标与边界

- **Issue**: #256 M23-5 将 fresh canonical met 动态生成固定 SHUD 站点 forcing
- **PR**: #265 (`feat/issue-256-fixed-station-forcing`)
- **In scope**: forcing_producer、met store 写入、SHUD forcing 包物化、runtime manifest
- **Out of scope**: SHUD 执行、Slurm 提交、parse/publish、前端

## 2. 角色分工（双端 + 三方编排）

| 角色 | 职责 |
|------|------|
| **Claude Code**（本地编排） | 状态评估、修复计划、候选去重、证据综合、git/PR、CI 跟踪、合并门、文档维护 |
| **fix subagent**（派发） | Phase 6 代码/测试/配置修复（leaf，禁止嵌套委派） |
| **review subagent**（派发） | Phase 4.5/6.5/7 cross-review 与独立验证评审（leaf，read-only，禁止嵌套委派） |
| **远端 node-22** | 真实环境测试执行（PostgreSQL/TimescaleDB、Slurm、Docker） |

**执行约定（用户决策）**：
- 修复与评审**均派发 subagent**（Agent 工具），Claude Code 只编排 + 验证。
- **Phase 8 预授权自动合并**：终评干净 + CI 全绿后自动合并 PR #265。
- 每个 subagent 是 leaf 任务：不得再派发子 agent / 不得调用 workflow skill / 不得嵌套委派。

**双端验证回路**：本地改 → commit → push → 远端 `git pull` → 远端跑验证命令 → 结果回流 → 本地修。

## 3. 状态机入口（动态裁剪）

基于实际进度跳过已完成阶段，从 **Phase 6** 进入：

| 阶段 | 状态 | 说明 |
|------|------|------|
| Phase 0/0.5 OpenSpec fixture + 风险分级 | ✅ 完成 | `specs/fixed-station-forcing-production/spec.md` |
| Phase 1 实现 + 测试 | ✅ 完成 | producer.py / seed 脚本 / 测试 |
| Phase 3 PR | ✅ 完成 | PR #265 OPEN |
| Phase 4 R1 + 4.5 cross-review | ✅ 完成 | 11 候选，10 CONFIRMED + 1 PLAUSIBLE，pattern escalation |
| Phase 5 修复计划 | ✅ 完成 | 4 个 blocking 修复组 |
| **Phase 6 修复** | ⏳ 进行中 | `4dcaf2d` 已部分应用 group 1；剩余 group 1 尾 + 2/3/4 + CI 回归 |
| Phase 6.2 不变式审计 | ⬜ 待办 | |
| Phase 6.5 重 cross-review | ⬜ 待办 | 仅当无 ordinary-loop gate 触发 |
| Phase 7 独立终评 | ⬜ 待办 | cross-review 干净后 |
| Phase 8 证据 + 中文总结 + CI + 合并 | ⬜ 待办 | **预授权自动合并**（终评干净 + CI 全绿） |

## 4. 待修复清单（Phase 5 修复计划 + CI 回归）

### 来自 Phase 5（4 个 blocking 组）
- [ ] **G1 身份闭合与下游传播**：producer 接收 scheduler candidate 身份（basin_id/basin_version_id/river_network_version_id/canonical_product_id），校验仓库 basin version，持久化 lineage/manifest，scheduler 传递+校验 forcing_version_id 传播。补 mismatch/传播/manifest 字段测试。（`4dcaf2d` 已部分）
- [ ] **G2 ready 完整性与 partial/stale DB 闭合**：返回 already_done 前校验 component + station_timeseries 子行完整性；缺失/损坏需重生或阻断；fake 与 real 仓库同步该检查。
- [ ] **G3 path/schema 身份 + 站点契约 + 资源边界**：校验 basin_version_id/object-key 路径分量；包文件名安全+反碰撞；站点数/连续 SHUD index 契约；ForcingProducerConfig 资源上限。
- [ ] **G4 单位兼容 + 证据-状态记账**：ERA5 降水 mm/day 显式转换（按 timestep）；聚合执行 mutation proof 含 met/hydro 写入与 unknowns。

### 来自 CI（PR #265 红，13 failed / 4094 passed / 16m39s）

> 关键：3 个 Evidence-Floor 目标测试文件均**未**在失败列表 → #256 PR 边界过窄，击穿 13 个旁路测试。CI 全绿是自动合并前提。

**CI-A forcing_grid 站点未 seed（8 个，e2e/ifs/smoke）** —— 新 producer 要求 active forcing_grid 站点，fixture 未 seed → `ForcingProductionError: No active forcing_grid ... basin_v1`。按 spec "无固定站点阻断 forcing" 是**预期行为**，故修复方向＝给这些测试 fixture/harness 补 seed forcing_grid 站点：
- [ ] `tests/test_e2e.py::test_m1_forecast_cycle_data_flow_and_api_response`
- [ ] `tests/test_e2e.py::test_m2_analysis_warm_start_spliced_curve_and_selection_e2e`
- [ ] `tests/test_e2e_ifs.py::test_ifs_adapter_canonical_forcing_run_parse_e2e`
- [ ] `tests/test_e2e_ifs.py::test_ifs_06z_144h_manifest_context_and_forcing_limit`
- [ ] `tests/test_ifs_forecast_integration.py::test_ifs_forcing_uses_surface_pressure_shortwave_and_precip_conversion`
- [ ] `tests/test_ifs_forecast_integration.py::test_ifs_max_lead_hours_limits_forcing_range`
- [ ] `tests/test_ifs_forecast_integration.py::test_ifs_max_lead_filter_runs_before_completeness_validation`
- [ ] `tests/test_worker_chain_smoke.py::test_worker_chain_smoke_uses_real_schema_and_local_object_store`

**CI-B validate_met 由 ready 变 blocked（6 个）** —— `AssertionError: assert 'blocked' == 'ready'`。需先判断是预期新行为（改测试断言）还是回归（改代码），不可盲目改测试：
- [ ] `tests/test_production_met_validation.py::test_validate_met_default_lane_writes_required_evidence_and_redacts`
- [ ] `tests/test_production_met_validation.py::test_validate_met_manifest_bound_counts_actual_deterministic_sources`
- [ ] `tests/test_production_met_validation.py::test_validate_met_same_run_requires_force_and_force_replaces_bundle`
- [ ] `tests/test_production_met_validation.py::test_validate_met_disabled_sources_record_skipped_without_success`
- [ ] `tests/test_production_met_validation.py::test_validate_met_cached_only_policy_uses_cached_fixture`
- [ ] `tests/test_production_met_validation.py::test_argparse_validate_met_fallback`

> 注："SQL Migration Dry Run" job 失败实为同一全量 pytest 套件（该 job 跑全量测试），与 Unit Tests job 同根因，非独立 SQL 迁移问题。

### seed/迁移 forcing_proxy 范围裁定（用户质询，已调查）

被 reviewer 点名的三处 `forcing_proxy`（`seed_qhh_smoke_met_station.py:50`、`seed_demo.py:479`、`000005_met.sql:53`）——**裁定：不在 #256 改，也非缺陷型 follow-up**。理由：
- 语义正确：`forcing_grid` 专指来自真实 SHUD `qhh.tsd.forc` 的固定站点（带连续 index + filename），由生产 bootstrap `qhh_production_bootstrap.py:1377` 写入；`forcing_proxy` 是 demo/smoke 的通用代理站，无对应真实 SHUD 工程。改成 forcing_grid 需伪造 index/filename = 造假。
- 无自动化破绽：生产走 bootstrap（正确）；`make seed` 仅本地 demo；`test_seed.py` 不跑 producer；smoke 脚本零调用方。远端 624 passed、CI 全量转绿均未受影响。
- 唯一可选增强：若要 demo 端到端演示 forcing→SHUD，需造带真实 `qhh.tsd.forc` 的 demo 工程（独立 demo-completeness 任务，与 #256 无关）。

## 5. 动态循环规则（沿用原 workflow ordinary-loop gate）

- 不变式层闭合优先于逐条 finding 追逐
- 按 failure-class 分组修复
- 重 cross-review 每轮后只在无 gate 触发时继续
- 第五轮硬门 + 五轮后预算；轮计数器不跨 commit/CI-only/同 PR 兄弟面重置
- 第六轮前必须先持久化 Gate-Level PR Strategy Review
- 修复 fixture level：`high`/`broad-expanded` 时 PLAUSIBLE 仍阻断合并

## 6. 验证门（Evidence Floor #256）

```bash
# 远端 node-22 执行（真实 DB）
uv run pytest -q tests/test_forcing_producer.py tests/test_orchestration_chain.py tests/test_production_scheduler.py
uv run ruff check .
openspec validate m23-qhh-22-production-automation --strict --no-interactive
```

- forcing evidence sample 必含：station count / variable count / valid time range / units / package URI / manifest checksum
- CI 全绿（Unit Tests + SQL Migration Dry Run 由红转绿）
- 非目标证据：不得执行 SHUD / 提交 Slurm / 写 hydro_run / parse / publish / 改前端

## 7. 进度日志（倒序，最新在上）

### 派发计划（避开 producer.py 并行冲突，串行两波）

- **Agent A（先行）**：Phase 5 G1-G4 producer 核心硬化。写集 `workers/forcing_producer/producer.py`、`workers/forcing_producer/store.py`、`services/orchestrator/scheduler.py`、`tests/test_forcing_producer.py`、`tests/test_production_scheduler.py`。敲定站点数量/连续 SHUD index 契约。
- **Agent B（A 之后）**：CI 13 回归 fixture seeding（CI-A + CI-B 同根因）。写集 e2e/ifs/smoke 测试 fixture + `services/production_closure/met_validation.py` 的确定性 fixture + 可能的共享 seeding helper / conftest。依赖 A 的最终站点契约。

> 验证回路：本地 ruff（macOS 无 DB）→ commit → push → 远端全量 pytest（DB）→ 回流。

## 7. 进度日志（倒序，最新在上）

| 时间 | 阶段 | 动作 | 结果 |
|------|------|------|------|
| 2026-06-03 | Phase 6.6 | 派发 fix subagent：补 mm/s 守卫测试 + qc[units] 断言 + CLAUDE.md 验证命令 | 进行中 |
| 2026-06-03 | Phase 6 验证 | 远端目标验证集（8 文件 DB，含全部 13 回归）| **624 passed / 1 skipped / 0 failed**（8m31s）✅ |
| 2026-06-03 | Phase 4.5 验证门 | 裁决 invariant 的 B1（seed/迁移 forcing_proxy）：核 bootstrap 证据→**降级 REFUTED**（生产走 bootstrap 写 forcing_grid，被引用三处是 smoke/demo/列默认）；F5（断言弱化）核 8a34480 tests diff 无 assert 删改→REFUTED | 完成 |
| 2026-06-03 | Phase 6.5 | 6 reviewer pack 全部返回：**0 blocking**（B1 经验证降级）；收敛低成本改进 2 项（mm/s 守卫测试、qc[units]断言）；2 项 pre-existing 越界→follow-up | 完成 |
| 2026-06-03 | Phase 6.5 | 并行派发 6 reviewer pack（spec/correctness/integration/security-perf/test-evidence/invariant），high/broad-expanded | 完成 |
| 2026-06-03 | Phase 6 验证 | 远端 ruff ✅ / openspec validate ✅ / **CI SQL-Migration(全量) fail→PASS** ✅ / CI Unit Tests pending（合并门才查）/ 远端 8 文件目标验证运行中 | 进行中 |
| 2026-06-03 | Phase 6 验证 | push `1690078`+`8a34480`；远端 pull 到 8a34480 | 完成 |
| 2026-06-03 | Phase 6 | Agent B 完成并验证：4 文件补 forcing_grid 站点 seed（`8a34480`），本地 46 passed；发现 ifs 失败实为 stale `mm/step` 单位（非 mm/day），改回 `mm` 断言不变 | ✅ ruff 干净 |
| 2026-06-03 | Phase 6 | 派发 Agent B：13 CI 回归 fixture seeding | 完成 |
| 2026-06-03 | Phase 6 | Agent A 完成并验证：G4 mm/day 换算真 bug 修复（`1690078`）；G1/G2/G3 经核已落地于 4dcaf2d | ✅ ruff 干净，本地 forcing 46/46 + orchestration 107/107 |
| 2026-06-03 | Phase 6 基线 | 远端真实 DB 跑 3 个目标测试文件 | **576 passed / 0 failed**（8m16s）✅ 证实回归全在下游旁路 |
| 2026-06-03 | Phase 6 | 派发 Agent A：producer 核心 G1-G4 | 完成 |
| 2026-06-03 | Phase 6 调查 | 定位 13 CI 回归＝2 类同根因（forcing_grid 站点未 seed）；CI-B 经 met_validation.py:805 走 producer | 完成 |
| 2026-06-03 | Phase 6 入口 | 定制动作流；远端基线验证启动 | 完成 |

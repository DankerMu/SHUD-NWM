# Issue #266 动态动作流 + 进度工作日志

## 1. 目标与边界
- Issue: #266 fix(forcing): reconcile PRCP output unit across GFS/ERA5/IFS with SHUD mm/day contract
- OpenSpec change: `forcing-prcp-unit-reconciliation`
- Branch: `feat/issue-266-prcp-unit-reconciliation`
- In scope: `OUTPUT_UNITS["PRCP"]`、`_precip_to_timestep_factor`、三源单位统一、回归测试、design/tasks 文档、数据迁移说明
- Out of scope: 非 PRCP 变量、台站选择/identity/打包、SHUD/Slurm/parse/publish 行为

## 2. 角色分工
- Orchestrator: Claude Code（本机）— 状态评估、契约裁定、commit/push、远端验证、评审门、merge
- fix subagent: 实施 producer + 测试 + 文档（不 commit）
- review subagent: 并行 reviewer-pack（只读）
- node-22: 真实 DB 测试 oracle
- CI: 最终 merge gate
- 用户决策: merge 已预授权（无需人工 gate）

## 3. 状态机入口
| phase | 状态 | 说明 |
|-------|------|------|
| A 评估 | ✅ | 无 PR；change 已存在；锁定 Decision A |
| B 修复 | ⏳ | 派发 fix subagent |
| C 远端验证 | ⬜ | node-22 已连通，tree clean |
| D 交叉评审 | ⬜ | 6-pack（contract/schema 类） |
| E 验证门 | ⬜ | |
| F 综合循环 | ⬜ | |
| G 证据/总结/merge | ⬜ | |

## 关键裁定 — Decision A (target = mm/day)
权威证据（本仓库 SHUD consumer 契约）：
- `AutoSHUD/Rfunction/LDAS_UnitConvert.R`: 每个适配器输出列名 `Precip_mm.d`，NLDAS `*86400/diff_seconds` 注释 "to mm/day (SHUD)"，GLDAS/CMIP6 `*86400` "to mm/day"，CMFD `*24` "to mm/day (SHUD)"
- `SHUD/VersionUpdate.md:25`: Precipitation (mm/day)
- `AutoSHUD/SubScript/Step5x_Analysis.R:57`: 'Prcp (mm/day)'
- `DT_QE_PRCP 1440`（1 天）

→ 目标单位 = mm/day。IFS(`24/step`)正确；GFS(`1.0`)、ERA5(`step/24`, #256 引入)错误。

新 `_precip_to_timestep_factor` 语义：
- `mm/day` → 1.0
- `mm`(per-step) → `24/step_hours`（GFS default 0.0 强制有 step；IFS default `ifs_precip_step_hours`=3.0）
- 其他已接受单位 → raise（exhaustive mapping，满足 spec 新场景）
- `OUTPUT_UNITS["PRCP"]="mm/day"`

幅值影响：GFS ×24/step（3h→×8）；ERA5 透传(×1.0)；IFS 不变(16.0)。

## 4. 待修复清单
- [ ] G1 producer.py: OUTPUT_UNITS + _precip_to_timestep_factor + docstring
- [ ] G2 tests: GFS happy-path 单位/幅值、ERA5 3h/1h、unknown-step 改 per-step mm、mm/s 注释、IFS 整合校验
- [ ] G3 新增回归测试 GFS/ERA5/IFS @ 1h/3h/6h + 单位 exhaustive-mapping 契约测试
- [ ] G4 design.md 记录 verified unit + Decision A；tasks.md 勾选；数据迁移说明

## 6. 验证门 (Evidence Floor)
```
uv run pytest -q tests/test_forcing_producer.py tests/test_ifs_forecast_integration.py tests/test_production_met_validation.py
uv run ruff check .
openspec validate forcing-prcp-unit-reconciliation --strict --no-interactive
```

## 5. Phase 4.5 验证门裁定台账 (round 1)
| Finding | 来源 pack | 裁定 | 处置 |
|---|---|---|---|
| INV-1 缓存复用旧单位 (currency key 不含输出语义) | invariant | CONFIRMED in-scope BLOCKING | G5 |
| INV-2 无 discriminator/仅散文迁移 | invariant | CONFIRMED | G5 关闭+design note |
| INV-4 复用旧字节贴新单位标签 | invariant | CONFIRMED | G5 关闭 |
| INT-1 package_manifest_unit 重复 OUTPUT_UNITS 无一致性测试 | integration | CONFIRMED low | G6a |
| TEST-1 unknown-step 缺 forcing_versions=={}/upsert==0 | test | CONFIRMED low | G6b |
| INV-3 IFS canonical unit==mm 跨层契约缺测 | invariant/correctness | PLAUSIBLE | G6c |
| CORR-1 native_time_resolution≠实际累积窗口 | correctness | PLAUSIBLE 架构/既存 | OOS issue |
| INT-2 SHUD runtime 终端单位无守卫 | integration | PLAUSIBLE 可选 | OOS issue |
| SPEC-1 Press "Pa" vs SHUD doc "kPa"/无压力列 | spec | PLAUSIBLE 越界 | OOS issue |
| security-perf | security | NO FINDINGS | - |

## 7. 进度日志 (倒序)
| 时间 | 阶段 | 动作 | 结果 |
|------|------|------|------|
| T9 | G | follow-up 硬化 issue #272 + worklog CLEAN | ✅ |
| T8 | D/E | round-2 comprehensive 6-pack 复审 + 验证门 | ✅ CLEAN: INV-1 闭合; 0 in-scope CONFIRMED; 2 PLAUSIBLE→#272 |
| T7 | C | node-22 真实 DB round-2 @b514d72 | ✅ 116 passed |
| T6 | F | OOS issue #269/#270/#271 创建 | ✅ |
| T5 | F/B | round-2 修复(G5 缓存失效 + G6 收敛守卫) commit b514d72 push | ✅ 本机 116 passed |
| T4 | D/E | 6-pack 并行评审 + 验证门裁定 | 1 BLOCKING(INV-1) + 3 low + 3 OOS |
| T3 | C | node-22 真实 DB 目标套件 @9223092 | ✅ 96 passed (仅 cfgrib warning) |
| T2 | B/C | fix subagent 交付 + 自验 + commit 9223092 + push + PR #268 | ✅ 本机 96 passed, ruff clean |
| T1 | B | 派发 fix subagent（Decision A 全契约） | ✅ 连带修 met_validation 硬编码单位 |
| T0 | A | 评估状态 + 锁定 Decision A（权威单位证据） | ✅ 无 PR；node-22 OK；契约确定 |

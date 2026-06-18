# Issue #269 动态动作流 + 进度工作日志

## 1. 目标与边界
- Issue #269: GFS/IFS 降水率用静态 native_time_resolution 重建 → 边界帧错误
- 决策: **Option B 根因统一**（用户裁定）。GFS/IFS converter 用实际 step 在内部转 mm/day（镜像 ERA5），canonical unit mm→mm/day，producer 对三源统一透传 1.0，消除 producer 对 native_time_resolution 的降水依赖与整个 bug 类。
- Branch: feat/issue-269-precip-mmday-at-converter

## 关键证据
- ERA5 converter 已用实际 step 转 mm/day（converter.py:709 `delta*1000*24/step_hours`，unit mm/day），producer 透传——正确，无需改。
- GFS APCP（converter.py:645）返回原始 per-step mm（未除 step），unit "mm"。
- IFS（convert_ifs_precipitation_with_metadata, converter.py:809）返回 per-step mm（deltas_mm），已算 step_hours（:773），unit "mm"。
- 端到端最终幅值不变（IFS 仍 16.0：换算从 producer 移到 converter）。

## 状态机
| phase | 状态 |
|---|---|
| A 评估+决策 | ✅ Option B |
| B 修复 | ✅ |
| C 远端验证 | ✅ c96b4ed 264 passed / afcc3a7 115 passed |
| D 评审 | ✅ round-1 6-pack + round-2 6/4-pack |
| E 验证门 | ✅ round-2 CLEAN（全 LOW，零阻断） |
| F 循环 | ✅ 1 fix 轮 → CLEAN |
| G merge | ⏳ 等 CI Unit Tests 绿 → auto-merge |

## 进度日志
| 时间 | 阶段 | 动作 | 结果 |
|---|---|---|---|
| T6 | E/F | round-2 6-pack：全 LOW、迁移闭环+集成 NO FINDINGS = CLEAN；3 test LOW 补强 afcc3a7（本机 8 passed/远端 115 passed） | ✅ |
| T5 | B/C | MED-A inline 修复 c96b4ed（orchestrator unit 正交判据+SQL投影unit+4用例+design Migration）；远端 264 passed | ✅ |
| T4 | D | round-1 6-pack 全 diff 复审：5 lens 通过/仅 LOW，唯一实质 MED-A（旧 mm 迁移无自愈） | ✅ |
| T3 | F | F1 修复折叠入本 PR（不再 follow-up #275，用户裁定"直接修"）；缺失-version 规则 8fd0b6e；远端 260 passed | ✅ |
| T2 | D/E | 4-pack 评审+裁定：1 latent-HIGH、CORR-F1、杂项 | - |
| T1 | B/C | fix subagent 交付 + commit 7160a83 + PR #274 + 远端 150 passed | ✅ |
| T0 | A | ground-truth #269 + 用户裁定 Option B | ✅ |

## Phase 4.5 裁定 (round 1)
- INV-F1 旧 GFS canonical_ready 硬失败: CONFIRMED-latent → **折叠入本 PR**（6c9ef6b/8fd0b6e orchestrator demote 重转），不再外移 #275（用户："不 follow-up，发现问题直接修"）
- CORR-F1 GFS 首帧非零起报 ×24: 已修（converter 首帧用 forecast_hour 作 step），专测覆盖
- 杂项: CONVERSION_PARAMS["tp"] 标签 / GFS unit 断言 → 已处理

## Phase 4.5 裁定 (round 2，post-MED-A-fix)
- MED-A 旧 `mm` canonical 降水产物（常缺 converter_version）无自愈、卡死 failed_forcing: **CONFIRMED in-scope**（本 PR 契约变更引入）→ inline 修复 c96b4ed：orchestrator 加正交 unit-stale 判据（unit≠mm/day→demote→重转），缺失 unit/version 仍兜底容忍，闭合迁移环。
- round-2 全部 LOW（producer/detector 归一化口径差异不可触发→YAGNI；test 正向断言/边界用例→已补 afcc3a7）；迁移闭环、SQL/集成两条 NO FINDINGS → CLEAN，放行 merge。

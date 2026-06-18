# issue-341 worklog — 删 HydroMetPage 玩具页 + 清理 (legacy-display-page-retirement)

## 角色/oracle/门
同 #337–#340：实现=dispatched fix subagent（leaf 不 commit）；验证=本地四件套；合并门=审核 clean（用户授权不等 CI）；node-27 live receipt 推迟到 EPIC 收尾。

## 关键事实（已勘查）
- `pages/hydroMet/HydroMetPage.tsx`（1904 行）导出 `HydroMetPage` + `ReadyHydroMetContent`。**非测试消费者：无**（grep 仅 ListProduction.test + AppRoutes.test 引用 ReadyHydroMetContent；两处 popup 仅注释提及）。
- `pages/hydroMet/BasinSelector.tsx`：唯一生产消费者是 HydroMetPage（+ 自身 test）→ 删 HydroMetPage 后变孤儿 → 一并删 BasinSelector.tsx + BasinSelector.test.tsx。
- `__tests__/legacyPagesHarness.tsx`：L27-29 lazy import HydroMetPage + L48 `/hydro-met` 路由 → 去除（MeteorologyPage/其余路由保留）。
- `__tests__/AppRoutes.test.tsx`（8894 行）：L12 import ReadyHydroMetContent；hydro-met 玩具页 `it` 块**连续**分布 ~L1306（`routes /hydro-met …`）→ ~L2636（`stops /hydro-met downstream bootstrap …` 收尾），全在 `describe('App route state')`(L1260) 内。**保留**：L1261-1305（`/`、`/overview`、`/meteorology` 两 tab）、L2641 起（overview/forecast/basin/flood/monitoring/ops 单页测试）、L3871/3891（M26-4 popup 集成）、L8754 起 `legacy route redirect query contract`（含 `/hydro-met` **重定向**断言，保留）。
- NavBar.tsx：#337 已删（仅注释残留 "无 NavBar"）。
- honest-display 库保留：`bootstrap.ts`、`lib/hydroMet/{stationSeries,riverForecast,runtime,queryState}.ts`、`components/m11/ReturnPeriodSection.tsx`。

## 诚实展示覆盖迁移审计（防静默丢失 — RED LINE）
删玩具页页级测试前，逐一核对其覆盖的诚实展示不变量是否已在更低层（lib/popup）保有等价覆盖：
| 不变量 | 删前覆盖 | 删后归宿 | 状态 |
|---|---|---|---|
| station-series 不画假曲线 / reject-on-any-invalid / metadata/QC/unit/truncation | AppRoutes 页级 | `lib/hydroMet/__tests__/stationSeries.test.ts`(11) + `M11StationForcingPopup.test.tsx`(4 护栏)（#340 已建）| ✅ 已迁 |
| 后端消息 redaction（sanitizeHydroMetMessage） | AppRoutes 页级 | `pages/hydroMet/__tests__/bootstrap.test.ts`（保留）| ✅ 已覆 |
| **river q_down shorter-horizon 标注 / 不补齐 padded 值 / 无水位措辞** | **仅** AppRoutes 页级（1595/2614）；`validateHydroMetRiverForecastForChart` **无 lib 直测** | **必须新增** `lib/hydroMet/__tests__/riverForecast.test.ts` | ⚠️ 缺口 → 本 issue 补 |
| RP 三态 / productReady 门控 / ok:false 不画 | AppRoutes 页级 | `M11RiverForecastPopup.test.tsx`(#340) | ✅ 已迁 |

## 决策
- D-341-1：删 4 文件（HydroMetPage.tsx / ListProduction.test.tsx / BasinSelector.tsx / BasinSelector.test.tsx）。
- D-341-2：legacyPagesHarness 去 HydroMetPage import + `/hydro-met` 路由。
- D-341-3：AppRoutes.test.tsx 去 L12 import + 连续 hydro-met `it` 块（~1306→~2636），保留前述非 hydro-met 测试；删后由 tsc + ruff/eslint no-unused 清孤儿 fixture/import（不得误删仍被保留 popup 测试引用的共享 fixture）。
- D-341-4（覆盖迁移，强制）：新增 `lib/hydroMet/__tests__/riverForecast.test.ts` 直测金标准 `validateHydroMetRiverForecastForChart`：① shorter-horizon（actual<expected → ok:true、horizonShorter=true、label 含实际 144h + expected 168h、renderedPoints 长度=实际点数不 padding）；② q_down=discharge 无 水位/water-level/stage 措辞；③ 身份不符/非有限值 → ok:false 不出图。
- D-341-5：全仓 grep 确认无悬挂 import / 死路由 / 残留 hydro-met 玩具页 testid（保留重定向测试中的 `/hydro-met` 路径字面量）；build 无未用导出阻断。

## 验证矩阵（编排者独立复验）
| 检查 | 状态 |
|---|---|
| tsc | ✅ EXIT=0 |
| vitest | ✅ 620 passed / 33 files |
| check:api-types | ✅ EXIT=0 |
| build | ✅ built 3.19s |

**it-count 对账**：基线 673/34 → 删 ListProduction.test+BasinSelector.test(2 文件) + AppRoutes 38 个 hydro-met it 块（共删 58 测试）+ 补 riverForecast.test.ts(5 测试) → **620 passed / 33 files**（673−58+5=620；34−2+1=33，双向自洽）。
**清理校验**（编排者复跑）：4 文件 git rm ✓；`HydroMetPage/ReadyHydroMetContent` 仅余注释；`BasinSelector` grep 空；`/hydro-met` 字面量仅余 App.tsx 重定向 + AppRoutes 两个 redirect-contract describe；无残留玩具页 testid。

## 候选/裁决 ledger（3 路并行 review，1 轮 clean）
| reviewer | 裁决 |
|---|---|
| R-A 删除完整性 | 无过度删除 CONFIRMED 正向（删 38 块全 hydro-met 玩具页，单页/popup/重定向测试 + honest-display 库零误伤，无悬挂/孤儿）；2 minor：保留 popup fixture 仍用 `hydroMet*` 前缀（命名误导，非阻断）/ 任务描述措辞与实际略偏（非代码）|
| R-B 诚实展示覆盖迁移（RED LINE）| **None**：station-series（stationSeries.test 10 + 站点 popup 7）、river q_down（新 riverForecast.test 5 + river popup 5）、redaction（bootstrap.test）全保留等价低层覆盖；riverForecast.test 逐行核对忠实非 vacuous；列表/分页/过滤按 spec 随功能删除非回归 |
| R-C 测试完整性+spec | **None**：spec 3 Requirement 全满足，5 river 用例真断言无 skip/永真，清理无残留 testid/悬挂 import |

裁决：**clean**（0 in-scope CONFIRMED，0 merge-blocking PLAUSIBLE）。R-A 两 minor REFUTED 为非阻断——删除型 PR rename 工作中共享 fixture 属无谓 churn（KISS/YAGNI），描述措辞非代码缺陷。

## 动态阶段
- [x] Phase 0 评估 + 基线 + 覆盖迁移审计
- [x] Phase 1 fix subagent（删 4 文件 + 改 harness/AppRoutes + 补 riverForecast.test）
- [x] Phase 2 本地验证（四件套全绿 + it-count 对账 673−58+5=620）
- [ ] Phase 3 commit + PR
- [x] Phase 4-6 review（3 路并行，1 轮 clean）
- [x] Phase 7 独立复核（编排者复跑删除/悬挂/testid/字面量校验 + 直读 riverForecast.test 确认忠实 + R-B 逐行 lib 语义交叉核对）
- [x] Phase 8 merge（PR #348 merge 2f79baf，用户授权不等 CI；#341 已关闭，tasks.md §5 已勾）

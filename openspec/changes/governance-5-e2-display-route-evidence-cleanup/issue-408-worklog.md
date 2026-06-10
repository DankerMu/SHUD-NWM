# Issue #408 Worklog — [Governance-5 E2-N27-02] Migrate mocked Playwright specs off old-page assertions

## Roles / oracle
- Orchestrator: scope + 派发 fix subagent + node-27 verify + review。
- Verify oracle: node-27 `corepack pnpm run test:e2e:mocked-regression`。本地浏览器已装(chromium 1217)，可本地迭代。
- 依赖 #405✓ #407✓（merged）。

## Ground truth (orchestrator scope — 静态分析)
mocked-regression = `playwright.config.ts`（testIgnore preview-deeplink + live-display），跑真实 `pnpm dev`（App.tsx，旧路由全 LegacyRedirect→`/`，NavBar 已于 #337 删）。各 spec：
- **forecast.spec.ts** — goto `/forecast`，断言旧页 '预报工作台' → **stale**，迁移单图。
- **flood-alerts.spec.ts** — goto `/flood-alerts`，断言 '洪水预警' → **stale**，迁移。
- **meteorology.spec.ts** — goto `/meteorology`(×9)，断言 '气象数据产品' + **已删的 'Main navigation' NavBar** → **stale**，迁移。
- **hydro-met.spec.ts** — goto `/hydro-met`(→`/`)，断言 `hydro-met-product-panel` 等 testId（**全 src 已不存在**）→ **stale**，迁移。
- **m15-visual-conformance.spec.ts** — 断言 '洪水预警'/'气象数据产品'/'Main navigation' NavBar/旧页 evidence；**无像素 toHaveScreenshot**（DOM+布局几何断言，本地可绿）→ **stale 部分迁移**。
- **m11-routes.spec.ts** — 测 `/overview`/`/` 单图 '全国总览' + map-surface → **当前态，保留**。
- **monitoring.spec.ts** — `/monitoring`/`/ops` 活跃 MonitoringPage → **保留**。
- live-display.spec.ts / preview-deeplink.spec.ts — testIgnore，不在 mocked-regression。

## Phase state
- [x] Phase 0 scope（本文件）
- [ ] Phase 1 fix subagent 迁移
- [ ] node-27 verify (`test:e2e:mocked-regression`)
- [ ] review panel
- [ ] merge

## Phase 1 实现 (fix subagent) — 完成
- **删 4 个 stale 旧页 spec**：forecast.spec.ts(11)、flood-alerts.spec.ts(3)、hydro-met.spec.ts(1)、meteorology.spec.ts(4)——均测已 redirect 的旧产品页（预报工作台/洪水预警页/hydro-met product panel[testId 已不存在]/气象数据产品+已删 NavBar），单图无对应产品概念；其 redirect 落点行为迁入 m11-routes。
- **重写 m11-routes.spec.ts**（13→8 test）：单图合同——`/`+`/overview` redirect→`m11-fullscreen-map`/全国总览/浮层切换+图例 + 断言无 NavBar；图层切换 URL `layer=` + met-raster/met-stations honest 占位；`/flood-alerts`→`?layer=flood-return-period`+重现期图例(保 mocked 请求 identity)；`/basins/:id`→`?basinId` 钻取；缺失流域 honest；ops 直链按角色显隐。内部 plumbing(registered-overlays 等)留给 AppRoutes.test 快照，不写脆弱浏览器断言。
- **monitoring.spec.ts 最小迁移**（13 test）：去 NavBar 后 AppShell 每路由挂 `GET /api/v1/runtime/config` → 两 mock 补该路由(compute_control 角色，mocked-not-live)；strict-identity 默认 cycle 对不上 → URL 钉 `source=gfs&cycle=<fixture>`；`/ops` 标题 运维工作台→内部诊断；SPA fallback 去已删 NavBar 'NHMS' 断言改断言 RBAC gate。
- **playwright.config.ts**：m15-visual-conformance 加入 testIgnore（M15 多页视觉门，单图前提不成立，有独立 runner test:e2e:m15-visual 且 CI 已 `&& false` 暂停，属历史 pinned-SHA 证据，不属 M26 单图 mocked regression 合同）。
- 未删旧页组件/未改 App.tsx/未碰 live-display·preview-deeplink/未改 src 业务码。

本地复核（orchestrator 亲跑）：`pnpm run test:e2e:mocked-regression` → **19 passed / exit 0**（m11-routes + monitoring）；`tsc --noEmit` 0 error。

## Review (2-pack panel) + Phase 4.5 裁定
- **pack A（覆盖丢失审计）**：4 删 spec + m11-routes 13→8 + m15 testIgnore 全审 → **VERDICT: CLEAN**。
  - 4 删 spec 的 redirect 落点参数（/forecast→bare/、/segments→?segmentId 等 7 条）由 #409 已迁的 vitest 契约 `AppRoutes.test.tsx:4531-4627` 兜底；layer 落点（flood-return-period / met-stations）已迁入新 m11-routes:358-385；其余系旧页专属，随 #410 删页合理消失。
  - m11-routes 删的 5 个旧 test 全为多页 shell/NavBar 断言，M26 前提消失。
  - m15-visual-conformance：`toHaveScreenshot`=0（纯多页 DOM/几何断言），多页+NavBar 前提 M26 不成立；有独立 runner `test:e2e:m15-visual`(package.json:14) + CI 已手动化(m15-visual-evidence.yml)；#408 验收语显式允许「explicitly retained mocked legacy coverage」→ testIgnore + 保留文件 = faithful，非 scope-dodge。
- **pack B（假绿/忠实度审计）**：19 passed 非假绿 → **VERDICT: CLEAN**。
  - 19 test 全有实质断言（含负向 toHaveCount(0)/not.toHaveURL），无永真/空断言/吞错。
  - monitoring runtime/config mock = `page.route(**/api/v1/**).fulfill`，role payload 自洽(compute_control)，**mocked-not-live**，未命中即 throw 无 continue。
  - cycle 钉 fixture = 正当 fixture 对齐（`defaultMonitoringCycleTime()`=运行时整点 vs fixture 固定 cycle），controlled 路径有 `cycle_time===controlledCycleTime` 强校验背书，非掩盖 identity bug。
  - /ops「内部诊断」源码 `MonitoringPage.tsx:222` 真实渲染；SPA fallback 改 RBAC gate 对应真实行为；flood identity 经 `expect.poll` 校验 mocked `/api/v1/runs` 带 source=GFS。
  - testIgnore 正则精确（m11≠m15），无误伤。
- **Phase 4.5 裁定：clean**（0 in-scope CONFIRMED / 0 blocking PLAUSIBLE，全 REFUTED）→ CI 门 + merge。

## node-27 live receipt（含 host 依赖修复）
- 首跑 19 failed：全 `browserType.launch ... exitCode=127`——**非测试逻辑**，本地同套件 19 passed。
- 根因：node-27 主机缺系统库 `libgbm.so.1`（Mesa GBM），链式还缺 `libxcb-randr.so.0`；nwm 无 passwordless sudo。
- 修复（userspace，无 root）：`apt-get download libgbm1 libdrm2 libwayland-server0 libxcb-randr0` → `dpkg-deb -x` 解到 `~/pwdeps/root` → `LD_LIBRARY_PATH=~/pwdeps/root/usr/lib/x86_64-linux-gnu` → chrome/headless-shell ldd 缺库归零。
- ⚠️ 此 host 缺库**同样阻塞 node-27 的 LIVE 浏览器 lane**（live-display.spec.ts / #389 popup live-click）→ 已记为 out-of-scope infra 待办（见下）。
- **receipt**：node-27 HEAD `d07cb02`，`corepack pnpm run test:e2e:mocked-regression` → **19 passed (33.1s) / exit 0**（log `node-27:/tmp/verify-408-mocked2.log`）。
- 本地：`test:e2e:mocked-regression` 19 passed / `tsc --noEmit` 0 error。

## Phase state（终态）
- [x] Phase 0 scope / [x] Phase 1 迁移 / [x] node-27 verify(19 passed) / [x] review panel(2-pack CLEAN) / [x] merge gate 满足

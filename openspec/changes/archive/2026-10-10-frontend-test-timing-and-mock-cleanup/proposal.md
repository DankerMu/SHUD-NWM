# frontend-test-timing-and-mock-cleanup

## Why

`apps/frontend` 的测试代码里有五处问题（#2868），本 change 处理前四处；第五处（图层目录 mock 字面量在十余个文件间重复）在 fixture review 后事前拆出、另行立单（#2879）——它是唯一跨十余个门内 spec、需要逐字段对照表的项，不应拖住前两处零重试合并门的修复。两处是落在零重试合并门上的时序假设：(1) 控制条旋转用例旋转后只等 CSS（`min-width`），随后断言的却是 React 状态决定的 DOM 结构；(2) `c4DisplayLane.test.tsx` 的一组用例用真实时钟的 25ms 整轮预算，且只接受三个失败码，墙钟恰在阶段边界耗尽时得到的 `WHOLE_RUN_TIMEOUT` 不在其中。另外两处是清理：(3) `pnpm run test:e2e:m15-visual` 被默认配置的 `testIgnore` 排除，选中 0 个测试、exit 1，却被文档与手动 workflow 当作入口；(4) `monitoring.mocked.spec.ts` 用 `force` 点击切角色、有两个未用回调参数。

## What Changes

只动测试代码与测试配置，不改产品代码：

- (1) 旋转用例在 `setViewportSize(LANDSCAPE)` 之后、几何量测之前，加一条只在 React 横屏结构提交后才成立的 web-first 等待。
- (2) 该组 vitest 用例改用注入时钟；三个用例各断言一个确定的失败码（连同阶段）。
- (3) 新增一份只匹配 `m15-visual-conformance.spec.ts` 的 Playwright 配置，`test:e2e:m15-visual` 改用它；默认配置的 `testIgnore` 不动，注释同步。
- (4) `monitoring.mocked.spec.ts` 的 `selectRole` 改用 `e2e/support/setRole.ts`，清掉未用参数。

不退役 M15 视觉证据车道（owner 决定）；不碰 `.github/workflows/`、`docs/governance/`、产品 `src/`。design.md 省略（compact）。

## Triage

```text
Issue type: test-only cleanup + flake fix
Fixture level: compact
Upstream suggested level: compact (agree)
Blast radius: mocked Playwright 合并门与 vitest；改坏时测试变松（假绿）
Selected risk packs: Concurrency / shared state / ordering（(1)(2) 的时序）；Legacy compatibility（(3) 的 script 名与默认车道排除、(4) 的切角色结果）
Evidence floor: (1) repeat-each=20 三个移动 project 全绿且等待门有判别力；(2) 每个用例确定的失败码 + 变异；(3) --list ≥ 1 且通过、mocked 车道仍不含该 spec；完整 mocked 车道与治理 hard gate
```

## Impact

- 受影响文件：`apps/frontend/e2e/m11-control-bar-landscape.mobile.mocked.spec.ts`、`src/__tests__/c4DisplayLane.test.tsx`、`package.json`、新 `playwright.m15-visual.config.ts`、`playwright.config.ts`（注释，外加可能新增的命名导出，行为不变）、`e2e/monitoring.mocked.spec.ts`。
- 受影响规格：`mobile-regression-evidence`。

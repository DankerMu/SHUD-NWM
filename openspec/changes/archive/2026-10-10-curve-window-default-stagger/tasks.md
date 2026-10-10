# Tasks

- [x] 1.1 先量再改：选两个使地图区宽 W ≤ 990 的视口（如 900×1000、960×1000），实测地图区盒、两窗默认盒、河段窗关闭按钮的盒与被遮程度。两窗的关闭按钮 label 不同——河段窗是 `关闭面板`（`M11RiverForecastPanel.tsx`），站点窗是 `关闭弹窗`（`M11PopupChrome.tsx`）；一律经 `e2e/support/curveSheet.mocked.ts` 的 `curveWindowParts(page, kind).close` 取，不手写 label。两个按钮几何相同：位于 frame 顶 +13 到 +41、距右缘 16–44px（按类推算，以实测为准）。
- [x] 1.2 `defaultPosition` 的桌面分支（W ≥ 900）：仅当两窗默认横向范围重叠（窗宽 > 0.44·W）时错开默认 y，河段窗在上、站点窗在下，两窗默认 y 之差不小于关闭按钮盒的底边偏移（约 41px）加一点余量，使河段窗关闭按钮的整个盒落在站点窗盒之外。约束：
  - 默认位置只依赖窗的 kind 与地图区尺寸，不依赖另一窗是否打开（不引入跨窗状态）；所以重叠区间内单开一个窗的默认 y 也会变，这是预期。
  - 不重叠时（W ≥ 1091）两窗的默认 x / y 与改动前逐像素相同。阈值必须是“横向范围是否重叠”，不能写成 `width === 480` 或 `W < 1143`（窗宽直到 W ≈ 1143 都是 480）。
  - 移哪个窗、移多少由实现者按实测定：下移站点窗时，在桌面形态的最矮视口 900×500（地图区高约 416）里夹回视口后仍须满足“关闭按钮盒在站点窗盒之外”；上移河段窗时，不得压到地图区左上角的控件区（`OverviewPage.tsx` 的 `left-4 top-4` 一组）与站点头部。做不到就停下报告实测数字。
  - 只改默认位置的计算，不动窗宽、拖拽、夹回规则与 z-index。
- [x] 1.3 新 e2e spec（桌面 project，文件名带 `mocked`）：
  - (a) 两个 W ≤ 990 的视口（用例内断言实测地图区宽在 900–990），先开河段窗再开站点窗：对河段窗关闭按钮四角内缩 1px 的四个点与中心做命中测试，命中元素都属于 `m11-river-forecast-panel`；点它能关掉河段窗且站点窗（`m11-station-popup`）仍在。改前红。
  - (b) 同视口反向顺序（先站点后河段）：两窗的关闭按钮都可命中。改前即绿，是回归护栏。
  - (c) W 在 1091–1142 的一个视口（如 1100×900，窗宽仍是 480 但不重叠）：两窗默认 y 相等，x 等于改前实测字面量。
  - 视口高度要让两个定位钩子的目标点（地图区中央）在改前改后都不被已开的窗盖住——钩子遇到被遮会拒绝（`HOOK_POINT_OCCLUDED` / `STATION_HOOK_POINT_OCCLUDED`）。既有的 `openBothWindows`（`e2e/m11-curve-anchor-desktop.mocked.spec.ts`）固定在 1280×900 正是这个原因；选不出满足条件的视口时，写明第二个窗的替代开法并报告。1024×768 不用作证据视口（地图中心落在窗盒内，且该宽度下关闭按钮中心改前就没被盖）。

## 约定

- Risk pack「Legacy compatibility」selected。既有字面量（已逐条查过，y 错开不会移动其中任何一条，全部不改通过）：`e2e/m11-curve-window-desktop.mocked.spec.ts`（1280×900 两窗 frame 与 chart 盒；它同时杀掉“错开条件恒真”的变异）、`e2e/m11-curve-window-min-size.mocked.spec.ts`（1024×768 只钉尺寸与包含；其余视口不在区间）、`src/components/map/__tests__/M11DraggableCurveWindowMinWidth.test.tsx`（只钉 `left`：1280 → 89.6、1000 → 40、1920 → 185.6——所以不许改 x）、`M11DraggableCurveWindowMobileForm.test.tsx`、`e2e/m11-curve-anchor-desktop.mocked.spec.ts`、`e2e/m11-curve-sheet.mobile.mocked.spec.ts`（回到 1280×900 的相对比较）。
- 未选：Public API、Config、File IO、Schema、Auth、Concurrency、Resource limits、Error handling、Release / dependency、Documentation。
- Must preserve：移动形态（抽屉、单窗）完全不受影响；768–899 分支（居中 ±18、y 64 / 88）不动；桌面形态下开一个窗不关另一个；默认位置只在挂载与从移动形态切回桌面时计算的既有语义不变。
- Non-goals：768–899 分支里关闭按钮被盖（pre-existing；本 change 的规格场景限定在地图区宽 ≥ 900，裁定不做，写进 PR 的范围外观察）；在不重叠的宽度开窗后把窗口缩窄进重叠区间（resize 只夹回现有位置，不重新错开）；D16 宽度；窗口重叠时的自动避让 / 层叠管理；真机。
- Evidence floor：
  - 改动前后同一张实测表：900×1000、960×1000、1024×768、1100×900、1280×900、900×500——地图区盒、两窗默认盒、河段窗关闭按钮盒、是否在站点窗盒之外。
  - 先红后绿：(a) 对未改的 `src/` 为红；(b)(c) 改前即绿（如实报告）；改后全绿。
  - 变异（按 sha256 还原）：去掉错开 -> (a) 红；错开条件改恒真 -> `m11-curve-window-desktop` 的 1280×900 字面量红；阈值放宽到 W < 1143 -> (c) 红；错开量减到 24px -> (a) 的四角点红；上下对调（站点在上）-> (a) 红。
  - `cd apps/frontend && pnpm test && pnpm exec tsc --noEmit && pnpm check:types && pnpm build`；新 spec 的 strict tsc；`pnpm exec playwright test` 跑新 spec 与上面列的既有桌面窗 spec 和 `m11-curve-sheet.mobile`，再跑完整 mocked 车道；新 spec `--repeat-each=5` 无 flaky。
  - `openspec validate curve-window-default-stagger --strict --no-interactive`。
  - node-27（编排者执行，PR 构建对 live API）：桌面 oracle exit 0；960×1000 下打开两窗后河段窗关闭按钮可命中（探针；若 live 上第二个窗开不出来则如实记为未覆盖）；1280×900 下两窗默认盒与改动前相同。

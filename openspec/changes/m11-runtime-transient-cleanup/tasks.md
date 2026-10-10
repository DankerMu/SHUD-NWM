# Tasks

- [x] 1.1 `useM11SelectedAnchorCamera`：一个 ref 存 `requestAnimationFrame` 句柄；effect 清理（键变化、含变空，以及卸载，都走这同一个清理）取消尚未执行的帧。现码靠“先记键”（`lastPanKeyRef`）挡 StrictMode 的 effect 重放——加了清理取消后，重放会命中“键没变”而不再排帧，挂载即非空键时零平移。所以清理取消帧的同时必须把 `lastPanKeyRef` 复位为空，或者去掉记键、让 effect 变成“键非空即排帧，清理即取消”；二选一由实现者定，判据是 (s6) 在 1.2 的新夹具下不改用例体通过。hook 头注释里“恰一次 `easeTo`”的口径保持为真，按需补一句取消语义。
- [x] 1.2 `src/components/map/__tests__/M11MapLibreSurfaceSheetAutoPan.test.tsx`：现有 `beforeEach` 把 `requestAnimationFrame` 桩成 `frames.push(callback)`（返回数组长度，不是稳定 id），没有 `cancelAnimationFrame` 桩——取消在这个夹具里是空操作，正确实现绿不了、错误实现红不了。**允许且必须改 `beforeEach` 夹具**：rAF 返回唯一 id，新增按 id 删除待执行回调的 `cancelAnimationFrame` 桩，`flushFrames` 语义不变；既有用例体 (s1)-(s10) 一个字不动。新增两条：(a) 同一帧内 `autoPan` 依次 K -> null -> K 后 `flushFrames()`，`fake.names()` 恰为 `['resize', 'easeTo']`；(b) 有待执行帧时 `unmount()`，断言取消本身——待执行队列为空（或取消桩以该帧句柄被调用）。不要把 (b) 写成“卸载后相机零调用”：卸载后 `mapRef` 已被置空，残留帧回调本来就提前返回，那个断言在未改代码上恒绿。
- [x] 2.1 `M11DraggableCurveWindow`：加一个只在卸载时运行的清理，调用 `endDrag`（无参数、引用稳定、只读 ref；没有拖拽时是空操作）。
- [x] 2.2 `src/components/map/__tests__/M11DraggableCurveWindowMobileForm.test.tsx`（或同目录新文件）新增：桌面形态在抓手上 `pointerdown` 开始拖拽后 `unmount()`，`removeEventListener` 的 spy 记录到 `pointermove` / `pointerup` / `pointercancel` 三个监听均以注册时的同一函数引用被移除；另一条：未拖拽时 `unmount()` 不抛错。
- [x] 3.1 `OverviewPage`：`yielded` 的实际路径是 `OverviewPage` -> `M11FullscreenMap` -> `M11BottomControlBarRegion` -> `M11BottomControlBar`，后者用同一个布尔既隐藏控制条又传给时间轴。做法写死为：只在 `OverviewPage.tsx` 内改——`useEffect` 把 `chromeYielded` 回写到一个 state，**底部控制条区域**收到的 `yielded` = `该 state && chromeYielded`（隐藏与暂停一起延后一个 effect 周期）；启动器列的隐藏与展开面板复位（`collapse`）仍读原来的 `chromeYielded`。为此把 `M11FullscreenMap` 现在的一个让位 prop 拆成两个（都在 `OverviewPage.tsx` 内），不碰 `M11BottomControlBar.tsx` / `M11Controls.tsx`，不改时间轴 `yielded` prop 的语义。可见后果，接受并写进 PR：崩溃路径上控制条不再闪隐；非离散路径（如经 `matchMedia` 进入移动形态）控制条比启动器列晚一个提交隐藏。时间轴内的暂停仍是单向 effect，不新增恢复播放路径。
- [x] 3.2 `src/pages/__tests__/OverviewPageSheetYieldsChrome.test.tsx` 移动形态块新增一条：播放中、曲线崩溃开关打开时点河段 -> 兜底块出现 -> 播放按钮标签仍为「暂停时间轴」，假计时器推进后有效时刻继续前进；接着清掉崩溃开关、点「重试」成功 -> 抽屉出现且已暂停。注意假计时器下不能用 `findBy*` / `waitFor`（RTL 靠 `jest` 全局识别假计时器，vitest 下会挂起）：兜底块在点击的 `act` 内已同步出现，用同步的 `getByTestId`。
- [x] 3.3 止损：`OverviewPageSheetYieldsChrome.test.tsx` 里“点击后同步断言已暂停”的既有用例、`src/pages/m11/__tests__/M11TimelineYielded.test.tsx` 与 e2e `m11-sheet-yields-chrome.mobile.mocked.spec.ts` 必须不改通过。若 3.1 让它们变红或 `--repeat-each=5` 下 flaky，或发现必须改 `M11BottomControlBar` / `M11Controls` / `RegionErrorBoundary` 才能做成：撤回 3.1 / 3.2 的全部改动，只交付 1.x 与 2.x，在最终报告里写明原因——不要改这些断言，也不要换成 rAF / 定时器延后。

## 约定

- Risk pack「Concurrency / shared state / ordering」selected：帧与提交的先后次序 -> 1.2 (a)(b)、3.2 钉住；StrictMode 重放 -> (s6)。
- Risk pack「Error handling」selected：面板首次渲染崩溃是故障路径 -> 3.2；`RegionErrorBoundary` 的接口与行为不动。
- 未选：Public API、Config、File IO、Schema、Auth、Resource limits、Legacy compatibility、Release / dependency、Documentation。
- Must preserve（全部不改通过）：vitest `M11MapLibreSurfaceSheetAutoPan` (s1)-(s10)（用例体不动，夹具按 1.2 改）、`OverviewPageSheetAutoPan` (p1)-(p10)、`OverviewPageSheetYieldsChrome` 全部既有用例、`src/pages/m11/__tests__/M11TimelineYielded.test.tsx`、`M11DraggableCurveWindowMobileForm` (m)-(q)、`M11DraggableCurveWindowMinWidth`、`RegionErrorBoundary*`；e2e `m11-sheet-auto-pan.mobile.mocked.spec.ts`、`m11-sheet-yields-chrome.mobile.mocked.spec.ts`、`m11-curve-anchor-desktop.mocked.spec.ts`、`m11-curve-window-desktop*`、`m11-curve-window-default-stagger.mocked.spec.ts`、`m11-curve-window-min-size*`、`m11-overlay-collision.mocked.spec.ts`。桌面形态：开窗不平移、播放中开窗继续播放、拖拽 / 默认位置 / 夹回视口不变。
- Non-goals：关窗时取消进行中的平移动画；兜底后自动续播；瞬时让位里的展开面板复位；`M11Timeline` 的 `playing` 受控化；e2e 的新增或改写。
- Evidence floor：
  - 先红后绿（未改的 `src/`）：在新夹具下 1.2 (a) 红（两次 `easeTo`）、(b) 红（帧未被取消）；2.2 第一条红；3.2 红。
  - 变异（按 sha256 还原）：1.1 去掉清理里的取消 -> 1.2 (a)(b) 红；1.1 保留取消但不复位 / 不去掉记键 -> (s6) 红（这条证明新夹具对 StrictMode 重放有判别力）；2.1 去掉卸载清理 -> 2.2 红；3.1 把传给控制条的信号改回 `chromeYielded` -> 3.2 红；3.1 把传给控制条的信号写死 `false` -> 既有“打开即暂停”用例红。
  - `cd apps/frontend && pnpm exec vitest run src/components/map/__tests__/M11MapLibreSurfaceSheetAutoPan.test.tsx src/components/map/__tests__/M11DraggableCurveWindowMobileForm.test.tsx src/pages/__tests__/OverviewPageSheetYieldsChrome.test.tsx src/pages/__tests__/OverviewPageSheetAutoPan.test.tsx && pnpm test && pnpm typecheck && pnpm build`。
  - mocked Playwright：Must preserve 点名的 e2e spec 全绿；`m11-sheet-auto-pan.mobile` 与 `m11-sheet-yields-chrome.mobile` 加 `--repeat-each=5 --workers=1` 无 flaky；完整 mocked 车道。
  - `openspec validate m11-runtime-transient-cleanup --strict --no-interactive`。
  - node-27（编排者执行，PR 构建对 live API，verify checkout + 预览端口）：证据脚本的 `mobile-portrait` / `mobile-landscape` 预设（含抽屉自动平移后的锚点）与桌面 oracle 均 exit 0。崩溃路径与卸载路径在 live 上不可达，只由 vitest 覆盖，如实记录。

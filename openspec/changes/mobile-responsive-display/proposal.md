# Proposal: mobile-responsive-display

## Why

公网展示入口 `https://test.nwm.ac.cn` 的单图页 `/` 只按桌面视口设计。2026-10-08 在 390×664（手机竖屏）、750×342（手机横屏）、768×1024（平板竖屏）三档视口实拍生产站，加上用户真机截图，确认：

- 图层面板、底图切换、图例三个浮层在竖屏互相叠压，图例（高 363px）盖住 580px 高地图区的大半；横屏时图例 `y=-141` 顶出视口。
- 点击河段 / 气象代站后曲线窗能打开，但窗口是“近全宽 + 16:9”（390px 宽时约 206px 高），标题和选择器占满后河段曲线只剩约 30px 细带，气象代站曲线完全不可见。
- 底部控制条的播放速度与时间轴滑块在竖屏被裁到屏幕外；真机上整条控制条被浏览器工具栏切掉一半（外壳用 `100vh`）。
- 头部固定 84px、标题在 390px 折成两行；`/` 上 14 个控件小于 44px。

页面级布局本身没有横向溢出，`/ops` 单列堆叠基本可用，所以工作量集中在 `/` 的地图浮层与曲线窗。

## What Changes

- 新增**移动形态**（视口宽 < 768px 或高 < 500px）作为全站唯一的移动判据，提供 CSS 变体与一个 React hook；桌面形态（宽 ≥ 768 且高 ≥ 500）的几何不变，唯一例外是下一条的曲线窗最小宽度。
- 桌面形态曲线窗加最小宽度 30rem：只改变视口宽 768–1142px 时曲线窗的默认尺寸（平板竖屏由约 322×181 变为 480×270），≥ 1143px 不变。
- 外壳改用动态视口高度并处理安全区；头部在移动形态压缩为 48px 单行。
- `/` 的图层面板、底图切换、图例在移动形态收成图标按钮，同一时刻只展开一个，展开面板限高并内部滚动；底部控制条竖屏改两行、矮视口横屏保持单行；隐藏地图缩放按钮；浮动提示、状态条、运维入口与区域错误兜底重新定位。
- 曲线窗在移动形态改为抽屉：竖屏贴底、横屏贴右；禁用拖拽；同一时刻只保留一个（河段 / 气象代站互相替换）；抽屉打开时隐藏控制条与图标按钮、收起已展开的面板、暂停时间轴播放，并自动平移地图把选中的河段 / 站点移到未被抽屉遮住的区域；图表高度由容器驱动，触屏用双指捏合 + 单指拖动缩放时间轴。
- 移动形态下 `/` 上所有可点控件不小于 44×44px；`/`、`/ops`、`/monitoring` 上可聚焦的表单控件字号不小于 16px。
- `/ops`、`/monitoring` 做可读可滚动兜底；`/system/model-assets` 只保证不撑破页面。
- mocked-regression Playwright 车道新增三个 Chromium 触屏 project（390×664、750×342、844×390）作为合并门，并新建“在 mocked 车道打开河段窗与气象代站窗”的测试夹具（含一个独立的、测试门控的气象代站定位钩子，以及一个测试门控的区域崩溃开关）；node-27 实拍脚本支持设备预设；epic 收尾设真机确认清单。
- 更新 `docs/spec/06B_frontend_ui_design_spec.md` §8：该文档现写“< 1280px 不推荐使用”，本 change 把移动形态定为受支持形态。
- **BREAKING（仅限窄窗）**：宽度 < 768px 或高度 < 500px 的**桌面浏览器窗口**也会进入移动形态（此前 520px 宽仍是桌面布局）。`map-first-layout-conformance` 的 520px 断言与 `map-feature-popups` 的“双窗并存可拖拽”因此收窄到桌面形态。

## Capabilities

### New Capabilities

- `mobile-viewport-shell`：移动形态判据、动态视口高度与安全区、压缩头部、触控尺寸与字号下限、桌面形态不变量。
- `mobile-map-overlay-layout`：`/` 上浮层的图标化收纳与互斥展开、图例限高滚动、底部控制条两行、缩放按钮隐藏、状态提示定位。
- `mobile-curve-sheet`：河段 / 气象代站曲线窗的抽屉形态、单窗策略、抽屉打开时让位与自动平移、图表最小可读高度、触屏时间轴缩放。
- `mobile-ops-fallback`：`/ops`、`/monitoring`、`/system/model-assets` 在移动形态的可读性兜底。
- `mobile-regression-evidence`：移动视口的自动化合并门、node-27 实拍、真机确认清单。

### Modified Capabilities

- `map-feature-popups`：`River and station curve windows can coexist and move`、`点击河段要素弹出 q_down 预报曲线`、`点击代站弹出当前 station-series forcing 曲线` 三条的“可拖拽”契约收窄到桌面形态；新增桌面形态曲线窗最小宽度。
- `map-first-layout-conformance`：`Map-first layout conformance` 的 84px 头部、64px 控制条与五宽度 attribution 断言收窄到桌面形态；520px 宽度改归移动形态。
- `pipeline-monitoring-frontend`：`Responsive Layout` 的宽度分段收窄到桌面形态；移动形态改为单列兜底，不要求手风琴 / tab / 抽屉。
- `frontend-river-click-live-evidence`：新增一条要求——气象代站定位用独立的门控全局；既有 river-click 钩子要求只改一处措辞，把“exactly”限定到它的三个方法（行为不变）。

未改文字但改变适用方式的既有要求：`map-layer-timeline-controls` 的“Legends reflect active hydrologic layer”与 `precipitation-raster-overlay` 的图例场景，在移动形态由“启动器常驻 + 展开后的图例面板内容”满足（写入 `mobile-map-overlay-layout`）。

## Impact

- 代码：仅 `apps/frontend/`（`src/index.css`、`src/components/layout/`、`src/components/map/`、`src/pages/`、`src/components/monitoring/`、`src/components/charts/`、`e2e/`、`playwright.config.ts`）与 `scripts/node27_display_v2_browser_evidence.mjs`、`docs/spec/06B_frontend_ui_design_spec.md` §8、两份新 runbook（移动实拍、真机确认清单）。
- 无后端、无 API、无 DB、无 OpenAPI 变更；`src/api/types.ts` 不动。
- CI：mocked-regression 车道用例数增加（新增三个移动 project 只跑移动专用 spec；既有 spec 仍只在桌面 project 跑一次；不新增浏览器安装）。
- 验收 oracle：本地 vitest + mocked Playwright 是合并门；涉及展示端的每个实现 PR 仍需 node-27 live receipt；真机确认只在 epic 收尾，不卡单个 PR。

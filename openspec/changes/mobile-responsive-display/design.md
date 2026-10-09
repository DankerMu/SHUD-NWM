# Design: mobile-responsive-display

## Context

`/` 是单一全屏地图页：`AppShell`（84px `SiteHeader` + `main`）内，`OverviewPage` 在地图区上用绝对定位叠放：

- 图层面板 `left-4 top-4`、底图切换 `right-16 top-4`、运维入口、图例 `bottom-[7.5rem] right-4`（`M11FloatingControls.tsx`）。
- 底部控制条 `bottom-10`（`M11BottomControlBar.tsx`），高度与 `m11VisualTokens.timelineHeight`（64px）类型级绑定。
- 浮动提示 `M11FloatingNotice`：`left-1/2 bottom-[11.5rem]`，由代站状态、加载中、空数据、数据异常、降水五条**互斥**提示共用（`OverviewPage.tsx` 的一条条件链）。
- 地图状态条 `M11MapStatusOverlays`：最多四条（流域边界不可用、地图不可用、选中河段不可用、地图源错误），顶部居中，各自写死 `top-20` / `top-32` / `top-44`，宽至 `100% - 2.5rem`（`m11MapRuntime.tsx`），可同时出现。
- 区域错误兜底 `RegionErrorBoundary`：地图上四个边界——“地图控件”（图层面板、底图切换、运维入口共用一个）、控制条、图例、曲线面板——className 写死所属区域的桌面偏移；组件没有向上报告错误的 prop。
- MapLibre `NavigationControl` 在 `top-right`。
- 开发用角色切换器：`AppShell` 在 `main` 的 `right-4 top-4`，仅当构建开启 role override 时渲染（mocked 测试车道恒开启，生产构建不开启）。
- 点击要素后的两个可拖拽曲线窗：`M11DraggableCurveWindow` 承载 `M11RiverForecastPanel`（头部与关闭按钮内联）与 `M11StationForcingPopup`（头部用共享的 `M11PopupHeader`）。曲线窗只能由地图要素点击打开，没有 URL 入口。

2026-10-08 实测（生产站，Chromium 模拟视口 + 用户真机截图）：

| 视口 | 现象 |
|---|---|
| 390×664 | 图层面板 x16–197、底图切换 x94–326、图例 x150–374 高 363 相互叠压；控制条 358px 宽放不下时间轴；头部标题折两行 |
| 750×342 | 图例 y=-141 顶出视口；图层面板高 242 几乎占满 258 高的地图区 |
| 768×1024 | 桌面布局可用，浮层不叠压 |
| 真机竖屏 | 曲线窗约 206px 高，河段曲线约 30px、气象代站曲线不可见；控制条被浏览器工具栏切掉一半 |

已有事实：`index.html` 的 viewport meta 存在；河段命中层线宽 16px；选中态有独立的 halo/line 图层；两张曲线图都用 ECharts `inside` dataZoom（捏合缩放恒可用；河段图的 `moveOnMouseMove` 现为 `false`，单指不能平移）；宽表格都已有 `overflow-x-auto` 外层；`AppShell` 根与 `main` 都是 `overflow-hidden`，所以 `document.scrollWidth == innerWidth` 恒真，不能当溢出 oracle。

测试设施现状：mocked-regression 车道没有任何一条用例打开过曲线窗——mock 的流域与有效时刻为空、没有瓦片夹具；唯一的定位钩子（`__NHMS_E2E_HOOKS__` 门控的 `window.__nhmsRiverClickEvidence`，既有规格规定它恰有三个方法）只能定位河段，调用方须给出 bbox、anchor 与四个身份字段，它会先把相机 fit 到该 bbox；气象代站没有定位手段。真实浏览器里也没有让某个区域渲染抛错的手段（现有区域边界测试靠 vitest 的模块 mock）。播放状态是时间轴组件的本地 state。

## Goals / Non-Goals

**Goals**

- `/` 在手机竖屏、手机横屏可用：地图默认无浮层遮挡，所有控件可达，点击河段 / 气象代站后曲线完整可读。
- 平板竖屏（走桌面形态）上曲线窗可读。
- `/ops`、`/monitoring` 在手机上可读、可滚动；`/system/model-assets` 不出现内容被裁的横向溢出。
- 桌面形态的布局几何不变（D2 列出的例外除外）。
- 移动视口进入自动化合并门。

**Non-Goals**

- 不做 `/ops`、`/monitoring`、`/system/model-assets` 的移动端专门交互设计（卡片化表格、移动筛选抽屉、阶段卡手风琴等）。
- 不改任何后端、API、OpenAPI、数据取数身份或 URL query 语义。
- 不做 PWA、离线、原生壳、深色模式。
- 平板竖屏（768×1024）不走移动形态；除 D16 的曲线窗最小宽度外不改其布局。
- 不为触屏新增“悬停高亮”的替代交互：触屏点选后的既有选中高亮即为反馈。
- 不把 M15 visual-conformance 车道改为自动门，也不依赖它做回归门。
- 不新增 WebKit / Firefox 的 CI 浏览器安装。

## Decisions

### D1 移动形态判据：宽 < 768px 或高 < 500px（用户拍板）

单一判据，CSS 与 JS 同源：

- CSS：`src/index.css` 用 Tailwind v4 **块写法**声明两个自定义变体：

  ```css
  @custom-variant mobile { @media (max-width: 767.98px), (max-height: 499.98px) { @slot; } }
  @custom-variant mobile-landscape { @media (max-height: 499.98px) and (orientation: landscape) { @slot; } }
  ```

  不得用单行简写：tailwindcss 4.3.0 实测，简写遇到逗号分隔的查询列表只保留第一条，第二条被编译成没有 `@media` 的无效规则，“高 < 500”这一半会静默失效。
- JS：`useMobileForm()` 用 `matchMedia` 订阅同两条查询，返回 `{ mobile: boolean, landscape: boolean }`；查询字符串从一个模块常量导出。
- 可观测性：`AppShell` 根节点带 `data-viewport-form="mobile|desktop"` 与 `data-viewport-short-landscape="true|false"`（来自 hook），并带一个经 `mobile:` 变体设置的 CSS 自定义属性 `--nhms-viewport-form: mobile`（桌面形态下为 `desktop`）。二者不可见，供浏览器测试在真实布局引擎里核对“CSS 侧与 JS 侧结论一致”。jsdom 的 `matchMedia` 是恒假桩，不能当边界 oracle。

`landscape` 仅在 `mobile` 为真时有意义，表示“矮视口横屏”。高 < 500 但非横屏（如 320×480）与宽 < 768 且高 ≥ 500 的视口都不是矮视口横屏。

备选：仅宽 < 768（大屏手机横屏如 844×390 会落到桌面形态）；宽 < 1024（平板实测桌面布局可用）。均被否决。

后果：宽 < 768 或高 < 500 的桌面浏览器窗口也进入移动形态。判据按视口而不是按设备。

### D2 桌面形态不变量与例外

所有移动样式只写在 `mobile:` / `mobile-landscape:` 变体或 `useMobileForm().mobile` 分支下。以下是全部例外，逐条说明为何不改变桌面几何或为何被接受：

1. `h-screen → h-dvh`、`w-screen → w-full`：无动态工具栏的浏览器上 `100dvh == 100vh`；根容器是块级全宽。几何不变。
2. viewport meta 加 `viewport-fit=cover`、根容器无条件加 `env(safe-area-inset-*)` 内边距：桌面浏览器 `env()` 为 0。几何不变。
3. 根节点的 `data-*` 属性与自定义属性（D1）：不可见。
4. 桌面形态曲线窗最小宽度（D16，用户拍板）：只改变视口宽 768–1142px 时曲线窗的默认尺寸；≥ 1143px 不变。
5. 图表容器的 `data-zoom-*`（D12）与地图容器的 `data-selected-anchor-*`（D17）：不可见。

回归门：`m11-overlay-collision.mocked.spec.ts` 的 1920 / 1440 / 1280 / 800 四个宽度，期望值不得改动。

### D3 外壳：动态视口高度 + 安全区

`AppShell` 根容器用 `h-dvh w-full`，并无条件应用四边 `env(safe-area-inset-*)` 内边距；`index.html` 的 viewport meta 加 `viewport-fit=cover`。无条件而不按形态分支，是因为走桌面形态的触屏设备（平板）同样有安全区。地图区内贴底元素不再重复加底部安全区。

模拟器里 `dvh == vh`、`env()` 为 0，合并门只能断言样式事实（声明用了 `dvh`、meta 含 `viewport-fit=cover`、内边距声明引用了 `env()`）；行为由真机清单验证（D15）。

### D4 头部：移动形态单行 48px（用户拍板）

`SiteHeader` 在移动形态：高 48px、徽标 32px、标题 16–18px 单行 `truncate`、不渲染英文副标题；合作单位条维持 `lg` 以上才显示。`SiteHeader` 由 `AppShell` 在所有路由渲染，所以 `/ops` 等页面的内容区同样多出 36px 高度。

不同步任何“导航高度 token”：`--m11-nav-height` 在源码中没有消费者，且既有 `single-map-shell-routing` 规格要求它“归 0”。本 change 不碰它。

### D5 浮层收纳：三个启动器，同一时刻只展开一个（用户拍板）

移动形态下 `M11FloatingLayerSwitcher`、`M11FloatingBasemapSwitcher`、`M11FloatingLegend` 各渲染为一个 44×44 的启动器按钮，竖排在地图区右上角（缩放按钮隐藏后腾出的位置，见 D7，故 D7 先落地）。展开状态是 `OverviewPage` 地图外壳持有的一个值 `'layers' | 'basemap' | 'legend' | null`：

- 点某启动器：若它已展开则收起，否则展开它并收起其他。
- 展开面板锚定在启动器列左侧，完全位于地图区内，不与控制条、启动器列相交；内容超出时面板内部纵向滚动。面板内的开关与桌面相同，每项高 ≥ 44px。
- 点地图（面板与启动器之外的任意地图点，含命中河段 / 站点 / 流域的点击——要素自身的既有行为照常发生）、按 `Escape`、曲线抽屉打开、视口形态切换，都把展开值复位为 `null`。
- 默认 `null`。展开 / 收起不改 URL query、不发数据请求。
- 展开时旋转（竖屏 ↔ 矮视口横屏，形态不变）：保持展开并按新地图区重新限高。
- testid 约定：启动器为 `m11-launcher-layers|basemap|legend`；面板沿用既有 `m11-floating-layer-switcher`、`m11-floating-basemap-switcher`、`m11-floating-legend`（收起时面板不在 DOM 中或不可见）。

图例收起与既有“图例必须显示”要求的关系：`map-layer-timeline-controls` 的“Legends reflect active hydrologic layer”与 `precipitation-raster-overlay` 的图例场景，在移动形态下由“启动器常驻可见 + 展开后的面板内容满足原要求”承接；本 change 把这一解读写进 `mobile-map-overlay-layout`，不改那两条要求的文字。

桌面形态渲染路径不变（三者仍是常驻面板），展开值在桌面形态不被读取。

开发用角色切换器：移动形态下收成宽不超过 56px 的紧凑触发器，移到 `main` 的左边缘、垂直居中，层级高于地图浮层，不与头部、启动器列、控制条、顶部提示条带相交；它只在开启 role override 的构建里存在，豁免 44px / 16px 审计，但必须保持可操作（mocked 车道靠它切角色）。抽屉打开时它可以被抽屉盖住。

备选：图例常驻精简色带（持续占地图空间，被否决）。

### D6 底部控制条

移动形态下 `M11BottomControlBar` 全宽减两侧 8px，保留“距地图区底 40px、让出 attribution 带”的既有规则：

- 非矮视口横屏：两行——第一行预报源切换 + 起报时次选择 + 播放速度选择，第二行步进 / 播放按钮 + 时间轴滑块（`flex-1`，≥ 120px）。播放速度放第一行，是为了 320px 宽时第二行仍放得下三个 44px 按钮与 120px 滑块。
- 矮视口横屏：单行，全部控件同一行，时间轴滑块 ≥ 120px。
- 所有按钮 ≥ 44×44；起报时次与播放速度两个 `<select>` 高 ≥ 44px、字号 ≥ 16px。

移动形态的条高不走 `m11VisualTokens.timelineHeight`：该 token 及其 64px 值保持为桌面形态事实，移动形态的高度由内容与移动变体类名决定。`map-first-layout-conformance` 的 64px 断言相应限定到桌面形态。

### D7 地图缩放按钮：移动形态隐藏（用户拍板）

`NavigationControl` 在移动形态不渲染。本 change 不改地图手势配置，双指缩放与拖动沿用 MapLibre 默认行为，其真机表现归真机清单。桌面形态不变。

### D8 提示、状态条、运维入口与区域错误兜底

移动形态下：

- `M11FloatingNotice`（五条互斥提示共用的那个组件）与 `M11MapStatusOverlays` 的各条状态条都放在地图区顶部、启动器列左侧的条带内，自上而下排列，彼此不相交、不与启动器和控制条相交；每条文本最多两行，超出截断。它们不需要避让**已展开的面板**与**已打开的曲线抽屉**（二者由用户主动打开、层级更高，可以盖住提示）。
- `M11OpsLink` 并入启动器列末位，≥ 44×44，仅对有权限角色渲染。
- 地图控件、图例、控制条三个区域的 `RegionErrorBoundary` 兜底不再用桌面偏移：兜底块位于地图区内，不与仍在渲染的控制条、启动器相交。
- 为了在真实浏览器里验证兜底几何，新增一个测试门控的区域崩溃开关：仅当 `window.__NHMS_E2E_HOOKS__ === true` 时，各区域边界内的一个探针读取 `window.__NHMS_E2E_CRASH_REGION__`（取值为区域标识：`map-controls` / `legend` / `control-bar` / `curve`）并在渲染期抛错；无门控时探针不渲染、不读取任何全局。
- 曲线区域的兜底：见 D11 的“崩溃不留死状态”。

### D9 曲线窗：移动形态为抽屉（用户拍板）

`M11DraggableCurveWindow` 在移动形态：

- 非矮视口横屏（含 320×480 这类竖向矮视口）：贴地图区底部、全宽、高度**固定**为 `min(60dvh, 地图区高度 − 8px)`，圆角只在上方。
- 矮视口横屏：贴地图区右侧、全高、宽度**固定**为 `min(50vw, 28rem)`。
- 尺寸不随内容状态（加载中 / 等待 / 空态 / 部分数据 / 已加载）变化；头部（标题 + 关闭按钮）始终可见，不随主体滚动。
- 不设 `aspect-video`；拖拽关闭（不挂 pointer 拖拽监听、不显示抓手光标、不做 clamp 定位，位置完全由 CSS 决定）。
- 层级高于底部控制条、启动器与浮层面板。
- 从移动形态回到桌面形态时，仍打开的曲线窗回到桌面默认位置（不恢复进入移动形态前的拖拽坐标）。
- 桌面形态：现有拖拽与 `aspect-video` 不变；宽度规则见 D16。

备选：全屏面板（看不到选中位置，被否决）；保留浮窗只加高（仍叠压、横屏仍局促，被否决）。

### D10 单窗策略：移动形态同一时刻只留一个曲线窗（用户拍板）

`OverviewPage` 在移动形态打开河段窗时清掉气象代站窗，反之亦然。桌面形态保持“双窗并存”。从桌面形态切到移动形态且两窗都开时，保留 `activeCurveWindow` 指向的那个，关闭另一个。

### D11 抽屉打开时地图外壳让位（用户拍板）

移动形态下只要有曲线抽屉打开（竖屏与矮视口横屏同一规则）：

- 底部控制条与启动器列都不可见、不可操作（保持挂载、仅隐藏）。
- D5 的展开值复位为 `null`。
- 时间轴若正在播放则**暂停**；关闭抽屉后不自动续播。
- 关闭抽屉后控制条与启动器重新可见，预报源、起报时次、有效时刻与打开前相同，播放状态为“未播放”，面板保持收起。

崩溃不留死状态：曲线区域的 `RegionErrorBoundary` 进入兜底时，控制条与启动器必须可见可操作——“隐藏”取决于是否有抽屉在正常渲染，而不是取决于是否有选中的河段 / 站点。兜底块位于地图区内，不与控制条、启动器相交。实现上需要两处共享组件改动：`RegionErrorBoundary` 增加一个可选的错误回调 prop，让页面得知曲线区域已进入兜底；时间轴的播放状态需要能被页面暂停（把“暂停”能力从时间轴组件的本地 state 暴露出来）。

### D12 图表：容器驱动高度 + 触屏时间轴缩放（用户拍板）

- 抽屉主体中，图表区占满头部与选择器之后的剩余高度，下限：非矮视口横屏 ≥ 160px、矮视口横屏 ≥ 120px。剩余高度不足时抽屉主体纵向滚动，而不是把图表压到下限以下。
- 图表随抽屉尺寸变化（旋转、形态切换）重排，画布尺寸与图表区一致。
- 移动形态下河段曲线与气象代站曲线：双指捏合缩放时间轴，单指拖动平移（河段图需把 `moveOnMouseMove` 在移动形态翻为开；桌面形态配置不变），触点处显示 tooltip。
- 两张图的图表容器暴露当前缩放窗口 `data-zoom-start` / `data-zoom-end`（0–100，随 `datazoom` 事件更新）。这是为可测性新增的产品代码，桌面形态同样暴露。
- 河段窗的“滚轮缩放时间轴”提示在移动形态替换为“双指缩放时间轴”；桌面形态文案不变。

备选：手机上不提供缩放（7 天预报挤在 360px 里看不清，被否决）；底部缩放滑块（再占 30px 高，被否决）。

### D13 抽屉内控件的触控下限

移动形态下三处落点：

1. 河段窗头部内联的关闭按钮（`M11RiverForecastPanel`）：≥ 44×44。
2. 气象代站窗头部的关闭按钮（共享的 `M11PopupHeader`）：≥ 44×44。
3. 起报时次选择触发器（河段窗的 cycle 条与气象代站窗的 source controls）：高 ≥ 44px、字号 ≥ 16px；其下拉选项每项高 ≥ 44px。

气象要素切换（PRCP / TEMP / RH / wind / Rn）每项高 ≥ 44px，可换行，不超出抽屉水平范围。河段名 / 站点 ID 行保持单行截断。

### D14 运维页兜底（含用户拍板的字号下限）

移动形态下 `/ops`、`/monitoring`（同一页面组件的两种模式）：

- 内容单列，页面滚动容器左右内边距 ≥ 12px。
- 内容超出时页面内部的滚动容器可纵向滚到底，窗口本身不滚（沿用既有“Operational pages own vertical scrolling”口径）。
- 任务表在自身容器内横向滚动。
- `LogModal` 的边界在视口内；日志区双向可滚；关闭按钮在视口内。
- 可聚焦的表单控件（筛选选择器、Cycle Time 输入等）字号 ≥ 16px。

`/system/model-assets`：页面滚动容器自身无横向溢出（`scrollWidth == clientWidth`），产品表在自身容器内横向滚动。不做其他改动。

既有 `pipeline-monitoring-frontend` 的“Responsive Layout”要求 < 1024px 时阶段卡收成横向滚动条或手风琴、趋势面板走专用 tab 或可展开区；代码现状是 < 800px 单列直堆。本 change 把移动形态明确改述为单列兜底，并把原“窄屏”场景收窄到 768–1023px 的桌面形态（现状与原文的偏差不在本 change 范围内）。

### D15 验收口径（用户拍板）

1. **合并门（自动）**：mocked-regression 车道新增三个触屏 project，全部强制 `browserName: 'chromium'`（Playwright 的 iPhone / iPad 预设默认 WebKit，而 CI 只装 Chromium；多点触控也只有 Chromium 的 CDP `Input.dispatchTouchEvent` 能合成）：
   - `mobile-portrait` 390×664；
   - `mobile-landscape` 750×342；
   - `mobile-landscape-wide` 844×390——宽 ≥ 768，**只靠高度条件**进入移动形态，专门守住 D1 的高度臂。

   三者只匹配文件名含 `.mobile.` 的 spec；既有 `mocked-regression-chromium` project 的 `testIgnore` 是**替换**而非合并顶层配置，必须重列顶层已忽略的四项再加 `.mobile.`。移动 spec 写成在三个 project 下都成立：按 project 是否为矮视口横屏分支期望（`e2e/support/` 提供统一的判定与夹具）。平板竖屏（768×1024）是桌面形态，它的断言放在桌面 project 的普通 spec 里用 `setViewportSize`，不设平板 project。断言是几何与样式 oracle，不是截图像素比对。
2. **曲线窗夹具**：mocked 车道新增打开河段窗与气象代站窗的夹具——非空的流域 / 图层 / 有效时刻 mock、径流瓦片夹具、气象站点与两条序列端点的 mock。河段用既有的 `window.__nhmsRiverClickEvidence`（不改，仍恰为三个方法）定位，入参来自夹具自身。气象代站新增**独立的**门控全局 `window.__nhmsStationLocateEvidence`，恰有一个方法 `locateRenderedStation({ stationId, lngLat })`：把相机移到该点、确认该点渲染着这个 `station_id` 的 `met-stations` 要素且未被遮挡，返回视口坐标与身份；只读、不调用产品回调、无门控不暴露。打开方式是“钩子定位 + 真实指针 / 触摸在该点点击”。
   夹具在桌面 project 验证；移动 project 下的首次触摸开窗在浮层收纳完成之后（见 Risks）。夹具不承担“抽屉打开后再点另一类要素”：钩子定位会把要素移到地图区中央，竖屏抽屉盖住地图区下部约 65%，中央点在抽屉之下。因此“河段窗与气象代站窗互相替换”“面板展开时点要素会收起面板”由 vitest 在页面组件层验证（既有的 MapLibre 桩可直接触发要素点击回调），e2e 只验单次打开后的几何。
3. **node-27 实拍**：`scripts/node27_display_v2_browser_evidence.mjs` 接受设备预设（视口、DPR、触屏、UA），布局检查按形态参数化（现有 84 / 64 钉值只对桌面形态成立），用第 2 点的两个钩子在 live 站定位并点开河段窗与气象代站窗——河段的 bbox / anchor / 身份按既有 live-river-click 车道 preflight 的同一方式从展示 API 取得，气象代站的 `stationId` 与经纬度从站点列表 API 取得；对 `mobile-portrait` 与 `mobile-landscape` 两档输出截图与几何 JSON，receipt 存入 `docs/runbooks/receipts/`。
4. **真机确认**：epic 收尾由用户在真机（至少一台 iOS Safari、一台 Android Chrome）按清单过一遍；不卡单个 PR。清单覆盖模拟器证明不了的行为与本文件 Open Questions 的全部条目。

### D16 桌面形态曲线窗最小宽度（用户拍板）

桌面形态曲线窗宽度由 `min(44rem, 42vw)` 改为 `min(44rem, max(42vw, 30rem))`，保持 16:9：视口宽 768 时窗口约 480×270（原约 322×181）。视口宽 ≥ 1143px 时 `42vw ≥ 30rem`，尺寸与改动前相同。既有的“窗口不超出地图区、拖拽 clamp”规则不变。

备选：接受现状（平板上曲线看不清）；把移动判据宽度上调到 1024（推翻 D1）。均被否决。

### D17 抽屉打开时自动平移地图（用户拍板）

移动形态下曲线抽屉打开时，地图平滑移动，使被选中要素的锚点落在**未被抽屉遮住的地图区**的中央：一次性的相机移动，以该锚点为目标并带像素偏移——非矮视口横屏向上偏移半个抽屉高度，矮视口横屏向左偏移半个抽屉宽度。缩放级别不变。不使用持久的相机内边距：那样关闭抽屉时清内边距本身就是一次相机变化，离开移动形态时还会残留。

- 触发：抽屉打开；一种窗替换另一种窗（选中要素变了）；抽屉开着时在竖屏与矮视口横屏之间切换（遮盖侧变了）。
- 平移只发生在上述三个触发点，各触发一次；除此之外不做任何跟随。所以用户在抽屉打开期间手动拖动 / 缩放地图后不会被拉回，但之后若换选要素或旋转屏幕，仍会按新的锚点 / 新的遮盖侧平移一次（本条优先级为编排者在验证上限之后补定，未经独立验证）。
- 关闭抽屉、离开移动形态都不移动地图。
- 锚点：河段用点击时记录的弹窗锚点经纬度；气象代站用该站点要素的经纬度（页面的站点弹窗状态需要多记这个值）。
- 桌面形态不平移，行为不变。
- 可观测性：地图容器暴露选中锚点（桌面形态双窗并存时取活动窗的锚点）当前的屏幕坐标 `data-selected-anchor-x` / `data-selected-anchor-y`（相对视口的 CSS px，相机静止后更新；没有选中要素时不带这两个属性；两种形态都暴露）。

实现期裁定（task 4.10 / #2811 的 fixture，编排者补定，细节见 tasks.md 该 task 的 Triage）：

- 触发由一个“触发键”表达：移动形态、有选中锚点、曲线区域不在兜底时，键 = 遮盖侧 + 选中锚点的身份；键从任意值变成另一个非空值时平移一次，其余情形不动相机。所以不需要检测“用户动过地图”，也没有任何跟随状态。
- 原规格没有单列、现已写进规格增量的触发点：窗开着时从桌面形态进入移动形态；曲线区域兜底后重试成功。同一种窗内换选另一个要素与“一种窗替换另一种窗”同样平移一次。
- 两窗并存时（桌面形态，以及移动形态收敛前那个未绘制的中间提交）选中锚点取活动窗的。
- 气象代站锚点取被点中要素的 Point 几何，缺失时退回点击点的经纬度。
- 为了在真实地图库上证明“缩放不变”“关闭 / 离开移动形态不移动地图”，地图容器另外暴露只读的 `data-camera-center` / `data-camera-zoom`（规格属性清单之外的可测性属性，只在测试门打开时输出）。
- 曲线区域处于兜底时不平移；平移时刻必须真的渲染着抽屉，抽屉尺寸取 DOM 实测。平移前先同步 `resize` 地图，避免以旧画布尺寸定目标点。

## Sketch seams under test

1. **mocked-regression Playwright 车道的三个移动 project**（车道已有，移动 project 与曲线窗夹具为本 change 新建）：真实浏览器布局下的几何与样式断言。理由：本 change 的缺陷全是布局几何，jsdom 没有布局引擎也没有真实 `matchMedia`；一条车道覆盖全部用户可见行为。
2. **vitest**（已有设施）：展开互斥状态机、单窗替换与进入移动形态的收敛、抽屉打开时的暂停、移动形态下不挂拖拽监听、桌面形态图表配置与提示文案不变。hook 在 vitest 里用可控的 `matchMedia` 桩驱动，只验分支逻辑，不验判据边界。
3. **既有桌面碰撞 spec**（已有）：1920 / 1440 / 1280 / 800 四个宽度的断言原样保留，是桌面形态不变量的回归门。

node-27 实拍与真机确认是证据，不是测试 seam。

## Risks / Trade-offs

- **窄桌面窗口行为变化**：520px 宽的桌面窗口从桌面布局变为移动形态。缓解：proposal 标 BREAKING，`map-first-layout-conformance` 与碰撞 spec 的 520px 行改归移动形态。
- **与设计规范文档的偏离**：`docs/spec/06B_frontend_ui_design_spec.md` §8 写“最小支持 1440×900、< 1280px 不推荐使用”。本 change 把移动形态定为受支持形态；由一条文档 task 更新 §8，偏离是经用户批准的。
- **模拟器测不出的真机行为**：合并门只能证明几何与样式事实；靠 D15 第 4 层兜底。
- **CI 成本**：新增三个 project。缓解：只跑 `.mobile.` spec，既有 spec 不重复执行。
- **抽屉打开时不能操作地图外壳**：看曲线时不能切图层、拖时间轴。已由用户接受。
- **测试夹具是新基建**：径流瓦片夹具、独立的站点定位钩子与区域崩溃开关工作量不小，后两者是产品代码里的新增（仅测试门下生效；既有 river-click 钩子不改）。它们被切成独立 task 并排在使用方之前。
- **移动 project 下的首次开窗排在浮层收纳之后**：竖屏下钩子定位点在地图区中央，而未收纳的图例（实测 x150–374、y181–544）正盖住中央。所以 1.3 / 1.4 只在桌面 project 验证开窗，三个移动 project 下的触摸开窗首次出现在 4.2，且 4.2 依赖 3.3。

## Migration Plan

纯前端、无数据迁移。按 tasks 的 `Depends on` 顺序逐 PR 合并；每个 PR 独立保绿。回滚 = revert 对应 PR。

## Open Questions

1. **全国缩放级别下手指能否点中河段**：无头浏览器在全国级别对 12 个河段像素点按均未弹窗，真机放大后可点中。未确定是探针局限、该缩放级别下要素不可交互，还是命中容差不足。在真机确认清单里验证；若确认为缺陷，另立 issue。本 change 不为它切实现 task。

## Not yet specified

- **真机 iOS / Android 差异的处置**：真机确认会查动态工具栏、聚焦放大、手势冲突等行为，但在清单跑完之前说不清会暴露哪些具体问题、是否需要代码改动。等结果出来再决定是否立后续 change。

## Grill ledger（2026-10-08）

| # | 分支 | 结论 | 决定者 | 落点 |
|---|---|---|---|---|
| 1 | 曲线窗移动形态 | 底部抽屉 | 用户 | D9 |
| 2 | 双窗并存 | 移动形态同一时刻只留一个 | 用户 | D10 |
| 3 | 浮层收纳 | 全部收成启动器，互斥展开 | 用户 | D5 |
| 4 | 断点判据 | 宽 < 768 或高 < 500 | 用户 | D1 |
| 5 | 横屏曲线窗 | 右侧抽屉 | 用户 | D9 |
| 6 | 头部 | 单行 48px | 用户 | D4 |
| 7 | 曲线缩放 | 双指捏合 + 单指拖动 | 用户 | D12 |
| 8 | 验收口径 | 自动化门 + node-27 实拍 + 真机确认 | 用户 | D15 |
| 9 | 竖屏抽屉与控制条 | 抽屉打开时控制条让位（落地为隐藏且不可操作） | 用户 | D11 |
| 10 | 缩放按钮 | 移动形态隐藏 | 用户 | D7 |
| 11 | 抽屉打开时的启动器与控制条（两种朝向） | 都隐藏，关抽屉后恢复 | 用户（Stage 3 后补问） | D11 |
| 12 | 平板竖屏曲线窗 | 桌面形态加最小宽度 | 用户（Stage 3 后补问） | D16 |
| 13 | 播放中打开抽屉 | 自动暂停，不自动续播 | 用户（Stage 3 后补问） | D11 |
| 14 | 16px 字号下限是否覆盖运维页 | 覆盖 | 用户（Stage 3 后补问） | D14 |
| 默认项 | 触控 ≥ 44px、表单字号 ≥ 16px；桌面不变；`/ops` 兜底、model-assets 不撑破 | 确认 | 用户 | D2 / D13 / D14 |
| 开放项 | 全国级别点中河段 | 真机确认时验证 | — | Open Questions 1 |
| 15 | 抽屉打开后被点中的要素是否自动移入可见区 | 自动平移地图，把选中要素移到可见区 | 用户（Stage 4 第 2 轮后） | D17 |
| 开放项 | 真机 iOS / Android 行为 | 真机确认后再定 | — | Not yet specified |

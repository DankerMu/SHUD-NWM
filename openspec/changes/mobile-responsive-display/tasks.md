# Tasks: mobile-responsive-display

## 约定（对所有 task 生效）

- **形态**：移动形态 = 视口宽 < 768px 或高 < 500px；矮视口横屏 = 移动形态且高 < 500px 横屏。
- **移动 project**：`mobile-portrait` 390×664、`mobile-landscape` 750×342、`mobile-landscape-wide` 844×390，全部 Chromium + 触屏。只匹配文件名含 `.mobile.` 的 spec。
- **移动 spec 的写法**：每个 `.mobile.` spec 必须在三个移动 project 下都通过；期望按“是否矮视口横屏”分支，统一用 `e2e/support/` 的形态判定助手，不各自发明跳过方式。需要特定视口（旋转、320×480、600×400）的用例在用例内 `setViewportSize`。
- **平板 / 桌面断言**：768×1024 与其他桌面形态视口的断言写在不含 `.mobile.` 的普通 spec 里（桌面 project，`setViewportSize`）。
- **spec 文件归属**：每个 task 新建自己的 spec 文件（需要时可以是一个移动 spec 加一个桌面 spec），不往别的 task 的 spec 里追加用例；共享的 mock 与助手放 `e2e/support/`。
- **共享选择器基础组件**：4.9 与 6.1 都需要它在移动形态下字号 ≥ 16px。两者不互相依赖：先合并者落地这条规则（只加移动形态字号，不改高度与桌面样式），后合并者只验证、不重复加。曲线窗起报时次触发器的高度与选项下限归 4.9。
- **溢出 oracle**：不用 `document.scrollWidth == innerWidth`（外壳 `overflow-hidden`，恒真）；用“元素包围盒在视口 / 容器内”或具体滚动容器的 `scrollWidth` 对 `clientWidth`。
- **本地验证命令**：`cd apps/frontend && corepack pnpm typecheck && corepack pnpm test && corepack pnpm run test:e2e:mocked-regression`。下文 Verify 只写该 task 新增的断言。
- **桌面回归**：每个实现 task 都必须让既有 `m11-overlay-collision.mocked.spec.ts` 的 1920 / 1440 / 1280 / 800 四个宽度不改期望值通过。
- **live receipt**：改动展示端运行时代码的 task 合并前按 CLAUDE.md 既有规则在 node-27 出 live receipt；移动形态的专门实拍在 7.2。
- **依赖**：`Depends on` 行列出的是必须先合并的 task；未列出的 task 之间可并行。
- **未新增验证的既有场景**：`pipeline-monitoring-frontend` 的 Wide / Medium / Narrow 三个宽度场景本 change 只加了“桌面形态”限定、不改变其行为，不为它们新增验证；它们与代码现状的既有偏差不在本 change 范围（design.md D14）。该 capability 新增的移动形态场景由 6.1 验证。

## 1. 验证基建与文档

- [x] 1.1 移动 project：`playwright.config.ts` 新增约定里的三个移动 project，强制 `browserName: 'chromium'`（设备预设默认 WebKit，CI 只装 Chromium）；既有 `mocked-regression-chromium` project 的 `testIgnore` 重列顶层已忽略的四项（preview-deeplink、live-display、live-c4-display、m15-visual-conformance）并追加 `.mobile.`——project 级 `testIgnore` 会替换而非合并顶层配置。新增 `e2e/support/` 的形态判定助手。新增 `e2e/m11-baseline.mobile.mocked.spec.ts`，只断言今天已成立的事实：`/` 加载后 viewport meta 存在、地图区可见。

  Depends on: 无

  Verify：车道日志里三个移动 project 各有非零 passed（skip 不计）；既有每个 spec 只在桌面 project 执行一次，用例总数与改动前相同；四个被忽略的 spec 不被任何 project 执行；`.github/workflows/ci.yml` 的浏览器安装步骤不变，配置里 retries 仍为 0；临时加入一条必败的移动断言确认车道退出码非零，确认后移除（在 PR 描述里记录这次确认的输出）。

  **Suggested fixture level:** compact - 只改测试配置并加断言既有事实的 spec；无运行时代码、无共享入口行为变化。

  **Minimal mergeable slice:** atomic - project 定义与“谁匹配哪些 spec”的划分必须同时落地，否则既有 spec 会被三个新 project 重复执行，或 live spec 混进合并门。

  Triage（#2786）：Issue type: test ｜ Fixture level: compact（与上游建议一致）｜ Blast radius: 合并门车道——project 划分写错会让既有 spec 被重复执行、live spec 混入合并门，或移动 project 空跑仍显示绿。
  - Risk pack「Config / project setup」selected：改 `playwright.config.ts` 的 project 与 `testIgnore` -> `playwright test --list` 逐 project 计数（桌面用例总数与改动前相同、四个被忽略 spec 计数为 0、三个移动 project 各只列 `.mobile.` spec）。
  - Risk pack「Error handling」selected：移动 project 必须能让车道失败 -> 临时必败断言使 `test:e2e:mocked-regression` 退出码非零，输出记入 PR 描述后移除。
  - Risk pack「Release / packaging / dependency compatibility」selected：CI 只装 Chromium -> 三个移动 project 的 `browserName` 为 `chromium`，`.github/workflows/ci.yml` 零 diff。
  - Risk pack「Legacy compatibility」selected：既有单测 `apps/frontend/src/__tests__/playwrightConfig.test.ts` 把 project 名单钉死为 `['mocked-regression-chromium']` -> 期望改为按配置顺序的四项 `['mocked-regression-chromium', 'mobile-portrait', 'mobile-landscape', 'mobile-landscape-wide']`，同文件的 `metadata` 断言不改。`src/` 下只允许改这一个测试文件（PR 边界相应放宽，issue 上已留言）。
  - 未选：Public API、File IO、Schema、Auth、Concurrency、Resource limits、Documentation——本 task 不改运行时代码。
  - 场景归属（显式 non-goal）：spec delta 的「Baseline mobile invariants」归 5.1；「A mobile regression fails the lane」（390×664 图例与控制条相交）归后续浮层 task。1.1 只交付 viewport meta 存在与地图区可见，并以临时必败断言作为“车道会失败”的代理证据。
  - Must preserve：既有 `mocked-regression-chromium` 的用例集合与期望值（含 `m11-overlay-collision` 四个桌面宽度）、`retries` 为 0、`test:e2e:m15-visual` 与 preview/live 各自的独立 runner 不受影响。
  - Evidence floor：约定的本地验证命令全绿 + 上述 `--list` 计数（改动前后）+ 必败断言的非零退出输出。

- [ ] 1.2 设计规范文档：更新 `docs/spec/06B_frontend_ui_design_spec.md` §8（最小支持分辨率与断点行为表），新增移动形态一行（判据、单图页的移动布局概要、运维页为兜底），并把“< 1280px 不推荐使用”改述为“768–1279px 为桌面形态下的既有降级行为、< 768px 或高 < 500px 为移动形态”，注明由本 change 引入。

  Depends on: 无

  Verify：`openspec validate mobile-responsive-display --strict --no-interactive` 通过；§8 的文字与 design.md D1 的判据数值一致。

  **Suggested fixture level:** none - 纯文档。

  **Minimal mergeable slice:** atomic - 一节文档的两张表，互相引用同一判据。

- [ ] 1.3 河段窗测试夹具：让 mocked 车道能打开河段窗。内容：`e2e/support/` 提供一组 mock——非空流域、带真实有效时刻与瓦片模板的图层、径流瓦片（checked-in 的最小矢量瓦片夹具及其生成脚本，至少含一条带完整身份属性的河段）、气象站点列表、站点序列、河段 forecast-series；导出 `openRiverWindow(page)`：开 `__NHMS_E2E_HOOKS__` 门控、等地图就绪、用既有只读钩子 `window.__nhmsRiverClickEvidence.locateRenderedRiver` 定位河段（bbox、anchor 与四个身份字段取自夹具自身）、在返回的视口点做真实点击（桌面）或 `touchscreen.tap`（触屏）。不改产品代码。夹具只负责“从无窗状态打开一个窗”；不要求抽屉打开后再点其他要素（钩子会把要素移到地图区中央，那里在竖屏抽屉之下）。新增 `e2e/m11-river-open.mocked.spec.ts`（桌面 project）。`openRiverWindow` 须同时支持点击与触摸，但移动 project 下的首次开窗在 4.2——竖屏下未收纳的图例正盖住钩子定位到的地图区中央。

  Depends on: 1.1

  Verify：桌面 project（1280×900）下河段窗打开且曲线为已加载状态。

  **Suggested fixture level:** compact - 只新增测试支撑代码与一个二进制测试夹具，不改产品代码。

  **Minimal mergeable slice:** atomic - mock 组、瓦片夹具与 `openRiverWindow` 缺一则河段窗打不开，没有可验证的中间态。

- [ ] 1.4 气象代站窗测试夹具：新增独立的门控全局 `window.__nhmsStationLocateEvidence`，恰有一个方法 `locateRenderedStation({ stationId, lngLat })`（把相机移到该点、确认该点渲染着这个 `station_id` 的 `met-stations` 要素且未被遮挡、返回视口点与身份；只读、不调用产品回调、无门控不暴露）；既有 `window.__nhmsRiverClickEvidence` 不改，仍恰为三个方法。`e2e/support/` 导出 `openStationWindow(page)`（入参取自 1.3 的站点 mock）。新增 `e2e/m11-station-open.mocked.spec.ts`（桌面 project）；移动 project 下的首次开窗同样在 4.2。

  Depends on: 1.3

  Verify：桌面 project（1280×900）下气象代站窗打开且曲线为已加载状态；不开门控时 `window.__nhmsStationLocateEvidence` 为 undefined；对未渲染的站点 id 调用返回失败；既有 river-click 钩子单测（含“恰为三个方法”的断言）不改通过。

  **Suggested fixture level:** expanded - 在产品代码的地图组件里新增测试门控的全局钩子（共享入口）。

  **Minimal mergeable slice:** atomic - 站点定位钩子与唯一使用它的 `openStationWindow` 必须同 PR，否则钩子没有调用方、助手没有定位手段。

## 2. 移动形态基座

- [ ] 2.1 移动形态判据：`src/index.css` 用块写法（含 `@slot`）声明 `mobile` 与 `mobile-landscape` 两个自定义变体，禁止单行简写；新增导出两条媒体查询字符串的模块与 `useMobileForm()` hook；`AppShell` 根节点输出 `data-viewport-form`、`data-viewport-short-landscape`，并经 `mobile:` 变体设置自定义属性 `--nhms-viewport-form`（桌面为 `desktop`）。零视觉变化。新增 `e2e/m11-viewport-form.mocked.spec.ts`（桌面 project，`setViewportSize`）。

  Depends on: 无

  Verify：e2e 断言 767×900 / 768×900、900×499 / 900×500、390×664、320×480、600×400 的两个 data 属性取值，390×664 → 750×342 不刷新即更新，844×390 与 1280×900 下 data 属性与计算后的 `--nhms-viewport-form` 一致；vitest 用可控 `matchMedia` 桩验证 hook 的订阅与更新。

  **Suggested fixture level:** expanded - 在所有路由共享的 `AppShell` 根节点上加属性与类名。

  **Minimal mergeable slice:** atomic - CSS 变体、hook 与根节点上的两侧可观测标记必须同 PR 落地，“CSS 与 JS 一致”的断言才成立；拆开后任一半都没有可验证行为。

- [ ] 2.2 外壳动态视口：`AppShell` 根容器由 `h-screen w-screen` 改为 `h-dvh w-full`。新增 `e2e/m11-shell-viewport.mocked.spec.ts`。

  Depends on: 2.1（无功能耦合；2.1、2.2、2.3 改的是 `AppShell` 根节点同一行 className，串行以免冲突）

  Verify：e2e 断言根节点高度声明解析自 `100dvh`（读匹配的样式规则，不只读计算值）；1280×900 下根节点渲染宽高等于视口宽高。

  **Suggested fixture level:** expanded - 改 `AppShell` 根容器的尺寸声明，影响每个路由的高度计算。

  **Minimal mergeable slice:** atomic - 高与宽是同一根节点同一 className 上的一次替换，是“控制条被浏览器工具栏切掉”的独立修复。

- [ ] 2.3 安全区：`index.html` viewport meta 加 `viewport-fit=cover`；`AppShell` 根容器无条件应用四边 `env(safe-area-inset-*)` 内边距。新增 `e2e/m11-shell-safe-area.mocked.spec.ts`。

  Depends on: 2.2

  Verify：e2e 断言 meta content 含 `viewport-fit=cover`、根节点内边距声明引用四个 `env(safe-area-inset-*)`、1280×900 下四边计算内边距为 0。真机上的遮挡行为归 7.3 清单。

  **Suggested fixture level:** expanded - 改全局 viewport meta 与共享外壳的内边距。

  **Minimal mergeable slice:** atomic - `viewport-fit=cover` 不配安全区内边距会让内容进入刘海区，二者必须同 PR。

- [ ] 2.4 头部压缩：`SiteHeader` 在移动形态为 48px 单行（徽标缩小、标题单行截断、不渲染英文副标题）；桌面形态不变。不改任何导航高度 token。新增 `e2e/m11-header.mobile.mocked.spec.ts` 与 `e2e/m11-header-desktop.mocked.spec.ts`。

  Depends on: 1.1, 2.1, 2.5

  Verify：移动 spec 断言三个移动 project 下头部高 48、标题为一行高、副标题不存在、地图区顶边 y=48，`/ops`（用 `setRole` 切到 operator）头部同为 48；桌面 spec 断言 `/` 在 768×1024 与 1280×900 下头部 84 且副标题存在，`/ops`、`/monitoring`、`/system/model-assets` 在 1280×900 下头部 84，`/` 在 520×900 下头部 48（窄桌面窗口走移动形态）。

  **Suggested fixture level:** expanded - `SiteHeader` 由 `AppShell` 在所有路由渲染，头部变矮会改变每个页面的内容区高度。

  **Minimal mergeable slice:** atomic - 一个组件的一组移动样式，头部高度与标题单行互相决定，无可独立交付的子集。

- [ ] 2.5 开发用角色切换器的移动位置：role override 开启时，移动形态下把 `AppShell` 的角色切换器收成宽不超过 56px 的紧凑触发器，移到 `main` 左边缘、垂直居中，层级高于地图浮层，保持可见可操作；桌面形态位置不变；未开启 role override 时两种形态都不渲染。在 `e2e/support/` 提供 `setRole(page, role)` 助手。新增 `e2e/m11-role-selector.mobile.mocked.spec.ts`。

  Depends on: 1.1, 2.1

  Verify：三个移动 project 下切换器包围盒在视口内、宽不超过 56px、不与头部和控制条相交，用它切到 operator 后角色生效；vitest 断言未开启 role override 时不渲染。它与启动器、顶部提示条带不相交的断言分别在 3.3、3.4（那时这些元素才存在）。

  **Suggested fixture level:** compact - 一个仅开发构建存在的控件的移动定位。

  **Minimal mergeable slice:** atomic - 一个元素的定位分支加一个测试助手。

## 3. 地图浮层（`/`）

- [ ] 3.1 隐藏地图缩放按钮：移动形态不渲染 MapLibre 缩放 / 指北控件；桌面形态保留；不改地图手势配置。新增 `e2e/m11-zoom-control.mobile.mocked.spec.ts` 与 `e2e/m11-zoom-control-desktop.mocked.spec.ts`。

  Depends on: 1.1, 2.1

  Verify：移动 spec 断言三个移动 project 下无 zoom-in / zoom-out / reset-bearing 按钮；桌面 spec 断言 1280×900 与 768×1024 下三个按钮存在。

  **Suggested fixture level:** compact - 单个控件的条件渲染。

  **Minimal mergeable slice:** atomic - 一个条件分支。

- [ ] 3.2 浮层展开状态与图例启动器：地图外壳持有展开值 `'layers' | 'basemap' | 'legend' | null`（默认 `null`；点地图空白、`Escape`、形态切换时复位）；移动形态下图例渲染为 44×44 启动器 `m11-launcher-legend`，位于地图区右上角启动器列，展开的图例面板沿用 `m11-floating-legend`，完全位于地图区内、不与控制条和启动器列相交、内容超出时内部滚动。展开 / 收起不改 URL、不发请求。桌面形态图例不变。**本 task 独占既有碰撞 spec 中 520px 行的全部改写**：图例相关行改为移动期望（默认无图例面板、有图例启动器）；控制条相关行改成与形态无关的“在视口内、不压 attribution”，去掉 520px 上的 64 / 40 钉值；其余四个宽度不动。新增 `e2e/m11-legend-launcher.mobile.mocked.spec.ts`。

  Depends on: 1.1, 2.1, 2.5, 3.1

  Verify：移动 spec 断言默认无图例面板、点启动器展开、再点收起、点地图空白收起、`Escape` 收起、展开收起前后 URL 不变且无 API 请求；390×664 与矮视口横屏下展开面板在地图区内、在控制条之上、不与启动器列相交、滚到底后最后一项在面板可视框内；径流图层 + 降水开启时面板列出径流分级（含单位）与六级降水；展开时由 390×664 变为 750×342 仍展开且在地图区内；390×664 → 1280×900 → 390×664 后无面板展开，1280×900 时桌面图例在原偏移。vitest 覆盖展开值状态机。

  **Suggested fixture level:** expanded - 引入地图外壳的新共享状态，并改写既有回归 spec 的 520px 期望。

  **Minimal mergeable slice:** atomic - 这已是首刀：状态值加它的第一个消费者（图例，最大的遮挡源）。只合状态值没有可验证行为；碰撞 spec 的 520px 行在图例收起的同一刻失效，必须同 PR 改。

- [ ] 3.3 图层与底图启动器：图层面板、底图切换在移动形态渲染为 44×44 启动器 `m11-launcher-layers`、`m11-launcher-basemap`，加入启动器列并接入 3.2 的展开值；展开面板内每个开关高 ≥ 44px，内容与产生的 query 变化与桌面一致。新增 `e2e/m11-layer-basemap-launchers.mobile.mocked.spec.ts` 与 `e2e/m11-launchers-desktop.mocked.spec.ts`（桌面 project）。

  Depends on: 3.2

  Verify：三个移动 project 下三个启动器可见且三个面板都不可见；展开一个会收起另外两个；矮视口横屏下图层面板在地图区内、在控制条之上；390×664 与 750×342 下三个面板各自展开时面板内每个可点项 ≥ 44×44；图层与底图面板展开再收起前后 URL 不变且无 API 请求；切换气象代站开关后的 URL query 与桌面形态同一操作的结果相同；角色切换器不与任何启动器相交。桌面 spec 断言 768×1024 与 1280×900 下三个面板常驻可见、无任何启动器，以及 390×664（展开一个面板）→ 1280×900 → 390×664 的往返：1280×900 时三个桌面面板都在原偏移且无任何启动器，回到 390×664 后无面板展开。

  **Suggested fixture level:** compact - 两个同文件组件接入既有状态值，状态契约在 3.2 已定。

  **Minimal mergeable slice:** atomic - 图层与底图两个启动器共用同一列与同一互斥断言；只做一个会出现“一个是启动器、一个是常驻面板”压在启动器列上的新叠压。

- [ ] 3.4 浮动提示与状态条的移动位置：移动形态下 `M11FloatingNotice`（代站状态 / 加载中 / 空数据 / 数据异常 / 降水五条互斥提示共用的组件）与 `M11MapStatusOverlays` 的各条状态条（流域边界不可用、地图不可用、选中河段不可用、地图源错误）放在地图区顶部、启动器列左侧的条带内，自上而下排列，彼此不相交、不与启动器和控制条相交，每条文本最多两行截断。不要求避让已展开的面板与已打开的曲线抽屉。桌面形态不变。新增 `e2e/m11-notices.mobile.mocked.spec.ts`。

  Depends on: 3.3

  Verify：三个移动 project 下，代站状态提示与加载中提示各自的包围盒在地图区内、不与任何启动器或控制条相交、高度不超过两行文本；让一个瓦片请求失败以触发地图源错误状态条，断言它单独出现时在地图区内、不与任何启动器或控制条相交、高度不超过两行，且与同时出现的浮动提示不相交；角色切换器不与浮动提示和状态条相交。

  **Suggested fixture level:** compact - 两个展示组件的移动定位，无状态与数据变化。

  **Minimal mergeable slice:** atomic - 浮动提示与状态条争用同一条顶部条带，位置互相约束，必须一起定。

- [ ] 3.5 运维入口并入启动器列：移动形态下 `M11OpsLink` 为启动器列末位（图例启动器之下），≥ 44×44，仅对有权限角色渲染。新增 `e2e/m11-ops-entry.mobile.mocked.spec.ts`。

  Depends on: 3.3

  Verify：viewer 角色下无运维入口；用 `setRole` 切到 operator 后入口存在、≥ 44×44、位于图例启动器之下、点击进入 `/ops`。

  **Suggested fixture level:** compact - 一个链接的移动定位与尺寸。

  **Minimal mergeable slice:** atomic - 一个元素。

- [ ] 3.6 区域错误兜底的移动位置：（a）新增测试门控的区域崩溃开关：仅当 `window.__NHMS_E2E_HOOKS__ === true` 时，地图控件、图例、控制条、曲线四个区域边界内各有一个探针，读取 `window.__NHMS_E2E_CRASH_REGION__`（`map-controls` / `legend` / `control-bar` / `curve`）并在渲染期抛错；无门控时探针不渲染、不读取任何全局。（b）移动形态下地图控件、图例、控制条三个区域的 `RegionErrorBoundary` 兜底块位于地图区内，不与仍在渲染的控制条、启动器和 attribution 相交；桌面形态的兜底位置不变。曲线区域的兜底行为在 4.6。新增 `e2e/m11-region-fallbacks.mobile.mocked.spec.ts`。

  Depends on: 3.3

  Verify：390×664 下用崩溃开关分别让图例、地图控件、控制条三个区域抛错：图例兜底在地图区内且不与控制条相交；地图控件兜底在地图区内且不与控制条和图例启动器相交；控制条兜底在地图区内且不与任何启动器和 attribution 相交。带门控且开关为 `legend` 时图例显示兜底，同值无门控时图例正常渲染（vitest 或 e2e）。

  **Suggested fixture level:** expanded - 在产品代码里新增测试门控的崩溃探针（共享的区域边界内），并改三处兜底定位。

  **Minimal mergeable slice:** atomic - 崩溃开关是验证兜底几何的唯一手段，没有它兜底定位无法在真实布局下断言；三处兜底用同一种定位方式与同一条断言，拆开是三份相同的 PR。

- [ ] 3.7 控制条竖屏两行：移动形态且非矮视口横屏时，控制条全宽减两侧 8px、两行（第一行预报源切换 + 起报时次 + 播放速度，第二行步进 / 播放按钮 + 时间轴滑块），滑块 ≥ 120px，按钮 ≥ 44×44，起报时次与播放速度两个 `<select>` 高 ≥ 44px、字号 ≥ 16px，底边距地图区底 40px、不压 attribution。播放速度选择器与其 state 现在在时间轴组件内部，把它排到第一行需要调整时间轴组件的结构或把该 state 上提到控制条。移动形态条高不受 64px token 约束；桌面形态仍为 64px 单行。新增 `e2e/m11-control-bar-portrait.mobile.mocked.spec.ts`（矮视口横屏 project 下本文件只断言“全部控件在视口内”）与 `e2e/m11-control-bar-desktop.mocked.spec.ts`。

  Depends on: 1.1, 1.3, 2.1, 3.2

  Verify：`mobile-portrait` 下以及 `setViewportSize(320, 568)` 后，全部控件包围盒在视口内、滑块 ≥ 120px、滑块顶边不高于预报源切换的底边（两行）、条底边距地图区底 40px、不与 attribution 相交、两个 `<select>` 达到尺寸与字号下限；前进一步后的 valid-time query 与桌面同一操作的结果相同；桌面 spec 断言 1280×900 下条高 64、单行。

  **Suggested fixture level:** expanded - 控制条驱动时间轴 query，是共享交互入口。

  **Minimal mergeable slice:** atomic - 这是首刀（竖屏两行），矮视口横屏单行已切为 3.8。依赖 3.2 是因为既有碰撞 spec 的 520px 行钉着条高 64，那一行的改写权归 3.2；依赖 1.3 是因为“前进一步”需要带有效时刻的图层 mock，既有 mock 的有效时刻为空。

- [ ] 3.8 控制条矮视口横屏单行：矮视口横屏下控制条为单行，全部控件同一行，滑块 ≥ 120px，尺寸与字号下限同 3.7，底边距地图区底 40px、不压 attribution。新增 `e2e/m11-control-bar-landscape.mobile.mocked.spec.ts`（`mobile-portrait` project 下本文件用 `setViewportSize(750, 342)` 运行同一组断言）。

  Depends on: 3.7

  Verify：750×342 与 844×390 下全部控件包围盒在视口内、滑块 ≥ 120px、滑块与预报源切换在垂直方向重叠（同一行）、条底边距地图区底 40px、不与 attribution 相交。

  **Suggested fixture level:** compact - 同一组件的一个形态分支，交互契约在 3.7 已定。

  **Minimal mergeable slice:** atomic - 一种视口下的一行布局。

## 4. 曲线抽屉（`/`）

- [ ] 4.1 单窗策略：移动形态下打开河段窗关闭气象代站窗、反之亦然；从桌面形态进入移动形态且双窗都开时保留活动窗、关闭另一个；桌面形态双窗并存不变。只用 vitest 在页面组件层验证（既有 MapLibre 桩可直接触发要素点击回调，`matchMedia` 用可控桩）；不新增 e2e——真实浏览器里抽屉会盖住钩子定位到的第二个要素。

  Depends on: 2.1

  Verify：vitest 覆盖：移动形态下河段 → 站点后只剩气象代站窗，站点 → 河段后只剩河段窗；桌面形态双窗都开且气象代站窗为活动窗时切到移动形态，只剩气象代站窗；桌面形态下两个方向都保持双窗并存。

  **Suggested fixture level:** expanded - 改页面级的曲线窗状态，并收窄既有“双窗并存”契约的适用范围。

  **Minimal mergeable slice:** atomic - 这是原“单窗 + 让位”的首刀：只动曲线窗状态。让位（隐藏控制条等）已切为 4.6。

- [ ] 4.2 曲线窗抽屉形态：`M11DraggableCurveWindow` 在移动形态渲染为抽屉——非矮视口横屏时贴底、全宽、高度固定为 `min(60dvh, 地图区高度 − 8px)`；矮视口横屏时贴右、全高、宽度固定为 `min(50vw, 28rem)`；尺寸不随内容状态变化；无固定宽高比；不挂拖拽监听；头部不随主体滚动；层级高于控制条、启动器与面板；回到桌面形态时为默认位置的可拖拽窗。桌面形态行为不变。新增 `e2e/m11-curve-sheet.mobile.mocked.spec.ts`。

  Depends on: 1.4, 3.3, 4.1

  Verify：移动 spec 断言河段窗与气象代站窗在 390×664 的左 / 右 / 底边与地图区重合且高度等于公式值，在 750×342 与 844×390 的上 / 下 / 右边与地图区重合且宽度等于公式值；320×480 为底部抽屉、600×400 为右侧抽屉；请求未返回时的包围盒与加载完成后相同且标题与关闭按钮可见；身份校验失败时原因文案在抽屉内；拖头部 100px 后包围盒不变；主体滚到底后标题与关闭按钮仍在抽屉可视框内；390×664 → 750×342 后同一河段、同一起报时次的窗变为右侧抽屉；390×664 → 1280×900 后为可拖拽桌面窗，包围盒等于在 1280×900 新打开的河段窗的包围盒，且控制条可见。vitest 断言移动形态不挂拖拽监听；既有桌面拖拽单测不改通过。

  **Suggested fixture level:** expanded - 改两个曲线窗共用的窗口容器，并改变既有 `map-feature-popups` 契约的适用范围。

  **Minimal mergeable slice:** atomic - 依赖 3.3 是因为三个移动 project 下的触摸开窗首次出现在本 task，而钩子定位点在地图区中央，需要浮层已收纳。容器只有一个，河段窗与气象代站窗同时受影响；竖屏与横屏两种锚定是同一组件同一条 CSS 分支上的两个取值，只做其一会让另一档视口的窗落到无定义位置。

- [ ] 4.3 桌面形态曲线窗最小宽度：桌面形态曲线窗默认宽度由 `min(44rem, 42vw)` 改为 `min(44rem, max(42vw, 30rem))`，保持 16:9 与既有的地图区内 clamp；组件里用于定位计算的尺寸回退公式须同步为同一宽度规则（只同步宽度表达式；该回退里区分“桌面摆位”的既有宽度阈值不动）。新增 `e2e/m11-curve-window-min-size.mocked.spec.ts`（桌面 project）。

  Depends on: 1.3

  Verify：768×1024 下河段窗 480×270 且在地图区内；1280×900 下窗宽等于视口宽的 42%；既有桌面拖拽单测与碰撞 spec 四个宽度不改通过。

  **Suggested fixture level:** compact - 一个宽度表达式；只影响 768–1142px 宽度下的默认尺寸。

  **Minimal mergeable slice:** atomic - 一条宽度规则的两处表达（类名与定位用的尺寸回退），必须一致。

- [ ] 4.4 河段抽屉图表可读高度：河段面板在抽屉内图表区占满头部与选择器之后的剩余高度，下限非矮视口横屏 160px、矮视口横屏 120px；不足时抽屉主体纵向滚动；图表画布与图表区同尺寸并随抽屉尺寸变化重排。新增 `e2e/m11-river-sheet-chart.mobile.mocked.spec.ts`。

  Depends on: 4.2

  Verify：已加载状态下，390×664 图表区 ≥ 160px、在抽屉内、内含同尺寸画布；750×342 图表区 ≥ 120px；390×664 → 750×342 后画布尺寸等于新图表区尺寸。

  **Suggested fixture level:** compact - 一个面板内部的布局约束。

  **Minimal mergeable slice:** atomic - 这是原“图表可读高度”的首刀（河段面板自成一个文件）；气象代站面板已切为 4.5。

- [ ] 4.5 气象代站抽屉图表可读高度：气象代站面板同 4.4 的下限与滚动兜底；矮视口横屏下图表区可被完整滚入抽屉可视框。新增 `e2e/m11-station-sheet-chart.mobile.mocked.spec.ts`。

  Depends on: 4.2

  Verify：已加载状态下，390×664 图表区 ≥ 160px、在抽屉内、内含同尺寸画布；750×342 图表区 ≥ 120px，且滚动抽屉主体后整个图表区位于抽屉可视框内。

  **Suggested fixture level:** compact - 一个面板内部的布局约束。

  **Minimal mergeable slice:** atomic - 一个面板文件。

- [ ] 4.6 抽屉打开时地图外壳让位：移动形态（两种朝向）下有曲线抽屉打开时，控制条与启动器列隐藏且不可操作（保持挂载）、展开值复位、时间轴若在播放则暂停；关闭抽屉后控制条与启动器恢复可见，预报源 / 起报时次 / 有效时刻与打开时相同、播放为停止、无面板展开。曲线区域的 `RegionErrorBoundary` 进入兜底时，控制条与启动器必须可见可操作，兜底块在地图区内且不与它们相交。需要两处共享组件改动：给 `RegionErrorBoundary` 加一个可选的错误回调 prop（页面据此得知曲线区域已进入兜底）；把时间轴的“暂停”能力从其本地 state 暴露给页面。新增 `e2e/m11-sheet-yields-chrome.mobile.mocked.spec.ts`。

  Depends on: 3.3, 3.6, 4.2

  Verify：vitest 覆盖“打开即暂停、关闭不续播”“面板展开时点要素后展开值为空”“曲线面板抛错后控制条不处于隐藏状态”“`RegionErrorBoundary` 不传回调时行为不变”；移动 spec 断言三个移动 project 下抽屉打开时控制条与启动器不可见且不接收指针输入、播放中打开抽屉后 valid time 不再变化、关闭后预报源 / 起报时次 / 有效时刻不变且未播放且无面板可见；用 3.6 的崩溃开关让曲线区域抛错，断言其兜底包围盒在地图区内、不与控制条和任何启动器相交，且控制条与启动器可见可点。

  **Suggested fixture level:** expanded - 改页面级的控制条可见性、播放状态与错误兜底行为，跨曲线窗状态与浮层展开状态。

  **Minimal mergeable slice:** atomic - 隐藏、暂停、恢复、崩溃兜底都由同一个“是否有抽屉在正常渲染”的页面级判定驱动；拆开会让中间态出现“控制条隐藏但仍在播放”或“隐藏后无法恢复”。

- [ ] 4.7 河段曲线触屏缩放：移动形态下河段曲线支持双指捏合缩放、单指拖动平移（移动形态下开启 dataZoom 的拖动平移，桌面形态配置不变）、触点显示 tooltip；图表容器暴露 `data-zoom-start` / `data-zoom-end`（两种形态都暴露）；“滚轮缩放时间轴”提示在移动形态改为双指手势文案。新增 `e2e/m11-river-chart-touch.mobile.mocked.spec.ts`，多点触控用 Chromium CDP `Input.dispatchTouchEvent` 合成，并把合成助手放 `e2e/support/`。

  Depends on: 4.4

  Verify：移动 spec 断言捏合后 `data-zoom-end − data-zoom-start < 100`、放大后单指横向拖动使 `data-zoom-start` 变化而跨度不变、点按绘图区后 tooltip 可见、提示文案含双指手势且不含滚轮字样；桌面 spec（`e2e/m11-river-chart-zoom-desktop.mocked.spec.ts`）断言 1280×900 下河段图容器为 `data-zoom-start="0"`、`data-zoom-end="100"`；vitest 断言桌面形态的 dataZoom 配置与提示文案与改动前相同。

  **Suggested fixture level:** compact - 一个图表组件的形态分支与一处文案，另加只读的 data 属性。

  **Minimal mergeable slice:** atomic - 这是原“触屏缩放”的首刀（河段图自有一份 dataZoom 配置与提示文案）；气象代站图已切为 4.8。

- [ ] 4.8 气象代站曲线触屏缩放：移动形态下气象代站曲线支持双指捏合缩放、单指拖动平移、触点显示 tooltip，图表容器暴露 `data-zoom-start` / `data-zoom-end`。新增 `e2e/m11-station-chart-touch.mobile.mocked.spec.ts`，复用 4.7 的合成助手。

  Depends on: 4.5, 4.7

  Verify：移动 spec 断言捏合后 `data-zoom-end − data-zoom-start < 100`、放大后单指横向拖动使 `data-zoom-start` 变化而跨度不变、点按绘图区后 tooltip 可见；桌面 spec（`e2e/m11-station-chart-zoom-desktop.mocked.spec.ts`）断言 1280×900 下气象代站图容器为 `data-zoom-start="0"`、`data-zoom-end="100"`；vitest 断言桌面形态的 dataZoom 配置与改动前相同。

  **Suggested fixture level:** compact - 一个图表配置的形态分支与只读 data 属性。

  **Minimal mergeable slice:** atomic - 一份图表配置。

- [ ] 4.9 抽屉内控件触控下限：移动形态下三处落点——河段面板头部内联的关闭按钮、气象代站窗共享头部的关闭按钮、两种窗的起报时次选择触发器及其选项——达到 44×44 / 高 44 / 字号 16 的下限；气象要素切换项高 ≥ 44px、可换行、不超出抽屉水平范围。桌面形态尺寸不变。新增 `e2e/m11-sheet-controls.mobile.mocked.spec.ts`。

  Depends on: 4.2

  Verify：390×664 下两种窗的关闭按钮 ≥ 44×44、起报时次触发器高 ≥ 44px 且字号 ≥ 16px、展开后每个选项高 ≥ 44px；气象代站窗的每个要素切换项在抽屉水平范围内且高 ≥ 44px。

  **Suggested fixture level:** compact - 三处控件的移动尺寸类名。

  **Minimal mergeable slice:** atomic - 三处落点共用同一条尺寸审计断言；按落点拆是三份几行的 PR，各自都要重复同一套夹具。

- [ ] 4.10 抽屉打开时自动平移地图：移动形态下曲线抽屉打开、一种窗替换另一种窗、抽屉开着时在底部抽屉与右侧抽屉布局之间切换时，地图平滑移动（缩放不变），做一次带像素偏移的相机移动（向上偏移半个抽屉高度，或向左偏移半个抽屉宽度；不使用持久的相机内边距），使锚点落在未被抽屉遮住的地图区内。平移只在这三个触发点各发生一次：用户在抽屉打开期间手动移动地图后不拉回，但之后的替换或布局切换仍会再平移一次。关闭抽屉、离开移动形态都不移动地图；桌面形态不平移。地图容器暴露 `data-selected-anchor-x` / `data-selected-anchor-y`（两种形态都暴露；桌面双窗并存时取活动窗的锚点；无选中要素时不带）。页面的气象代站弹窗状态需要多记站点要素的经纬度作为锚点；河段沿用已有的弹窗锚点。新增 `e2e/m11-sheet-auto-pan.mobile.mocked.spec.ts` 与 `e2e/m11-curve-anchor-desktop.mocked.spec.ts`（桌面 project）。

  Depends on: 4.2

  Verify：移动 spec 断言三个移动 project 下河段窗与气象代站窗打开并等相机静止后，锚点坐标在地图区内且在抽屉之外（底部抽屉之上 / 右侧抽屉之左）；390×664 → 750×342 后锚点在右侧抽屉之左；关闭抽屉后地图容器不带两个锚点属性。桌面 spec 断言 1280×900 下打开河段窗后锚点坐标与点击点相差不超过 1px，双窗都开且气象代站窗为活动窗时属性给出站点锚点。vitest 断言：平移不改变缩放级别；抽屉打开期间用户移动地图后不触发平移；手动移动后再切换布局会平移一次；关闭抽屉与离开移动形态都不触发相机移动；河段 → 站点替换时以新锚点再平移一次。

  **Suggested fixture level:** expanded - 改动共享地图组件的相机行为与页面级弹窗状态，并在地图容器上新增属性。

  **Minimal mergeable slice:** atomic - 平移、触发条件与可观测的锚点属性互为前提：没有属性无法在真实布局下断言平移结果，没有平移属性就只是点击点的回显。

## 5. 全页审计（`/`）

- [ ] 5.1 触控与字号审计：新增 `e2e/m11-touch-audit.mobile.mocked.spec.ts`，在三种状态（默认、每个面板分别展开、河段窗 / 气象代站窗分别打开）下遍历 `/` 上所有可见可点控件，断言 ≥ 44×44；遍历所有可见 `select` / select 触发器，断言字号 ≥ 16px；并断言三个移动 project 各自的视口下以及 `setViewportSize(320, 568)` 后，所有可见可点控件的包围盒在视口内。并加一条静态检查（vitest 或车道内的一条用例均可）：`e2e/` 下所有 `.mobile.` spec 都不含截图比对断言，也不含文档滚动宽度与窗口宽度的比较。豁免 MapLibre attribution 与开发用角色切换器。本 task 默认只加断言；若审计暴露残余不达标控件，在本 PR 内修掉并在 PR 描述里列出。

  Depends on: 2.5, 3.3, 3.5, 3.8, 4.6, 4.9

  Verify：三个移动 project 下审计通过。

  **Suggested fixture level:** compact - 以测试为主，可能附带零星尺寸类名修正。

  **Minimal mergeable slice:** atomic - 一条遍历式审计，按状态参数化；拆开只是同一断言的多个副本。

## 6. 运维页兜底

- [ ] 6.1 `/ops` 与 `/monitoring`：移动形态下内容单列，页面滚动容器左右内边距 ≥ 12px 且自身无横向溢出，内容超出时该容器可纵向滚到底而窗口不滚；任务表在自身容器内横向滚动；日志弹窗与其关闭按钮在视口内、日志区双向可滚；可聚焦的表单控件字号 ≥ 16px。把 `e2e/monitoring.mocked.spec.ts` 里可复用的 mock 构造抽到 `e2e/support/`（不改该 spec 的断言）。新增 `e2e/ops.mobile.mocked.spec.ts`。

  Depends on: 1.1, 2.1, 2.5

  Verify：对 `/ops` 与 `/monitoring` 两个路由，390×664 下卡片左右边距 ≥ 12px 且无并排卡片、页面滚动容器 `scrollWidth == clientWidth`、任务表容器 `scrollWidth > clientWidth`、滚动页面容器后最后一张卡片底边进入视口且 `window.scrollY == 0`（750×342 同样断言纵向滚动）、日志弹窗与关闭按钮包围盒在视口内且日志区双向可滚、所有可见 `select` / select 触发器 / `input` 字号 ≥ 16px；既有 `monitoring.mocked.spec.ts` 不改断言通过。

  **Suggested fixture level:** expanded - 字号下限要动全站共享的选择器基础组件的移动样式；其余是一个页面组件与一个弹窗的移动样式分支。

  **Minimal mergeable slice:** atomic - `/ops` 与 `/monitoring` 是同一个页面组件的两种模式；内边距、滚动与字号都落在同一组容器类名上。

- [ ] 6.2 `/system/model-assets`：移动形态下页面滚动容器自身无横向溢出，产品表在自身容器内横向滚动。新增 `e2e/model-assets.mobile.mocked.spec.ts`。既有的模型资产 mock 不含产品行；在 6.1 抽到 `e2e/support/` 的 mock 构造上扩展出“非空产品表”的变体。先确认既有 mock 下产品表是否已非空（前端对缺失产品有回退行），已非空则直接复用。若该页今天已满足约束，运行时代码零改动。

  Depends on: 6.1

  Verify：390×664 下、产品表非空时，页面滚动容器 `scrollWidth == clientWidth`，产品表容器 `scrollWidth > clientWidth`。

  **Suggested fixture level:** compact - 单页面的溢出约束；可能为纯断言。

  **Minimal mergeable slice:** atomic - 一个页面一条不变量。

## 7. 实机证据与收尾

- [ ] 7.1 node-27 实拍脚本支持设备预设：`scripts/node27_display_v2_browser_evidence.mjs` 新增可选的设备预设参数（视口、DPR、触屏、UA；至少 `mobile-portrait` 与 `mobile-landscape`）；布局检查按预设的形态参数化——现有 84 / 64 钉值只在桌面形态生效，移动形态检查头部 48、控件在视口内、图表区下限、选中锚点在未被抽屉遮住的地图区内；带预设时注入测试门控并用两个定位钩子点开河段窗与气象代站窗——河段的 bbox / anchor / 身份按既有 live-river-click 车道 preflight 的同一方式从展示 API 取得，气象代站的 `stationId` 与经纬度从站点列表 API 取得——输出默认 / 河段窗 / 气象代站窗三种状态的截图与几何 JSON。不带预设时行为与报告结构不变。脚本改为导出预设解析与“按形态选择期望”两个纯函数，并只在作为入口执行时才运行 `main()`。新建 `docs/runbooks/display-mobile-evidence.md`，写明命令、输出位置与通过条件。

  Depends on: 1.4, 4.4, 4.5, 4.10

  Verify：`node --test scripts/__tests__/node27_display_v2_browser_evidence.test.mjs` 通过，覆盖预设解析、非法预设报错、桌面与移动两种形态各自选出的期望；在 node-27 上不带预设对 live 展示入口运行，报告的键集合与改动前的 receipt 逐键一致；在 node-27 上以 `mobile-portrait` 预设运行，三张截图与几何 JSON 都能产出（采集路径跑通即可；报告是否通过取决于其余实现 task 是否已部署，归 7.2）。

  **Suggested fixture level:** expanded - 给脚本入口加 CLI 参数并承担默认行为不变的义务。

  **Minimal mergeable slice:** atomic - 预设解析、按形态参数化的检查与三状态采集是同一条新代码路径；runbook 是它的使用说明。

- [ ] 7.2 node-27 移动实拍 receipt：全部实现 task 部署到 node-27 后，按 `docs/runbooks/display-mobile-evidence.md` 以 `mobile-portrait` 与 `mobile-landscape` 两个预设对 live 展示入口运行 7.1 的脚本，receipt 存入 `docs/runbooks/receipts/`。

  Depends on: 2.3, 2.4, 3.4, 3.5, 3.6, 3.8, 4.3, 4.6, 4.8, 4.9, 4.10, 5.1, 6.1, 6.2, 7.1

  Verify：receipt 含两条完整命令行、部署 commit、六张截图与两份几何 JSON 的路径；两份报告均为通过，河段与气象代站图表区分别 ≥ 160px（竖屏）与 ≥ 120px（矮视口横屏），两种窗打开后选中锚点都在未被抽屉遮住的地图区内。

  **Suggested fixture level:** none - 运维证据，无代码变化。

  **Minimal mergeable slice:** atomic - 这是原“实拍 + 真机确认”的首刀（自动化产物）；真机确认已切为 7.3。

- [ ] 7.3 真机确认清单与结果：新建 `docs/runbooks/display-mobile-real-device-checklist.md`，列出模拟器证明不了的检查项——底部控件不被浏览器工具栏遮挡、控件不进安全区、选择器与输入框聚焦不自动放大、地图与图表的捏合 / 拖动无手势冲突、抽屉打开后选中要素在抽屉旁可见——以及 design.md Open Questions 的全部条目（全国级别能否点中河段）；iOS Safari 与 Android Chrome 各至少一台。记录用户逐项结果，失败项各立后续 issue 并在 receipt 引用。

  Depends on: 7.2

  Verify：receipt 中每个检查项在两类设备上都有结果；每个失败项有后续 issue 编号。

  **Suggested fixture level:** none - 文档与人工验收记录。

  **Minimal mergeable slice:** atomic - 清单与它的结果记录是同一份验收产物；只合清单不合结果没有验收价值。

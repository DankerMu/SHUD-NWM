# Tasks: mobile-responsive-display

## 约定（对所有 task 生效）

- **形态**：移动形态 = 视口宽 < 768px 或高 < 500px；矮视口横屏 = 移动形态且高 < 500px 横屏。
- **移动 project**：`mobile-portrait` 390×664、`mobile-landscape` 750×342、`mobile-landscape-wide` 844×390，全部 Chromium + 触屏。只匹配文件名含 `.mobile.` 的 spec。
- **移动 spec 的写法**：每个 `.mobile.` spec 必须在三个移动 project 下都通过；期望按“是否矮视口横屏”分支，统一用 `e2e/support/` 的形态判定助手，不各自发明跳过方式。需要特定视口（旋转、320×480、600×400）的用例在用例内 `setViewportSize`。
- **平板 / 桌面断言**：768×1024 与其他桌面形态视口的断言写在不含 `.mobile.` 的普通 spec 里（桌面 project，`setViewportSize`）。
- **spec 文件归属**：每个 task 新建自己的 spec 文件（需要时可以是一个移动 spec 加一个桌面 spec），不往别的 task 的 spec 里追加用例；共享的 mock 与助手放 `e2e/support/`。
- **共享选择器基础组件**：4.9 与 6.1 都需要它在移动形态下字号 ≥ 16px。两者不互相依赖：修订（#2810 fixture）：4.9 把曲线窗起报时次触发器与选项的移动字号、高度落在 `M11IssueTimeSelect` 自己的类上，**不碰基础组件**（基础组件由 `/ops` 等页面共用，改它会把 4.9 的影响面带出曲线窗）；基础组件的移动形态字号规则仍由 6.1 落地（只加移动形态字号，不改高度与桌面样式），不是“只验证”。
- **溢出 oracle**：不用 `document.scrollWidth == innerWidth`（外壳 `overflow-hidden`，恒真）；用“元素包围盒在视口 / 容器内”或具体滚动容器的 `scrollWidth` 对 `clientWidth`。
- **本地验证命令**：`cd apps/frontend && corepack pnpm typecheck && corepack pnpm test && corepack pnpm run test:e2e:mocked-regression`。下文 Verify 只写该 task 新增的断言。
- **治理门**：新增或改名 `apps/frontend/e2e/**` 文件的 task 另跑 `uv run pytest -q tests/test_entropy_audit_report_contract.py`。含宽 `page.route('**/api/v1/**')` 的文件，路径里必须有独立的 `mock` / `mocked` / `fixture` / `fixtures` / `deterministic` / `preview` / `visual` token 之一（按非字母数字切分；复数 `mocks` 不算），否则 Production Topology Hard Gate 变红（#2788 实测）。
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

- [x] 1.2 设计规范文档：更新 `docs/spec/06B_frontend_ui_design_spec.md` §8（最小支持分辨率与断点行为表），新增移动形态一行（判据、单图页的移动布局概要、运维页为兜底），并把“< 1280px 不推荐使用”改述为“768–1279px 为桌面形态下的既有降级行为、< 768px 或高 < 500px 为移动形态”，注明由本 change 引入。

  Depends on: 无

  Verify：`openspec validate mobile-responsive-display --strict --no-interactive` 通过；§8 的文字与 design.md D1 的判据数值一致。

  **Suggested fixture level:** none - 纯文档。

  **Minimal mergeable slice:** atomic - 一节文档的两张表，互相引用同一判据。

- [x] 1.3 河段窗测试夹具：让 mocked 车道能打开河段窗。内容：`e2e/support/` 提供一组 mock——非空流域、带真实有效时刻与瓦片模板的图层、径流瓦片（checked-in 的最小矢量瓦片夹具及其生成脚本，至少含一条带完整身份属性的河段）、气象站点列表、站点序列、河段 forecast-series；导出 `openRiverWindow(page)`：开 `__NHMS_E2E_HOOKS__` 门控、等地图就绪、用既有只读钩子 `window.__nhmsRiverClickEvidence.locateRenderedRiver` 定位河段（bbox、anchor 与四个身份字段取自夹具自身）、在返回的视口点做真实点击（桌面）或 `touchscreen.tap`（触屏）。不改产品代码。夹具只负责“从无窗状态打开一个窗”；不要求抽屉打开后再点其他要素（钩子会把要素移到地图区中央，那里在竖屏抽屉之下）。新增 `e2e/m11-river-open.mocked.spec.ts`（桌面 project）。`openRiverWindow` 须同时支持点击与触摸，但移动 project 下的首次开窗在 4.2——竖屏下未收纳的图例正盖住钩子定位到的地图区中央。

  Depends on: 1.1

  Verify：桌面 project（1280×900）下河段窗打开且曲线为已加载状态。

  **Suggested fixture level:** compact - 只新增测试支撑代码与一个二进制测试夹具，不改产品代码。

  **Minimal mergeable slice:** atomic - mock 组、瓦片夹具与 `openRiverWindow` 缺一则河段窗打不开，没有可验证的中间态。

  Triage（#2788）：Issue type: test ｜ Fixture level: compact（与上游建议一致；虽选了 File IO 与 Legacy compatibility 两个 pack，但只涉及离线生成的测试二进制、`src/` 零 diff、无运行时或车道配置变化，不触发 expanded）｜ Blast radius: 后续所有曲线窗 task（1.4、3.7、4.x、7.1）的 e2e 都建在这套夹具上——夹具打开的不是真实产品点击路径，或 mock 形状与真实 API 不符，会让下游断言建在假象上。
  - Risk pack「File IO / 二进制夹具」selected：checked-in 的最小矢量瓦片 + 生成脚本 -> 生成脚本可重复执行且输出与入库文件逐字节相同（在 PR 描述记录一次重跑的 `cmp` 结果）；瓦片内至少一条河段带完整身份属性，`openRiverWindow` 用到的 bbox / anchor / 四个身份字段取自夹具自身的单一导出，不在 spec 里另写一份。
  - Risk pack「Schema / field names」selected：mock 响应的形状须与 `src/api/types.ts` 的生成类型一致（图层、流域、站点列表、站点序列、河段 forecast-series）-> mock 数据用这些类型标注（`satisfies` 或显式类型），类型错误由下条的 tsc 命令暴露。
  - Risk pack「Release / dependency compatibility」selected：生成瓦片需要编码库 -> 只用 lockfile 里已有的包；若必须新增，只能是 devDependency 并在偏离记录里说明。`pnpm typecheck` / `check:types` 都不覆盖 `e2e/`，所以实现者须对新增的 `e2e/support/**` 与新 spec 另跑一次 strict `tsc --noEmit` 并报告结果。
  - Risk pack「Legacy compatibility」selected：既有只读钩子契约 -> `window.__nhmsRiverClickEvidence` 仍恰为三个方法，`src/` 零 diff（`git diff --stat origin/master -- apps/frontend/src` 为空）；既有 `riverClick*` 单测与 live 车道配置不受影响（`corepack pnpm test` 全绿、`check:types` exit 0）。
  - Risk pack「Error handling」selected：夹具静默失效 -> `openRiverWindow` 在钩子缺失、定位失败或窗未出现时抛出带原因的错误，不返回假成功；spec 断言曲线为“已加载”状态（有数据点的图表），不是只断言窗口节点存在。
  - 必备 mock（任务正文列出的之外，缺一则窗开不了或曲线到不了已加载）：(a) 全国径流周期目录 `/api/v1/layers/discharge/cycles?source=`，返回非空 `cycles` 与非 null `default_cycle`，否则全国径流层 fail-closed 置灰；图层 metadata 带 `url_template`、`maplibre_source_layer`、`min_zoom` / `max_zoom`、`default_source`、`default_cycle`、`valid_times`，形状参照 `src/test/overviewDataFixture.ts`。(b) `/api/v1/mvp/qhh/latest-product?identity_only=true`（GFS 与 IFS 各一次），河段面板先取它再取 forecast-series。(c) 夹具瓦片之外的径流瓦片与 `/api/v1/basemap/tianditu/*` 底图瓦片快速返回空或 204，否则地图等不到 `idle`。
  - 跨 mock 身份一致性：latest-product 的 run 身份、`cycle_time`、有效时段与 forecast-series 响应一致，能通过 `validateHydroMetRiverForecastForChart`；瓦片要素的 `basin_id` 等于流域 mock 的流域。
  - 瓦片要素属性名：`basin_id`、`basin_version_id`、`river_network_version_id`、`river_segment_id`；若同时带 `segment_id` 必须与 `river_segment_id` 相等（否则钩子拒绝）。
  - 瓦片坐标：夹具模块导出瓦片的 `(z, x, y)` 与 source-layer 名；所给 bbox 经钩子的 `fitBounds`（padding 48、maxZoom 14）并受图层 `max_zoom` 钳制后，1280×900 下请求须恰好落在该瓦片（spec 里断言该瓦片 URL 确被请求过）。
  - “曲线已加载”的 oracle：`m11-river-panel-chart` 可见，且 `m11-river-panel-empty`、`m11-river-panel-loading`、`m11-river-panel-pending` 均不存在；GFS 与 IFS 两个源都 mock 成功，`m11-river-panel-partial` 不存在。
  - 未选：Public API、Auth、Concurrency、Resource limits、Documentation——不改产品代码、不改车道配置。
  - Must preserve：既有 33 条桌面用例与期望值、三个移动 project 的基线、`playwright.config.ts` 不改；新 spec 文件名不含 `.mobile.`（移动 project 下的首次开窗在 4.2）。
  - 显式 non-goal：气象代站开窗（1.4）；移动 project 下开窗；抽屉打开后再点其他要素。站点列表与站点序列的 mock 在本 task 落地但不被本 task 的 spec 断言（由 1.4 消费）。
  - Evidence floor：约定的本地验证命令全绿 + 新 spec 在点击被跳过时为红（证明窗口确由点击打开）+ 重跑生成脚本（报告其路径与命令）后 `cmp <再生成文件> <入库文件>` exit 0 + 对 `e2e/support/**` 与新 spec 的 strict `tsc --noEmit`（用仓库外的临时 tsconfig，报告确切命令）exit 0。不得把 `e2e/support/**` 加进 `tsconfig.node-playwright.json`：它的 include 清单被 `src/__tests__/riverClickTypecheck.test.ts` 钉住。无运行时代码变化，不需要 node-27 live receipt。

- [x] 1.4 气象代站窗测试夹具：新增独立的门控全局 `window.__nhmsStationLocateEvidence`，恰有一个方法 `locateRenderedStation({ stationId, lngLat })`（把相机移到该点、确认该点渲染着这个 `station_id` 的 `met-stations` 要素且未被遮挡、返回视口点与身份；只读、不调用产品回调、无门控不暴露）；既有 `window.__nhmsRiverClickEvidence` 不改，仍恰为三个方法。`e2e/support/` 导出 `openStationWindow(page)`（入参取自 1.3 的站点 mock）。新增 `e2e/m11-station-open.mocked.spec.ts`（桌面 project）；移动 project 下的首次开窗同样在 4.2。

  Depends on: 1.3

  Verify：桌面 project（1280×900）下气象代站窗打开且曲线为已加载状态；不开门控时 `window.__nhmsStationLocateEvidence` 为 undefined；对未渲染的站点 id 调用返回失败；既有 river-click 钩子单测（含“恰为三个方法”的断言）不改通过。

  **Suggested fixture level:** expanded - 在产品代码的地图组件里新增测试门控的全局钩子（共享入口）。

  **Minimal mergeable slice:** atomic - 站点定位钩子与唯一使用它的 `openStationWindow` 必须同 PR，否则钩子没有调用方、助手没有定位手段。

  Triage（#2789）：Issue type: feature（测试门控的产品代码）｜ Fixture level: expanded（与上游建议一致；设计见 design.md D15 与 `specs/frontend-river-click-live-evidence`）｜ Blast radius: `M11MapLibreSurface` 是 `/` 的地图本体——钩子在未开门控时泄漏、改动既有 river-click 钩子，或给测试留下绕过真实点击的口子，都会破坏 live 证据车道的可信度；钩子定位不准则下游所有代站窗 e2e 建在假象上。
  - Change surface：`src/components/map/M11MapLibreSurface.tsx`（安装 / 卸载新全局）、新的站点定位钩子模块（与 `src/lib/riverClickEvidence/` 并列或同目录，沿用其结构）、`e2e/support/`（`openStationWindow` 与所需 mock 补充）、新 spec `e2e/m11-station-open.mocked.spec.ts`。
  - Governing invariant：进入产品点击路径的唯一方式是在钩子返回的视口点上做一次真实指针 / 触摸激活；钩子自身只读，不暴露 map ref 或通用查询面，不调用任何产品回调；未开 `window.__NHMS_E2E_HOOKS__ === true` 时两个全局都不存在。
  - Sibling surfaces：(1) 既有 `window.__nhmsRiverClickEvidence`（恰三个方法、安装 / 清理的代次 token 逻辑不变）；(2) 产品点击目标解析（站点聚合 -> 站点 -> overlay 命中层 -> 流域，river 钩子与产品点击共用）——站点钩子判定“该点渲染着这个站点”须复用同一解析，不另写一套；(3) 钩子的安装 / 卸载生命周期（StrictMode 双挂载、组件卸载后全局被移除、过期实例的清理不得删掉新实例的全局）；(4) live 车道（`playwright.live-*.config.ts`、`playwright.river-click-*.ts`、`src/lib/riverClickEvidence/**`）不改；(5) 测试侧 `e2e/support/openRiverWindow.ts` 与 `riverWindow.mocked.ts`（1.3 的站点列表 / 站点序列 mock 在此首次被真实请求打到，运行时形状须对上消费方）。
  - Must preserve（源码扫描）：`src/__tests__/riverClickPhase2Closure.test.ts` 取 `M11MapLibreSurface.tsx` 里第一个门控 `useEffect` 的文本，断言其中含 `createRiverClickEvidenceHook({ controller, pointerCapture })` 且不含 `onOverlayClick`。站点钩子的安装要么并入该 effect，要么放在它之后；放在它之前即红。
  - Must preserve：`src/components/map/__tests__/M11MapLibreSurfaceHook.test.tsx`、`src/__tests__/riverClickRealClick.test.ts`、`riverClickPhase2Closure.test.ts`、`riverClickLiveDisplayContract.test.ts`、`riverClickTypecheck.test.ts` 不改期望通过（其中有对源码文本与 tsconfig include 清单的钉死断言——新模块若放进 `src/lib/riverClickEvidence/` 会被 `tsconfig.node-playwright.json` 的 glob 收进去，须确认这些测试仍绿；不得为了过测试改它们的期望）；未开门控时普通指针、hover、选中、弹窗与请求行为不变；`src/test/maplibreStub.tsx` 的既有用法不被破坏。
  - Risk pack「Public API / entry」selected：新增全局钩子 -> vitest：开门控时全局恰有一个方法 `locateRenderedStation`；未开门控时为 undefined；river 钩子仍恰三个方法。e2e：不设门控加载 `/` 后两个全局均为 undefined。
  - Risk pack「Auth / 边界」selected（测试门控不得成为产品后门）-> 钩子对象上除该方法外无其他可枚举属性、返回值只含视口点与站点身份（无 map / feature 对象引用）；vitest 断言调用钩子不触发任何产品回调（选中、开窗、请求）。
  - Risk pack「Concurrency / shared state / ordering」selected：安装 / 清理生命周期 -> vitest 覆盖卸载后全局被移除、重挂载后仍可用、过期清理不删新实例。
  - Risk pack「Error handling」selected -> 未渲染的站点 id、站点图层未开、坐标非法、地图未就绪超时、该点被其他元素遮挡：各自返回带稳定错误码的失败且不返回点（vitest 逐项；e2e 至少覆盖“未渲染的站点 id”）。
  - Risk pack「Schema / field names」selected：站点要素属性与站点 mock -> 真实 `met-stations` 源属性为 `station_id` / `station_name` / `basin_id`（`src/pages/m11/useStationLayer.ts`）；站点列表与站点序列 mock 的形状对上 `bootstrap.ts` 与 `validateHydroMetStationSeriesIdentity`，代站窗曲线到达已加载状态即为证据。
  - Risk pack「Legacy compatibility」selected -> 上面 Must preserve 列出的五个既有测试文件零 diff 且通过。
  - 未选：File IO、Resource limits、Release、Documentation——无新二进制夹具（站点来自 GeoJSON 源而非瓦片）、无新依赖。
  - 实现须知：(a) 含宽 `page.route('**/api/v1/**')` 的 e2e 支撑文件，路径里必须有独立的 `mock` / `mocked` / `fixture` 等 token（治理门 `broad-e2e-api-mock`；复数 `mocks` 不算），否则 Production Topology Hard Gate 变红；(b) 站点可能被聚合成 cluster，钩子移动相机后须确认渲染的是单个站点而不是聚合点；(c) 站点图层默认关（`src/lib/m11/queryState.ts`），且只有要素数 > 0 时才显示：`openStationWindow` 用产品 URL 状态 `?metStations=1` 打开它，并先等站点层就绪（站点要素已渲染）再调钩子；Error handling 的“站点图层未开”即不带该参数时的状态。(d) 新模块只用相对 import（`tsconfig.node-playwright.json` 无 `paths`、无 jsx）；产品点击目标解析 `resolveM11ClickTarget` 像 river 钩子那样由 `M11MapLibreSurface` 注入，不在模块里 import `@/…`。
  - “曲线已加载”的 oracle：`m11-station-popup-loaded` 与至少一个 `m11-station-variable-<变量>-chart` 可见，`m11-station-popup-partial` 与 `m11-station-panel-refreshing` 不存在（GFS、IFS 两源都成功）。决定能否出图的消费方除 `validateHydroMetStationSeriesIdentity` 外，还有 `src/lib/hydroMet/stationSeries.ts` 的逐变量图表校验（unit、metadata、quality_flag、limit）——1.3 的站点序列 mock 若过不了它，在本 task 修 mock。
  - Seams under test：浏览器里的 `window.__nhmsStationLocateEvidence` 与真实点击后的代站窗；vitest 里的钩子模块（用既有 maplibre 桩）。
  - Non-goals：移动 project 下开窗（4.2）；抽屉；对 river 钩子的任何改动；live 车道接入站点钩子。
  - Review focus：(1) 钩子确为只读且无产品回调；(2) 未开门控零暴露；(3) 复用产品点击目标解析而非另写；(4) river 钩子及其测试零改动；(5) 跳过真实点击时 spec 为红。
  - Evidence floor：约定的本地验证命令全绿 + `corepack pnpm run check:types` exit 0（CI 另跑它）+ `uv run pytest -q tests/test_entropy_audit_report_contract.py` 通过 + 新 vitest 与新 e2e 先红后绿 + 跳过点击为红；本 task 改展示端运行时代码，合并前在 node-27 出 live receipt（PR 构建对 live API：不设门控时两个全局均不存在，桌面布局 oracle 不回归）。

## 2. 移动形态基座

- [x] 2.1 移动形态判据：`src/index.css` 用块写法（含 `@slot`）声明 `mobile` 与 `mobile-landscape` 两个自定义变体，禁止单行简写；新增导出两条媒体查询字符串的模块与 `useMobileForm()` hook；`AppShell` 根节点输出 `data-viewport-form`、`data-viewport-short-landscape`，并经 `mobile:` 变体设置自定义属性 `--nhms-viewport-form`（桌面为 `desktop`）。零视觉变化。新增 `e2e/m11-viewport-form.mocked.spec.ts`（桌面 project，`setViewportSize`）。

  Depends on: 无

  Verify：e2e 断言 767×900 / 768×900、900×499 / 900×500、390×664、320×480、600×400 的两个 data 属性取值，390×664 → 750×342 不刷新即更新，844×390 与 1280×900 下 data 属性与计算后的 `--nhms-viewport-form` 一致；vitest 用可控 `matchMedia` 桩验证 hook 的订阅与更新。

  **Suggested fixture level:** expanded - 在所有路由共享的 `AppShell` 根节点上加属性与类名。

  **Minimal mergeable slice:** atomic - CSS 变体、hook 与根节点上的两侧可观测标记必须同 PR 落地，“CSS 与 JS 一致”的断言才成立；拆开后任一半都没有可验证行为。

  Triage（#2790）：Issue type: feature ｜ Fixture level: expanded（与上游建议一致；设计见 design.md D1）｜ Blast radius: `AppShell` 包住每个路由——判据或订阅写错会让后续所有移动 task 建在错误的形态信号上；根节点 className 写错会改变全站布局。
  - Change surface：`src/index.css`（两个 `@custom-variant`）、新的查询字符串模块与 `useMobileForm()` hook、`src/components/layout/AppShell.tsx` 根节点（两个 data 属性 + 一个 CSS 自定义属性）。
  - Governing invariant：任一视口下，CSS 变体 `mobile` 是否命中、`useMobileForm().mobile`、根节点 `data-viewport-form` 三者结论相同，且都等于“宽 < 768px 或高 < 500px”；`mobile-landscape`、`.landscape`、`data-viewport-short-landscape` 同理等于“高 < 500px 且横屏”。
  - Sibling surfaces：CSS 侧（`index.css` 变体）、JS 侧（hook 与查询常量）、可观测面（`AppShell` 两个 data 属性与 `--nhms-viewport-form`）、测试侧镜像（`e2e/support/viewportForm.ts`，1.1 已落地，数值须一致）。除 `AppShell` 外本 task 不接入任何组件——“none：消费者在后续 task”。
  - Must preserve（测试环境）：`apps/frontend/src/test/setup.ts` 的全局 `matchMedia` 桩恒 `matches: false`，本 task 不改它；经它渲染的既有 `AppShell.test.tsx` / `AppFrame.test.tsx` 须得到 `data-viewport-form="desktop"`、`data-viewport-short-landscape="false"`。hook 用例的“可控桩”在用例内覆盖 `window.matchMedia` 并在用例后还原。
  - 取值表补充：1280×900 -> `desktop` / `false` / 计算后的 `--nhms-viewport-form` 为 `desktop`。
  - Must preserve：零视觉变化——`AppShell` 根节点既有 className（`h-screen w-screen` 等）逐字保留，只追加；既有 `AppShell.test.tsx`、`AppFrame.test.tsx` 不改期望通过；`m11-overlay-collision` 四个桌面宽度不改期望通过；不引入新依赖。
  - Risk pack「Config / project setup」selected：Tailwind v4 自定义变体写法 -> e2e 在 844×390（只靠高度臂）断言计算后的 `--nhms-viewport-form` 为 `mobile`，证明块写法下高度臂没有被丢。另加一条 vitest：读 `src/index.css` 原文，断言 `mobile` 与 `mobile-landscape` 两个 `@custom-variant` 块各自含查询模块导出的那条查询字符串且含 `@slot`（`mobile-landscape` 在本 task 没有 CSS 消费者，这条是它 CSS 侧的唯一证据，同时把“查询字符串单一来源”变成测试）。
  - Risk pack「Concurrency / shared state / ordering」selected：`matchMedia` 订阅与清理 -> vitest 用可控桩验证初值、change 事件后更新、卸载后移除监听；e2e 验证 390×664 → 750×342 不刷新即更新。
  - Risk pack「Public API / entry」selected：共享外壳根节点新增属性 -> e2e 七个视口的属性取值表（767×900 / 768×900、900×499 / 900×500、390×664、320×480、600×400）。
  - Risk pack「Error handling」selected：`window.matchMedia` 不存在或抛错的环境（旧 jsdom 桩）-> hook 回落到桌面形态且不抛，vitest 覆盖。
  - 未选：File IO、Schema、Auth、Resource limits、Legacy compatibility、Release、Documentation——无数据、无接口、无依赖变化；文档在 1.2 已更新。
  - Seams under test：浏览器里的 `AppShell` 根节点（data 属性 + 计算样式）；`useMobileForm()` 的返回值。
  - Non-goals：dvh / 安全区（2.2 / 2.3）、任何组件消费该 hook 或变体、任何可见样式。
  - Review focus：(1) 两个变体确为块写法且含 `@slot`；(2) 查询字符串单一来源，CSS 与 JS 的阈值一致（767.98 / 499.98）；(3) hook 的监听在卸载时移除、两条查询都订阅；(4) 根节点既有 className 未被改动；(5) e2e 断言读的是真实计算样式而不是 data 属性自证。
  - Evidence floor：约定的本地验证命令全绿 + 新 e2e spec 与新 vitest 用例先红后绿；本 task 改展示端运行时代码，合并前在 node-27 出 live receipt（部署后桌面布局 oracle 不回归）。

- [x] 2.2 外壳动态视口：`AppShell` 根容器由 `h-screen w-screen` 改为 `h-dvh w-full`。新增 `e2e/m11-shell-viewport.mocked.spec.ts`。

  Depends on: 2.1（无功能耦合；2.1、2.2、2.3 改的是 `AppShell` 根节点同一行 className，串行以免冲突）

  Verify：e2e 断言根节点高度声明解析自 `100dvh`（读匹配的样式规则，不只读计算值）；1280×900 下根节点渲染宽高等于视口宽高。

  **Suggested fixture level:** expanded - 改 `AppShell` 根容器的尺寸声明，影响每个路由的高度计算。

  **Minimal mergeable slice:** atomic - 高与宽是同一根节点同一 className 上的一次替换，是“控制条被浏览器工具栏切掉”的独立修复。

  Triage（#2791）：Issue type: bugfix ｜ Fixture level: expanded（与上游建议一致；设计见 design.md D2 / D3）｜ Blast radius: `AppShell` 根容器决定每个路由的可用高度——写错会让所有页面溢出或塌缩。
  - Change surface：`src/components/layout/AppShell.tsx` 根节点 className 的两个 token（`h-screen` -> `h-dvh`，`w-screen` -> `w-full`），其余 token 逐字保留。
  - Governing invariant：外壳根节点恰好占满动态视口——渲染宽高等于视口宽高，子布局（头部 + `main` 的 flex 列）不变。
  - Sibling surfaces：(1) `AppShell` 包住的每个路由（`/`、`/ops`、`/monitoring`、`/system/model-assets`）；(2) 其他使用视口单位的地方——全仓 `src` 只有 `components/ui/toast.tsx` 的 `max-h-screen`，它是 fixed 定位的 toast 视口上限，不在本 task 范围（保持不动，写为 non-goal）；(3) `html` / `body` / `#root` 的高度链（已核实）：`src/index.css` 是 `html, body, #root { min-height: 100% }` 加 `body { margin: 0; min-width: 320px }`，`html` / `#root` 无宽度约束；`w-full` 只在视口 < 320px（规格域外）或文档出现纵向滚动条时与 `w-screen` 不同；(4) node-27 桌面布局 oracle（84 / 64 / 无横向滚动）。
  - Must preserve（既有测试）：`src/components/layout/__tests__/AppShellViewportForm.test.tsx` 有一条断言钉死根节点 className 以 `relative flex h-screen w-screen flex-col overflow-hidden …` 开头（2.1 为“只追加”写的护栏）。本 task 合法地改这两个 token，须同步更新该断言为新前缀，除此之外该文件不改；`AppShell.test.tsx`、`AppFrame.test.tsx` 不改通过；`m11-overlay-collision` 四个桌面宽度不改期望通过；`e2e/monitoring.mocked.spec.ts` 里依赖外壳高度的断言（`windowScrollY == 0`、`documentScrollHeight <= innerHeight + 1`）不改通过。
  - Risk pack「Public API / entry」selected：共享外壳尺寸 -> e2e：`/` 在 1280×900、768×1024、390×664、750×342 下，根节点（选择器 `[data-viewport-form]`）`getBoundingClientRect()` 的宽高与 `window.innerWidth` / `innerHeight` 相差不超过 1px；`/ops` 在 1280×900 下同样成立。
  - Risk pack「Config / project setup」selected：Tailwind 是否生成 `h-dvh` -> e2e 在 `/` @ 390×664 读匹配根节点 `[data-viewport-form]` 的样式规则：递归进入分组规则（`@layer` / `@media` / `@supports` 的 `.cssRules`——Tailwind v4 的工具类在 `@layer utilities` 里，只遍历顶层找不到），用 `root.matches(rule.selectorText)` 选规则，断言声明了 `height` 的命中规则恰有一条且 `rule.style.height === '100dvh'`（不只读计算值——模拟器里 `dvh == vh`，计算值区分不出来）；变异证据：改回 `h-screen` 时该断言为红。
  - Risk pack「Legacy compatibility」selected：不支持 `dvh` 的旧浏览器会让高度声明失效、根节点塌缩。Tailwind v4 自身的浏览器基线（Safari 16.4+、Chrome 111+、Firefox 128+）已高于 `dvh` 的支持线（Safari 15.4、Chrome 108、Firefox 101），所以不加 `vh` 回退——写为显式 non-goal。
  - 未选：File IO、Schema、Auth、Concurrency、Resource limits、Release、Error handling、Documentation。
  - Seams under test：浏览器里的外壳根节点（匹配规则文本 + 包围盒）。
  - Non-goals：安全区内边距与 viewport meta（2.3）；头部（2.4）；toast 的 `max-h-screen`；“控制条是否避开真机浏览器工具栏”的行为验证（真机清单，7.3）。
  - Review focus：(1) 只改了两个 token；(2) 断言读的是规则文本里的 `100dvh`；(3) `w-full` 下根节点宽度仍等于视口（无父级约束）；(4) 2.1 的 className 护栏断言被更新而非删除。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；新 e2e 先红后绿；node-27 live receipt（PR 构建对 live API：既有脚本 `scripts/node27_display_v2_browser_evidence.mjs` 的桌面 oracle 不回归；四个视口的根节点包围盒由编排者用仓库外的一次性探针脚本量取并记入 receipt，不入库、不放宽 PR 边界）。
  - spec 文件归属：新 spec `e2e/m11-shell-viewport.mocked.spec.ts` 文件名不含 `.mobile.`，在桌面 project 里用 `setViewportSize` 覆盖 390×664 与 750×342（先例：2.1 的 `m11-viewport-form.mocked.spec.ts`）。

- [x] 2.3 安全区：`index.html` viewport meta 加 `viewport-fit=cover`；`AppShell` 根容器无条件应用四边 `env(safe-area-inset-*)` 内边距。新增 `e2e/m11-shell-safe-area.mocked.spec.ts`。

  Depends on: 2.2

  Verify：e2e 断言 meta content 含 `viewport-fit=cover`、根节点内边距声明引用四个 `env(safe-area-inset-*)`、1280×900 下四边计算内边距为 0。真机上的遮挡行为归 7.3 清单。

  **Suggested fixture level:** expanded - 改全局 viewport meta 与共享外壳的内边距。

  **Minimal mergeable slice:** atomic - `viewport-fit=cover` 不配安全区内边距会让内容进入刘海区，二者必须同 PR。

  Triage（#2792）：Issue type: feature ｜ Fixture level: expanded（与上游建议一致；设计见 design.md D3）｜ Blast radius: 全站——viewport meta 是文档级配置，外壳内边距影响每个路由的内容区；`viewport-fit=cover` 生效而内边距缺失 / 写错时，刘海屏设备上头部与控制条会被系统区域遮住。
  - Change surface：`apps/frontend/index.html` 的 viewport meta（追加 `viewport-fit=cover`，既有 `width=device-width, initial-scale=1.0` 保留）；`src/components/layout/AppShell.tsx` 根节点追加四个物理方向的内边距类 `pt-[env(safe-area-inset-top)]`、`pr-[…right]`、`pb-[…bottom]`、`pl-[…left]`（不得用 `px-` / `py-` / `ps-` / `pe-`：它们编译成逻辑属性 `padding-inline` / `padding-block`，`rule.style.paddingTop` 等读出来为空），无条件、不按形态分支；既有 token 逐字保留。
  - Governing invariant：外壳根节点仍恰好等于视口（border-box：内边距向内吃，不撑大根节点）；四边内边距各自等于对应的安全区 inset，inset 为 0 时布局与改动前逐像素相同。
  - Sibling surfaces：(1) `AppShell` 包住的每个路由的内容区（头部 + `main`）；(2) 不在根节点流内、因而不受这层内边距保护的 fixed / portal 元素：`components/ui/toast.tsx` 的 toast 视口（`fixed bottom-0 right-0`）、Radix Dialog / Select 的弹层——本 task 不处理，写为 non-goal 并在 7.3 真机清单里核对；(3) 地图区内贴底 / 贴边的绝对定位元素（控制条、图例、启动器）位于 `main` 内，随内容区内缩，不重复加安全区（D3）；(4) 既有读 viewport meta 的测试：`e2e/m11-baseline.mobile.mocked.spec.ts` 断言 content 匹配 `/width=device-width/`，追加后仍成立；(5) 2.2 的规则文本护栏 `e2e/m11-shell-viewport.mocked.spec.ts`（声明 `height` 的命中规则恰一条）——本 task 只加 padding 类，不得引入第二条声明 `height` 的规则；(6) `AppShellViewportForm.test.tsx` 的 className 前缀护栏——只在末尾追加，前缀不变。
  - Must preserve：上述 (4)(5)(6) 三个既有测试文件零 diff 且通过；`m11-overlay-collision` 四个桌面宽度与 `monitoring.mocked.spec.ts` 不改期望通过；node-27 桌面布局 oracle（头部 84、控制条 64、无横向滚动）不回归。
  - Risk pack「Config / project setup」selected：文档级 meta 与 Tailwind 任意值类 -> 新 e2e `e2e/m11-shell-safe-area.mocked.spec.ts`（桌面 project，文件名不含 `.mobile.`，`setViewportSize`）在 `/` @ 390×664：meta content 同时含 `width=device-width`、`initial-scale=1` 与 `viewport-fit=cover`；用 2.2 同样的递归取法读命中根节点 `[data-viewport-form]` 的样式规则，对 `padding-top` / `-right` / `-bottom` / `-left` 每一边，命中规则里文本含对应 `env(safe-area-inset-<边>)` 的声明恰一条（Tailwind preflight 对 `*` 声明了 `padding: 0`，也命中根节点，所以不能断言“每边只有一条声明”；不只读计算值——模拟器里 `env()` 为 0）。变异证据：去掉任一内边距类，对应断言为红；把 meta 的 `viewport-fit=cover` 去掉，meta 断言为红。
  - Risk pack「Public API / entry」selected：共享外壳 -> e2e：`/` @ 1280×900 与 390×664、`/ops` @ 1280×900 下根节点四边计算内边距均为 `0px`，且根节点包围盒仍等于视口（±1px）。
  - Risk pack「Legacy compatibility」selected：`env()` 与 `viewport-fit` 在不支持的浏览器上 -> `env(safe-area-inset-*)` 的支持线（Safari 11.1、Chrome 69）远低于 Tailwind v4 基线，未知 `viewport-fit` 值被浏览器忽略；不加回退，写为显式 non-goal。
  - 未选：File IO、Schema、Auth、Concurrency、Resource limits、Release、Error handling、Documentation。
  - Seams under test：浏览器里的文档 meta 与外壳根节点（规则文本 + 计算内边距 + 包围盒）。
  - Non-goals：fixed / portal 元素（toast、Dialog、Select 弹层）的安全区处理；地图区内元素重复加安全区；真机上刘海 / 底部指示条是否遮挡控件的行为验证（7.3 真机清单）；头部（2.4）。
  - Review focus：(1) 内边距无条件应用、四边齐全、引用的是对应方向的 `env()`；(2) 根节点为 border-box，内边距不改变其外尺寸；(3) meta 既有两项保留；(4) 断言读的是规则文本；(5) 没有往别的 task 的 spec 追加用例，三个 must-preserve 测试文件零 diff。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；新 e2e 先红后绿并附上述两条变异证据；node-27 live receipt（PR 的生产构建对 live API：既有脚本的桌面 oracle 不回归；编排者用仓库外的一次性探针量取 meta、四边规则文本与计算内边距，记入 receipt）。

- [x] 2.4 头部压缩：`SiteHeader` 在移动形态为 48px 单行（徽标缩小、标题单行截断、不渲染英文副标题）；桌面形态不变。不改任何导航高度 token。新增 `e2e/m11-header.mobile.mocked.spec.ts` 与 `e2e/m11-header-desktop.mocked.spec.ts`。

  Depends on: 1.1, 2.1, 2.5

  Verify：移动 spec 断言三个移动 project 下头部高 48、标题为一行高、副标题不存在、地图区顶边 y=48，`/ops`（用 `setRole` 切到 operator）头部同为 48；桌面 spec 断言 `/` 在 768×1024 与 1280×900 下头部 84 且副标题存在，`/ops`、`/monitoring`、`/system/model-assets` 在 1280×900 下头部 84，`/` 在 520×900 下头部 48（窄桌面窗口走移动形态）。

  **Suggested fixture level:** expanded - `SiteHeader` 由 `AppShell` 在所有路由渲染，头部变矮会改变每个页面的内容区高度。

  **Minimal mergeable slice:** atomic - 一个组件的一组移动样式，头部高度与标题单行互相决定，无可独立交付的子集。

  Triage（#2816）：Issue type: feature ｜ Fixture level: expanded（与上游建议一致；设计见 design.md D4，规格见 `specs/mobile-viewport-shell` 的「The site header is compact in mobile form」）｜ Blast radius: 全站——`SiteHeader` 由 `AppShell` 在每个路由渲染，头部高度决定每个页面内容区的高度；写错会让桌面头部变矮（品牌口径回归），或让移动形态头部换行把内容区再压掉一截。
  - Change surface：只有 `src/components/layout/SiteHeader.tsx` 的 className（追加 `mobile:` 变体类）、两个新 spec、它们共用的新助手 `e2e/support/siteHeader.mocked.ts`、必要时一个新 vitest 文件。`AppShell.tsx`、`src/index.css`、`useMobileForm.ts`、任何高度 token 零 diff。
  - 实现口径（裁定）：纯 CSS——在既有元素上**追加** `mobile:` 变体类（`src/index.css` 的自定义变体，2.1 已落地），不引入 `useMobileForm()`、不按形态分支 JSX。理由：头部是首屏元素，CSS 变体没有首帧闪动；既有 `SiteHeader.test.tsx` 钉着 `h-[84px]`、`text-[28px]`、`font-extrabold`、`h-14`、`object-contain` 等桌面类，追加类不动它们。“不渲染英文副标题”因此落为移动形态下 `display: none`（不占位、对辅助技术不可见）；e2e 的 oracle 是“副标题不可见”（`toBeHidden`），不是“不在 DOM”。
  - Governing invariant：头部高度只由视口形态决定——移动形态恒 48px 单行、桌面形态恒 84px，对所有路由一致；头部内任何元素都不得把头部撑高或溢出头部的包围盒。
  - Sibling surfaces：(1) 头部内全部四个元素——徽标（移动 32px）、标题（移动 16–18px、单行截断，其容器须能收缩：`min-w-0`）、英文副标题（移动隐藏）、合作单位条；(2) 合作单位条的**显示规则不改**（仍是 `lg` 以上才显示），但移动形态经高度臂可以与 `lg` 同时成立（例如 1280×400 的窗口）：此时 56px 高的图片会溢出 48px 头部，须在移动形态收它的高度使其留在头部包围盒内——这是“头部不被撑破”的一部分，不是改显示规则；(3) `AppShell` 根容器的安全区上内边距（2.3）：头部在内边距之下，48px 不含安全区；(4) 依赖头部高度的既有元素都按实测几何推算、不钉 84：`main` 左缘垂直居中的角色切换器（2.5）、启动器列与展开面板限高（3.2 / 3.3）、控制条；(5) `--m11-nav-height` 与 `m11VisualTokens` 不碰（D4）。
  - Must preserve：桌面形态（宽 ≥ 768 且高 ≥ 500）头部 84px、标题 28px extrabold、副标题可见、徽标 48px、合作单位条 `lg` 以上 56px 高——既有 `src/components/layout/__tests__/SiteHeader.test.tsx` 零 diff 通过；全部既有 vitest 文件与全部既有 e2e spec 零 diff 且通过，其中头部变矮后仍须原样通过的移动 spec：`m11-baseline.mobile`、`m11-role-selector.mobile`（触发器不与头部、控制条相交）、`m11-zoom-control.mobile`、`m11-legend-launcher.mobile`、`m11-layer-basemap-launchers.mobile`，以及桌面 project 里用 `setViewportSize` 进入移动形态的 `m11-shell-viewport`、`m11-shell-safe-area`、`m11-viewport-form`、`m11-launchers-desktop`、`m11-overlay-collision` 的 520 行；碰撞 spec 的 1920 / 1440 / 1280 / 800 四个宽度不改期望值通过。对多出的 36px 敏感的既有断言有两处，都是矮视口横屏“必须先溢出”的前置：`m11-legend-launcher.mobile` 的图例面板（内容约 347px 对可用约 222px，安全）与 `m11-layer-basemap-launchers.mobile` 的图层面板（844×390 下内容 240px 对预计可用约 220px，余量约 20px）。若某条既有断言因头部 48px 变红，停下报告确证的原因，不改那个 spec。
  - Risk pack「Public API / entry」selected：全路由可见的头部 -> 移动 spec `e2e/m11-header.mobile.mocked.spec.ts`（三个移动 project）：`/` 上 `header`（`role="banner"`）包围盒高 48（±0.5）、y=0；标题元素可见且渲染高度为一行（标题包围盒高 ≤ 其计算 `line-height` 的 1.5 倍，且 `white-space: nowrap`）、计算字号在 16–18px；副标题 `toBeHidden`；徽标可见且 32×32；`m11-fullscreen-map` 顶边 y=48（±0.5）、底边等于视口高；`/ops` 用例的顺序钉死（auth store 不持久化，`goto` 会把角色复位成 viewer；顺序同 `m11-role-selector.mobile` 既有用例）：`goto('/ops')` -> 「权限不足」可见 -> `setRole('operator')` -> 标题「内部诊断」可见 -> 再量头部 = 48。桌面 spec `e2e/m11-header-desktop.mocked.spec.ts`（桌面 project，`setViewportSize`）：`/` 在 768×1024 与 1280×900 下头部 84、副标题可见、标题计算字号 28px、徽标 48×48；`/ops`（operator，标题「内部诊断」）、`/monitoring`（operator，标题「监控工作台」）、`/system/model-assets`（model_admin，标题「模型资产管理」）在 1280×900 下头部 84——每条都是先 `goto`、再 `setRole`、断言到授权后的页面标题可见之后才量头部；`/` 在 520×900 下头部 48（窄桌面窗口走移动形态）。
  - Mock 来源：这些路由需要非只读的 runtime-config 与各页自己的 API mock；既有的都是 spec 私有函数（`m11-role-selector.mobile` 的 `mockApi`、`monitoring.mocked.spec.ts` 的 `mockControlledOpsApi` / `mockModelAssetsApi`），既有 spec 零 diff，所以两个新 spec 共用一个新助手 `e2e/support/siteHeader.mocked.ts`（文件名含独立的 `mocked` token），只 mock 到“授权后的页面标题能渲染”为止。
  - Risk pack「Resource limits / 溢出」selected：窄宽与高度臂 -> 移动 spec 一条用例内 `setViewportSize(320, 568)`：头部仍 48、标题单行、标题包围盒右边 ≤ 头部右边（截断而非溢出）、徽标完整可见；桌面 spec 一条 `/` 在 1280×400（高度臂 + `lg`）：头部 48，合作单位条 `toBeVisible`（1280 满足 `lg`，显示规则不改——用 `mobile:hidden` 把它藏掉即违反 non-goal）且其包围盒完全在头部包围盒内；`/` 在 1280×900 下合作单位条可见且高 56（显示规则未变的证据）。
  - Risk pack「Legacy compatibility」selected -> Must preserve 所列各项；PR 描述贴出 `git diff --stat --diff-filter=MDR origin/master -- apps/frontend/e2e 'apps/frontend/src/**/__tests__/**'`（期望输出为空）证明既有测试零 diff。
  - Risk pack「Concurrency / shared state」not selected：纯 CSS，无状态；形态往返由媒体查询即时决定（520×900 用例与既有 `m11-viewport-form` 覆盖形态信号）。
  - Risk pack「Documentation」not selected：`docs/spec/06B_frontend_ui_design_spec.md` §8 已在 1.2 写明移动形态头部 48px 单行，无需再改；§143 的“高度：84px”是桌面口径，仍成立。
  - 未选：File IO、Auth、Schema、Release、Config、Error handling。
  - Seams under test：浏览器里的 `header` 包围盒、标题 / 副标题 / 徽标 / 合作单位条的可见性与计算样式、地图区顶边。vitest 不新增形态断言（jsdom 不算媒体查询）；若新增 vitest，只钉“桌面类仍在、`mobile:` 类已加”。
  - Non-goals：任何导航高度 token；合作单位条的显示规则；头部渐变是否铺进安全区上内边距带（7.3 真机清单，已留言 #2818）；`/ops` 等页面内容区自身的移动布局（6.x）；利用多出的 36px 重排地图浮层（各自的 task）。
  - Review focus：(1) 桌面类逐字保留、只做追加；(2) 头部在移动形态下不可能被任何子元素撑高（含 1280×400 的合作单位条、320 宽的标题）；(3) 既有移动 spec 零 diff 通过，没有因头部变矮而被改写；(4) 每条移动断言在三个移动 project 下都成立，没有按 project 跳过；(5) 没有碰 token 与 `AppShell`。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；新 spec 的 strict `tsc --noEmit` exit 0；两个新 spec 先红后绿（红：对 `origin/master` 的 `src/`；桌面 spec 里改动前本就成立的 84px 断言可以先绿，须在报告里点名哪些用例是红的），移动 spec 三个移动 project 各非零 passed，`--repeat-each=5` 无 flaky；改展示端运行时代码，合并前出 node-27 live receipt（PR 的生产构建对 live API：390×664 / 750×342 / 844×390 下头部 48、副标题不可见、地图区顶边 y=48、三个启动器与控制条仍在地图区内；1280×900 与 768×1024 下头部 84、副标题可见——这些视口的量测来自编排者在仓库外的一次性探针；`scripts/node27_display_v2_browser_evidence.mjs` 对头部 ≠ 84 与控制条 ≠ 64 硬失败，只按它默认的 1920×1080 运行、零 diff、不回归）。

- [x] 2.5 开发用角色切换器的移动位置：role override 开启时，移动形态下把 `AppShell` 的角色切换器收成宽不超过 56px 的紧凑触发器，移到 `main` 左边缘、垂直居中，层级高于地图浮层，保持可见可操作；桌面形态位置不变；未开启 role override 时两种形态都不渲染。在 `e2e/support/` 提供 `setRole(page, role)` 助手。新增 `e2e/m11-role-selector.mobile.mocked.spec.ts`。

  Depends on: 1.1, 2.1

  Verify：三个移动 project 下切换器包围盒在视口内、宽不超过 56px、不与头部和控制条相交，用它切到 operator 后角色生效；vitest 断言未开启 role override 时不渲染。它与启动器、顶部提示条带不相交的断言分别在 3.3、3.4（那时这些元素才存在）。

  **Suggested fixture level:** compact - 一个仅开发构建存在的控件的移动定位。

  **Minimal mergeable slice:** atomic - 一个元素的定位分支加一个测试助手。

  Triage（#2793）：Issue type: feature（仅开发 / 测试构建可见的控件）｜ Fixture level: compact（与上游建议一致；设计见 design.md D5 末段。虽选了 Public API、Auth、Legacy compatibility 三个 pack，但该控件只在开发 / 测试构建渲染、生产构建零渲染，桌面形态的计算几何与行为不变，设计已在共用的 design.md，不触发 expanded）｜ Blast radius: mocked 车道——后续所有需要 operator 角色的移动 spec 都靠这个切换器与 `setRole` 助手；定位写错会让它在移动形态下被头部 / 控制条盖住而点不到。生产构建不渲染它，线上无影响。
  - Risk pack「Public API / entry」selected：`AppShell` 内的共享控件 -> 新 spec `e2e/m11-role-selector.mobile.mocked.spec.ts`（三个移动 project）：在 `/` 上切换器触发器包围盒在视口内、宽 ≤ 56px、与头部（`SiteHeader`）和底部控制条的包围盒不相交；`elementFromPoint` 在其中心命中的是触发器自身或其后代（未被地图浮层盖住）。移动形态下切换器的 z-index 须大于 130（现有地图浮层最高为浮动提示的 `z-[130]`，图层面板 `z-[120]`）且小于 `--z-popover`（300，Radix 弹层）；spec 断言计算后的 z-index 落在该区间。
  - Risk pack「Legacy compatibility」selected：既有消费者按可访问名找它 -> 触发器的 `aria-label="Role"` 不变；桌面形态下的位置（`main` 的 `right-4 top-4`）、宽度（`w-36`）与显示文本不变——`e2e/monitoring.mocked.spec.ts`（`getByLabel('Role')` 可见、可点、`toHaveText('Viewer')`）零 diff 且通过；`src/components/layout/__tests__/AppShell.test.tsx` 零 diff 且通过；`src/test/c4DisplayFakePage.ts` 不改。
  - Risk pack「Auth / permissions」selected：角色覆盖只在开发 / 测试构建存在 -> vitest：`isRoleOverrideEnabled` 为 false 时两种形态都不渲染切换器（新测试文件里用可控 `matchMedia` 桩覆盖移动形态；桌面形态已有既有断言）；`src/stores/auth.ts` 零 diff。
  - Risk pack「Error handling」selected：助手不得假成功 -> `setRole(page, role)` 通过真实 UI 操作切换：对触发器与选项用 `locator.click()`，不带 `force`、不用 `tap()`（既有 `monitoring.mocked.spec.ts` 里的私有 `selectRole` 用 `click({ force: true })`，会跳过可操作性检查，不得照抄）；成功判据是 `getByLabel('Role')` 的 `toHaveText(<该角色的显示名>)`——为此移动形态的紧凑触发器须把当前角色的显示名保留在 DOM 里（`SelectValue` 文本不移除，只做视觉截断 / 隐藏溢出），使该断言在两种形态下都成立；找不到切换器或选项时抛带原因的错误。
  - 未选：File IO、Schema、Concurrency、Resource limits、Release、Documentation、Config。
  - “角色生效”的 oracle：输入为 `/ops`，且新 spec 自带 `/api/v1/runtime/config` 的 mock 返回 `display_readonly: false`（`/ops` 的 `RBACGate` 带 `allowDisplayReadonly`：不 mock 时 viewer 先等 runtime config，若为只读部署则 viewer 直接放行，oracle 落空；`monitoring.mocked.spec.ts` 里的同类 mock 是它私有的，不改它）。期望：`getByText('权限不足')` 可见 -> `setRole(page, 'operator')` -> 该文本计数为 0 且 heading「内部诊断」可见；三个移动 project 下都成立。
  - Must preserve：桌面形态下切换器的计算几何（`main` 内 `right: 16px; top: 16px`、宽 144px）、`aria-label` 与行为不变（移动形态用 `mobile:` 变体或 `useMobileForm()` 分支追加）；2.1–2.3 的三个外壳 spec 与 `m11-overlay-collision` 四个桌面宽度不改期望通过；`setRole` 助手是新文件，不改 `monitoring.mocked.spec.ts` 里它自带的切角色代码。
  - 显式 non-goal：切换器与启动器列、顶部提示条带不相交（3.3 / 3.4 时那些元素才存在）；3.3 之前的过渡期里，切换器在两个横屏 project 下会盖住仍未收纳的图层面板的一小块（图层面板占着 `main` 左缘并盖到垂直中点）——接受，不为此改图层面板；44px / 16px 审计（design 明示豁免）；抽屉打开时被盖住（允许）。
  - 实现须知：移动形态的三个视口里头部此时仍是 84px（2.4 尚未做），750×342 下 `main` 只有 258px 高、控制条占底部 64px——“左边缘垂直居中”须在这个高度下仍不与头部、控制条相交；Radix Select 的弹层走 portal，展开后的选项须在三个移动视口内可点。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；新移动 spec 在三个移动 project 各非零 passed；新 spec 先红后绿（改动前切换器在 `right-4 top-4`、宽 144px，宽度断言为红）；改展示端运行时代码，合并前出 node-27 live receipt（PR 的生产构建对 live API：切换器不渲染——`getByLabel('Role')` 计数为 0，桌面布局 oracle 不回归）。

## 3. 地图浮层（`/`）

- [x] 3.1 隐藏地图缩放按钮：移动形态不渲染 MapLibre 缩放 / 指北控件；桌面形态保留；不改地图手势配置。新增 `e2e/m11-zoom-control.mobile.mocked.spec.ts` 与 `e2e/m11-zoom-control-desktop.mocked.spec.ts`。

  Depends on: 1.1, 2.1

  Verify：移动 spec 断言三个移动 project 下无 zoom-in / zoom-out / reset-bearing 按钮；桌面 spec 断言 1280×900 与 768×1024 下三个按钮存在。

  **Suggested fixture level:** compact - 单个控件的条件渲染。

  **Minimal mergeable slice:** atomic - 一个条件分支。

  Triage（#2794）：Issue type: feature ｜ Fixture level: compact（与上游建议一致；设计见 design.md D7。改的是 `/` 地图本体里一个控件的条件渲染，无共享状态、无接口变化，不触发 expanded）｜ Blast radius: `/` 的地图——条件写反会让桌面丢掉缩放 / 指北按钮，或移动形态仍渲染它们而与 3.3 的启动器列抢位置。
  - Risk pack「Public API / entry」selected：地图本体的控件 -> 新 spec `e2e/m11-zoom-control.mobile.mocked.spec.ts`（三个移动 project）：地图就绪后（等 `[data-testid="m11-map-surface"]` 与地图 canvas 可见）`.maplibregl-ctrl-zoom-in`、`.maplibregl-ctrl-zoom-out`、`.maplibregl-ctrl-compass` 计数均为 0；新 spec `e2e/m11-zoom-control-desktop.mocked.spec.ts`（桌面 project，`setViewportSize`）：1280×900 与 768×1024 下三个按钮各恰一个且可见。两个 spec 都先等地图就绪再断言，避免“地图还没挂上所以计数为 0”的空过——移动 spec 另断言 `.maplibregl-ctrl-scale` 计数 ≥ 1（`ScaleControl` 与 `NavigationControl` 是同一次提交挂载的兄弟控件，它在才证明“子控件已挂、缩放按钮确实没有”；attribution 由 `Map` 构造时挂上，早于子控件，不足以证明）以及 `.maplibregl-ctrl-attrib` 存在（计数 ≥ 1，不用 `toBeVisible`）。
  - Risk pack「Concurrency / shared state / ordering」selected：形态在运行中翻转（旋转、窗口缩放）-> 桌面 spec 追加一例：1280×900 -> `setViewportSize(390×664)` 后三个按钮消失，再回 1280×900 后重新出现且仍各恰一个（不重复挂载、不残留）。
  - Risk pack「Legacy compatibility」selected：既有单测用 `MaplibreControlStub` 桩掉 `NavigationControl`（`M11MapLibreSurfaceHook` / `HoverOrder` / `Precip` / `StationHook` 与四个 `OverviewPage*` 测试文件）-> 全部零 diff 且通过（jsdom 的 `matchMedia` 桩恒为桌面形态，控件仍渲染）；`src/__tests__/riverClickPhase2Closure.test.ts` 对 `M11MapLibreSurface.tsx` 的源码扫描（第一个门控 `useEffect` 的文本）不受影响——形态 hook 的调用不得插到该 effect 之前改变“第一个门控 effect”的身份；`e2e/m11-overlay-collision.mocked.spec.ts` 零 diff 且全部通过（含它的 520 宽度用例：520 属移动形态，本 task 之后那里不再有缩放按钮；该用例今天不断言缩放按钮，若实测因此变红，停下报告，不改它的期望——520 的期望改写归 3.2）。
  - 未选：File IO、Schema、Auth、Resource limits、Release、Error handling、Documentation、Config。
  - Must preserve：桌面形态下 `NavigationControl` 的位置（`top-right`）与 `visualizePitch` 不变；地图手势配置（`dragPan`、`touchZoomRotate`、`scrollZoom`、`doubleClickZoom` 等）零改动；`AttributionControl` 等其他控件不动。
  - 显式 non-goal：真机上双指缩放 / 拖动的手感（7.3 真机清单）；启动器列占用腾出的右上角（3.2 / 3.3）；底图切换器当前 `right-16` 的让位偏移（3.3 收纳时处理）。
  - vitest：新测试文件为 `NavigationControl` 装一个可观测的专用桩（带独立 testid 的元素或 `vi.fn` 调用计数；共享的 `MaplibreControlStub` 返回 `null` 且与 `ScaleControl` 共用，照它写出的“渲染 / 不渲染”断言两个分支 DOM 相同，是空断言），断言桌面形态计数为 1（正对照）、移动形态（用例内可控 `matchMedia` 桩，用后还原）计数为 0。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；移动 spec 在三个移动 project 各非零 passed、先红后绿；改展示端运行时代码，合并前出 node-27 live receipt（PR 的生产构建对 live API：1280×900 与 768×1024 下三个按钮存在，390×664 / 750×342 / 844×390 下不存在且 scale 控件与 attribution 存在；五个视口的计数由编排者用仓库外的一次性探针量取，不改 `scripts/node27_display_v2_browser_evidence.mjs`；既有脚本的桌面布局 oracle 不回归）。

- [x] 3.2 浮层展开状态与图例启动器：地图外壳持有展开值 `'layers' | 'basemap' | 'legend' | null`（默认 `null`；点地图空白、`Escape`、形态切换时复位）；移动形态下图例渲染为 44×44 启动器 `m11-launcher-legend`，位于地图区右上角启动器列，展开的图例面板沿用 `m11-floating-legend`，完全位于地图区内、不与控制条和启动器列相交、内容超出时内部滚动。展开 / 收起不改 URL、不发请求。桌面形态图例不变。**本 task 独占既有碰撞 spec 中 520px 行的全部改写**：图例相关行改为移动期望（默认无图例面板、有图例启动器）；控制条相关行改成与形态无关的“在视口内、不压 attribution”，去掉 520px 上的 64 / 40 钉值；其余四个宽度不动。新增 `e2e/m11-legend-launcher.mobile.mocked.spec.ts`。

  Depends on: 1.1, 2.1, 2.5, 3.1

  Verify：移动 spec 断言默认无图例面板、点启动器展开、再点收起、点地图空白收起、`Escape` 收起、展开收起前后 URL 不变且无 API 请求；390×664 与矮视口横屏下展开面板在地图区内、在控制条之上、不与启动器列相交、滚到底后最后一项在面板可视框内；径流图层 + 降水开启时面板列出径流分级（含单位）与六级降水；展开时由 390×664 变为 750×342 仍展开且在地图区内；390×664 → 1280×900 → 390×664 后无面板展开，1280×900 时桌面图例在原偏移。vitest 覆盖展开值状态机。

  **Suggested fixture level:** expanded - 引入地图外壳的新共享状态，并改写既有回归 spec 的 520px 期望。

  **Minimal mergeable slice:** atomic - 这已是首刀：状态值加它的第一个消费者（图例，最大的遮挡源）。只合状态值没有可验证行为；碰撞 spec 的 520px 行在图例收起的同一刻失效，必须同 PR 改。

  Triage（#2795）：Issue type: feature ｜ Fixture level: expanded（与上游建议一致；设计见 design.md D5，规格见 `specs/mobile-map-overlay-layout` 的前两条 requirement）｜ Blast radius: `/` 的地图外壳——展开值是 3.3（图层 / 底图启动器）、3.5、4.x（抽屉打开时复位）共用的状态；写错会让移动形态看不到图例（违反“图例必须可得”的既有要求），或让桌面图例位置漂移。
  - Change surface：`src/pages/OverviewPage.tsx` 的地图外壳（持有展开值、图例区的移动分支、启动器列容器）、`src/components/map/M11FloatingControls.tsx`（`M11FloatingLegend` 的移动渲染：启动器 + 限高可滚面板）、展开值状态机（新的纯函数 / hook 模块）、地图点击的上报通路（`M11MapLibreSurface` / `m11MapInteractions`：新增一个独立回调，地图上的每一次点击都通知外壳，无论是否命中要素）、既有 `e2e/m11-overlay-collision.mocked.spec.ts` 的 520px 行、新 spec 与其 mock 支撑。
  - Governing invariant：移动形态下任一时刻至多一个浮层面板展开；展开值只由“点启动器 / 点地图（面板与启动器之外的任意地图点）/ Escape / 形态切换”改变，不进 URL、不触发数据请求；桌面形态不读展开值，图例恒为常驻面板且几何不变。
  - Sibling surfaces：(1) 展开值的全部写入点——启动器点击、地图空白点击、`Escape`、形态切换（`useMobileForm()` 的 `mobile` 翻转）；(2) 展开值的读取点——本 task 只有图例；`'layers'` / `'basemap'` 两个取值在本 task 没有消费者（3.3 接入），状态机须已支持它们并由 vitest 覆盖互斥；(3) 地图点击通路——必须是独立的新回调，不复用 `onOverlayClick`：`m11MapInteractions.ts` 在未命中目标时直接返回，`m11MapInteractions.test.ts` 钉死此时 `onOverlayClick` 不被调用，复用即破坏“既有 vitest 零 diff”。产品点击目标解析（站点聚合 -> 站点 -> overlay 命中层 -> 流域）与命中要素时的既有行为（开曲线窗、选中、聚合展开）不变；`window.__nhmsRiverClickEvidence` / `__nhmsStationLocateEvidence` 两个只读钩子不改；(4) 图例内容的既有契约——`map-layer-timeline-controls` 的“Legends reflect active hydrologic layer”与 `precipitation-raster-overlay` 的图例场景，移动形态由“启动器常驻 + 展开后的面板内容”满足，面板内容须与桌面图例同源（同一个内容组件，不另写一份）；(5) 图例区的 `RegionErrorBoundary`（`region-error-legend`）——移动分支仍在同一个边界内，兜底的移动定位归 3.6；(6) 仍未收纳的图层面板 / 底图切换器（3.3）与角色切换器（2.5，`z-[200]` 左缘）。
  - Must preserve：桌面形态（宽 ≥ 768 且高 ≥ 500）图例的 testid、偏移（`bottom-[7.5rem] right-4`）、内容与层级不变（`M11FloatingControls.test.tsx` 的 `expectSizedToContent` 对图例卡 className token 的钉值属于桌面分支，不得变）——`e2e/m11-overlay-collision.mocked.spec.ts` 的 1920 / 1440 / 1280 / 800 四个宽度的每一行零改动且通过；该 spec 之外的既有 e2e spec 与全部既有 vitest 文件零 diff 且通过（`riverClickPhase2Closure.test.ts` 的源码扫描约束仍适用：新代码不得插到 `M11MapLibreSurface.tsx` 第一个门控 `useEffect` 之前改变它的身份）；`src/lib/m11/queryState.ts` 零 diff（展开值不进 URL）。
  - 520px 行的改写口径（本 task 独占）：“图例不压盖版权归属 @ 520”与“图例与空清单提示不压盖控制条 @ 520”改为移动期望——默认 `m11-floating-legend` 不可见（或不在 DOM），`m11-launcher-legend` 可见、在地图区内、不与 attribution 和控制条相交；“控制条不压盖版权归属 @ 520”去掉 `height == 64` 与 `底边距 == 40` 两个钉值（控制条的移动几何归 3.7 / 3.8），保留“在视口内（x ≥ 0 且右边 ≤ 视口宽）”与“不与 attribution 相交”。改写须让 520 行在本 task 之后以及 3.7 之后都成立，不写会被 3.7 推翻的数值。第三行里 `m11-overview-empty` 可见且不压控制条的那一半保留不动（提示的移动定位归 3.4）。其余四个宽度所在的循环体若与 520 共用代码，按宽度分支而不是改公共断言。
  - Risk pack「Public API / entry」selected：地图外壳的共享状态与新 testid -> 新移动 spec `e2e/m11-legend-launcher.mobile.mocked.spec.ts`（三个移动 project）逐条覆盖 Verify：默认无面板；`m11-launcher-legend` 可见且包围盒 ≥ 44×44、位于地图区右上角；点启动器展开、再点收起；`Escape` 收起；点地图空白收起；展开 / 收起前后 `page.url()` 不变且期间零 `/api/v1/**` 请求——观察窗口的定义：`/api/v1/**` 请求计数连续 ≥ 1s 不增长后开窗（`/` 上无后台轮询：`usePolling` 只在 MonitoringPage，时间轴定时器只在播放时），窗口只含启动器开 / 关、Escape、地图 tap，不含 `setViewportSize`（resize 会拉瓦片）；启动器带 `aria-expanded` 与可访问名。“位于右上角”的 oracle：启动器右边到地图区右边 ≤ 16px、整体在控制条顶边之上、位于启动器列容器内；不钉 `top` 值（3.3 会在它上方插入两个启动器）。
  - Risk pack「Concurrency / shared state / ordering」selected：状态机与形态切换 -> vitest 覆盖：点同一启动器切换、点另一启动器互斥替换（三个取值两两）、空白点击 / Escape 复位、形态翻转复位、桌面形态下不渲染启动器；e2e：展开时 390×664 -> 750×342（用例内 `setViewportSize`）仍展开且面板在地图区内；390×664 -> 1280×900 -> 390×664 后无面板展开，中间 1280×900 时相对 `m11-fullscreen-map` 桌面图例右边距 16px、底边距 120px 且 `m11-launcher-legend` 计数为 0（这一处桌面断言留在移动 spec 的同一用例里，是“约定”里“桌面断言写在非 `.mobile.` spec”的显式例外：场景本身要求跨形态往返）。vitest 的移动形态用用例内可控 `matchMedia` 桩造出、用后还原（全局桩恒为桌面）。
  - Risk pack「Resource limits / 溢出」selected：面板限高 -> e2e：390×664 与矮视口横屏（750×342、844×390）下展开面板的包围盒在地图区（`m11-fullscreen-map`）内、底边在控制条顶边之上、不与启动器列相交；面板的滚动容器带独立 testid（写进 spec）；“最后一项”= 有降水时最后一个 `m11-floating-legend-precip-row`，否则 `m11-floating-legend-entries` 的末项。矮视口横屏 + 双图例用例须先断言 `scrollHeight > clientHeight` 且滚动前末项不在可视框内，再断言滚到底后末项在可视框内（否则恒真）。面板限高由地图区与控制条的实际几何推出，不写死 104px 的底部占位，也不写死 84px 头部——2.4 把头部改 48px、3.7 把竖屏控制条改两行后，本 spec 的断言仍须成立，后续 task 不改本 spec。
  - Risk pack「Schema / 内容契约」selected：图例内容 -> e2e：径流图层 + 降水开启（`precip=1` 且降水图例数据可得）时展开面板列出径流分级（含单位文本）与六级降水；mock：`/api/v1/layers` 在 discharge 之外追加一个 `precip` 目录条目，形状同 `src/test/overviewDataFixture.ts` 的 `precipLayer`（`metadata.legend` 六项、`unit: 'mm/24h'`），不需要 index / PNG；`precip` 的 URL 缺省即开启。mock 数据用 `satisfies components['schemas'][…]` 标注，不从 `src/test` 引运行时值，放进命名合规的 `e2e/support/*.mocked.ts`。
  - Risk pack「Legacy compatibility」selected -> Must preserve 所列各项；碰撞 spec 的 diff 只落在 520 行（PR 描述里贴出该 spec 的 `git diff --stat` 与四个桌面宽度逐行通过的日志）。
  - Risk pack「Error handling」selected：地图点击的裁决——按规格场景「Dismiss by tapping the map」（面板与启动器之外的任意地图点），**任何**地图点击都收起面板，含命中河段 / 站点 / 站点聚合 / 流域的点击；命中要素时其既有行为照常发生（D5 写的“点地图空白处”收窄为规格的写法，记为对 D5 措辞的澄清，不是偏离）。vitest：新回调在命中与未命中两种点击下各被调用一次，且 `onOverlayClick` 的既有调用条件不变。e2e 的点选取：运行时在地图区的候选网格里按实测包围盒找，取首个 `elementFromPoint` 命中 `canvas.maplibregl-canvas` 的点，不写死坐标（750×342 下须避开图层面板、空清单提示、底图切换器、启动器、展开的图例面板、控制条、scale / attribution、左缘角色切换器）；触屏 project 用 `touchscreen.tap`。这是 mocked 车道第一次在移动 project 对地图做 tap：若实测 tap 到不了外壳（变不成 MapLibre `click`），停下报告确证的原因，不得改用 `page.mouse.click` 冒充。
  - Escape：`src` 里今天没有任何 `keydown` 处理。挂在 document / window 级，只在移动形态且有展开值时生效；e2e 在点启动器后直接 `page.keyboard.press('Escape')`，不依赖面板内焦点；卸载 / 形态切换时移除监听（vitest 覆盖）。
  - 场景归属：本 task 的 e2e 承接 Collapsed by default（只断言图例启动器在、图例面板不在——图层 / 底图面板此时仍常驻，不断言“三个面板都不可见”）、Toggle collapses（图例）、Dismiss by tapping the map、Dismiss by Escape、URL / network（图例）、Expanded legend carries the legend content、Rotation keeps the expanded panel、Leaving and re-entering mobile form（图例部分）、Legend fits a short-landscape viewport、Legend fits a portrait viewport；仅 vitest 承接 Expanding one collapses the others（状态机互斥）。归 3.3（显式 non-goal）：上一条的 e2e、Panel content matches desktop、Layer panel fits a short-landscape viewport、三个启动器齐全的 Collapsed by default。
  - 未选：File IO、Auth、Release、Documentation、Config。
  - Seams under test：浏览器里的启动器与图例面板（testid、包围盒、URL、请求计数）；vitest 里的展开值状态机与 `M11FloatingLegend` 的形态分支。
  - Non-goals：图层 / 底图启动器（3.3）；提示条带与状态条定位（3.4）；运维入口并入启动器列（3.5）；区域错误兜底的移动定位（3.6）；控制条的移动几何（3.7 / 3.8）；曲线抽屉打开时复位（4.x——状态机留出复位入口即可，不接线）；过渡期里展开的图例面板与仍未收纳的图层面板 / 底图切换器相互遮挡（3.3 之后消失），以及启动器列与底图切换器（当前 `right-16 top-4`）贴近——接受，但启动器自身必须点得到（`elementFromPoint` 命中启动器）。
  - Review focus：(1) 桌面渲染路径与图例几何逐像素不变；(2) 展开值单一来源、四个写入点齐全、不进 URL / 不发请求；(3) 图例面板内容与桌面同源；(4) 碰撞 spec 只动 520 行且新期望不会被 3.7 推翻；(5) 地图空白点击通路不改变命中要素时的行为、不触碰两个只读钩子；(6) 每条移动断言在三个移动 project 下都成立，没有按 project 跳过。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；对新 spec 与 `e2e/support/**` 另跑 strict `tsc --noEmit`（`typecheck` / `check:types` 都不覆盖 `e2e/`）exit 0；新移动 spec 在三个移动 project 各非零 passed、先红后绿、`--repeat-each=5` 无 flaky；碰撞 spec 四个桌面宽度零改动通过；改展示端运行时代码，合并前出 node-27 live receipt（PR 的生产构建对 live API，编排者用仓库外的一次性探针：390×664 / 750×342 / 844×390 下默认无图例面板、启动器在、点启动器后面板在地图区内且在控制条之上、列出 live 的径流分级；1280×900 与 768×1024 下桌面图例在、无启动器；既有脚本的桌面布局 oracle 不回归）。

- [x] 3.3 图层与底图启动器：图层面板、底图切换在移动形态渲染为 44×44 启动器 `m11-launcher-layers`、`m11-launcher-basemap`，加入启动器列并接入 3.2 的展开值；展开面板内每个开关高 ≥ 44px，内容与产生的 query 变化与桌面一致。新增 `e2e/m11-layer-basemap-launchers.mobile.mocked.spec.ts` 与 `e2e/m11-launchers-desktop.mocked.spec.ts`（桌面 project）。

  Depends on: 3.2

  Verify：三个移动 project 下三个启动器可见且三个面板都不可见；展开一个会收起另外两个；矮视口横屏下图层面板在地图区内、在控制条之上；390×664 与 750×342 下三个面板各自展开时面板内每个可点项 ≥ 44×44；图层与底图面板展开再收起前后 URL 不变且无 API 请求；切换气象代站开关后的 URL query 与桌面形态同一操作的结果相同；角色切换器不与任何启动器相交。桌面 spec 断言 768×1024 与 1280×900 下三个面板常驻可见、无任何启动器，以及 390×664（展开一个面板）→ 1280×900 → 390×664 的往返：1280×900 时三个桌面面板都在原偏移且无任何启动器，回到 390×664 后无面板展开。

  **Suggested fixture level:** compact - 两个同文件组件接入既有状态值，状态契约在 3.2 已定。

  **Minimal mergeable slice:** atomic - 图层与底图两个启动器共用同一列与同一互斥断言；只做一个会出现“一个是启动器、一个是常驻面板”压在启动器列上的新叠压。

  Triage（#2796）：Issue type: feature ｜ Fixture level: compact（与上游建议一致；状态机、限高 hook、启动器列与地图点击通路都在 3.2 落地，本 task 只接两个消费者；设计见 design.md D5，规格见 `specs/mobile-map-overlay-layout` 前两条 requirement）｜ Blast radius: `/` 的地图控件区——图层开关与底图切换是改 URL query 的唯一入口，移动分支写错会让移动形态切不了图层 / 底图，或让桌面两个面板的位置漂移。
  - Change surface：`src/components/map/M11FloatingControls.tsx`（`M11FloatingLayerSwitcher`、`M11FloatingBasemapSwitcher` 的移动渲染：启动器 + 限高可滚面板，写法与 `M11FloatingLegend` 的移动分支同构）、`src/pages/OverviewPage.tsx`（地图控件区边界在移动形态进启动器列、把 `expanded` / `toggle` / `panelMaxHeight` 传给两个组件）、两个新 spec 与 `e2e/support/` 里的共享助手、对应 vitest。`useM11OverlayExpansion.ts`、`m11MapInteractions.ts`、`M11MapLibreSurface.tsx`、`src/lib/m11/queryState.ts` 零 diff。
  - 列内顺序与锚点：启动器列自上而下为 图层、底图、图例（DOM 顺序即视觉顺序）；三个展开面板共用同一个锚点（列左侧、顶边对齐列顶）与同一个 `panelMaxHeight`。面板内容与桌面同源：图层行、底图按钮不另写第二份，移动与桌面共用同一段内容渲染。
  - 区域错误边界：`region-error-map-controls` 仍是**一个**边界，覆盖图层、底图与运维入口；移动形态下它整体进启动器列（`RegionErrorBoundary` 非错误态不加包裹元素，三个子节点直接成为列的 flex 子项），`className` 在移动形态传 `undefined`（兜底块就地入流，与 3.2 图例边界同一做法；桌面仍是 `absolute left-4 top-4 z-[120]`）。图例边界（`region-error-legend`）保持独立、不被它包住。vitest 覆盖：移动形态桩 + 让图层组件抛错 -> `region-error-map-controls` 出现且不带 `absolute` 类、`m11-launcher-legend` 仍在 DOM。兜底块的专门移动定位归 3.6。
  - 运维入口（本 task 只给移动落点，裁定）：因为边界不加包裹，`M11OpsLink` 会随边界进列；保留它的桌面类 `absolute right-4 top-28` 会以列为定位祖先盖住图例启动器中心。所以本 task 给 `M11OpsLink` 加一个 `mobile` prop：移动形态下它是列里的在流子项（去掉 `absolute right-4 top-28`），用 `order-last` 排在图例启动器之下——列的视觉顺序为 图层 / 底图 / 图例 / 运维入口，与 3.5 的终态位置一致；尺寸、文案、`to="/ops"`、testid 不变，桌面类逐字不变。3.5 因此收窄为：尺寸 ≥ 44×44、它自己的 spec、以及矮视口横屏下四项放进列高（750×342 且头部 84px 时列顶到控制条顶 146px，三个启动器已占 140px，运维入口会伸到控制条右端之上——仅 operator 角色、过渡期，记为本 task 的显式 non-goal，由 3.5 与 2.4 收口）。移动 spec 的 operator 用例（`setRole` 切到 operator）断言：三个启动器各自 `elementFromPoint` 命中自身；运维入口可见、在图例启动器之下、与三个启动器都不相交；图层面板展开时运维入口不与面板相交。
  - Escape（#2795 携带项的评估结论）：两个面板里只有普通 `<button>`，没有 Radix 弹层或原生 `<select>`，document 级 Escape 不需要按焦点范围收窄，`useM11OverlayExpansion` 不改。
  - Must preserve：桌面形态下 `m11-floating-layer-switcher`（`absolute left-4 top-4 z-[120] w-max max-w-52 p-2`）与 `m11-floating-basemap-switcher`（`absolute right-16 top-4 z-[120]`，按钮 `h-8`）的 testid、className、内容、DOM 结构不变（`M11FloatingControls.test.tsx` 的 `expectSizedToContent` 钉着图层卡的 className token）；全部既有 vitest 文件与全部既有 e2e spec 零 diff 且通过，其中 `e2e/m11-legend-launcher.mobile.mocked.spec.ts`（3.2 的 spec，断言不钉 `top`、点位按实测包围盒找）在列里多出两个启动器之后仍须原样通过，`e2e/m11-overlay-collision.mocked.spec.ts` 五个宽度全部零改动通过，`e2e/m11-routes.mocked.spec.ts` 的桌面断言不变。既有的 `aria-label`（「地图图层切换」「底图切换」「X底图」）、`aria-pressed`、`m11-layer-toggle-precip` 的禁用语义在移动面板里原样保留。
  - Risk pack「Public API / entry」selected：新 testid 与入口 -> 移动 spec `e2e/m11-layer-basemap-launchers.mobile.mocked.spec.ts`（三个移动 project）：默认三个启动器可见、各 ≥ 44×44、都在启动器列内且自上而下为图层 / 底图 / 图例、带 `aria-expanded="false"` 与可访问名，`m11-floating-layer-switcher` / `m11-floating-basemap-switcher` / `m11-floating-legend` 三个面板计数为 0；点图层启动器展开、再点收起（Toggle collapses）；图层与底图面板各自“展开再收起”前后 `page.url()` 不变且期间零 `/api/v1/**` 请求（观察窗口口径同 3.2：`waitForApiQuiet` 之后开窗，窗口只含启动器点击）；角色切换器包围盒不与任何启动器相交。
  - Risk pack「Concurrency / shared state / ordering」selected：互斥 -> e2e：三个面板两两切换（图例 -> 图层 -> 底图 -> 图例），每一步只有被点的那个面板可见、其启动器 `aria-expanded="true"`、另两个为 `false`；在面板内切换开关（气象代站）之后面板保持展开（面板内操作不是地图点击）；点地图空白 / Escape 对图层面板同样收起（各一条，复用 `findBlankMapPoint`）。桌面 spec `e2e/m11-launchers-desktop.mocked.spec.ts`（桌面 project，`setViewportSize`）：768×1024 与 1280×900 下三个面板常驻可见、三个启动器与启动器列计数为 0；390×664（展开图层面板）-> 1280×900 -> 390×664 往返：1280×900 时相对 `m11-fullscreen-map` 图层面板左 16 / 上 16、底图切换器右 64 / 上 16、图例右 16 / 下 120（±0.5px），无任何启动器，回到 390×664 后三个面板计数为 0。
  - Risk pack「Schema / 内容契约」selected：query 变化与桌面一致 -> 移动 spec 一条用例：移动形态下展开图层面板点「气象代站」，记下 `location.search`；同一用例内 `setViewportSize(1280, 900)`、重新 `goto('/')`、点桌面面板的同一开关，断言两次的 `location.search` 相等，且都不等于操作前的值（否则恒真）。这是“约定”里“桌面断言写在非 `.mobile.` spec”的显式例外，理由同 3.2：场景本身要求跨形态对照。底图同理再做一组（点「卫星底图」）。
  - Risk pack「Resource limits / 溢出」selected：面板限高与触控下限 -> e2e：三个移动 project 下图层面板、底图面板展开时包围盒在 `m11-fullscreen-map` 内、底边在控制条顶边之上、不与启动器列相交；矮视口横屏下图层面板内容高于可用高度时面板内部滚动——滚动容器带独立 testid（写进 spec），先断言 `scrollHeight > clientHeight` 再断言滚到底后末行（「气象代站」）在面板可视框内（若实测不溢出则只断言末行在可视框内，并在 PR 里写明实测高度）。三个移动 project（390×664、750×342、844×390，都执行、不按 project 跳过）下三个面板各自展开时，面板内每个可点项（`button, a, [role="button"], input, select`）包围盒 ≥ 44×44；图例面板内没有可点项，spec 对它断言“可点项计数为 0”，不让它静默跳过。
  - Risk pack「Legacy compatibility」selected -> Must preserve 所列各项；PR 描述贴出 `git diff --stat origin/master -- apps/frontend/e2e` 证明既有 spec 零 diff。
  - Risk pack「Error handling」selected -> 上面“区域错误边界”一条的 vitest。
  - 未选：File IO、Auth、Release、Documentation、Config。
  - Non-goals：展开状态机与限高 hook 的任何改动（3.2 已定）；运维入口的尺寸下限、专属 spec 与矮视口横屏下不压控制条（3.5）；提示条带（3.4）——`m11-overview-empty` 等提示在展开面板下方被盖住属 3.4 明确不要求避让的情形；区域兜底的移动定位（3.6）；控制条几何（3.7 / 3.8）；曲线抽屉打开时复位（4.x）。
  - Review focus：(1) 桌面两个面板的 className / DOM 逐字不变；(2) 移动与桌面共用同一份行 / 按钮渲染与同一个 `onQueryChange` patch；(3) 一个地图控件边界、图例边界独立；(4) 3.2 的 spec 零 diff 通过；(5) 每条移动断言在三个移动 project 下都成立，没有按 project 跳过。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；新 spec 与 `e2e/support/**` 的 strict `tsc --noEmit` exit 0；两个新 spec 先红后绿（红：对 `origin/master` 的 `src/`），移动 spec 在三个移动 project 各非零 passed，`--repeat-each=5` 无 flaky；改展示端运行时代码，合并前出 node-27 live receipt（PR 的生产构建对 live API：390×664 / 750×342 / 844×390 下三个启动器在、三个面板不在，展开图层面板后在地图区内且在控制条之上、点「气象代站」后 URL query 变化；1280×900 与 768×1024 下三个桌面面板在原偏移、无启动器；既有脚本的桌面布局 oracle 不回归）。

- [x] 3.4 浮动提示与状态条的移动位置：移动形态下 `M11FloatingNotice`（代站状态 / 加载中 / 空数据 / 数据异常 / 降水五条互斥提示共用的组件）与 `M11MapStatusOverlays` 的各条状态条（流域边界不可用、地图不可用、选中河段不可用、地图源错误）放在地图区顶部、启动器列左侧的条带内，自上而下排列，彼此不相交、不与启动器和控制条相交，每条文本最多两行截断。不要求避让已展开的面板与已打开的曲线抽屉。桌面形态不变。新增 `e2e/m11-notices.mobile.mocked.spec.ts`。

  Depends on: 3.3

  Verify：三个移动 project 下，代站状态提示与加载中提示各自的包围盒在地图区内、不与任何启动器或控制条相交、高度不超过两行文本；让一个瓦片请求失败以触发地图源错误状态条，断言它单独出现时在地图区内、不与任何启动器或控制条相交、高度不超过两行，且与同时出现的浮动提示不相交；角色切换器不与浮动提示和状态条相交。

  **Suggested fixture level:** compact - 两个展示组件的移动定位，无状态与数据变化。

  **Minimal mergeable slice:** atomic - 浮动提示与状态条争用同一条顶部条带，位置互相约束，必须一起定。

  Triage（#2797）：Issue type: feature ｜ Fixture level: compact（与上游建议一致；两个展示组件的移动定位，无状态、无数据、无接口变化；设计见 design.md D8 第一条，规格见 `specs/mobile-map-overlay-layout` 的「Notices and status overlays do not collide with mobile chrome」）｜ Blast radius: `/` 的提示面——`m11-overview-empty` 是 bootstrap 失败的唯一渲染面，状态条是地图源错误的唯一渲染面；移动定位写错会让它们被启动器 / 控制条盖住或伸出地图区，桌面位置漂移则破坏既有碰撞断言。
  - Change surface：`src/components/map/M11FloatingControls.tsx` 的 `M11FloatingNotice`、`src/components/map/m11MapRuntime.tsx` 的 `M11MapStatusOverlays` / `M11MapStatusNotice`（两处都只动 className 与必要的包裹结构）、新 spec `e2e/m11-notices.mobile.mocked.spec.ts` 与 `e2e/support/` 下它的 mock 助手（文件名含独立 `mocked` token）、对应 vitest。`OverviewPage.tsx` 的提示互斥链（谁在什么条件下出现）与 `useM11MapSourceError` 零行为改动。
  - 实现口径（裁定）：纯 CSS——在既有元素上追加 `mobile:` 变体类，不引入 `useMobileForm()`。条带 = 地图区顶部、左起 8px、右边界在启动器列左侧（列宽 44px + 右边距 8px + 间隙 8px，即距地图区右边 60px）。两个组件在不同的 DOM 父节点下（提示是 `M11FullscreenMap` 的 children，状态条在 `M11MapLibreSurface` 内），不搬 DOM、不用 portal，用固定分槽：(a) 浮动提示占条带顶部第一槽（移动形态去掉水平居中与 `bottom` 偏移，改 `top` 8px）；(b) 四条状态条在移动形态下成为**同一个**纵向 flex 容器的在流子项（容器在桌面形态用 `display: contents`，不改变桌面布局与各条的 `absolute` 定位），容器顶边在第一槽之下——第一槽按“两行提示的高度 + 间隙”恒定预留，不随提示是否出现而变；(c) 提示与每条状态条的文本都最多两行截断（`line-clamp-2` 或等价），超出部分不可见。因此四条状态条之间、状态条与提示之间按构造不相交。若实现者发现更简单且满足下列不变量的写法，可以用，但须在报告的偏离里写明。
  - 底部界限：状态条容器不得伸进控制条——容器在移动形态带一个底部界限并 `overflow: hidden`（按控制条底边距 40px + 64px 条高 + 间隙写成 CSS；竖屏两行控制条（3.7）更高，但竖屏条带余量超过 400px，四条全出也到不了）。容器必须 `pointer-events-none` 且不得吞掉地图手势：空容器或容器内最后一条状态条下方的区域，`elementFromPoint` 须仍命中 `canvas.maplibregl-canvas`（移动 spec 在状态条出现时断言这一点；`findBlankMapPoint` 只要任一点命中 canvas 就通过，抓不到这类回归）。已知限制（显式记录）：矮视口横屏（750×342：第一槽到约 66px，两行状态条每条约 58px，控制条顶 190px）下**两条及以上**两行状态条同时出现时，排在后面的状态条会被这个界限裁掉一部分；规格要求的组合（单条状态条、提示 + 单条状态条）放得下——不与控制条相交优先于完整显示；3.7 / 3.8 改控制条几何后须复核这个界限（已留言）。
  - Must preserve：桌面形态下 `M11FloatingNotice`（`absolute left-1/2 bottom-[11.5rem] z-[110] max-w-[min(30rem,calc(100%-8rem))] -translate-x-1/2 …`）与四条状态条（`top-20` / `top-20` / `top-44` / `top-32`、`left-1/2 -translate-x-1/2 z-[90]`）的计算位置、testid、`role="status"`、文案与出现条件不变；全部既有 vitest 文件（含 `m11MapRuntime.test.tsx`、`OverviewPagePrecipOverlay.test.tsx`、`OverviewPageDataAnomaly.test.tsx`）与全部既有 e2e spec 零 diff 且通过——`e2e/m11-overlay-collision.mocked.spec.ts` 五个宽度全部通过（520 行 3 的“`m11-overview-empty` 可见且不压控制条”在提示移到顶部后仍成立）；`src/lib/c4DisplayEvidence/dom.ts`、`src/test/c4DisplayFakePage.ts` 与 `scripts/node27_display_v2_browser_evidence.mjs` 读取的 testid 不变。
  - Risk pack「Public API / entry」selected：提示面的移动几何 -> 移动 spec（三个移动 project，无按 project 跳过）逐条覆盖规格四个场景与 issue 验收：(1) 代站状态提示 `m11-met-station-status`（开代站图层并让其状态说明出现——触发方式由实现者从 `useStationLayer.ts` 的 `statusNote` 分支里选一个可在 mock 下稳定复现的）；(2) 加载中提示 `m11-overview-loading`（挂起一个总览请求直到断言完成再放行）；(3) 地图源错误状态条 `m11-map-source-error` 单独出现（对一个 `/api/v1/basemap/tianditu/*` 底图瓦片返回 5xx 或 `route.abort()`——不能用 404：MapLibre 对 404 瓦片不发 error 事件；该路径命中 `m11BasemapSourceIds` 分支，文案为固定的 `M11_BASEMAP_UNAVAILABLE_NOTICE`。此场景的 mock 须保证 `m11-basin-layer-unavailable` 与任何浮动提示都不出现；若实测点不亮，停下报告确证的原因）；(4) 浮动提示与地图源错误状态条同时出现时两者包围盒不相交。每个场景断言：包围盒在 `m11-fullscreen-map` 内、不与三个启动器中的任何一个相交、不与 `m11-bottom-control-bar` 相交、高度不超过两行文本（由该元素计算 `line-height` × 2 + 纵向内边距 + 边框推出上限，±1px）；(5) 角色切换器包围盒不与场景 (4) 里的提示和状态条相交——三个 project 都断言、不跳过；750×342 下余量很小（切换器垂直居中于约 147px、顶边约 127–129px，两行状态条底边约 124px），实现者须报告该视口实测的切换器与状态条包围盒，若第一槽预留高度使两者相交，按偏离上报而不是在该 project 跳过。
  - Mock 基线：新助手对底图瓦片默认返回合法 PNG、其余瓦片 204（同 `e2e/support/riverWindow.mocked.ts` 的做法），不沿用 `legendLauncher.mocked.ts` 的“全部 `/api/v1/**` 回 JSON”（那会让底图瓦片解码失败，可能使 `m11-map-source-error` 常亮，场景 (1)(2) 的“提示单独出现”与先红后绿判定失真）。场景 (1)(2) 须断言 `m11-map-source-error` 计数为 0。
  - Risk pack「Resource limits / 溢出」selected：两行截断 -> 移动 spec 一条用例（优先直接用场景 (3) 的固定底图文案——43 字、14px，在 390 宽条带里自然超过两行；若某个 project 下实测不溢出，则在页面上把该元素文本加长，做法同 2.4 的截断用例，并写明）断言：前置 `scrollHeight > clientHeight`（否则恒真），包围盒高仍不超过两行上限，且仍在地图区内。vitest：四条状态条同时渲染时（直接渲染 `M11MapStatusOverlays`，props 造出四条全出）都在同一个容器元素下、各带截断类；桌面类原样保留。
  - Risk pack「Legacy compatibility」selected -> Must preserve 所列各项；PR 描述贴出 `git diff --stat --diff-filter=MDR origin/master -- apps/frontend/e2e 'apps/frontend/src/**/__tests__/**'`（期望为空）。桌面不变的证据：新增桌面 project 的 spec 不是本 task 的交付物（issue 只列了一个移动 spec），桌面几何由碰撞 spec 四个宽度 + 实现者的一次性前后对比（1280×900 下提示与一条状态条的包围盒改动前后相等，数字写进报告）承担。
  - 未选：Concurrency（无状态）、File IO、Auth、Schema、Release、Documentation、Config、Error handling（出现条件不改）。
  - Non-goals：提示的触发条件与文案；避让已展开的面板与已打开的曲线抽屉（规格明示不要求）；operator 角色下被运维入口撑宽的启动器列与条带右边界的关系（3.5 把入口收到 44px 后消失）；矮视口横屏下两条及以上两行状态条的完整显示（见“底部界限”）；角色切换器与两条及以上状态条同时出现时的相交（切换器只在开发 / 测试构建存在，断言范围限于场景 (4)）；区域错误兜底（3.6）。
  - Review focus：(1) 桌面类逐字保留、只做追加，`display: contents` 包裹不改变桌面定位上下文；(2) 互斥链与出现条件零改动；(3) 每个场景的触发是真实产品路径（请求失败 / 挂起），不是往 DOM 里塞假节点（截断用例加长文本除外，须写明）；(4) 两行上限断言非恒真；(5) 三个移动 project 都跑、无跳过。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；新 spec 与助手的 strict `tsc --noEmit` exit 0；新 spec 先红后绿（红：对 `origin/master` 的 `src/`，点名红的用例与原因），三个移动 project 各非零 passed，`--repeat-each=5` 无 flaky；改展示端运行时代码，合并前出 node-27 live receipt（PR 的生产构建对 live API，编排者的仓库外探针：390×664 / 750×342 / 844×390 下开代站图层后若出现状态提示则其在地图区内、不压启动器与控制条——live 下无提示可出时如实记录“未出现”并改用拦截一个瓦片请求触发状态条来量；1280×900 下提示 / 状态条位置与 master 相同；既有脚本默认视口的桌面布局 oracle 不回归）。

- [x] 3.5 运维入口并入启动器列：移动形态下 `M11OpsLink` 为启动器列末位（图例启动器之下），≥ 44×44，仅对有权限角色渲染。新增 `e2e/m11-ops-entry.mobile.mocked.spec.ts`。

  Depends on: 3.3

  Verify：viewer 角色下无运维入口；用 `setRole` 切到 operator 后入口存在、≥ 44×44、位于图例启动器之下、点击进入 `/ops`。

  **Suggested fixture level:** compact - 一个链接的移动定位与尺寸。

  **Minimal mergeable slice:** atomic - 一个元素。

  Triage（#2798）：Issue type: feature ｜ Fixture level: compact（与上游建议一致；一个链接的移动尺寸与列内收纳，落点已由 3.3 给出；设计见 design.md D8 第二条，规格见 `specs/mobile-map-overlay-layout` 的「The ops entry joins the launcher column」）｜ Blast radius: `/` 的启动器列——入口尺寸决定列宽，列宽决定三个展开面板的锚点、面板宽度上限（`100vw - 5rem` 按 44px 列估算）与 3.4 提示条带的右边界（距地图区右 60px）；矮视口横屏下列高再多一项会压到控制条。
  - Change surface：`src/components/map/M11FloatingControls.tsx` 的 `M11OpsLink` 移动分支、`src/pages/OverviewPage.tsx` 启动器列容器的 className（仅矮视口横屏的间距 / 顶距）、新 spec `e2e/m11-ops-entry.mobile.mocked.spec.ts`（需要的 mock 复用 `e2e/support/` 既有助手，必要时新增含独立 `mocked` token 的助手）、对应 vitest。
  - 入口形态（裁定）：移动形态下运维入口是与三个启动器同形的 44×44 图标按钮（`h-11 w-11`、扳手图标），仍是指向 `/ops` 的链接，文字「运维」视觉上不可见但保留为文本节点：放进链接内的子 `span` 并加 `sr-only`（不能用纯 `aria-label` 替代，也不能把 `sr-only` 加在链接自身——既有 vitest 在移动分支断言 `toHaveTextContent('运维')`）；保持 3.3 的 `order-last` 在流末项。宽度必须正好 44px——宽于 44px 会撑宽列、推左面板锚点并侵入提示条带。桌面形态（`absolute right-4 top-28`、图标 + 文字）逐字不变。
  - 矮视口横屏放进列高（裁定）：750×342（头部 48px）下列顶 y=56、控制条顶 y=238，可用 182px；四个 44px 加三个 4px 间距 = 188px，超 6px。处理：仅在矮视口横屏把列的顶距收到 4px、间距收到 2px（用 `src/index.css` 已有的矮视口横屏变体，不新造变体），四项占 4 + 176 + 6 = 186px，底边 y=234，距控制条顶 4px。竖屏的列几何（顶距 8px、间距 4px）不变。副作用（须实测并写进报告）：矮视口横屏下三个启动器的 y 由 56 / 104 / 152 变为 52 / 98 / 144，展开面板顶边上移 4px、限高增加 4px（844×390 下图层面板“必须先溢出”的前置余量由 18px 降到约 14px：内容 240 对可用 222 -> 约 226；750×342 为 172 -> 约 178）。覆盖边界：竖屏几何放下四项需视口高 ≥ 348，矮视口横屏新几何需 ≥ 338（头部 48、控制条高 64、底边距 40）；本 change 的验收视口（390×664、320×480、600×400、750×342、844×390）全部满足，520×480 走矮视口横屏变体且放得下。
  - Must preserve：viewer 角色下任何形态都没有运维入口（权限判定 `OPERATOR_ROLES` 零 diff）；桌面形态入口的 className、文案、`href` 不变（`M11FloatingControls.test.tsx`、`OverviewPageLayerBasemapLaunchers.test.tsx` 的桌面 token 钉值）；移动分支的既有钉值同样保留——`M11FloatingLayerBasemapMobile.test.tsx` 断言移动链接自身不含 `absolute` / `right-4` / `top-28` / `z-[120]`、含 `order-last`、`href=/ops`、`toHaveTextContent('运维')`，`OverviewPageLayerBasemapLaunchers.test.tsx` 断言入口是启动器列的直接子项；全部既有 vitest 文件与全部既有 e2e spec 零 diff 且通过——尤其是 3.2 / 3.3 / 3.4 / 2.4 的四个移动 spec 在矮视口横屏列几何变化后仍原样通过（3.3 的 operator 用例：入口在图例启动器之下、与三个启动器和三个面板都不相交、面板在地图区内；两处“必须先溢出”前置）。若某条既有断言因此变红，停下报告确证的原因与实测数字，不改那个 spec。碰撞 spec 五个宽度零改动通过。
  - Risk pack「Public API / entry」selected：入口的可见性与导航 -> 移动 spec（三个移动 project，无按 project 跳过）：(1) viewer 角色下 `m11-ops-link` 计数为 0；(2) `setRole` 切到 operator 后入口可见、包围盒 ≥ 44×44 且宽 ≤ 44.5、在 `m11-launcher-column` 内、顶边 ≥ 图例启动器底边、是列内视觉上的最后一项、`elementFromPoint`（入口中心）命中入口自身、可访问名为「运维」；(3) 触屏 tap 入口后 `location.pathname` 为 `/ops`，且授权后的页面标题「内部诊断」可见、「权限不足」计数为 0、角色触发器仍显示 Operator（角色在同一会话的客户端导航里保持）。Mock：runtime config 必须钉 `display_readonly: false`（同 `e2e/support/siteHeader.mocked.ts` 的做法——只读配置下 `/ops` 的 `RBACGate` 会直接放行 viewer，用例就空过了）；`page.route` 后注册者优先、两个宽 `**/api/v1/**` 助手叠加不会合并，所以用**一个**同时覆盖 `/` 的启动器与 `/ops` 页面的助手。
  - Risk pack「Resource limits / 溢出」selected：列高与列宽 -> 同一 spec 在 operator 角色下断言：四项（三个启动器 + 入口）两两不相交、都在 `m11-fullscreen-map` 内、入口底边 ≤ `m11-bottom-control-bar` 顶边（三个 project 都断言，750×342 是咬合点）；启动器列包围盒宽 ≤ 44.5（列没有被撑宽）；operator 角色下展开图层面板，面板在地图区内且不与入口相交。
  - 列几何的形态归属 oracle（否则“只在矮视口横屏生效”无人看守）：同一 spec 断言 390×664 下列顶 − 地图区顶 = 8、相邻启动器间距 = 4；750×342 与 844×390 下为 4 与 2（±0.5）。
  - Risk pack「Auth / permissions」selected：只动呈现、不动权限判定 -> (1) 的 viewer 用例 + 既有 vitest（`visible=false` 不渲染）零 diff；新增 vitest 钉移动分支的类与可访问名、桌面分支 token 不变。
  - Risk pack「Legacy compatibility」selected -> Must preserve；PR 描述贴出 `git diff --stat --diff-filter=MDR origin/master -- apps/frontend/e2e 'apps/frontend/src/**/__tests__/**'`（期望为空）。
  - 未选：Concurrency、File IO、Schema、Release、Documentation、Config、Error handling（区域兜底归 3.6）。
  - Non-goals：视口高 < 338 的横屏（如 568×320）下四项放进列高；`/ops` 页面自身的移动布局（6.x）；控制条的移动几何（3.7 / 3.8——它们改条高后须复核本 task 的列高算术，已留言 #2800）；区域错误兜底在列内的定位（3.6）；桌面入口的任何变化。
  - Review focus：(1) 桌面入口逐字不变；(2) 权限判定零 diff；(3) 列宽回到 44px、面板锚点与提示条带右边界重新成立；(4) 矮视口横屏的列几何只经矮视口横屏变体生效，竖屏不变；(5) 既有四个移动 spec 零 diff 通过；(6) 三个移动 project 都断言、无跳过。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；新 spec 的 strict `tsc --noEmit` exit 0；新 spec 先红后绿（红：对 `origin/master` 的 `src/`，点名红的用例与原因——viewer 用例改动前本就成立，可先绿），三个移动 project 各非零 passed，`--repeat-each=5` 无 flaky；改展示端运行时代码，合并前出 node-27 live receipt（生产构建没有角色切换器、默认角色看不到入口：探针断言三个移动视口下 `m11-ops-link` 计数为 0、三个启动器仍在地图区内且在控制条之上、矮视口横屏的列顶距 / 间距按新值生效，1280×900 下桌面面板偏移不变；operator 角色的入口几何由 mocked 车道承担，如实写进 receipt 的限制；既有脚本默认视口的桌面布局 oracle 不回归）。

- [x] 3.6 区域错误兜底的移动位置：（a）新增测试门控的区域崩溃开关：仅当 `window.__NHMS_E2E_HOOKS__ === true` 时，地图控件、图例、控制条、曲线四个区域边界内各有一个探针，读取 `window.__NHMS_E2E_CRASH_REGION__`（`map-controls` / `legend` / `control-bar` / `curve`）并在渲染期抛错；无门控时探针不渲染、不读取任何全局。（b）移动形态下地图控件、图例、控制条三个区域的 `RegionErrorBoundary` 兜底块位于地图区内，不与仍在渲染的控制条、启动器和 attribution 相交；桌面形态的兜底位置不变。曲线区域的兜底行为在 4.6。新增 `e2e/m11-region-fallbacks.mobile.mocked.spec.ts`。

  Depends on: 3.3

  Verify：390×664 下用崩溃开关分别让图例、地图控件、控制条三个区域抛错：图例兜底在地图区内且不与控制条相交；地图控件兜底在地图区内且不与控制条和图例启动器相交；控制条兜底在地图区内且不与任何启动器和 attribution 相交。带门控且开关为 `legend` 时图例显示兜底，同值无门控时图例正常渲染（vitest 或 e2e）。

  **Suggested fixture level:** expanded - 在产品代码里新增测试门控的崩溃探针（共享的区域边界内），并改三处兜底定位。

  **Minimal mergeable slice:** atomic - 崩溃开关是验证兜底几何的唯一手段，没有它兜底定位无法在真实布局下断言；三处兜底用同一种定位方式与同一条断言，拆开是三份相同的 PR。

  Triage（#2799）：Issue type: feature（测试门控的产品代码 + 三处兜底的移动定位）｜ Fixture level: expanded（与上游建议一致；设计见 design.md D8 第三、四条，规格见 `specs/mobile-map-overlay-layout` 的「Region error fallbacks fit the mobile layout」与 `specs/mobile-regression-evidence` 的崩溃开关一句及场景「Crash switch works only behind the gate」）｜ Blast radius: `/` 的四个区域错误边界——探针住在产品渲染路径里，门控写错会让线上用户的页面在某个全局被设置时掉进兜底，或让无门控的构建多读一个全局；兜底定位写错会让“出错时仍能操作其余区域”这条降级承诺在手机上失效。
  - Change surface：新的探针组件（一个小模块，放 `src/components/layout/` 或 `src/lib/` 下，由实现者就近决定）、`src/pages/OverviewPage.tsx`（四个边界内各挂一个探针；必要时调整三处兜底在移动形态的 `className`）、必要时 `RegionErrorBoundary.tsx` 兜底块的类名（仅限类名：不加 prop、不改行为——错误回调 prop 归 4.6；桌面形态兜底的包围盒不变）、新 spec `e2e/m11-region-fallbacks.mobile.mocked.spec.ts` 与其助手、对应 vitest。`M11MapLibreSurface.tsx` 零 diff（既有两个门控 effect 与 `riverClickPhase2Closure.test.ts` 的源码扫描约束不受影响）。
  - 探针契约（裁定）：(1) 探针是渲染期组件，区域标识取 `map-controls` / `legend` / `control-bar` / `curve` 四值之一；(2) 先判门控：`window.__NHMS_E2E_HOOKS__ !== true` 时直接返回 `null`，**不读取** `window.__NHMS_E2E_CRASH_REGION__`（vitest 用访问器桩证明零次读取）；(3) 有门控且开关值等于本区域标识时在渲染期 `throw`，否则返回 `null`；(4) 探针不渲染任何 DOM、不持有状态、不订阅——非崩溃态下四个区域的 DOM 与改动前逐字相同（`RegionErrorBoundary` 的“happy-path DOM 逐字不变”承诺）；(5) 每个边界内恰一个探针，与该区域的真实内容同处一个边界之内（兜底覆盖的是整个区域）；(6) 曲线区域的探针只在有曲线面板渲染时挂载（河段窗或代站窗打开时），这样 4.6 可以“先设开关、再开窗”复现“曲线面板渲染期抛错”；没有面板打开时设 `curve` 不产生兜底；(7) 兜底的「重试」沿用既有语义：开关仍为该值时重试后再次进入兜底，清掉开关后重试则区域恢复。
  - 兜底的移动定位口径（裁定）：先量再改——3.2 / 3.3 已让图例与地图控件两个边界在移动形态下把兜底块就地放进启动器列（`className` 为 `undefined`，在流），控制条边界的兜底仍是 `absolute bottom-10 left-1/2 -translate-x-1/2`。实现者先用崩溃开关在三个移动 project 下实测三处兜底的包围盒：已满足下列断言的不改类名；不满足的只做让断言成立所需的最小改动。地图控件与图例两处兜底**保持在流**、不加任何定位类（既有 `OverviewPageMapControlsBoundaryMobile.test.tsx` 钉着：移动形态下地图控件兜底的父节点就是启动器列，类名不含 `absolute` / `left-4` / `top-4` / `z-[120]`）；可调的只有控制条兜底的移动定位与兜底块自身的尺寸类。新增或保留的每个移动定位类都要有断言咬住。
  - Governing invariant：任一区域进入兜底时，兜底块完全在地图区内，且不遮挡、不与仍在渲染的其他区域（控制条、各启动器、运维入口）和 attribution 相交——其余区域保持可操作；无门控时探针对产品行为与全局读取零影响。
  - Sibling surfaces：(1) 四个边界：`region-error-map-controls`、`region-error-legend`、`region-error-control-bar`、`region-error-map-panels`（曲线）；第五个边界 `region-error-map`（地图本体）不加探针、不在本 task；(2) 兜底进列后撑宽启动器列的连带：列宽决定展开面板锚点与 3.4 提示条带的右边界——降级态下（某区域已崩）这两处允许被兜底块挤占，见 Non-goals，但仍在渲染的启动器必须点得到；(3) `useM11MobilePanelMaxHeight` 在控制条崩溃时找不到 floor 元素、退到 attribution 带之上（既有分支）——控制条兜底不得与展开面板的这条退路冲突到让面板伸出地图区；(4) 既有两个门控钩子（river-click、station-locate）与它们的“无门控不暴露”断言；(5) 桌面形态四处兜底的定位类（`MAP_CONTROLS_REGION_DESKTOP_CLASS`、`LEGEND_REGION_DESKTOP_CLASS`、控制条与曲线边界的 `className`）不变。
  - Must preserve：无门控时四个区域的渲染结果、DOM 与全局读取与改动前相同（`e2e/m11-station-open.mocked.spec.ts` 等断言无门控时 `__NHMS_E2E_HOOKS__` 未定义的用例照旧）；全部既有 vitest 文件（含 `OverviewPageRegionBoundaries.test.tsx`、`OverviewPageMapControlsBoundaryMobile.test.tsx`、`RegionErrorBoundary` 的既有测试、`riverClickPhase2Closure.test.ts`、`riverClickTypecheck.test.ts`）与全部既有 e2e spec 零 diff 且通过；桌面形态兜底位置不变；碰撞 spec 五个宽度零改动通过。
  - Risk pack「Public API / entry」selected：新的门控全局是测试车道的对外接口（4.6 等后续 task 依赖）-> vitest：探针四个标识各自“门控 + 匹配 -> 抛错、门控 + 不匹配 -> null、无门控 -> null 且零次读取开关”；`OverviewPage` 层面四个边界各自在“门控 + 对应标识”下出现对应的 `region-error-*` 且**只有**那一个。曲线区域今天没有任何 vitest 能经 `OverviewPage` 打开河段 / 代站窗，做法钉为：在新 vitest 里 mock `M11MapLibreSurface`，由桩调用 `onOverlayClick` 置出河段窗，再断言“门控 + `curve`”下 `region-error-map-panels` 出现、未开窗时设 `curve` 不出现；若这套桩超出一个测试文件的合理规模，退为对条件挂载处的组件级测试，并在偏离里写明页面级证据归 4.6；e2e（规格场景「Crash switch works only behind the gate」）：门控 + `legend` -> `region-error-legend` 可见；同值无门控 -> 图例启动器正常、无任何 `region-error-*`，且该用例先自证前提：页面上 `__NHMS_E2E_CRASH_REGION__ === 'legend'` 而 `__NHMS_E2E_HOOKS__` 为 `undefined`。新 spec 用自己的 `addInitScript` 设门控与开关（既有 `openRiverWindow` / `openStationWindow` 把设门控与开窗绑在一起，不适用）。
  - Risk pack「Error handling」selected：兜底几何与降级可用性 -> 移动 spec（三个移动 project 都跑、无按 project 跳过；规格场景只点名 390×664，另两个 project 同样断言）分别让 `legend`、`map-controls`、`control-bar` 崩溃：(a) 图例兜底：包围盒在 `m11-fullscreen-map` 内、不与 `m11-bottom-control-bar` 相交、不与仍在的图层 / 底图启动器相交；(b) 地图控件兜底：在地图区内、不与控制条相交、不与 `m11-launcher-legend` 相交，且图例启动器 `elementFromPoint` 命中自身、点它能展开图例面板；(c) 控制条兜底：在地图区内、不与三个启动器中的任何一个相交、不与 `.maplibregl-ctrl-attrib`（attribution）相交，且此时 `m11-bottom-control-bar` 计数为 0、三个启动器仍可见；(d) 每个场景断言页面上 `region-error-*` 恰为那一个、地图 canvas 仍在；(e) 重试：在页面上清掉开关后 tap 兜底里的「重试」，对应区域恢复（兜底消失、该区域的 testid 重新出现）——至少对图例做一次。(f) 列最高的组合：`setRole` 切到 operator 后让 `legend` 崩溃（列里是图层 / 底图启动器 + 图例兜底 + 运维入口），三个 project 都断言兜底不与控制条相交、运维入口 `elementFromPoint` 命中自身且底边 ≤ 控制条顶边——750×342 是咬合点（余量约 4px，兜底块高于 44px 即失败，届时收兜底块在移动形态的高度）。operator 角色下 `map-controls` 崩溃时运维入口随区域一起被兜底取代（它在同一个边界内），不另断言。
  - Risk pack「Auth / permissions / 发布面」selected：测试门控代码随生产包发布 -> 与既有两个钩子同一信任模型（门控是页面自己的 window 全局，只有能在该页面执行脚本的人能设；设了也只是让自己的页面区域进兜底，不读写任何数据、不调用产品回调）；证据 = 上述“无门控零读取”vitest + node-27 live receipt 里生产构建在不设门控、只设 `__NHMS_E2E_CRASH_REGION__ = 'legend'` 时图例正常、无 `region-error-*`。
  - Risk pack「Legacy compatibility」selected -> Must preserve；PR 描述贴出 `git diff --stat --diff-filter=MDR origin/master -- apps/frontend/e2e 'apps/frontend/src/**/__tests__/**'`（期望为空）与 `git diff --stat origin/master -- apps/frontend/src/components/map/M11MapLibreSurface.tsx`（期望为空）。
  - 未选：Concurrency（探针无状态）、File IO、Schema、Documentation（design.md D8 与规格已写明开关；runbook 归 7.x）、Config、Resource limits。
  - Seams under test：浏览器里三处兜底的包围盒与其余区域的可见 / 可点；vitest 里的探针纯逻辑与四个边界的接线。
  - Non-goals：曲线区域兜底的移动几何与“抽屉让位”（4.6——本 task 只放探针并用 vitest 证明它在有面板时抛错）；地图本体边界的探针；降级态下兜底块撑宽启动器列导致的展开面板锚点左移与提示条带右侧被挤占（仍在的启动器必须点得到，面板是否完整在地图区内不作断言）；兜底块内「重试」按钮的 44px 触控下限（5.1 审计）；桌面形态兜底的任何变化。
  - Review focus：(1) 无门控路径零读取、零 DOM、零行为差异；(2) 每个边界恰一个探针且位于边界之内、真实内容同侧；(3) 曲线探针只在有面板时挂载；(4) 兜底的移动类名是“量出来需要才改”，每个类都被断言咬住；(5) `M11MapLibreSurface.tsx` 与两个既有钩子零 diff；(6) 三个移动 project 都断言、无跳过；(7) 重试语义未被探针破坏。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；新 spec 与助手的 strict `tsc --noEmit` exit 0；新 spec 与新 vitest 先红后绿（红：对 `origin/master` 的 `src/`——没有探针时三个崩溃场景都等不到兜底；“无门控正常渲染”那条改动前本就成立，可先绿，须点名），三个移动 project 各非零 passed，`--repeat-each=5` 无 flaky；改展示端运行时代码，合并前出 node-27 live receipt（PR 的生产构建对 live API：三个移动视口 + 1280×900 下不设门控只设开关 -> 无任何 `region-error-*`、图例 / 启动器正常；设门控 + `legend` / `map-controls` / `control-bar` 各一次 -> 对应兜底在地图区内且满足上面 (a)(b)(c) 的相交断言；既有脚本默认视口的桌面布局 oracle 不回归）。

- [x] 3.7 控制条竖屏两行：移动形态且非矮视口横屏时，控制条全宽减两侧 8px、两行（第一行预报源切换 + 起报时次 + 播放速度，第二行步进 / 播放按钮 + 时间轴滑块），滑块 ≥ 120px，按钮 ≥ 44×44，起报时次与播放速度两个 `<select>` 高 ≥ 44px、字号 ≥ 16px，底边距地图区底 40px、不压 attribution。播放速度选择器与其 state 现在在时间轴组件内部，把它排到第一行需要调整时间轴组件的结构或把该 state 上提到控制条。移动形态条高不受 64px token 约束；桌面形态仍为 64px 单行。新增 `e2e/m11-control-bar-portrait.mobile.mocked.spec.ts`（矮视口横屏 project 下本文件只断言“全部控件在视口内”）与 `e2e/m11-control-bar-desktop.mocked.spec.ts`。

  Depends on: 1.1, 1.3, 2.1, 3.2

  Verify：`mobile-portrait` 下以及 `setViewportSize(320, 568)` 后，全部控件包围盒在视口内、滑块 ≥ 120px、滑块顶边不高于预报源切换的底边（两行）、条底边距地图区底 40px、不与 attribution 相交、两个 `<select>` 达到尺寸与字号下限；前进一步后的 valid-time query 与桌面同一操作的结果相同；桌面 spec 断言 1280×900 下条高 64、单行。

  **Suggested fixture level:** expanded - 控制条驱动时间轴 query，是共享交互入口。

  **Minimal mergeable slice:** atomic - 这是首刀（竖屏两行），矮视口横屏单行已切为 3.8。依赖 3.2 是因为既有碰撞 spec 的 520px 行钉着条高 64，那一行的改写权归 3.2；依赖 1.3 是因为“前进一步”需要带有效时刻的图层 mock，既有 mock 的有效时刻为空。

  Triage（#2800）：Issue type: feature ｜ Fixture level: expanded（与上游建议一致；设计见 design.md D6，规格见 `specs/mobile-map-overlay-layout` 的「The bottom control bar is fully operable in mobile form」）｜ Blast radius: `/` 的唯一时间与预报源入口——控制条的三个控件写 URL（`source` / `cycle` / `validTime`），布局写错会让手机上切不了预报源、拖不了时间轴；条高变化牵动所有按“控制条顶边”取位的移动浮层（启动器列、展开面板限高、状态条容器）。
  - Change surface：`src/pages/m11/M11BottomControlBar.tsx`（竖屏两行布局、移动形态的宽度与控件尺寸）、`src/pages/m11/M11Controls.tsx` 的 `M11Timeline`（播放速度的受控入口、移动形态的控件尺寸）、`src/components/map/m11MapRuntime.tsx`（仅状态条容器的移动底部界限类，见下）、两个新 spec（`e2e/m11-control-bar-portrait.mobile.mocked.spec.ts`、`e2e/m11-control-bar-desktop.mocked.spec.ts`）与其助手、对应 vitest。`src/lib/m11/visualTokens.ts`、`deriveM11ControlBarModel`、`buildM11TimelineViewModel`、`src/lib/m11/queryState.ts` 零 diff。
  - 布局机制（裁定）：(1) 形态信号用 `useMobileForm()`（`mobile && !landscape` = 竖屏两行；矮视口横屏在本 task 保持与桌面相同的单行结构，只吃下面的移动尺寸类，其收尾归 3.8）。(2) 播放速度的 state 从 `M11Timeline` 上提到 `M11BottomControlBar`：`M11Timeline` 新增可选的受控 props（速度值、变更回调、是否自己渲染速度选择器），不传时行为、根节点默认类串与 DOM 结构（节点树 / 顺序 / 无刻度行）和今天相同（既有 vitest 钉的是根节点 `m11-timeline` 的类串与刻度行不存在；内部控件允许追加 `mobile:` / `mobile-landscape:` 变体类）；`playing` 仍是 `M11Timeline` 的本地 state。(3) 竖屏时控制条根节点改为可换行的 flex：子节点顺序固定为 预报源分段、起报时次 `<select>`、〔仅竖屏渲染的〕播放速度 `<select>`、`M11Timeline`（占满整行 -> 落到第二行，内部是步进 / 播放按钮 + 滑块列）、禁用原因。不加包裹元素、不改子节点的相对位置——`M11Timeline` 在竖屏 / 横屏 / 桌面之间切换时不得重挂（`playing` 与滑块焦点不因旋转丢失；vitest 用“形态翻转前后 `m11-timeline` 是同一个 DOM 节点”证明）。(4) 任一形态下页面上恰有一个「播放速度」选择器；速度值跨形态保持。(5) 移动形态（两种朝向）下控制条宽 = 地图区宽 − 16px、仍 `bottom-10`；竖屏条高由内容决定（不套 `h-16`），矮视口横屏与桌面仍套 64px token 类。(6) 移动形态下三个步进 / 播放按钮与预报源分段按钮 ≥ 44×44，两个 `<select>` 高 ≥ 44px、字号 ≥ 16px（纯 `mobile:` 尺寸类，两种朝向都生效，3.8 复用）。若实现者发现更简单且满足下列不变量的写法，可以用，但须在偏离里写明。
  - 禁用原因：`m11-control-bar-disabled-reason` 在竖屏排在第二行之后（自成一行或截断），不得把任何控件挤出视口；其文案与出现条件不变。
  - Governing invariant：控制条在任何形态下都把五类控件（预报源分段、起报时次、步进 / 播放、播放速度、滑块）完整放在视口内，底边恒距地图区底 40px、不压 attribution；控件产生的 query patch 与形态无关；桌面形态（宽 ≥ 768 且高 ≥ 500）的条高 64px、单行、DOM 结构与计算几何与今天相同（不删改任何在桌面生效的类；允许在既有元素上追加 `mobile:` / `mobile-landscape:` 变体类）。
  - Sibling surfaces（都按“控制条顶边”取位，竖屏条变高后须逐个复核）：(1) 启动器列与运维入口（竖屏余量大）；(2) `useM11MobilePanelMaxHeight`：控制条根节点仍是同一个元素、已被 `ResizeObserver` 观察，条高变化会触发重量——本 task 不得让控制条根节点按形态换元素 / 换 key；(3) 3.4 的状态条容器：其底部界限 `mobile:bottom-[6.5rem]` 是按 64px 条高写死的，竖屏两行后容器会伸进控制条所在的带（`m11-notices.mobile` 的“状态条下方的点命中地图 canvas”在 390×664 会变红）——本 task 把竖屏的界限改到竖屏控制条顶边之上（含出现禁用原因行时的条高，取一个有余量的值），矮视口横屏保留现值；(4) 3.6 的控制条兜底（`bottom-10` 居中，不依赖条高）；(5) 既有碰撞 spec 520 行（控制条“在视口内、不压 attribution”；空清单提示已在顶部条带）；(6) `scripts/node27_display_v2_browser_evidence.mjs` 对“控制条 ≠ 64”硬失败——只在默认桌面视口运行，零 diff。
  - Must preserve：桌面形态控制条的 DOM 结构、计算几何、64px 条高（逐字钉死的只有两处：`M11Timeline` 不传 `className` 时根节点的默认类串与默认 DOM 结构（节点树 / 顺序 / 无刻度行）——既有 vitest「keeps its default DOM byte-identical when no cycle is passed」，以及 `m11-timeline-rows` 的子节点数——行数预算 oracle；其余元素，包括 `M11Timeline` 内部的按钮与速度 `<select>`，允许追加变体类）、三个控件的 patch 语义（切源清 `cycle`、已选分段幂等、fail-closed 时全部禁用）——既有 `src/pages/m11/__tests__/M11BottomControlBar.test.tsx` 与其余全部既有 vitest 文件零 diff 且通过（已在跑移动竖屏分支、只断言控制条存在的有 `OverviewPageLegendLauncher`、`OverviewPageLayerBasemapLaunchers`、`OverviewPageMapControlsBoundaryMobile`、`OverviewPageRegionCrashProbes`）；全部既有 e2e spec 零 diff 且通过，其中须在竖屏条变高后仍原样通过的移动 spec：`m11-legend-launcher.mobile`、`m11-layer-basemap-launchers.mobile`、`m11-notices.mobile`、`m11-ops-entry.mobile`、`m11-region-fallbacks.mobile`、`m11-role-selector.mobile`（切换器不与控制条相交）、`m11-header.mobile`、`m11-baseline.mobile`、`m11-zoom-control.mobile`，以及桌面 project 里 `setViewportSize` 进入竖屏移动形态的用例；碰撞 spec 五个宽度零改动通过。注意：`valid_times: []` 的既有移动 spec（legend-launcher、ops-entry、layer-basemap、region-fallbacks、碰撞 520 行）渲染的正是带禁用原因的最高条，它们零 diff 通过就是最大条高下的回归证据。若某条既有断言因此变红，停下报告确证的原因与实测数字，不改那个 spec。
  - 补充裁定（fixture review）：(a) 换行机制：`controlBarTimelineClassName` 是 `flex-1 min-w-0`，只加 `flex-wrap` 不会换行——竖屏须经 className 给 `M11Timeline` 100% 的 flex-basis（不换元素）。(b) 320 宽的行预算：内容宽 = 304 − 左右内边距；第一行（两个 ≥ 44px 的分段按钮 + 起报时次 + 播放速度）余量不足 10px，所以竖屏的行内间距与内边距须收紧到让它成立，并把起报时次 `<select>` 定为可收缩项（`min-w-0`，文本截断）——空周期选项「无可用起报时次」在 16px 字号下约 140px，不收缩必然溢出或把速度挤到下一行。“播放速度与预报源同在第一行”在有周期与无周期两种 mock 下都要成立。(c) 禁用原因保持单行截断（否则条高无上界，状态条容器的静态界限不可证），竖屏下自成第三行。(d) 上提后的速度选择器的 `disabled` 与 `M11Timeline` 内部用同一表达式（活动图层 `validTimes.length === 0`，见 `M11Controls.tsx` 的 `disabled` 派生），不引入新的聚合禁用位。(e) 状态条容器界限写成 `mobile:bottom-[X] mobile-landscape:bottom-[6.5rem]`，X ≥ 40px + 最高竖屏条高（两行 + 禁用原因行 + 间距内边距，按实测取整到有余量的 rem 值，预计 ≥ 14rem），同步改 `m11MapRuntime.tsx` 里那段过期注释。这处改动越出 issue 的 PR Boundary（“控制条与时间轴组件”），记入偏离：`e2e/m11-notices.mobile.mocked.spec.ts` 钉的是“容器内状态条下方的点命中地图 canvas / 不与控制条相交”而不是类名，条高超过 64 即红，所以改动必需且该 spec 可零 diff。(f) vitest 的矮视口横屏分支：既有 `src/test/mobileFormMatchMedia.ts` 让横屏查询恒不命中——在新 vitest 文件内自建可控桩（或在不改既有导出行为的前提下给该助手加一个可选参数，并把它列入改动文件）。
  - Risk pack「Public API / entry」selected：控件与 query -> 移动 spec `e2e/m11-control-bar-portrait.mobile.mocked.spec.ts`：竖屏（`mobile-portrait` project，以及三个 project 里都用 `setViewportSize(320, 568)` 再跑同一组断言）下：预报源分段的每个按钮、起报时次、播放速度、三个步进 / 播放按钮、滑块的包围盒都在视口内；滑块宽 ≥ 120；滑块顶边 ≥ 预报源分段底边（两行）、播放速度与预报源分段纵向重叠（同在第一行）、步进按钮与滑块纵向重叠（同在第二行）；控制条底边距 `m11-fullscreen-map` 底 40px（±0.5）、不与 `.maplibregl-ctrl-attrib` 相交；两个 `<select>` 高 ≥ 44 且计算字号 ≥ 16px；按钮 ≥ 44×44；页面上「播放速度」选择器恰一个。矮视口横屏 project 下本文件的默认视口用例只断言“全部控件在视口内”（约定如此），不跳过。`valid-time` 等价：用带有效时刻的图层 mock（1.3 的 `installRiverWindowMocks` 或等价助手）在竖屏点「下一个有效时刻」记下 `location.search`，同一用例内 `setViewportSize(1280, 900)` 后重新 `goto('/')` 做同一操作，断言两次相等且都不等于操作前（跨形态对照的显式例外，同 3.2 / 3.3）。
  - Risk pack「Concurrency / shared state / ordering」selected：上提的速度与不重挂 -> vitest：竖屏分支速度选择器在控制条里、不在 `m11-timeline` 里，桌面 / 横屏分支在 `m11-timeline` 里，任一形态恰一个；改速度后翻转形态速度值保持；形态翻转前后 `m11-timeline` 是同一个节点且 `playing` 未被重置（播放中翻转后仍显示「暂停时间轴」）；上提后的速度确实驱动播放间隔（假定时器：2x 时 500ms 前进一步）。e2e：竖屏展开图例面板后面板底边仍在控制条顶边之上（限高随条高重量）。
  - Risk pack「Resource limits / 溢出」selected：320 宽与禁用原因 -> 上面 320×568 的整组断言；另一条用例让禁用原因出现——在 `installRiverWindowMocks` 之后叠加一条 `/api/v1/layers` 窄路由（做法同 `e2e/support/notices.mocked.ts`：`valid_times: []` + `default_cycle: null` 得 fail-closed 文案与空周期选项；或挂起该请求得“目录未就绪”文案），在 390×664 与 `setViewportSize(320, 568)` 两个视口下断言全部控件与该原因文本都在视口内、控制条仍在地图区内且不压 attribution、启动器列不与控制条相交。状态条容器的新界限：390×664 下让地图源错误状态条 + 浮动提示出现，断言二者不与控制条相交，且状态条下方、容器范围内的点命中地图 canvas（承接 #2797 的携带项；既有 `m11-notices.mobile` 零 diff 通过是同一件事的回归证据）。
  - Risk pack「Legacy compatibility」selected：桌面不变 -> 桌面 spec `e2e/m11-control-bar-desktop.mocked.spec.ts`（桌面 project）：1280×900 与 768×1024 下控制条高 64（±0.5）、单行（滑块与预报源分段纵向重叠）、播放速度选择器在 `m11-timeline` 内、控制条宽与水平居中同今天（`min(64rem, 100% − 2rem)`）；Must preserve 所列各项；PR 描述贴出 `git diff --stat --diff-filter=MDR origin/master -- apps/frontend/e2e 'apps/frontend/src/**/__tests__/**'`（期望为空）。
  - Risk pack「Error handling」selected：fail-closed 与禁用态在竖屏仍如实呈现 -> vitest：竖屏分支下 fail-closed 时上提的速度选择器同样禁用（既有断言“每个选择器都禁用”在新位置继续成立）；另补一条非 fail-closed 的空有效时刻列表态（对应既有“某周期 valid-times 取回失败时起报时次仍可用”那条）：速度选择器禁用、起报时次可用。
  - 未选：File IO、Auth、Schema、Release、Documentation（06B §8 已写明两行 / 单行）、Config。
  - Seams under test：浏览器里控制条各控件的包围盒、计算样式与 URL；vitest 里控制条 / 时间轴的形态分支、速度受控通路与不重挂。
  - Non-goals：矮视口横屏单行的收尾与其专属 spec（3.8）；滑块拇指的触控命中区与全页 44px 审计（5.1）；抽屉打开时暂停播放与隐藏控制条（4.6——本 task 只上提速度，不暴露“暂停”）；控制条内文字信息行（当前有效时刻、来源、分辨率、刻度）在竖屏的取舍——保持现有内容，放不下时按既有 `truncate` 截断；桌面形态的任何变化。
  - Review focus：(1) 桌面 DOM 结构与计算几何不变（只追加变体类、不删改桌面生效类），`M11Timeline` 不传新 props 时根节点默认类串与 DOM 结构不变（内部控件只追加变体类）；(2) `M11Timeline` 跨形态不重挂、`playing` 不丢；(3) 恰一个速度选择器、速度跨形态保持并真实驱动间隔；(4) 三个控件的 patch 语义零改动；(5) 状态条容器界限与其余按控制条顶边取位的浮层在竖屏条变高后仍成立；(6) 320×568 的断言在三个 project 都跑；(7) 没有碰 token 与模型派生。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；两个新 spec 与助手的 strict `tsc --noEmit` exit 0；新 spec 与新 vitest 先红后绿（红：对 `origin/master` 的 `src/`，点名红的用例与原因；桌面 spec 改动前本就成立，可先绿），移动 spec 三个移动 project 各非零 passed，`--repeat-each=5` 无 flaky；改展示端运行时代码，合并前出 node-27 live receipt（PR 的生产构建对 live API：390×664 与 320×568 下五类控件在视口内、两行、滑块 ≥ 120、条底距地图区底 40px、不压 attribution、点「下一个有效时刻」后 `validTime` query 变化；750×342 与 844×390 下全部控件在视口内；1280×900 与 768×1024 下条高 64 单行；既有脚本默认视口的桌面布局 oracle 不回归）。

- [x] 3.8 控制条矮视口横屏单行：矮视口横屏下控制条为单行，全部控件同一行，滑块 ≥ 120px，尺寸与字号下限同 3.7，底边距地图区底 40px、不压 attribution。新增 `e2e/m11-control-bar-landscape.mobile.mocked.spec.ts`（`mobile-portrait` project 下本文件用 `setViewportSize(750, 342)` 运行同一组断言）。

  Depends on: 3.7

  Verify：750×342 与 844×390 下全部控件包围盒在视口内、滑块 ≥ 120px、滑块与预报源切换在垂直方向重叠（同一行）、条底边距地图区底 40px、不与 attribution 相交。

  **Suggested fixture level:** compact - 同一组件的一个形态分支，交互契约在 3.7 已定。

  **Minimal mergeable slice:** atomic - 一种视口下的一行布局。

  Triage（#2801）：Issue type: feature ｜ Fixture level: compact（与上游建议一致；同一组件的一个形态分支，速度上提、尺寸下限与条宽已在 3.7 落地；设计见 design.md D6，规格见 `specs/mobile-map-overlay-layout` 的「The bottom control bar is fully operable in mobile form」中的矮视口横屏场景与“滑块永不窄于 120px”）｜ Blast radius: `/` 在手机横屏下的时间与预报源入口——3.7 的 44px 控件让横屏单行更挤：实测 750×342 在出现禁用原因时滑块只剩 27.5px、时间轴内容 101px 高并溢出 64px 的条。
  - 现状（3.7 之后，node-27 实测）：矮视口横屏有周期时已是单行、条高 64、滑块宽 274（750×342）/ 368（844×390）、控件 44px / 16px、底距 40px。所以本 task 的实质是 (a) 把“出现禁用原因 / 无周期”这个最挤的状态修到满足规格，(b) 交付横屏专属 spec 把单行契约钉住。
  - Change surface：`src/pages/m11/M11BottomControlBar.tsx`（矮视口横屏下禁用原因的收缩 / 截断）、`src/pages/m11/M11Controls.tsx` 的 `M11Timeline`（滑块列的最小宽度、底行文本不折行）、新 spec `e2e/m11-control-bar-landscape.mobile.mocked.spec.ts`（mock 复用 `e2e/support/controlBar.mocked.ts`，需要时在该文件追加导出）、必要的 vitest。只追加 `mobile-landscape:` 变体类，不新增 JS 分支、不改 3.7 的竖屏结构；`m11MapRuntime.tsx`、`OverviewPage.tsx`、token 与模型派生零 diff。
  - 布局口径（裁定；本 task 新增的类一律用 `mobile-landscape:` 变体，不用 `mobile:`——竖屏的条高与布局必须与 3.7 合并时逐像素相同）：(1) 矮视口横屏的条高保持 64px（不改条高——启动器列的四项算术、状态条容器的 `6.5rem` 界限、3.6 的兜底都按它成立）；(2) 滑块所在列（`m11-timeline-rows`）加 `mobile-landscape:` 的 120px 最小宽度，任何状态下滑块 ≥ 120px；(3) 被压缩的是禁用原因，机制写死：时间轴根节点是 `min-w-0 flex-1`（basis 0），只让原因“可收缩”不会生效（剩余空间为正时它停在 192px 不收缩，滑块列的最小宽度反而让滑块溢出时间轴盒压到原因文字上）——所以给原因加矮视口横屏专属的宽度上限（约 80px 量级，按 750×342 fail-closed 实测取值，使“预报源 + 起报时次 + 时间轴内容下限 + 原因 + 间距”≤ 条内容宽），单行截断，`title` 仍带全文；**不改时间轴根节点的类串**（`M11BottomControlBarMobileForm.test.tsx` 在矮视口横屏与桌面两轮里逐字钉着 `flex min-w-0 flex-1 items-center gap-3 text-sm`）；(4) 时间轴内的文本行不得折行把内容撑高：底行的 `Analysis / Forecast` 在矮视口横屏不折行，其余文本沿用既有 `truncate`。时间轴的自然高度今天就是约 69px（滑块行的行盒约 21px 而非 16px），在 64px 的条里上下各溢出约 2.5px——这是桌面也有的既有事实，本 task 不要求消掉，只要求不再因折行而变得更高。起报时次 `<select>` 不另加收缩规则（750 宽下它不必让）。若实测某条用另一种写法更简单且满足下面的断言，可以用，须在偏离里写明。
  - Must preserve：桌面形态与移动竖屏的布局与 3.7 合并时相同——`e2e/m11-control-bar-portrait.mobile.mocked.spec.ts`、`e2e/m11-control-bar-desktop.mocked.spec.ts`、`M11BottomControlBarMobileForm.test.tsx`、`M11BottomControlBar.test.tsx` 及其余全部既有 vitest / e2e 零 diff 且通过（`m11-timeline-rows` 的子节点数不变、`M11Timeline` 根节点默认类串不变）；矮视口横屏有周期时的现状几何不退化（条高 64、底距 40、滑块不变窄到 120 以下）；`m11-ops-entry.mobile`（750×342 运维入口距条顶 4px）、`m11-notices.mobile`、`m11-region-fallbacks.mobile` 原样通过；碰撞 spec 五个宽度零改动通过。
  - Risk pack「Public API / entry」selected：横屏单行契约 -> 新移动 spec（三个移动 project，无按 project 跳过；`mobile-portrait` project 下用 `setViewportSize(750, 342)` 跑同一组断言，另在每个 project 里用 `setViewportSize(844, 390)` 与 `setViewportSize(750, 342)` 各跑一遍，使两个验收视口在三个 project 都被断言）：有周期的目录下——预报源分段各按钮、起报时次、播放速度、三个步进 / 播放按钮、滑块的包围盒都在视口内；滑块宽 ≥ 120；滑块与预报源分段纵向重叠（同一行）、步进按钮与预报源分段纵向重叠；控制条高 64（±0.5）、底边距 `m11-fullscreen-map` 底 40px（±0.5）、不与 `.maplibregl-ctrl-attrib` 相交；按钮 ≥ 44×44、两个 `<select>` 高 ≥ 44 且计算字号 ≥ 16px；「播放速度」选择器恰一个且在 `m11-timeline` 内；点「下一个有效时刻」后 `validTime` query 变化。
  - Risk pack「Resource limits / 溢出」selected：最挤状态 -> 同一 spec 用 fail-closed 目录（`installFailClosedDischargeLayer`：无周期 + 禁用原因）在 750×342 与 844×390 下断言：滑块宽 ≥ 120（硬断言——改动前 750×342 为 27.5px，这是本 task 的红）；全部控件在视口内；`m11-timeline` 的包围盒高 ≤ 70px 且上下各不超出控制条包围盒 3px（既有的约 2.5px 行盒溢出之内，折行即失败）；滑块包围盒在 `m11-timeline` 包围盒内，且不与 `m11-control-bar-disabled-reason` 相交（防“溢出式通过”）；条高仍 64、底距 40、不压 attribution；`m11-control-bar-disabled-reason` 若有可见宽度则在控制条盒内且为单行（高 ≤ 其行高 × 1.5），其 `title` 为全文；启动器列（含 `setRole` 切到 operator 后的运维入口）不与控制条相交。
  - 形态归属 oracle（承接 #2790 的携带项：`mobile-landscape` 变体须有“命中 / 不命中”的计算证据）：同一 spec 一条用例 `setViewportSize(320, 480)`（高 < 500 但非横屏）断言控制条是竖屏两行（滑块顶边 ≥ 预报源分段底边），且 `m11-timeline-rows` 的计算 `min-width` 在 320×480 下不是 `120px`、在 750×342 下是 `120px`（矮视口横屏专属类在竖向的矮视口不生效、在横屏生效）；同一用例顺带断言 320×480 下出现禁用原因时竖屏条高与 3.7 相同的结构（原因自成一行、不受横屏宽度上限影响）。
  - Risk pack「Legacy compatibility」selected -> Must preserve；PR 描述贴出 `git diff --stat --diff-filter=MDR origin/master -- apps/frontend/e2e 'apps/frontend/src/**/__tests__/**'`（期望为空，`e2e/support/controlBar.mocked.ts` 若只追加导出须说明）。
  - 未选：Concurrency（不动 state）、File IO、Auth、Schema、Release、Documentation、Config、Error handling（禁用原因的文案与出现条件不变，只改它在横屏的可收缩性）。
  - Non-goals：改矮视口横屏的条高或行数；禁用原因在横屏被截断后的“看全文”交互（`title` 之外）；视口宽 < 568 的横屏（如 480×320）下的单行可行性——本 change 的横屏验收视口是 750×342 与 844×390；滑块拇指命中区与 44px 全页审计（5.1）；抽屉打开时的控制条行为（4.6）。
  - Review focus：(1) 只追加变体类，桌面与竖屏的计算布局不变；(2) 滑块 ≥ 120 在 fail-closed 下由硬断言守住；(3) 时间轴内容不因折行变高（高 ≤ 70、上下各不超出条 3px）、滑块不压到禁用原因；(4) 两个验收视口在三个 project 都断言、无跳过；(5) 形态归属 oracle 非恒真。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；新 spec 的 strict `tsc --noEmit` exit 0；新 spec 先红后绿（红：对 `origin/master` 的 `src/`——预期红的是 fail-closed 的滑块宽度与内容溢出；有周期的用例改动前可能本就成立，须点名），三个移动 project 各非零 passed，`--repeat-each=5` 无 flaky；改展示端运行时代码，合并前出 node-27 live receipt（PR 的生产构建对 live API：750×342 与 844×390 下单行、滑块 ≥ 120、条高 64、底距 40、不压 attribution、步进后 `validTime` 变化；390×664 两行不变；1280×900 条高 64；fail-closed 状态 live 下无法稳定复现，如实写进限制；既有脚本默认视口的桌面布局 oracle 不回归）。

## 4. 曲线抽屉（`/`）

- [x] 4.1 单窗策略：移动形态下打开河段窗关闭气象代站窗、反之亦然；从桌面形态进入移动形态且双窗都开时保留活动窗、关闭另一个；桌面形态双窗并存不变。只用 vitest 在页面组件层验证（既有 MapLibre 桩可直接触发要素点击回调，`matchMedia` 用可控桩）；不新增 e2e——真实浏览器里抽屉会盖住钩子定位到的第二个要素。

  Depends on: 2.1

  Verify：vitest 覆盖：移动形态下河段 → 站点后只剩气象代站窗，站点 → 河段后只剩河段窗；桌面形态双窗都开且气象代站窗为活动窗时切到移动形态，只剩气象代站窗；桌面形态下两个方向都保持双窗并存。

  **Suggested fixture level:** expanded - 改页面级的曲线窗状态，并收窄既有“双窗并存”契约的适用范围。

  **Minimal mergeable slice:** atomic - 这是原“单窗 + 让位”的首刀：只动曲线窗状态。让位（隐藏控制条等）已切为 4.6。

  Triage（#2802）：Issue type: feature ｜ Fixture level: expanded（与上游建议一致；设计见 design.md D10，规格见 `specs/mobile-curve-sheet` 的「Mobile form shows one curve window at a time」与 `specs/map-feature-popups`（MODIFIED）的「River and station curve windows can coexist and move」）｜ Blast radius: `/` 的曲线窗状态——`OverviewMode` 里的 `riverPopup` / `stationPopup` / `activeCurveWindow` 是河段窗与气象代站窗的唯一开合来源，也决定地图上的选中高亮与曲线区域错误边界的复位键；分支写错会让桌面丢掉“双窗并存”，或让移动形态在 4.2 的抽屉落地后叠出两个抽屉。
  - Change surface：`apps/frontend/src/pages/OverviewPage.tsx` 的 `OverviewMode`（读 `useMobileForm().mobile`，加一处状态收敛）；新 vitest 文件 `src/pages/__tests__/OverviewPageSingleCurveWindow.test.tsx`。不改 `M11RiverForecastPanel` / `M11StationForcingPopup` / `M11DraggableCurveWindow` / `M11MapLibreSurface` / `useMobileForm`；不新增、不修改任何 e2e。
  - Governing invariant：移动形态下 `riverPopup` 与 `stationPopup` 至多一个非空；两者同时非空时关闭的是 `activeCurveWindow` **没有**指向的那个。桌面形态下两者相互独立（任何一次打开、关闭都不动另一个）。
  - 实现口径（裁定）：不变量在**一处**收敛——`OverviewMode` 里一个依赖 `mobile`、两个 popup 是否非空与 `activeCurveWindow` 的 layout effect：`mobile` 且两窗都开时，`activeCurveWindow === 'station'` 则 `setRiverPopup(null)`，否则 `setStationPopup(null)`。点击处理函数 `handleMapOverlayClick` 不加形态分支：它本来就在打开某类窗时把 `activeCurveWindow` 指向该类，所以“河段替换站点”“站点替换河段”“桌面进入移动形态”三种场景由同一条规则覆盖。用 layout effect 而不是普通 effect：收敛发生在浏览器绘制之前，移动形态下不会画出一帧双窗。“关闭”是清状态而不是隐藏：回到桌面形态后被关掉的窗不复现。不新增 state、不改 `activeCurveWindow` 的类型与初值。
  - Sibling surfaces：(1) `selectedSegmentId` / `selectedStationId`（由两个 popup 派生，经 `M11FullscreenMap` 传给 `M11MapLibreSurface` 做选中高亮）——被关掉的那类高亮随之清除，是期望行为，须断言；(2) 留下的那个面板的 `active` prop 为 `true`（既有表达式在另一窗为空时即为真，不改表达式）；(3) 曲线区域 `RegionErrorBoundary` 的 `resetKeys=[selectedSegmentId, selectedStationId]`——替换时键变化、边界复位，与今天“选中另一个要素即复位”同一语义，不改；(4) 两个既有清理 effect（切图层清两窗、关 `metStations` 清站点窗）不动；(5) `RegionCrashProbe region="curve"` 的挂载条件不动（仍是“有任一面板”）。
  - Must preserve：桌面形态下河段 → 站点、站点 → 河段都保持双窗并存，关闭其中一个不影响另一个；移动形态下同类再点（河段 A → 河段 B）仍是替换该类的选中、不动另一类（此时另一类本就为空）；移动形态下关闭唯一的窗后两者皆空；全部既有 vitest 文件（含 `OverviewPageRegionCrashProbes.test.tsx`、`OverviewPageRegionBoundaries.test.tsx`）与全部既有 e2e（含 `m11-overlay-collision.mocked.spec.ts` 的 1920 / 1440 / 1280 / 800、`m11-station-open.mocked.spec.ts`、河段窗夹具 spec）不改期望值通过。
  - Risk pack「Public API / entry」selected：曲线窗开合是 `/` 的用户可见契约 -> 新 vitest（做法沿用 `OverviewPageRegionCrashProbes.test.tsx`：mock `M11MapLibreSurface`，由桩调用 `onOverlayClick` 触发河段 / 站点点击，桩同时把收到的 `selectedSegmentId` / `selectedStationId` 写到 DOM 属性；两个面板用带 testid 的桩，桩把收到的 `active` 写到 DOM 属性并暴露 `onClose`；`matchMedia` 用 `src/test/mobileFormMatchMedia.ts`），用例：(a) 移动形态：河段 → 站点后只有站点桩、其 `active` 为真；(b) 移动形态：站点 → 河段后只有河段桩、其 `active` 为真；(c) 在 (a)(b) 里被关掉那类的选中 id 在地图桩上为空，留下那类的选中 id 仍在。
  - Risk pack「Concurrency / 状态迁移」selected：形态切换时的收敛 -> 同一 vitest：(d) 桌面形态双窗都开、站点窗为活动窗（后点站点），`setMobile(true)` 后只有站点桩；(e) 对称：桌面形态双窗都开、河段窗为活动窗，`setMobile(true)` 后只有河段桩；(f) 承接 (d)：再 `setMobile(false)` 后仍只有站点桩（被关的窗不复现）；(g) 移动形态下关闭唯一的窗（调用桩拿到的 `onClose`）后两个桩都不在，再点河段能重新打开。
  - Risk pack「Legacy compatibility」selected：收窄既有“双窗并存”契约的适用范围 -> 同一 vitest：(h) 桌面形态河段 → 站点后两个桩都在；(i) 桌面形态站点 → 河段后两个桩都在；(j) 桌面形态双窗都开时关闭其中一个，另一个仍在。PR 描述贴出 `git diff --stat --diff-filter=MDR origin/master -- 'apps/frontend/src/**/__tests__/**'`（期望为空）与 `git diff --stat origin/master -- apps/frontend/e2e`（期望为空）。
  - 未选：Error handling（边界与兜底不改，复位键语义不变，见 Sibling surfaces (3)；曲线兜底时的让位归 4.6）、Auth、File IO、Schema、Config、Resource limits、Documentation（规格与 design.md D10 已写明；`docs/spec/06B` §8 已在 1.2 落地）。
  - Seams under test：`OverviewMode` 的曲线窗状态机——输入是地图点击回调与形态信号，输出是渲染了哪些面板、各自的 `active` 与传给地图的选中 id。
  - Non-goals：抽屉形态（4.2）；抽屉打开时让位与暂停（4.6）；自动平移与站点锚点（4.10）；任何 e2e（issue 明文：真实浏览器里窗会盖住钩子定位到的第二个要素）；“不画出一帧双窗”不作自动化断言（jsdom 无绘制；由“用 layout effect”这条实现口径与 review 保证）；从移动形态回到桌面形态时恢复被关掉的窗。
  - Review focus：(1) 不变量只在一处收敛，`handleMapOverlayClick` 无形态分支；(2) 桌面形态下该 effect 是空操作（`mobile` 为假时不调用任何 setter）；(3) effect 的依赖完整且不会自激（清掉一个之后条件不再成立）；(4) 关的是非活动窗，方向没写反——(d)(e) 两条对称用例都在；(5) 用的是 layout effect；(6) 既有测试与 e2e 零改动。
  - Evidence floor：约定的本地验证命令 + `check:types` 全绿（本 task 不新增 e2e 文件，治理门测试照跑、期望不变）；新 vitest 先红后绿——红：对 `origin/master` 的 `src/`，(a)(b)(c)(d)(e)(f) 红（移动形态下两个桩都在），(g)(h)(i)(j) 改动前已成立、可先绿，须在 PR 里点名；变异：把 effect 里的方向写反时 (a)(b)(d)(e) 变红，去掉 `mobile` 条件时 (h)(i)(j) 变红。改展示端运行时代码，合并前出 node-27 live receipt（PR 的生产构建对 live API）：桌面布局 oracle exit 0；1280×900 下经门控钩子先开河段窗再开站点窗，两窗并存；390×664 下开河段窗成功，再经站点钩子点站点——若站点可点到则断言只剩站点窗，若被河段窗盖住点不到则如实写进限制（单窗替换的权威证据是 vitest）。

- [x] 4.2 曲线窗抽屉形态：`M11DraggableCurveWindow` 在移动形态渲染为抽屉——非矮视口横屏时贴底、全宽、高度固定为 `min(60dvh, 地图区高度 − 8px)`；矮视口横屏时贴右、全高、宽度固定为 `min(50vw, 28rem)`；尺寸不随内容状态变化；无固定宽高比；不挂拖拽监听；头部不随主体滚动；层级高于控制条、启动器与面板；回到桌面形态时为默认位置的可拖拽窗。桌面形态行为不变。新增 `e2e/m11-curve-sheet.mobile.mocked.spec.ts`。

  Depends on: 1.4, 3.3, 4.1

  Verify：移动 spec 断言河段窗与气象代站窗在 390×664 的左 / 右 / 底边与地图区重合且高度等于公式值，在 750×342 与 844×390 的上 / 下 / 右边与地图区重合且宽度等于公式值；320×480 为底部抽屉、600×400 为右侧抽屉；请求未返回时的包围盒与加载完成后相同且标题与关闭按钮可见；身份校验失败时原因文案在抽屉内；拖头部 100px 后包围盒不变；主体滚到底后标题与关闭按钮仍在抽屉可视框内；390×664 → 750×342 后同一河段、同一起报时次的窗变为右侧抽屉；390×664 → 1280×900 后为可拖拽桌面窗，包围盒等于在 1280×900 新打开的河段窗的包围盒，且控制条可见。vitest 断言移动形态不挂拖拽监听；既有桌面拖拽单测不改通过。

  **Suggested fixture level:** expanded - 改两个曲线窗共用的窗口容器，并改变既有 `map-feature-popups` 契约的适用范围。

  **Minimal mergeable slice:** atomic - 依赖 3.3 是因为三个移动 project 下的触摸开窗首次出现在本 task，而钩子定位点在地图区中央，需要浮层已收纳。容器只有一个，河段窗与气象代站窗同时受影响；竖屏与横屏两种锚定是同一组件同一条 CSS 分支上的两个取值，只做其一会让另一档视口的窗落到无定义位置。

  Triage（#2803）：Issue type: feature ｜ Fixture level: expanded（与上游建议一致；设计见 design.md D9，规格见 `specs/mobile-curve-sheet` 的「Curve windows render as sheets in mobile form」、`specs/map-feature-popups`（MODIFIED 三条）与 `specs/mobile-regression-evidence` 的「Station window opens in a mobile project」）｜ Blast radius: 河段窗与气象代站窗共用的唯一窗口容器 `M11DraggableCurveWindow`——移动分支写错会让手机上看不到曲线（窗落到视口外或保持隐藏），桌面分支被牵动则破坏既有的拖拽、默认摆位与 16:9。
  - Change surface：`apps/frontend/src/components/map/M11DraggableCurveWindow.tsx`（唯一产品文件）；新移动 spec `e2e/m11-curve-sheet.mobile.mocked.spec.ts`、新桌面 spec `e2e/m11-curve-window-desktop.mocked.spec.ts`、助手 `e2e/support/curveSheet.mocked.ts`（需要的 mock 变体三个，都用在 `installRiverWindowMocks` 之后注册的更窄 `page.route` 覆盖实现：河段预报请求挂起；气象代站序列身份校验失败；latest-product 的 `available_issue_times` 多列一个更早的起报时次——只用于断言“旋转后选中值不变”，不要求该时次的曲线能加载，选中后面板进入什么内容状态不作断言；复用 `riverWindow.mocked.ts` / `openRiverWindow.ts` / `openStationWindow.ts` / `viewportForm.ts`，不改它们的既有导出行为——若必须给既有助手加可选参数，缺省行为不变并写进偏离记录）；新 vitest `src/components/map/__tests__/M11DraggableCurveWindowMobileForm.test.tsx`。不改 `M11RiverForecastPanel.tsx`、`M11StationForcingPopup.tsx`、`M11PopupChrome.tsx`、`OverviewPage.tsx`、`RegionErrorBoundary.tsx`。
  - Governing invariant：移动形态下曲线窗的包围盒只由形态与地图区尺寸决定——与内容状态、拖拽输入、历史拖拽坐标无关；桌面形态下窗的 DOM 盒、默认摆位、拖拽与 clamp 行为与改动前相同。
  - 实现口径（裁定）：
    - (1) 形态信号用 `useMobileForm()` 的 `{ mobile, landscape }`，窗的类串按三态在 JS 里整串选择：桌面 = 现有类串逐字不变；底部抽屉（`mobile && !landscape`）= `absolute inset-x-0 bottom-0 h-[min(60dvh,calc(100%-8px))]` + 只在上方的圆角；右侧抽屉（`mobile && landscape`）= `absolute inset-y-0 right-0 w-[min(50vw,28rem)]`。两个抽屉类串都保留 `flex flex-col overflow-hidden` 与 `M11_POPUP_GLASS`（主体容器的 `flex-1` 依赖它），都不含 `aspect-video`、不含 `md:` 宽度、不含 `max-h`。不用 `mobile:` / `mobile-landscape:` 变体叠在桌面类串上——`md:`（宽 ≥ 768）在 844×390 同时命中，层叠次序不可靠。`100%` 以窗的包含块（地图区）为准：实现者须确认窗的 offsetParent 就是 `m11-fullscreen-map`（曲线区域的 `RegionErrorBoundary` 无错时不产生包裹元素），不是则停下报告。
    - (2) 移动形态不写 `left` / `top` / `visibility` 内联样式（位置完全由类决定，首帧即可见）；`zIndex`（活动 142 / 非活动 132）两种形态都保留——它已高于控制条（115）与启动器列（120）。
    - (3) 移动形态不挂拖拽：抓手容器不绑 `onPointerDown`、不带 `cursor-grab` / `cursor-grabbing` / `touch-none`（`select-none` 可留）；`startDrag` 不可达；`resize` 监听在移动形态不注册或为空操作。`onPointerDownCapture` / `onFocusCapture` / `onClickCapture` 的 `onActivate` 两种形态都保留。
    - (4) 进入移动形态时清掉 `positionRef` 与 `position`、终止进行中的拖拽（移除 window 监听、`dragging` 归零）；回到桌面形态时由一个依赖形态的 layout effect 重新计算**默认**位置（不恢复进入移动形态前的拖拽坐标）。
    - (5) 头部不随主体滚动：`children` 外包一层主体容器 `data-testid="${testId}-body"`，两种形态都渲染（形态切换不重挂子树——图表实例与面板内部 state 不丢）；桌面类为 `contents`（不产生布局盒，桌面几何不变），移动类为 `flex min-h-0 flex-1 flex-col overflow-y-auto`。头部（抓手容器）留在主体容器之外。
    - (6) 不新增 prop；两个面板的调用点零改动。
  - Sibling surfaces：(1) 两个消费者 `M11RiverForecastPanel`（头部内联关闭按钮、可选的起报时次条、图表 / 加载 / 空态）与 `M11StationForcingPopup`（`M11PopupHeader`、要素选择器、图表 / `M11PopupLoading` / `M11PopupEmpty`）——四种内容状态都经同一容器；(2) `OverviewPage` 曲线区域的 `RegionErrorBoundary`（兜底块 `absolute left-1/2 top-24`，不改；曲线兜底的移动几何归 4.6）与 `RegionCrashProbe region="curve"`；(3) 4.1 的单窗 layout effect：移动形态下至多一个窗，本 task 不需要处理双抽屉；(4) 控制条、启动器列与展开面板：抽屉盖在其上（层级更高），本 task 不隐藏它们（4.6）；(5) `src/__tests__/riverClickPhase2Closure.test.ts`、`riverClickFakePage.test.ts` 引用窗的 testid——testid 与 `data-m11-curve-window-*` 属性不变；(6) 既有桌面窗 spec `m11-river-open.mocked.spec.ts`、`m11-station-open.mocked.spec.ts`。
  - Must preserve：桌面形态（含 768×1024 与 800 宽）窗的类串逐字不变、默认摆位公式与 `DESKTOP_PLACEMENT_WIDTH` 阈值不变、拖拽 / clamp / pointer capture / 忽略交互控件的规则不变、未定位前 `visibility: hidden` 不变；`data-testid`、`${testId}-drag-handle`、`data-m11-curve-window-kind`、`data-m11-curve-window-active` 不变；全部既有 vitest（含 `M11RiverForecastPanel.test.tsx`、`M11StationForcingPopup.test.tsx` 里的拖拽用例）与全部既有 e2e（含 `m11-overlay-collision.mocked.spec.ts` 的 1920 / 1440 / 1280 / 800、`m11-river-open`、`m11-station-open`、`m11-region-fallbacks.mobile`）不改期望值通过。
  - Risk pack「Public API / entry」selected：两个曲线窗的容器契约 -> 新移动 spec（三个移动 project 都跑、无按 project 跳过；需要特定视口的用例在用例内 `setViewportSize`；河段窗与气象代站窗各自断言，除非下文点名只测一种）：
    - (a) 390×664：河段窗、气象代站窗的左 / 右 / 底边与 `m11-fullscreen-map` 重合（±0.5px），高度 = `min(0.6 × 视口高, 地图区高 − 8)`（±0.5px）；
    - (b) 750×342 与 844×390：两种窗的上 / 下 / 右边与地图区重合，宽度 = `min(0.5 × 视口宽, 448)`；
    - (c) 320×480：河段窗为底部抽屉（同 (a) 的三边 + 高度公式）；600×400：河段窗为右侧抽屉（同 (b)）；
    - (d) 层级：抽屉头部中心点与抽屉内靠近底边 / 右边的一个点 `elementFromPoint` 都落在抽屉内（控制条与启动器列在其下）；
    - (e) 三个移动 project 下窗都由真实轻触打开（`openRiverWindow` / `openStationWindow` 的 `input` 为 `tap`），且打开后窗内图表 canvas 可见（规格「Station window opens in a mobile project」的 THEN 是“带已加载的曲线可见”；河段窗同样断言）。
  - Risk pack「Resource limits / 溢出」selected：尺寸与内容状态无关 -> 同一移动 spec：
    - (f) 390×664 河段预报请求挂起时（mock 不应答）抽屉包围盒与 (a) 的公式值相同，标题与关闭按钮可见且在抽屉盒内；放行请求、曲线加载完成后包围盒逐值不变；
    - (g) 390×664 气象代站序列身份校验失败时，不可用原因文案的包围盒在抽屉盒内，抽屉包围盒等于公式值；
    - (h) 750×342 气象代站窗：把 `${testId}-body` 的 `scrollTop` 设到 `scrollHeight` 后，标题与关闭按钮仍在抽屉可视框内；另加结构断言——抓手容器不是主体容器的后代，主体容器计算 `overflow-y` 为 `auto`。PR 里报告该状态下主体 `scrollHeight` 与 `clientHeight` 的实测值；若两者相等（本 task 的内容还不会溢出——图表下限归 4.4 / 4.5），如实写进限制，不为制造溢出改产品代码。
  - Risk pack「Concurrency / 状态迁移」selected：拖拽与形态切换 -> 同一移动 spec：
    - (i) 390×664：在头部按下、**纵向向上**移动 100px、抬起（鼠标指针与 CDP 触摸各一次）后包围盒不变（必须纵向：改动前 390 宽时窗的水平 clamp 区间只有一个点，水平拖动本来就不动，测不出东西）；
    - (j) 390×664 → 750×342：同一河段（面板标题文本不变）、同一起报时次（用多起报时次的 mock 变体，先在竖屏把起报时次切到非默认项，旋转后选择器的选中值不变）的窗变为右侧抽屉（(b) 的三边 + 宽度断言）；
    - (k) 390×664 → 1280×900：窗的包围盒等于“在 1280×900 新开页面、新打开的河段窗”的包围盒（同一用例内用第二个页面量出，不写死数值），抓手容器计算 `cursor` 为 `grab`，拖头部 100px 后窗移动 100px（±1px），`m11-bottom-control-bar` 可见；
    - (l) 承接 (k) 的反向：1280×900 把窗拖离默认位置 → 390×664（成为底部抽屉，(a) 的断言）→ 1280×900：包围盒回到默认位置而不是拖拽后的位置。
    - vitest（`installMobileFormMatchMedia`）：(m) 移动形态渲染后在抓手上派发 `pointerdown`，`window.addEventListener` 没有被以 `pointermove` / `pointerup` / `pointercancel` 调用，窗无 `left` / `top` / `visibility` 内联样式，抓手类不含 `cursor-grab` 与 `touch-none`；(n) 桌面形态下同样操作会注册这三个监听（对照，防止 (m) 恒真）；(o) 桌面拖拽进行中切到移动形态：三个监听被移除；(p) 两种形态下主体容器都存在且子节点不重挂（同一 DOM 节点身份）；(q) 移动形态下 `pointerdown` 仍触发 `onActivate`。
  - Risk pack「Legacy compatibility」selected：桌面不变量 -> 新桌面 spec（桌面 project）：1280×900 下河段窗、气象代站窗的包围盒，以及窗内图表容器的包围盒，等于在 `origin/master` 上实测的字面值（实现者先在未改动的 `src/` 上量出并写进 spec——这条用例改动前后都绿，是“加了主体容器后桌面几何不变”的钉子，须在 PR 里点名）；1280×900 与 768×1024 下窗的计算 `aspect-ratio` 为 `16 / 9`、主体容器计算 `display` 为 `contents`。字面包围盒只钉 1280×900（宽 ≥ 1143px，4.3 不改它）；768×1024 的包围盒不写字面值——4.3 会把该视口的默认宽度从 42vw 改为 30rem，那一行归 4.3 的 spec。PR 描述贴出 `git diff --stat --diff-filter=MDR origin/master -- apps/frontend/e2e 'apps/frontend/src/**/__tests__/**'`（期望为空；给既有助手加可选参数时该文件会出现在这里，须逐个说明）。
  - Risk pack「Error handling」selected：非正常内容状态仍在抽屉内 -> (f)(g)；曲线区域崩溃兜底不在本 task（Non-goals）。
  - 未选：Auth、File IO、Schema、Config、Documentation（design.md D9 与规格已写明；`docs/spec/06B` §8 已在 1.2 落地）。
  - Seams under test：浏览器里窗的包围盒 / 层级 / 可拖性随视口的变化；vitest 里拖拽监听的注册与形态切换时的清理。
  - Non-goals：图表在抽屉内的最小高度与重排（4.4 / 4.5——本 task 之后图表可能偏矮，不作断言）；抽屉打开时隐藏控制条 / 启动器、收起面板、暂停播放（4.6）；自动平移（4.10）；抽屉内控件的 44px / 16px 下限（4.9）；桌面最小宽度 30rem（4.3）；安全区内边距（外壳已处理，真机确认归 7.x）；曲线区域错误兜底的移动几何（4.6）；抽屉的进出场动画与下拉关闭手势。
  - Review focus：(1) 桌面类串逐字不变、桌面分支的每个 JS 路径与改动前等价；(2) 移动形态没有任何内联定位、没有可达的拖拽路径；(3) 形态切换时拖拽状态与位置被清干净，回桌面取默认位置；(4) 主体容器两种形态都渲染、子树不重挂；(5) 高度 / 宽度公式与规格逐字一致，`100%` 的包含块是地图区；(6) 三个移动 project 都断言、无跳过；(7) 桌面 spec 的字面值确实量自 `origin/master`。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；新 spec 与助手的 strict `tsc --noEmit` exit 0；新移动 spec 与新 vitest 先红后绿——红：对 `origin/master` 的 `src/`，(a)(b)(c)(f)(g)(i)(j)(l) 与 (m)(o)(p) 红；(e) 的轻触开窗、(k) 的部分断言、(n)(q) 与桌面 spec 可能改动前已绿，须逐条点名实际结果；三个移动 project 各非零 passed，`--repeat-each=5` 无 flaky；变异：给底部抽屉类串加回 `aspect-video` 时 (a) 变红，移动形态保留内联 `left` / `top` 时 (a)(b) 变红，移动形态保留 `onPointerDown` 时仅 (m) 变红（没有内联定位，包围盒仍不变），同时保留 `onPointerDown` 与内联定位时 (i) 变红，回桌面不重算位置时 (k) 或 (l) 变红。改展示端运行时代码，合并前出 node-27 live receipt（PR 的生产构建对 live API）：桌面布局 oracle exit 0；390×664 与 750×342 下经门控钩子打开河段窗、气象代站窗，包围盒满足 (a)(b) 的公式；1280×900 下两窗为可拖拽桌面窗且并存。

- [x] 4.3 桌面形态曲线窗最小宽度：桌面形态曲线窗默认宽度由 `min(44rem, 42vw)` 改为 `min(44rem, max(42vw, 30rem))`，保持 16:9 与既有的地图区内 clamp；组件里用于定位计算的尺寸回退公式须同步为同一宽度规则（只同步宽度表达式；该回退里区分“桌面摆位”的既有宽度阈值不动）。新增 `e2e/m11-curve-window-min-size.mocked.spec.ts`（桌面 project）。

  Depends on: 1.3

  Verify：768×1024 下河段窗 480×270 且在地图区内；1280×900 下窗宽等于视口宽的 42%；既有桌面拖拽单测与碰撞 spec 四个宽度不改通过。

  **Suggested fixture level:** compact - 一个宽度表达式；只影响 768–1142px 宽度下的默认尺寸。

  **Minimal mergeable slice:** atomic - 一条宽度规则的两处表达（类名与定位用的尺寸回退），必须一致。

  Triage（#2804）：Issue type: feature ｜ Fixture level: compact（与上游建议一致；一条宽度规则的两处表达——单组件、无状态 / 接口 / 共享入口变化，不触发 expanded；设计见 design.md D16，规格见 `specs/map-feature-popups`（ADDED）的「Desktop-form curve windows have a minimum width」）｜ Blast radius: 桌面形态下河段窗与气象代站窗的默认尺寸——只在视口宽 768–1142px 变化（768 宽时由约 322×181 变为 480×270）；两处表达不一致在浏览器里看不出来（clamp 用实测盒），只在窗的包围盒为 0 的路径（jsdom、首帧布局前）上让默认摆位按旧宽度算。
  - Change surface：`apps/frontend/src/components/map/M11DraggableCurveWindow.tsx`——(1) `DESKTOP_WINDOW_CLASS` 里的 `md:w-[min(44rem,42vw)]` 改为 `md:w-[min(44rem,max(42vw,30rem))]`，该类串其余部分逐字不变；(2) `curveWindowSize` 的尺寸回退里桌面一支的宽度由 `min(704, 宽 × 0.42)` 改为 `min(704, max(宽 × 0.42, 480))`（新增一个具名常量表示 30rem = 480px）。回退里区分“桌面摆位”的 `DESKTOP_PLACEMENT_WIDTH`（900）阈值、`defaultPosition`、`clampPosition`、高度计算（`宽 × 9/16` 再按地图区高封顶）、两个抽屉类串与全部移动分支都不动。新 spec `e2e/m11-curve-window-min-size.mocked.spec.ts`（桌面 project）；既有 vitest `src/components/map/__tests__/M11RiverForecastPanel.test.tsx` 的两处期望值（同一用例的 x 与 y）按下条裁定更新。
  - 既有单测的裁定（与 issue 验收措辞的偏离，已核对代码）：issue 写“既有桌面拖拽单测不改通过”，但 `M11RiverForecastPanel.test.tsx` 的用例「drags only from the header and clamps the river window inside the viewport」在 1024×768 下把旧宽度公式写进了期望值（`Math.min(704, 1024 * 0.42)`，两处）。jsdom 里窗的包围盒为 0，走的正是本 task 要求同步的尺寸回退；1024 落在被 D16 改变的 768–1142 区间内（回退宽由 430.08 变为 480），该期望必然变红。裁定：只把这两处期望里的宽度表达式改为新规则 `Math.min(704, Math.max(1024 * 0.42, 480))`，用例的其余断言、步骤与视口不动——这是把测试里抄写的旧规则同步到用户拍板的新规则，不是放宽断言。除此之外不改任何既有测试；实现者若发现还有别的既有用例因同一原因变红，停下报告而不是自行修改。
  - Must preserve：视口宽 ≥ 1143px 时默认尺寸与改动前相同（`e2e/m11-curve-window-desktop.mocked.spec.ts` 的 1280×900 字面包围盒不改通过）；16:9；窗不超出地图区（既有 `max-h-[calc(100%-1.5rem)]` 与 clamp）；拖拽行为；移动形态两种抽屉的几何（`e2e/m11-curve-sheet.mobile.mocked.spec.ts` 不改通过）；`m11-overlay-collision.mocked.spec.ts` 的 1920 / 1440 / 1280 / 800；除上条点名的两处期望外全部既有 vitest 不改通过。
  - Risk pack「Public API / entry」selected：桌面默认尺寸是用户可见契约 -> 新桌面 spec：(a) 768×1024 河段窗、气象代站窗宽 480、高 270（±0.5px），包围盒完全在 `m11-fullscreen-map` 内，且两种窗的图表 canvas 可见（改动前该视口 canvas 高度为 0）；(b) 1280×900 河段窗宽 = 0.42 × 1280 = 537.6（±0.5px）；(c) 1024×768 河段窗宽 480、高 270（30rem 一支，区间内部的点）；(d) 1143×900 河段窗宽 = 0.42 × 1143（±0.5px，≥ 480——边界上两支相接）；(e) 1680×600 河段窗宽 704、高 396（44rem 封顶一支不变；原定 1920×1080，CI runner 上该画布在助手的 5s 定位预算内等不到地图空闲，改用同样选中封顶一支的小画布）。
  - Risk pack「Legacy compatibility」selected：两处表达一致 + 既有摆位不漂 -> (f) 新 spec：768×1024 把河段窗拖到右下角（超出边界的拖拽），窗右边 = 地图区右边 − 12、下边 = 地图区下边 − 12（±1px）——浏览器里 clamp 用的是实测盒，这条是“新尺寸下 clamp 仍贴边”的保持性钉子，改动前后都绿；(g) 上条裁定更新后的既有 vitest 用例证明 jsdom 回退路径用新宽度（红：只改类串不改回退时，该用例在更新后的期望下为红；只改回退不改期望时也为红）；(h) 新增一条 vitest（放在新文件 `src/components/map/__tests__/M11DraggableCurveWindowMinWidth.test.tsx`）：地图容器宽 1280 时回退宽为 537.6（默认摆位 x 与改动前相同——`≥ 1143` 不变量在回退路径上的钉子），容器宽 1000 时回退宽为 480，容器宽 1920 时为 704；经由默认摆位的 `left` 观测，不导出内部函数：用 `window.innerWidth` 驱动宽度（父容器与窗的 rect 在 jsdom 里都是 0，才会走回退），**不得**把窗自身的 `getBoundingClientRect` mock 成非零（那会绕过回退、断言空转）；期望的河段窗 `left`：1280 -> 89.6，1000 -> 40（改动前 70），1920 -> 185.6。PR 描述贴出 `git diff --stat --diff-filter=MDR origin/master -- apps/frontend/e2e 'apps/frontend/src/**/__tests__/**'`（期望只有 `M11RiverForecastPanel.test.tsx` 一个文件、两行期望值）。
  - 未选：Concurrency（无状态变化）、Error handling、Auth、File IO、Schema、Config、Resource limits（窗仍受 `max-h` 与 clamp 约束；768 宽时 480 + 两侧 12px 边距在 768 宽的地图区内有余量，由 (a)(f) 覆盖）、Documentation（design.md D16 与规格已写明；`docs/spec/06B` 不含曲线窗宽度数值）。
  - Non-goals：移动形态；拖拽与 clamp 规则本身；`DESKTOP_PLACEMENT_WIDTH` 阈值（900）——768–899 宽时默认摆位仍走“居中 ± 18px”那一支，回退宽仍取“地图区宽 − 24”，不在本 task（issue 明文：回退里区分桌面摆位的既有宽度阈值不动）；768×1024 下窗内图表的可读性之外的任何图表改动（图表随窗变大自然获得高度，不作数值断言，只断言图表 canvas 可见）。
  - Review focus：(1) 类串与回退是同一条规则（`max(42vw, 30rem)` 封顶 44rem），常量具名；(2) 桌面类串除宽度一段外逐字不变，抽屉类串与移动分支零 diff；(3) 既有测试只动了裁定点名的两处期望；(4) `≥ 1143` 不变量有浏览器与 jsdom 两侧的钉子。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；新 spec 的 strict `tsc --noEmit` exit 0；先红后绿——红：对 `origin/master` 的 `src/`，(a)(c) 红（窗宽 322.56 / 430.08），(h) 的“宽 1000 -> left 40”红；(b)(d)(e)(f) 与 (h) 的 1280 / 1920 改动前已绿，须点名；`--repeat-each=5` 无 flaky；变异：只改类串不改回退 -> (g)(h) 红、e2e 全绿；只改回退不改类串 -> (a)(c) 红。改展示端运行时代码，合并前出 node-27 live receipt（PR 的生产构建对 live API）：桌面布局 oracle exit 0；768×1024 下经门控钩子打开河段窗，包围盒 480×270 且在地图区内、图表 canvas 可见；1280×900 下窗宽 537.6。

- [x] 4.4 河段抽屉图表可读高度：河段面板在抽屉内图表区占满头部与选择器之后的剩余高度，下限非矮视口横屏 160px、矮视口横屏 120px；不足时抽屉主体纵向滚动；图表画布与图表区同尺寸并随抽屉尺寸变化重排。新增 `e2e/m11-river-sheet-chart.mobile.mocked.spec.ts`。

  Depends on: 4.2

  Verify：已加载状态下，390×664 图表区 ≥ 160px、在抽屉内、内含同尺寸画布；750×342 图表区 ≥ 120px；390×664 → 750×342 后画布尺寸等于新图表区尺寸。

  **Suggested fixture level:** compact - 一个面板内部的布局约束。

  **Minimal mergeable slice:** atomic - 这是原“图表可读高度”的首刀（河段面板自成一个文件）；气象代站面板已切为 4.5。

  Triage（#2805）：Issue type: feature ｜ Fixture level: compact（与上游建议一致；一个面板内部的布局约束——单组件、无状态 / 接口 / 共享入口变化，不触发 expanded；设计见 design.md D12 前两条，规格见 `specs/mobile-curve-sheet` 的「The curve chart keeps a minimum height inside a sheet」中的河段场景）｜ Blast radius: 河段曲线窗的图表区——下限写错会让矮抽屉里的曲线被压成细带（本 change 的起因之一），或让桌面窗里的图表区被撑出 16:9 的窗。
  - Change surface：`apps/frontend/src/components/map/M11RiverForecastPanel.tsx`（已加载分支的容器与图表区的类）；图表区上允许加让“图表内容不参与图表区固有高度”的布局类（如 `relative` + 内层绝对定位，或等效写法——见布局口径 (5)）。仅当重排或防棘轮在面板一侧做不到时，才可改 `src/components/charts/ForecastChart.tsx`，且只限 `fill` 模式的包裹样式或尺寸变化时的重排，不改配置、不改非 `fill` 调用方的外观；改了要在偏离记录里写明原因与实测。新 spec `e2e/m11-river-sheet-chart.mobile.mocked.spec.ts`，需要的助手加进新文件 `e2e/support/sheetChart.mocked.ts`（4.5 的气象代站 spec 会复用其中与面板无关的量具）。不改 `M11DraggableCurveWindow.tsx`、`M11StationForcingPopup.tsx`（4.5）、`OverviewPage.tsx`。
  - 布局口径（裁定）：先量再改。4.2 实测抽屉主体（`m11-river-forecast-panel-body`）在 390×664 高 317px、750×342 高 213px、320×480 高 191px——前两档里图表区多半已高于下限，下限真正咬住的是更矮的抽屉。要求：(1) 图表区（`m11-river-panel-chart`）在移动形态带最小高度：非矮视口横屏 160px、矮视口横屏 120px，用 `mobile:` / `mobile-landscape:` 变体加在图表区上（这两个变体互不依赖 `md:`，可以直接叠；`mobile-landscape:` 的值须确实盖过 `mobile:` 的值，以计算样式断言为准）；(2) 剩余高度足够时图表区仍占满剩余高度（保留 `flex-1`）；(3) 剩余高度不足时由**抽屉主体**（4.2 的 `-body` 容器，`overflow-y-auto`）滚动，且已加载分支的容器随内容长高——可观测口径：图表区的包围盒始终完整落在它的父容器（已加载容器）盒内，主体滚到底时图表区底边距主体可视底边不小于容器的下内边距（8px）；不新增第二个滚动容器；(4) 新增的移动专属取值全部带 `mobile:` 或 `mobile-landscape:` 前缀，桌面形态下图表区的计算 `min-height` 与图表容器、河段窗的包围盒不变（为 (5) 加的无前缀定位类不算违反，只要这两点成立）；(5) 防“高度棘轮”：已加载容器改为按内容定最小高之后，图表库上一次写进自身根节点的像素高度不得反过来把图表区撑住——从高抽屉转到矮抽屉后，图表区高度必须等于在矮抽屉里新打开时的高度。起报时次条、图例行、“滚轮缩放时间轴”提示（4.7 改文案）、部分失败提示的内容与顺序不动。
  - Must preserve：桌面形态河段窗内图表容器的包围盒不变（`e2e/m11-curve-window-desktop.mocked.spec.ts` 的 1280×900 字面值、`e2e/m11-curve-window-min-size.mocked.spec.ts` 的 768×1024 canvas 可见，均不改通过）；抽屉本身的包围盒与“尺寸与内容状态无关”（`e2e/m11-curve-sheet.mobile.mocked.spec.ts` 全部用例不改通过，含 (f) 加载中 / 加载后同盒与 (h) 头部不随主体滚动）；加载中、等待、空态三个分支的 DOM 与类不动；全部既有 vitest（含 `M11RiverForecastPanel.test.tsx`）与 `m11-overlay-collision.mocked.spec.ts` 的 1920 / 1440 / 1280 / 800 不改期望值通过。
  - Risk pack「Resource limits / 溢出」selected：图表下限与滚动兜底 -> 新移动 spec（三个移动 project 都跑、无按 project 跳过；每条用例内 `setViewportSize`；都在“曲线已加载”（图表 canvas 可见）之后量）：
    - (a) 390×664：图表区高 ≥ 160，包围盒在抽屉盒内，区内 canvas 的宽高与图表区相等（±1px）；
    - (b) 750×342 与 844×390：图表区高 ≥ 120，在抽屉盒内，canvas 与图表区同尺寸；
    - (c) 下限咬住的竖向矮视口 320×480：图表区高 = 160（±0.5px，硬前提：主体 `scrollHeight > clientHeight`——若实测不溢出，换成实现者量出的、确实溢出的移动非横屏视口并写进偏离记录）；图表区盒完整在其父容器盒内；把主体滚到底后图表区完整落在抽屉的可视框内（图表区盒在主体可视盒内），图表区底边距主体可视底边 ≥ 8px（±0.5px），标题与关闭按钮仍可见；
    - (d) 下限咬住的矮视口横屏 568×320：图表区高 = 120（±0.5px，同样的溢出硬前提；余量可能只有几像素——若实测不溢出，优先收窄宽度（如 480×320，抽屉更窄、头部折行更高）而不是再压高度，并写进偏离记录），图表区盒在父容器盒内、滚动后完整落在可视框内且底边留 ≥ 8px；PR 里报告 (c)(d) 所用视口在改动前的图表区实测高度；
    - (e) 剩余高度足够时仍占满：390×664 下图表区高 > 160，且主体不溢出（`scrollHeight === clientHeight`）——防止把图表区钉死在下限；
    - (f) 形态归属：图表区计算 `min-height` 在 390×664 为 `160px`、750×342 为 `120px`、1280×900（用例内 `setViewportSize`）为 `0px`（或改动前的原值——以 `origin/master` 实测为准）。
  - Risk pack「Concurrency / 尺寸变化」selected：重排 -> 同一 spec：(g) 390×664 → 750×342 后（同一河段窗、不重开）canvas 的宽高等于新的图表区宽高（±1px），与旋转前的 canvas 尺寸不同，图表区仍在抽屉盒内，且图表区高度等于“在 750×342 新开页面、新打开的河段窗”的图表区高度（±1px，同一用例内用第二个页面量出——防棘轮）；(h) 750×342 → 390×664 反向同样成立（对照值取 390×664 新开窗）。
  - Risk pack「Legacy compatibility」selected -> Must preserve；PR 描述贴出 `git diff --stat --diff-filter=MDR origin/master -- apps/frontend/e2e 'apps/frontend/src/**/__tests__/**'`（期望为空）与 `git diff --stat origin/master -- apps/frontend/src`（期望只有 `M11RiverForecastPanel.tsx`；出现 `ForecastChart.tsx` 时须有对应的偏离说明）。
  - 未选：Public API（无接口 / 契约面变化，容器契约在 4.2）、Error handling（空态 / 加载分支不动）、Auth、File IO、Schema、Config、Documentation（design.md D12 与规格已写明）。
  - Non-goals：气象代站面板（4.5）；触屏缩放与提示文案（4.7）；抽屉内控件的 44px / 16px 下限（4.9）；加载中 / 等待 / 空态分支在矮抽屉里的排版；图表配置（坐标轴、图例、tooltip）在窄图里的可读性调优；桌面形态的任何变化。
  - Review focus：(1) 新类全部带移动前缀，桌面计算样式不变；(2) 溢出交给抽屉主体滚动，没有新增第二个滚动容器；(3) 下限咬住的两条用例确实处在溢出状态（硬前提），不是空转；(4) (e) 防钉死；(5) 重排用例比较的是旋转前后不同的尺寸；(6) 三个移动 project 都断言、无跳过。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；新 spec 与助手的 strict `tsc --noEmit` exit 0；先红后绿——红：对 `origin/master` 的 `src/`，(c)(d)（图表区被压到下限以下、主体不溢出）与 (f) 红；(a)(b)(e)(g)(h) 可能改动前已绿，须逐条点名实际结果并报告各视口下图表区与主体的实测高度；`--repeat-each=5` 无 flaky；变异：去掉 `mobile:` 下限 -> (c)(f) 红；去掉 `mobile-landscape:` 下限 -> (d)(f) 红；已加载容器不随内容长高（图表区溢出其父容器）-> (c)(d) 的“图表区在父容器盒内 / 底边留 8px”红；去掉防棘轮的处理 -> (g) 的“等于新开窗的图表区高度”红（若实现者证明现状下不存在棘轮、无需处理，则如实报告并说明 (g) 该断言靠什么保持为绿）。改展示端运行时代码，合并前出 node-27 live receipt（PR 的生产构建对 live API）：桌面布局 oracle exit 0；390×664 与 750×342 下经门控钩子打开河段窗，曲线加载后图表区高 ≥ 160 / ≥ 120、canvas 与图表区同尺寸；320×480 下图表区高 160 且可滚入可视框（live 上该河段若无曲线数据则如实写进限制）；1280×900 下图表容器盒与改动前相同。

- [x] 4.5 气象代站抽屉图表可读高度：气象代站面板同 4.4 的下限与滚动兜底；矮视口横屏下图表区可被完整滚入抽屉可视框。新增 `e2e/m11-station-sheet-chart.mobile.mocked.spec.ts`。

  Depends on: 4.2

  Verify：已加载状态下，390×664 图表区 ≥ 160px、在抽屉内、内含同尺寸画布；750×342 图表区 ≥ 120px，且滚动抽屉主体后整个图表区位于抽屉可视框内。

  **Suggested fixture level:** compact - 一个面板内部的布局约束。

  **Minimal mergeable slice:** atomic - 一个面板文件。

  Triage（#2806）：Issue type: feature ｜ Fixture level: compact（与上游建议一致；一个面板内部的布局约束，做法沿用已合并的 4.4。对上游切片的一处 override：issue 写“atomic - 一个面板文件”，本块另外改 `ForecastChart.tsx` 并在 `components/charts/` 新增一个内部共享 hook——理由是气象代站图表有 4.4 查明的同一个重排竞态，把那段对生命周期敏感的逻辑复制第二份比抽出来更危险；该 hook 是内部实现、非公开入口，无状态 / 接口变化，4.4 也是在 compact 下改的 `ForecastChart.tsx`，不触发 expanded；设计见 design.md D12 前两条，规格见 `specs/mobile-curve-sheet` 的「The curve chart keeps a minimum height inside a sheet」中的气象代站场景）｜ Blast radius: 气象代站曲线窗的图表区——下限写错会让矮抽屉里的曲线被压成细带（4.2 之前的浮窗形态下，390×664 与 844×390 的该图表 canvas 高度为 0——见 PR #2838 的红跑记录；4.2 的抽屉固定高度后已可见，当前各视口的实际高度由实现者先量），或让桌面窗里的图表卡片被撑出 16:9 的窗。
  - Change surface：`apps/frontend/src/components/map/M11StationForcingPopup.tsx`（已加载分支 `m11-station-popup-loaded`、图表卡片 `m11-station-variable-<要素>-chart` 与其内的图表区的类；图表组件接上尺寸重排）；`apps/frontend/src/components/charts/` 下新增一个小 hook 承载 4.4 在 `ForecastChart.tsx` 里写的“观察容器尺寸、与实例不一致时 resize”逻辑，`ForecastChart.tsx` 改为调用该 hook（行为零变化——既有 `src/components/charts/__tests__/ForecastChartFillResize.test.tsx` 不改通过即为证据），气象代站图表同样调用它。新 spec `e2e/m11-station-sheet-chart.mobile.mocked.spec.ts`，复用 `e2e/support/sheetChart.mocked.ts` 的量具（它已按窗种类 / 图表 testid 参数化；新 spec 显式传 `chartTestId: 'm11-station-panel-chart'`；确需扩展时只加可选参数、缺省行为不变，并写进偏离记录）。`e2e/support/curveSheet.mocked.ts` 里 `CURVE_WINDOWS.station.chart`（指向卡片）不得改——桌面字面值 spec 量的就是卡片盒。不改 `M11DraggableCurveWindow.tsx`、`M11RiverForecastPanel.tsx`、`OverviewPage.tsx`。
  - “图表区”的定义（裁定）：规格说的气象代站图表区 = 图表卡片里直接包住图表的那一层（现为无 testid 的 `min-h-0 flex-1` div），给它加 `data-testid="m11-station-panel-chart"`（与河段的 `m11-river-panel-chart` 对称；这是为可测性新增的属性，两种形态都带）。卡片（`m11-station-variable-<要素>-chart`，含要素标题与徽标行）不是图表区；它的 testid 不变。
  - 布局口径（裁定，与 4.4 同构，先量再改）：(1) 图表区在移动形态带最小高度：非矮视口横屏 160px、矮视口横屏 120px（`mobile:` / `mobile-landscape:` 变体，以计算样式为准）；(2) 剩余高度足够时仍占满剩余高度；(3) 不足时由抽屉主体（`m11-station-popup-body`）滚动，已加载容器与图表卡片都随内容长高——可观测口径：图表区盒在卡片盒内、卡片盒在已加载容器盒内；不新增第二个滚动容器（量具返回的 `parent` 是图表区的父元素，对气象代站是卡片；“卡片在已加载容器内”这一级由新 spec 自己量 `m11-station-popup-loaded` 的盒）；(4) 图表内容不参与图表区的固有高度（4.4 实测：否则画布的整数高度会把图表区撑大、主体多出 1px 假溢出）；(5) 新增的移动专属取值全部带移动前缀，桌面形态下图表区的计算 `min-height`、图表卡片与气象代站窗的包围盒不变；(6) 图表画布跟随图表区尺寸：气象代站图表直接用图表封装库且 `height: 100%`，有 4.4 查明的同一个竞态（封装库吞掉绑定后的第一次尺寸回调，其后约 60ms 内的容器变化被合并，画布停在旧尺寸）——用上面的共享 hook 处理。要素选择条、起报时次、图例行、徽标、部分失败提示的内容与顺序不动。
  - Must preserve：桌面形态气象代站窗内图表卡片的包围盒不变（`e2e/m11-curve-window-desktop.mocked.spec.ts` 的 1280×900 字面值、`e2e/m11-curve-window-min-size.mocked.spec.ts` 的 768×1024 canvas 可见，均不改通过）；抽屉的包围盒与“尺寸与内容状态无关”（`e2e/m11-curve-sheet.mobile.mocked.spec.ts` 全部用例不改通过，含气象代站的 (e)(g)(h)）；河段图表的全部行为（`e2e/m11-river-sheet-chart.mobile.mocked.spec.ts` 与 `ForecastChartFillResize.test.tsx` 不改通过）；加载中 / 空态 / 无产品分支的 DOM 与类不动；全部既有 vitest（含 `M11StationForcingPopup.test.tsx`）与 `m11-overlay-collision.mocked.spec.ts` 的 1920 / 1440 / 1280 / 800 不改期望值通过。
  - Risk pack「Resource limits / 溢出」selected：图表下限与滚动兜底 -> 新移动 spec（三个移动 project 都跑、无按 project 跳过；每条用例内 `setViewportSize`，不用大于 1280×900 的视口，单用例最多两个页面；都在“曲线已加载”（图表 canvas 可见）之后量，默认要素即可）：
    - (a) 390×664：图表区高 ≥ 160，包围盒在抽屉盒内，区内 canvas 的宽高与图表区相等（±1px）。先量：按类名估算该视口的剩余高度就在 160px 上下；若下限在这里已经咬住、图表区不滚动就越出抽屉盒，这是与规格场景「Station chart height on a portrait phone」（要求在抽屉内）的冲突——停下上报，不得把断言改成“滚动后在盒内”来放宽；
    - (b) 750×342 与 844×390：图表区高 ≥ 120，canvas 与图表区同尺寸；把图表区滚入视野（`scrollIntoView({ block: 'nearest' })`）后图表区完整落在主体可视盒内，标题与关闭按钮仍可见（规格场景「Station chart reachable in short-landscape」——气象代站窗的要素选择条比河段的起报条高，750×342 下下限可能已经咬住）；
    - (c) 下限咬住的竖向矮视口 320×480：图表区高 = 160（±0.5px，硬前提：主体 `scrollHeight > clientHeight`；实测不溢出则换成确实溢出的移动非横屏视口并写进偏离记录）；图表区在卡片内、卡片在已加载容器内；滚入视野后图表区完整落在主体可视盒内；
    - (d) 下限咬住的矮视口横屏：实现者先量——750×342 若已溢出就用它，否则用 568×320；图表区高 = 120（±0.5px，溢出硬前提），同样的包含与滚入断言；PR 里报告 (c)(d) 所用视口在改动前的图表区实测高度；
    - (e) 剩余高度足够时仍占满且无假溢出：取一个实测不咬下限的移动视口（预期 390×664；若它也咬住，换成实现者量出的更高的移动竖屏视口如 390×800，并写进偏离记录），图表区高 > 160 且主体 `scrollHeight === clientHeight`；
    - (f) 形态归属：图表区计算 `min-height` 在 390×664 为 `160px`、750×342 为 `120px`、1280×900 为 `0px`。
  - Risk pack「Concurrency / 尺寸变化」selected：重排 -> 同一 spec：(g) 390×664 → 750×342 后（同一气象代站窗、不重开）canvas 的宽高等于新的图表区宽高（±1px），与旋转前的 canvas 尺寸不同，图表区高度与主体 `scrollHeight` 等于“在 750×342 新开页面、新打开的气象代站窗”的值（±1px）；(h) 反向同理。另加确定性的 vitest（新文件，放 `src/components/map/__tests__/` 或 `src/components/charts/__tests__/`，写法照 `ForecastChartFillResize.test.tsx`）：(i) 接线测试：气象代站图表把 `onChartReady` 交给被 mock 的封装库；调用它之后观察器观察的是实例的 dom，尺寸不一致时触发 `resize({ width: 'auto', height: 'auto' })`，卸载时断开。观察器的其余行为（尺寸一致不调、已销毁不调、重复就绪先断开旧的）已由 `ForecastChartFillResize.test.tsx` 经同一个 hook 钉住，不在这里重复。
  - Risk pack「Legacy compatibility」selected -> Must preserve；PR 描述贴出 `git diff --stat --diff-filter=MDR origin/master -- apps/frontend/e2e 'apps/frontend/src/**/__tests__/**'`（期望为空；扩展 `sheetChart.mocked.ts` 时它会出现在这里，须说明）与 `git diff --stat origin/master -- apps/frontend/src`（期望：`M11StationForcingPopup.tsx`、`ForecastChart.tsx`、新 hook 文件与新测试文件）。
  - 未选：Public API（无接口 / 契约面变化）、Error handling（空态 / 加载分支不动）、Auth、File IO、Schema、Config、Documentation（design.md D12 与规格已写明）。
  - Non-goals：河段面板（4.4 已完成）；触屏缩放（4.8）；抽屉内控件与要素切换项的 44px / 16px 下限（4.9——届时要素选择条会更高，本 task 的 (e) 所用视口可能需要随之调整，由 4.9 负责）；切换要素后的重排专项断言（同一图表区、同一 hook）；`QueueDonut` / `TrendLine` / `StageDurationBar` 等其他用同一封装库的图表；桌面形态的任何变化。
  - Review focus：(1) 新类全部带移动前缀，桌面计算样式与包围盒不变；(2) 共享 hook 是从 `ForecastChart.tsx` 原样搬出的同一逻辑（含“不渲染图表时断开”），`ForecastChart` 行为零变化；(3) 下限咬住的用例确实处在溢出状态；(4) (e) 防钉死与防假溢出；(5) 重排用例与新开窗对照；(6) 三个移动 project 都断言、无跳过；(7) 新 testid 只加在图表区上，既有 testid 不变。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；新 spec 的 strict `tsc --noEmit` exit 0；先红后绿——红：对 `origin/master` 的 `src/`，全部用例会先红在“图表区 testid 不存在”，这不算有信息量的红。做法：先只给未改动的 `src/` 加上那一个 testid（不加任何布局类、不接 hook），在这个基线上实跑新 spec，报告真实红 / 绿的用例与各视口的图表区高度、canvas 尺寸（可能为 null / 0——等待条件用“`m11-station-popup-loaded` 与卡片已挂上”，不要等 canvas 可见）与主体 client / scroll；再做完整实现转绿；(i) 改动前红（气象代站图表不接 `onChartReady`）；`--repeat-each=5` 无 flaky，(g)(h) 另跑 `--repeat-each=8`；变异：去掉 `mobile:` 下限 -> (c)(f) 红；去掉 `mobile-landscape:` 下限 -> (d)(f) 红；已加载容器或卡片不随内容长高 -> (c)(d) 的包含断言红；去掉“图表不参与固有高度”的处理 -> 预期 (e) 红（4.4 的这条红来自亚像素；若气象代站在 (e) 的视口恰为整数高度而不红，如实报告，并说明该处理靠什么被覆盖或为什么仍保留）；气象代站图表不接 hook -> (i) 红。改展示端运行时代码，合并前出 node-27 live receipt（PR 的生产构建对 live API）：桌面布局 oracle exit 0；390×664、750×342、320×480 下经门控钩子打开气象代站窗，曲线加载后图表区满足对应下限、canvas 与图表区同尺寸、图表区可完整滚入视野；1280×900 下图表卡片盒与改动前相同。

- [x] 4.6 抽屉打开时地图外壳让位：移动形态（两种朝向）下有曲线抽屉打开时，控制条与启动器列隐藏且不可操作（保持挂载）、展开值复位、时间轴若在播放则暂停；关闭抽屉后控制条与启动器恢复可见，预报源 / 起报时次 / 有效时刻与打开时相同、播放为停止、无面板展开。曲线区域的 `RegionErrorBoundary` 进入兜底时，控制条与启动器必须可见可操作，兜底块在地图区内且不与它们相交。需要两处共享组件改动：给 `RegionErrorBoundary` 加一个可选的错误回调 prop（页面据此得知曲线区域已进入兜底）；把时间轴的“暂停”能力从其本地 state 暴露给页面。新增 `e2e/m11-sheet-yields-chrome.mobile.mocked.spec.ts`。

  Depends on: 3.3, 3.6, 4.2

  Verify：vitest 覆盖“打开即暂停、关闭不续播”“面板展开时点要素后展开值为空”“曲线面板抛错后控制条不处于隐藏状态”“`RegionErrorBoundary` 不传回调时行为不变”；移动 spec 断言三个移动 project 下抽屉打开时控制条与启动器不可见且不接收指针输入、播放中打开抽屉后 valid time 不再变化、关闭后预报源 / 起报时次 / 有效时刻不变且未播放且无面板可见；用 3.6 的崩溃开关让曲线区域抛错，断言其兜底包围盒在地图区内、不与控制条和任何启动器相交，且控制条与启动器可见可点。

  **Suggested fixture level:** expanded - 改页面级的控制条可见性、播放状态与错误兜底行为，跨曲线窗状态与浮层展开状态。

  **Minimal mergeable slice:** atomic - 隐藏、暂停、恢复、崩溃兜底都由同一个“是否有抽屉在正常渲染”的页面级判定驱动；拆开会让中间态出现“控制条隐藏但仍在播放”或“隐藏后无法恢复”。

  Triage（#2807）：Issue type: feature ｜ Fixture level: expanded（与上游建议一致；页面级判定同时驱动控制条 / 启动器的可见性、播放状态、展开值与错误兜底，跨 `OverviewMode` 的曲线窗状态与 `M11FullscreenMap` 的浮层状态，并改两个共享组件的接口；设计见 design.md D11，规格见 `specs/mobile-curve-sheet` 的「An open sheet yields the map chrome」与 `specs/mobile-map-overlay-layout` 的「Region error fallbacks fit the mobile layout」中的曲线区域一句与场景「Curve region fallback does not strand the chrome」）｜ Blast radius: 移动形态 `/` 的全部地图外壳——判定写错会留下“控制条隐藏但仍在播放”“曲线面板崩溃后控制条与启动器再也回不来”这类死状态；写到桌面分支上会让桌面开窗时控制条消失或播放被打断。
  - Governing invariant（唯一判定，唯一持有者）：`让位 = 移动形态 && 有曲线面板在渲染（河段或气象代站）&& 曲线区域不在兜底`。在 `OverviewMode` 里算一次（曲线窗状态与曲线区域的边界都在这里），作为一个布尔 prop 传给 `M11FullscreenMap`；隐藏、暂停、展开值复位全部只读这一个布尔，不在第二处重算。桌面形态恒为 false。形态切换（桌面开着窗 → 移动：让位；反向：恢复）由“读实时的 `mobile`”自然得到，不另写分支。
  - Change surface：
    - `apps/frontend/src/components/layout/RegionErrorBoundary.tsx`：新增一个可选 prop `onErrorChange?: (error: Error | null) => void`——`componentDidCatch` 里以捕获的错误调用；`componentDidUpdate` 里在“上一状态有错、当前无错”时以 `null` 调用（覆盖「重试」与 `resetKeys` 复位两条清除路径）。**必须报告清除**：只报捕获的话，重试成功后抽屉已正常渲染而控制条仍可见，违反 D11。不传该 prop 时零行为变化（不加包裹元素、不多一次 setState）。同一次“替换要素又崩溃”里回调可能连续触发，页面侧的写入须幂等。
    - `apps/frontend/src/pages/OverviewPage.tsx`：`OverviewMode` 持有“曲线区域在兜底”的状态并接到曲线区域边界的 `onErrorChange`；算出让位布尔传给 `M11FullscreenMap`。`M11FullscreenMap`：让位为真时 (1) 启动器列 `m11-launcher-column` 的 div 直接加 `invisible`；(2) 把同一个布尔经 `M11BottomControlBarRegion` 传给控制条；(3) 一个显式的 effect 调 `collapse()`——不依赖“点要素时地图点击顺带收起”（真实轻触开窗时地图点击会先收起面板，但兜底态下「重试」成功重新进入让位、以及页面直接收到要素点击回调的路径都不经地图点击）。曲线区域边界的兜底定位类：先量，390×664 / 750×342 / 844×390 下若与控制条或任一启动器相交、或伸出地图区，只加带移动前缀的类修正，桌面类串逐字不变。
    - `apps/frontend/src/pages/m11/M11BottomControlBar.tsx`：新增一个可选布尔 prop（缺省 false），为真时根节点 `m11-bottom-control-bar` 加 `invisible`，并原样传给 `M11Timeline`。
    - `apps/frontend/src/pages/m11/M11Controls.tsx` 的 `M11Timeline`：新增同一个可选布尔 prop（缺省 false），做法照抄既有的 `disabled → setPlaying(false)` effect：为真时 `setPlaying(false)`。`playing` 仍是本地 state，不改成受控——“关闭不续播”因此是结构性的（布尔回到 false 不会把 `playing` 设回 true）。
    - 新 spec `apps/frontend/e2e/m11-sheet-yields-chrome.mobile.mocked.spec.ts`；新 vitest 文件（`src/pages/__tests__/` 与 `src/components/layout/__tests__/`、`src/pages/m11/__tests__/` 下按就近原则新建，不往既有文件里加）；`e2e/support/` 只做加法（可选参数、缺省行为不变）：`openRiverWindow` / `openStationWindow` 现在都强制导航且拒绝已有窗，而本 spec 多条用例要在已加载的页面上先布置状态再开窗——至少导出“只定位”与“轻触已定位点”两步函数（(c) 先定位后轻触、(e) 在崩溃开关下开窗时面板不会出现，都必须用它）；不导航的整体模式可选；需要气象代站的用例一开始就带门控加载 `/?metStations=1`。隐藏元素的包围盒若 `locator.boundingBox()` 返回 null，改用 `evaluate` 取 `getBoundingClientRect`（同样只做加法）。
    - `.github/workflows/ci.yml`：`Frontend Build` job 的 `timeout-minutes` 由 20 改为 30 并同步其上方注释（单 worker 串行的 mocked lane 上一次 CI 实测 16 分 41 秒 / 14 分 10 秒，本 PR 再加一个三 project 的移动 spec；只改这一个 job 的这一个数，`SQL Migration Dry Run` 的 20 不动）。
  - 隐藏方式（裁定）：`visibility: hidden`（Tailwind `invisible`），不用 `display: none`、不卸载。理由：包围盒仍可量（`e2e/m11-curve-sheet.mobile.mocked.spec.ts` (d) 与 `useM11MobilePanelMaxHeight` 的“以控制条顶为底”都靠它）；浏览器原生就不给指针与焦点；Playwright 的 `toBeHidden()` 认它；内部 state（速度、起报时次下拉的值）因保持挂载而保留。边界的 `className` 只在兜底态生效，所以类不能加在边界上；不新增包裹元素（非错误态 DOM 结构不变）。
  - 既有 spec 的裁定（与“既有用例不改通过”的偏离，已核对代码）：`e2e/m11-curve-sheet.mobile.mocked.spec.ts` 的 (d) 末段用“交集中心的命中栈里既有控制条 / 启动器列、栈顶又在抽屉内”证明层级（约第 112–137 行）。让位后控制条与启动器列 `visibility: hidden`，`elementsFromPoint` 不再返回它们，其中的前提断言 `hit.beneath` 必然变红。裁定：只改这一段——保留三条几何断言（抽屉与控制条的交集非空；右侧抽屉与启动器列的交集非空；底部抽屉与启动器列的交集为空）；把“前提：在命中栈里”换成“该元素 `toBeHidden()` 且仍在 DOM（`toHaveCount(1)`）”，“交集中心命中抽屉内”的断言保留。这是把用例同步到用户拍板的 D11，不是放宽。除这一段外不改任何既有 e2e / vitest 的期望；实现者若发现还有别的既有用例因让位变红，停下报告，不自行修改。
  - Must preserve：桌面形态开窗时控制条、启动器与缩放按钮照常可见可操作，**正在播放的时间轴继续播放**（D11 只管移动形态——下面的 (v5) 与 (g) 钉住它）；`RegionErrorBoundary` 不传回调时的全部既有行为（既有 `RegionErrorBoundary` 测试不改通过）；其余三个区域边界（地图控件 / 图例 / 控制条）不接回调、行为不变（`e2e/m11-region-fallbacks.mobile.mocked.spec.ts` 不改通过）；抽屉的几何（`m11-curve-sheet.mobile` 除 (d) 点名一段外不改通过）、图表下限（两个 `*-sheet-chart.mobile` spec 不改通过）；单窗规则（`OverviewPageSingleCurveWindow.test.tsx` 不改通过）；控制条两行 / 单行几何与速度选择器（`m11-control-bar-*.mobile` 不改通过）；`m11-overlay-collision.mocked.spec.ts` 的 1920 / 1440 / 1280 / 800 与全部既有 vitest 不改期望值通过。
  - Risk pack「Concurrency / 状态机」selected：让位 ↔ 恢复 ↔ 兜底三态 -> vitest（移动形态用 `src/test/mobileFormMatchMedia.ts`）：
    - (v1) `M11Timeline`：播放中把新 prop 置真 -> 按钮标签回到「播放时间轴」、假计时器再走若干周期 `onQueryChange` 不再被调用；再置假 -> 仍未播放、仍不调用（打开即暂停、关闭不续播）；不传该 prop 时播放行为与改动前相同；
    - (v2) 页面级：移动形态下展开一个面板后选中要素（不经地图点击）-> 三个面板都不在展开态；关闭曲线窗后仍无面板展开；
    - (v3) 页面级：移动形态下开窗 -> 控制条根与启动器列带 `invisible`；河段窗开着时经桩选中气象代站（替换）-> 两者仍带 `invisible`；曲线面板渲染期抛错 -> 两者都不带 `invisible`、兜底 `region-error-map-panels` 在场；点「重试」且不再抛错 -> 曲线窗在场、两者重新带 `invisible`；桌面形态下曲线区域兜底的类串仍含 `absolute left-1/2 top-24 z-[130] -translate-x-1/2`（钉住“桌面类串逐字不变”）；
    - (v4) `RegionErrorBoundary`：传回调时——子树抛错调用一次且参数是该错误；「重试」成功后以 `null` 调用；`resetKeys` 变化清错后以 `null` 调用；未出错的普通重渲染不调用；不传回调时抛错 / 重试 / 复位的渲染结果与改动前相同且不抛异常；
    - (v5) 页面级，桌面形态：播放中开窗 -> 控制条根不带 `invisible`、按钮标签仍为「暂停时间轴」、假计时器推进后有效时刻继续前进（以页面可观测的量为准：URL 或时间轴滑块值）。
  - Risk pack「Public API / entry」selected：用户可见契约 -> 新移动 spec（三个移动 project 都跑、无按 project 跳过；每条用例内 `setViewportSize`，不用大于 1280×900 的视口，单用例最多两个页面；不得用 `force: true`、不得用 `dispatchEvent` 绕过可操作性检查）：
    - (a) 390×664 与 750×342，河段窗与气象代站窗各一次：开窗前先量控制条中心与每个启动器中心；开窗后控制条与启动器列 `toBeHidden()` 且 `toHaveCount(1)`，上述每个点的 `document.elementFromPoint` 都不落在控制条 / 启动器列之内（750×342 下控制条另取其左半、不在右侧抽屉之下的一点，如预报源控件中心——那一点只有 `visibility` 能挡住）；替换路径不在浏览器里做（站点定位钩子把站点移到地图区中央，那里在抽屉之下，4.1 已记录同一事实），由 (v3) 覆盖；
    - (b) 390×664：先展开图例面板，再真实轻触河段开窗（规格场景原文；地图点击本身就会收起面板，这条改动前可能已绿，须点名）-> 河段窗可见、三个面板都不可见；关闭后三个面板仍不可见。展开的图例面板若盖住河段定位点（`HOOK_POINT_OCCLUDED`），改用三个面板里最小的一个，仍盖住则停下报告；
    - (c) 390×664 播放（mock 只有 9 个有效时刻，播到末位会自行停止，顺序与前提必须钉死，否则空转）：速度设为 2x（非默认值）-> 点播放 -> 确认有效时刻至少前进一次 -> 定位河段点 -> 立刻轻触该点（实现期实测后的修订：径流瓦片 source 的身份含有效时刻，播放每前进一步就换一次 source；“先定位、前进后再轻触”恰好点在换瓦片的窗口里，对 `origin/master` 同样偶发点空——定位钩子本身等到河段渲染出来才返回，所以定位必须放在前进之后、紧挨轻触）。硬前提：轻触前播放按钮标签为「暂停时间轴」；抽屉可见后读到的有效时刻不是 mock 有效时刻的最后一项。然后等待不少于 3 个播放周期，有效时刻不变。播放中换瓦片若使轻触在 `--repeat-each=8` 下不稳，停下报告，不自行换成气象代站窗或降级断言；
    - (d) 接 (c) 关闭抽屉：控制条与启动器列可见，控制条中心的命中测试落在控制条内；预报源、起报时次、有效时刻等于开窗时的值；播放按钮标签为「播放时间轴」，硬前提「下一个有效时刻」按钮未禁用（否则“未播放”是播到末位的假象），再等不少于 3 个播放周期有效时刻仍不变；开窗前设的 2x 速度值保持（状态因保持挂载而保留）；三个面板都不可见；随后展开一个面板，面板底边 ≤ 控制条顶边（面板限高的测量路径在恢复后仍然成立）；750×342 下同样走一遍“开窗隐藏 -> 关闭恢复且有效时刻不变、未播放”；
    - (g) 桌面对照（用例内 `setViewportSize` 1280×900；与 3.2 / 3.3 相同，是「约定」里“移动 spec 只断言移动形态”的显式例外）：开窗后控制条可见、其中心命中控制条内。
  - Risk pack「Error handling」selected：兜底不留死状态 -> 同一 spec（崩溃开关的顺序：先置门控与 `curve`，再开窗——探针只在有曲线面板渲染时挂载）：
    - (e) 390×664、750×342 与 844×390：曲线区域兜底 `region-error-map-panels` 可见，其包围盒在地图区内、与控制条及每一个启动器都不相交；控制条与启动器列可见；可操作——点一个启动器其面板展开，点「下一个有效时刻」有效时刻改变；
    - (f) 清除后恢复让位（也是显式 `collapse` effect 在真实浏览器里的证据——这条路径没有地图点击）：接 (e)，兜底态下先点一个启动器展开其面板，再清掉崩溃开关、点兜底的「重试」-> 曲线窗可见，控制条与启动器列重新 `toBeHidden()`；关闭后两者恢复可见，三个面板都不在场（`toHaveCount(0)`）且各启动器 `aria-expanded="false"`。
  - Risk pack「Legacy compatibility」selected -> Must preserve 与上面的既有 spec 裁定；PR 描述贴出 `git diff --stat --diff-filter=MDR origin/master -- apps/frontend/e2e 'apps/frontend/src/**/__tests__/**'`（期望只有 `m11-curve-sheet.mobile.mocked.spec.ts` 一个文件，另加可能的 `e2e/support/` 加法）与 `git diff --stat origin/master -- apps/frontend/src .github`。
  - Risk pack「Config」selected：CI 超时 -> 只改 `Frontend Build` 的 `timeout-minutes` 与注释；PR 的 CI 结果里报告该 job 的实际耗时。
  - 未选：Auth、File IO、Schema、Resource limits（兜底块的几何归 Error handling 的 (e)）、Documentation（design.md D11 与规格已写明）。
  - Non-goals：控制条区域自身处于兜底（`region-error-control-bar`）时又打开抽屉的表现（该兜底不参与让位，仍在抽屉之下）；选中要素出画时的平移（4.10）；抽屉内控件尺寸（4.9）；触屏缩放（4.7 / 4.8）；把 `playing` 提升为页面状态或写进 URL；CI mocked lane 的并行 / 分片（另行决定）；桌面形态的任何变化。
  - Review focus：(1) 让位布尔只算一次，隐藏 / 暂停 / 复位三处都读它，桌面恒 false；(2) `onErrorChange` 报告了清除（重试与 `resetKeys` 两条路），不传时零变化；(3) 隐藏用 `visibility`、保持挂载、未新增包裹元素，类没有加在边界上；(4) 暂停是单向的 effect，没有把 `playing` 改成受控，关闭后不可能续播；(5) (f) 的“展开后重试”确实不经地图点击、(c)(d) 的硬前提（播放中、未到末位、下一步未禁用）确实成立，不是空转；(6) (e) 的不相交断言逐个启动器、三个视口都做；(7) `m11-curve-sheet.mobile` (d) 只动了裁定点名的一段；(8) 三个移动 project 都断言、无跳过、无 `force`；(9) `ci.yml` 只动了一个数与其注释。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；新 spec 的 strict `tsc --noEmit` exit 0；先红后绿——红：对 `origin/master` 的 `src/`，(a)(c)(d 的“未播放 / 有效时刻不变”)(f) 红，(v1)(v2)(v3)(v4 的回调部分)红；(b)(e)(g)(v5) 与 (v4) 的“不传回调”部分改动前可能已绿，须逐条点名实际结果，并报告三个视口下兜底块、控制条、各启动器在改动前后的实测包围盒；`--repeat-each=5` 无 flaky，(c)(d) 另跑 `--repeat-each=8`；变异：让位判定去掉“不在兜底” -> (e)(v3) 红；`onErrorChange` 不报清除 -> (f)(v3)(v4) 红；去掉暂停 effect -> (c)(v1) 红；去掉显式 `collapse` effect -> (f)(v2) 红；让位判定去掉“移动形态” -> (g)(v5) 红；`invisible` 换成 `opacity-0` -> (a) 的 `toBeHidden()` 与命中测试红。改展示端运行时代码，合并前出 node-27 live receipt（PR 的生产构建对 live API）：桌面布局 oracle exit 0；390×664 与 750×342 下经门控钩子打开河段窗，控制条与启动器列计算 `visibility` 为 `hidden`、关闭后为 `visible` 且有效时刻与开窗时相同；390×664 下置门控与 `curve` 后开窗，兜底块在地图区内且不与控制条 / 启动器相交、两者 `visible`；1280×900 下开窗后控制条 `visible`。

- [x] 4.7 河段曲线触屏缩放：移动形态下河段曲线支持双指捏合缩放、单指拖动平移（移动形态下开启 dataZoom 的拖动平移，桌面形态配置不变）、触点显示 tooltip；图表容器暴露 `data-zoom-start` / `data-zoom-end`（两种形态都暴露）；“滚轮缩放时间轴”提示在移动形态改为双指手势文案。新增 `e2e/m11-river-chart-touch.mobile.mocked.spec.ts`，多点触控用 Chromium CDP `Input.dispatchTouchEvent` 合成，并把合成助手放 `e2e/support/`。

  Depends on: 4.4

  Verify：移动 spec 断言捏合后 `data-zoom-end − data-zoom-start < 100`、放大后单指横向拖动使 `data-zoom-start` 变化而跨度不变、点按绘图区后 tooltip 可见、提示文案含双指手势且不含滚轮字样；桌面 spec（`e2e/m11-river-chart-zoom-desktop.mocked.spec.ts`）断言 1280×900 下河段图容器为 `data-zoom-start="0"`、`data-zoom-end="100"`；vitest 断言桌面形态的 dataZoom 配置与提示文案与改动前相同。

  **Suggested fixture level:** compact - 一个图表组件的形态分支与一处文案，另加只读的 data 属性。

  **Minimal mergeable slice:** atomic - 这是原“触屏缩放”的首刀（河段图自有一份 dataZoom 配置与提示文案）；气象代站图已切为 4.8。

  Triage（#2808）：Issue type: feature ｜ Fixture level: compact（与上游建议一致；一个图表组件的形态分支、一处文案与只读的 data 属性，无页面级状态 / 接口契约变化；设计见 design.md D12 后三条，规格见 `specs/mobile-curve-sheet` 的「Curve time axes are zoomable by touch」中的河段场景、提示场景与桌面场景）｜ Blast radius: 河段曲线窗的图表——形态分支写错会让桌面的缩放 / 平移配置变掉（桌面拖动开始平移时间轴），或让手机上图表吃掉抽屉主体的纵向滚动、捏合时连页面一起缩放。
  - Change surface：`apps/frontend/src/components/charts/ForecastChart.tsx`（新增可选 prop，缺省值下行为与配置逐字不变）；`apps/frontend/src/components/map/M11RiverForecastPanel.tsx`（按形态传 prop、提示文案、图表区上的 data 属性）。新 spec `e2e/m11-river-chart-touch.mobile.mocked.spec.ts` 与 `e2e/m11-river-chart-zoom-desktop.mocked.spec.ts`；多点触控合成助手放新文件 `e2e/support/touchGestures.ts`（用 Chromium CDP `Input.dispatchTouchEvent`；与面板无关，4.8 的气象代站 spec 复用；单指拖动可复用 `e2e/support/curveSheet.mocked.ts` 已有的 `dragWithTouch`，也可在新助手里自成一套，不改既有助手的行为）；新 vitest 文件（不往既有文件里加）。不改 `M11StationForcingPopup.tsx`（4.8）、`useChartFollowsContainer.ts`、`M11DraggableCurveWindow.tsx`、`OverviewPage.tsx`、`ForecastPanel.tsx`。
  - 口径（裁定）：
    - (1) 平移开关：`ForecastChart` 新增一个可选布尔 prop（缺省 false），为真时 inside dataZoom 的配置 = 桌面字面值 + `moveOnMouseMove: true` + `preventDefaultMouseMove: false`（后者见 (5)）；为假时就是桌面字面值，不出现 `preventDefaultMouseMove` 键。河段面板按 `useMobileForm().mobile` 传入；该 prop 进配置的 memo 依赖。桌面形态下 `dataZoom` 配置与改动前深相等：`[{ type: 'inside', zoomOnMouseWheel: true, moveOnMouseMove: false, moveOnMouseWheel: false, filterMode: 'none' }]`。不 `zoomable` 的调用方（`ForecastPanel.tsx`）`dataZoom` 仍为 `undefined`。
    - (2) 缩放窗口属性的落点：`data-zoom-start` / `data-zoom-end` 加在既有的图表区元素 `m11-river-panel-chart` 上（规格说的“图表容器”= 它；4.8 对称地用 `m11-station-panel-chart`），两种形态都带，曲线已加载（图表在渲染）时才带。取值是图表**当前实际**的窗口（0–100 的数字字符串，初始为 `0` / `100`）：随 `datazoom` 事件更新，以实例里的当前值为准而不是只信事件载荷；图表配置被整体重设（`notMerge`，如切换起报时次或形态切换导致缩放回到全范围）时属性必须跟着回到 `0` / `100`，不得停在旧窗口——重设不发 `datazoom` 事件，所以复位不能只靠事件监听。机制（回调 prop 交给面板存成 state，或其他等价做法）由实现者定，但 `ForecastChart` 的新 prop 都必须可选、缺省不产生任何 DOM / 配置差异。
    - (3) 提示文案：提示元素加 `data-testid="m11-river-panel-zoom-hint"`（为可测性新增，两种形态都带）；移动形态文案「双指缩放时间轴」，桌面形态仍是「滚轮缩放时间轴」，类不变。
    - (4) tooltip 的可观测口径：河段图 tooltip 是 `renderMode: 'richText'`，画在 canvas 里、没有 DOM 节点，“tooltip 可见”无法用选择器断言。裁定：图表区元素再暴露一个只读属性 `data-tooltip-visible`（`"true"` / `"false"`，由图表库的 tooltip 显示 / 隐藏事件驱动，两种形态都带）——这是规格属性清单之外为可测性新增的产品代码，写进偏离记录。实现者先实测触屏点按是否触发该事件；不触发则停下报告（不要改 tooltip 的渲染方式，不要用像素比对）。
    - (5) 手势归属：图表区内双指捏合只缩放时间轴，不得触发浏览器的页面缩放（捏合后 `visualViewport.scale` 仍为 1）；单指纵向拖动不得被图表吃掉——抽屉主体溢出时，在图表区内起手的纵向拖动仍能滚动抽屉主体（4.4 的下限咬住时图表占了主体的大半，吃掉滚动等于够不着图表下方的内容）。机制（fixture 审核按图表库源码核对）：图表库的缩放控制器在绘图区（grid）内按下后会对其后的每个 touchmove 调 `preventDefault`，与 `moveOnMouseMove` 无关——所以改动前在绘图区内起手的纵向拖动就已经滚不动抽屉主体，`touch-action` 也救不回已被取消的 touchmove。库给的开关是 inside dataZoom 的 `preventDefaultMouseMove: false`：移动形态关掉它把纵向滚动还给浏览器，再给图表区加带移动前缀的 `touch-action: pan-y`，不让浏览器接管横向拖动与捏合。两者都做后仍做不到“横向平移 + 捏合 + 纵向滚动”三者兼得时停下报告。
    - (6) 形态切换（移动 ↔ 桌面）会重设图表配置、缩放回到全范围，可接受；竖屏 ↔ 矮视口横屏都是移动形态，旋转不得重置已缩放的窗口。
  - Must preserve：桌面形态的 `dataZoom` 配置深相等（上条）与提示文案；桌面滚轮缩放仍可用、鼠标拖动仍不平移；`ForecastChart` 其余配置（tooltip、grid、series、markLine）与既有 `ForecastChartFillResize.test.tsx`、`M11RiverForecastPanel.test.tsx` 不改通过；图表下限与重排（`e2e/m11-river-sheet-chart.mobile.mocked.spec.ts` 不改通过）；抽屉几何与让位（`m11-curve-sheet.mobile`、`m11-sheet-yields-chrome.mobile` 不改通过）；`m11-overlay-collision.mocked.spec.ts` 的 1920 / 1440 / 1280 / 800 与全部既有 vitest 不改期望值通过。
  - Risk pack「Public API / entry」selected：触屏手势与只读属性是用户可见契约 -> 新移动 spec（三个移动 project 都跑、无按 project 跳过；每条用例内 `setViewportSize`，不用大于 1280×900 的视口，单用例最多两个页面；都在“曲线已加载”（图表 canvas 可见）之后做；手势一律经 `e2e/support/touchGestures.ts` 的 CDP 合成，不用 `dispatchEvent` 伪造 DOM 事件、不直接调图表实例）：
    - (a) 390×664 与 750×342 初始：图表区 `data-zoom-start="0"`、`data-zoom-end="100"`；提示文案含「双指」且不含「滚轮」；
    - (b) 390×664 与 750×342：图表绘图区内双指向外捏合后 `0 ≤ start < end ≤ 100` 且 `end − start < 100`；`visualViewport.scale` 仍为 1，抽屉包围盒与捏合前相同；
    - (c) 接 (b)（硬前提：跨度 < 100）：单指横向拖动后 `data-zoom-start` 改变、跨度不变（±0.5）；反方向拖回去 `start` 朝反方向变化；
    - (d) 未缩放时单指横向拖动：属性仍为 `0` / `100`；
    - (e) 390×664：点按绘图区内一点后 `data-tooltip-visible="true"`（硬前提：点按前为 `"false"`）；
    - (f) 320×480（硬前提：抽屉主体 `scrollHeight > clientHeight`）：先做对照——在主体内、图表区之外（起报时次条或图例行）起手的 CDP 单指纵向拖动使主体 `scrollTop` 改变（证明合成手势能驱动浏览器滚动），把 `scrollTop` 复位；再在**绘图区（grid）内**（与 (b)(e) 同口径，不是图表区的边距）起手做同样的拖动，主体 `scrollTop` 改变，且属性仍为 `0` / `100`。仅当对照拖动也滚不动时才允许降级为断言图表区的计算 `touch-action` 并写进偏离记录；对照能滚而绘图区内不能滚就是本 task 要修的缺陷，必须修到绿；
    - (g) 旋转保持：390×664 捏合后切到 750×342，跨度仍 < 100（±0.5 等于旋转前）。
  - Risk pack「Legacy compatibility」selected：桌面不变 -> 新桌面 spec `e2e/m11-river-chart-zoom-desktop.mocked.spec.ts`（桌面 project，1280×900）：(h) 河段图表区 `data-zoom-start="0"`、`data-zoom-end="100"`，提示文案为「滚轮缩放时间轴」；(i) 绘图区内滚轮放大后跨度 < 100，随后按住鼠标横向拖动 `data-zoom-start` 不变（桌面不平移）。vitest（新文件）：(j) 桌面形态下面板交给图表的 `dataZoom` 与上面的字面值深相等、提示文案为「滚轮缩放时间轴」；移动形态下 `moveOnMouseMove` 为 true、`preventDefaultMouseMove` 为 false、其余键相同，提示文案为「双指缩放时间轴」——即移动形态与桌面字面值只差 `moveOnMouseMove` 与 `preventDefaultMouseMove` 两个键；(k) `ForecastChart` 不传新 prop 时：`zoomable` 的 `dataZoom` 与字面值深相等、不 `zoomable` 时为 `undefined`；(l) 属性跟随：驱动被 mock 的图表库发出缩放事件 -> 图表区属性更新为实例当前窗口；数据换新（配置重设）后属性回到 `0` / `100`；(m) tooltip 显示 / 隐藏事件 -> `data-tooltip-visible` 翻转。PR 描述贴出 `git diff --stat --diff-filter=MDR origin/master -- apps/frontend/e2e 'apps/frontend/src/**/__tests__/**'`（期望为空）与 `git diff --stat origin/master -- apps/frontend/src`。
  - 未选：Concurrency（无页面级状态；旋转保持归 (g)）、Error handling（空态 / 超预算分支不动）、Auth、File IO、Schema、Config、Resource limits、Documentation（design.md D12 与规格已写明）。
  - Non-goals：气象代站图（4.8）；桌面形态的缩放 / 平移配置；底部缩放滑块；双击复位缩放；tooltip 的内容、样式与渲染方式；抽屉内控件尺寸（4.9）；iOS Safari 上的真机手感（#2818）。
  - Review focus：(1) 新 prop 全部可选，缺省下 `ForecastChart` 的配置与 DOM 零差异，`ForecastPanel` 不受影响（不传新 prop 时不得经 `onChartReady` 去调实例的 `on` / `getOption`——既有 `ForecastChartFillResize.test.tsx` 的假实例没有这些方法，且断言非 `fill` 时 `onChartReady` 为 `undefined`）；(2) 桌面 `dataZoom` 深相等有 vitest 字面值钉子，且浏览器里 (i) 证明桌面拖动不平移；(3) 属性读的是实例当前窗口，配置重设后回到 0 / 100；(4) 手势全部经 CDP 合成，(c) 的“跨度 < 100”硬前提成立，属性读数用轮询（缩放 / 平移的派发有节流与约 100ms 动画）；(5) 捏合不触发页面缩放、纵向拖动不被吃掉；(6) 三个移动 project 都断言、无跳过；(7) 新增的移动专属类带移动前缀，桌面计算样式不变。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；新 spec 与助手的 strict `tsc --noEmit` exit 0；先红后绿——红：对 `origin/master` 的 `src/`，(a)(b)(c)(e)(h) 红（属性 / testid 不存在），(j) 的移动分支与 (l)(m) 红；须另外在未改动的 `src/` 上实测并报告：CDP 捏合改动前是否已能缩放（预期能——捏合不受任何 `*OnMouse*` 键门控）、单指拖动改动前是否平移（预期不能）、(f) 的滚动基线（预期：对照能滚、绘图区内起手滚不动）；(d)(i 的滚轮部分)(k) 改动前可能已绿，须点名；`--repeat-each=5` 无 flaky（`--workers=1`），手势用例 (b)(c)(f)(g) 另跑 `--repeat-each=8`；变异：移动形态不翻 `moveOnMouseMove` -> (c)(j) 红；桌面也翻 -> (i)(j) 红；属性只写初值不跟事件 -> (b)(l) 红；配置重设后不复位 -> (l) 红；移动形态不加 `preventDefaultMouseMove: false` -> (f)(j) 红；去掉 `touch-action` 类 -> 报告实际变红的用例（(b) 的页面缩放断言改动前多半已绿，这条可能没有用例变红，如实写）。改展示端运行时代码，合并前出 node-27 live receipt（PR 的生产构建对 live API）：桌面布局 oracle exit 0；390×664 下经门控钩子打开河段窗，曲线加载后属性为 0 / 100、提示文案含「双指」，CDP 捏合后跨度 < 100；1280×900 下属性为 0 / 100、提示文案为「滚轮缩放时间轴」。

- [x] 4.8 气象代站曲线触屏缩放：移动形态下气象代站曲线支持双指捏合缩放、单指拖动平移、触点显示 tooltip，图表容器暴露 `data-zoom-start` / `data-zoom-end`。新增 `e2e/m11-station-chart-touch.mobile.mocked.spec.ts`，复用 4.7 的合成助手。

  Depends on: 4.5, 4.7

  Verify：移动 spec 断言捏合后 `data-zoom-end − data-zoom-start < 100`、放大后单指横向拖动使 `data-zoom-start` 变化而跨度不变、点按绘图区后 tooltip 可见；桌面 spec（`e2e/m11-station-chart-zoom-desktop.mocked.spec.ts`）断言 1280×900 下气象代站图容器为 `data-zoom-start="0"`、`data-zoom-end="100"`；vitest 断言桌面形态的 dataZoom 配置与改动前相同。

  **Suggested fixture level:** compact - 一个图表配置的形态分支与只读 data 属性。

  **Minimal mergeable slice:** atomic - 一份图表配置。

  Triage（#2809）：Issue type: feature ｜ Fixture level: compact（与上游建议一致；一份图表配置的形态分支与只读 data 属性，做法沿用已合并的 4.7，无页面级状态 / 接口契约变化；设计见 design.md D12 后三条，规格见 `specs/mobile-curve-sheet` 的「Curve time axes are zoomable by touch」中的气象代站场景与桌面场景）｜ Blast radius: 气象代站曲线窗的图表——形态分支写错会让桌面的缩放 / 平移配置变掉，或让手机上图表吃掉抽屉主体的纵向滚动（4.5 的下限咬住时图表占了主体的大半）。
  - 现状（先量再改，实现者须实测确认并写进 PR）：气象代站图的配置是 `dataZoom: [{ type: 'inside', xAxisIndex: 0, filterMode: 'none' }]`，没有 `moveOnMouseMove` 键——图表库缺省为 true，所以**桌面按住鼠标拖动本来就平移**，触屏单指横拖与双指捏合预期在改动前就已生效（与河段图不同：河段图显式关掉了拖动平移）。预期改动前唯一坏掉的行为是：绘图区内起手的单指纵向拖动滚不动抽屉主体（库在绘图区内按下后取消其后每个 touchmove）。本 task 真正要加的是：移动形态的 `preventDefaultMouseMove: false`、只读属性、`touch-action`。实测与预期不符时停下报告。
  - Change surface：`apps/frontend/src/components/map/M11StationForcingPopup.tsx`（`StationVariableEcharts` 的 dataZoom 形态分支与事件；图表区 `m11-station-panel-chart` 上的属性与一个移动前缀类）；`apps/frontend/src/components/charts/` 下新增一个小 hook，承载 4.7 写在 `ForecastChart.tsx` 里的“缩放窗口 / tooltip 可见性观察”逻辑（回调进 ref、稳定的 `onEvents`、`datazoom` 时读实例窗口、配置重设后对发过 `datazoom` 的实例重读），`ForecastChart.tsx` 改为调用该 hook——行为零变化，证据是 4.7 的 `ForecastChartZoomConfig.test.tsx`、`M11RiverForecastPanelTouchZoom.test.tsx`、`ForecastChartFillResize.test.tsx` 与两个河段 spec 不改通过；气象代站图同样调用它。**边界偏离（显式）**：issue 写的 Module / Scope 是 `src/components/map`、Out of Scope 含河段图，而这次抽取会动 `ForecastChart.tsx`（它有两个调用方：`M11RiverForecastPanel.tsx` 与 `forecast/ForecastPanel.tsx`）。裁定仍然抽取——另抄一份同样的观察逻辑是这次改动自己制造的重复，4.5 抽 `useChartFollowsContainer` 是同一做法；条件是 `ForecastChart.tsx` 的 diff 只有“内联逻辑换成 hook 调用”，对外 prop、配置与 DOM 零变化，并写进 PR 偏离记录。新 spec `e2e/m11-station-chart-touch.mobile.mocked.spec.ts` 与 `e2e/m11-station-chart-zoom-desktop.mocked.spec.ts`，手势复用 `e2e/support/touchGestures.ts`（确需扩展只加可选参数、缺省行为不变，并写进偏离记录），开窗与量具复用 `e2e/support/sheetChart.mocked.ts`。不改 `M11DraggableCurveWindow.tsx`、`M11RiverForecastPanel.tsx`、`OverviewPage.tsx`。
  - 口径（裁定）：
    - (1) dataZoom：桌面形态与改动前深相等——`[{ type: 'inside', xAxisIndex: 0, filterMode: 'none' }]`，不新增任何键；移动形态 = 该字面值 + `preventDefaultMouseMove: false`，只多这一个键。按 `useMobileForm().mobile` 分支，竖屏 ↔ 矮视口横屏之间配置对象引用不变。
    - (2) 属性：`data-zoom-start` / `data-zoom-end` / `data-tooltip-visible` 加在既有的 `m11-station-panel-chart` 上，两种形态都带，口径与 4.7 相同（读实例当前窗口；初值 `0` / `100` / `"false"`；配置整体重设后回到 `0` / `100`）。`data-tooltip-visible` 同 4.7，是规格属性清单之外为可测性新增的产品代码，写进偏离记录。
    - (3) 切换气象要素会重设图表配置：属性回到 `0` / `100`。切到没有可绘制序列的要素再切回来时图表实例被销毁重建——属性必须从 `0` / `100` 重新开始，不得停在旧窗口（4.7 审核指出的路径：河段到不了，气象代站要核）。`data-tooltip-visible` 同理回到 `"false"`（实例销毁不发 `hidetip`）。做法由实现者定（状态与图表一起卸载，或等价做法）。覆盖：vitest (l) 是下限；浏览器里可在 spec 内对气象代站序列接口追加一个路由覆盖（两源都去掉某个要素）造出该路径，做得到就加一条 e2e，做不到在 PR 里说明。
    - (4) 手势归属：图表区加移动前缀的 `touch-action: pan-y`（桌面计算值仍为 `auto`）；捏合后 `visualViewport.scale` 仍为 1；抽屉主体溢出时，绘图区内起手的单指纵向拖动能滚动主体。
    - (5) 不加缩放提示文案（气象代站窗没有这条提示，「单要素双源同轴」不动）。
    - (6) 移动 ↔ 桌面互切重设配置、缩放回到全范围，可接受；旋转不得重置。
  - Must preserve：桌面 dataZoom 深相等；桌面滚轮缩放与鼠标拖动的行为与改动前实测一致；图表其余配置（tooltip、grid、legend、series）不动；`M11StationForcingPopup.test.tsx`、`M11StationForcingPopupChartResize.test.tsx`、`e2e/m11-station-sheet-chart.mobile.mocked.spec.ts`、`m11-curve-sheet.mobile`、`m11-sheet-yields-chrome.mobile`、4.7 的全部 spec 与 vitest、`m11-overlay-collision.mocked.spec.ts` 的 1920 / 1440 / 1280 / 800 不改通过；河段图行为零变化；`forecast/ForecastPanel.tsx`（不 `zoomable`、不传回调）交给封装库的 `dataZoom` 与 `onEvents` 仍均为 `undefined`（4.7 的 (k) 已钉，不改通过）。
  - Risk pack「Public API / entry」selected：触屏手势与只读属性 -> 新移动 spec（三个移动 project 都跑、无按 project 跳过；每条用例内 `setViewportSize`，不用大于 1280×900 的视口，单用例最多两个页面；都在图表 canvas 可见之后做；手势一律经 `touchGestures.ts`；属性读数用轮询；绘图区按图表配置的 grid 内缩独立抄写——left 48 / right 16 / top 18 / bottom 28）：
    - (a) 390×664 与 750×342 初始：`data-zoom-start="0"`、`data-zoom-end="100"`、`data-tooltip-visible="false"`，图表区计算 `touch-action` 为 `pan-y`；
    - (b) 390×664 与 750×342：绘图区内双指向外捏合后 `0 ≤ start < end ≤ 100` 且跨度 < 100；`visualViewport.scale` 仍为 1，抽屉包围盒不变；
    - (c) 接 (b)（硬前提：跨度 < 100）：单指横向拖动后 `data-zoom-start` 改变、跨度不变（±0.5）；反向拖回 `start` 朝反方向变化；
    - (d) 390×664：点按绘图区内一点后 `data-tooltip-visible="true"`（硬前提：点按前为 `"false"`）；
    - (e) 下限咬住且主体溢出的视口（预期 320×480；实测不溢出则换成确实溢出的移动视口并写进偏离记录；硬前提 `scrollHeight > clientHeight`）：对照——主体内、图表区之外起手的单指纵向拖动使 `scrollTop` 改变，复位；再在绘图区内起手做同样的拖动，`scrollTop` 改变，且属性仍为 `0` / `100`；起手点须同时落在绘图区内与主体可视盒内（该视口下滚动前图表区不完整可见）。实现期修订：320×480 未滚动时绘图区只露出约 3.5px，在这条缝里起手改动前也能滚、照不出缺陷——所以对照之后不是把 `scrollTop` 复位到 0，而是预置到让绘图区中线落在主体可视底边上方约 12px 的位置再起手，断言 `scrollTop` 大于起手前的值，并加硬前提“剩余可滚余量 ≥ 拖动幅度”；
    - (f) 旋转保持：390×664 捏合后切到 750×342，跨度与旋转前相等（±0.5）；
    - (g) 真实图表库上的复位：390×664 捏合（硬前提：跨度 < 100）后切换到另一个有曲线的气象要素，属性回到 `0` / `100`（4.7 审核留下的缺口：复位此前只有桩驱动的 vitest）。
  - Risk pack「Legacy compatibility」selected -> 新桌面 spec（桌面 project，1280×900）：(h) 图表区 `data-zoom-start="0"`、`data-zoom-end="100"`，计算 `touch-action` 为 `auto`；(i) 绘图区内滚轮放大后跨度 < 100，随后按住鼠标横向拖动的结果与实现者在未改动 `src/` 上量到的桌面基线一致（预期：平移，`start` 改变而跨度不变；实测不同则按实测断言并报告）。vitest（新文件）：(j) 桌面形态交给封装库的 `dataZoom` 与字面值深相等；移动形态只多 `preventDefaultMouseMove: false`；竖屏 ↔ 矮视口横屏不换配置对象；(k) 属性随 `datazoom` 读实例窗口、切换要素（配置重设）后回到 `0` / `100`、交给封装库的 `onEvents` 引用稳定；(l) 口径 (3) 的销毁重建路径：重建后 `data-zoom-start` / `data-zoom-end` 为 `0` / `100`、`data-tooltip-visible` 为 `"false"`；(m) `showtip` / `hidetip` 驱动 `data-tooltip-visible`。
  - 未选：Concurrency、Error handling（空态 / 加载分支不动）、Auth、File IO、Schema、Config、Resource limits、Documentation。
  - Non-goals：河段图的行为；桌面形态的缩放 / 平移配置；缩放提示文案；底部缩放滑块、双击复位；tooltip 的内容与渲染方式；抽屉内控件尺寸（4.9）；`QueueDonut` / `TrendLine` / `StageDurationBar`；真机手感（#2818）。
  - Review focus：(1) 共享 hook 是从 `ForecastChart.tsx` 原样搬出的同一逻辑，`ForecastChart` 不传回调时仍不给 `onEvents`、不碰 `onChartReady`，4.7 的测试不改通过；(2) 桌面 dataZoom 深相等有字面值钉子；(3) 属性读实例窗口，要素切换与销毁重建后回到 0 / 100；(4) (c) 与 (g) 的硬前提成立，读数用轮询；(5) (e) 的对照拖动与溢出前提；(6) 三个移动 project 都断言、无跳过、无 `force`；(7) 新类带移动前缀，既有 testid 与 DOM 结构不变。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；新 spec 的 strict `tsc --noEmit` exit 0；改动前基线表（未改动的 `src/`，仓库外探针读实例的 `dataZoom[0]`）：CDP 捏合、放大后单指横拖、(e) 的对照与绘图区内起手拖动、捏合后页面缩放、桌面滚轮与鼠标拖动；先红后绿并逐条点名哪些红只是“属性不存在”、哪条红有信息量（预期 (e)）；`--repeat-each=5` 无 flaky（`--workers=1`），手势用例 (b)(c)(e)(f)(g) 另跑 `--repeat-each=8`；变异：移动形态不加 `preventDefaultMouseMove: false` -> (e)(j) 红；桌面也加 -> (j) 红；属性不跟事件 -> (b)(c)(k) 红；配置重设后不复位 -> (g)(k) 红；去掉 `touch-action` 类 -> (a) 红；状态不随图表销毁 -> (l) 红。`git diff --stat --diff-filter=MDR origin/master -- apps/frontend/e2e 'apps/frontend/src/**/__tests__/**'` 期望为空。改展示端运行时代码，合并前出 node-27 live receipt（PR 的生产构建对 live API）：桌面布局 oracle exit 0；390×664 与 750×342 经门控钩子打开气象代站窗，属性初值 0 / 100、捏合后跨度 < 100、单指横拖平移且跨度不变、页面缩放为 1；1280×900 属性 0 / 100、滚轮可缩放。

- [x] 4.9 抽屉内控件触控下限：移动形态下三处落点——河段面板头部内联的关闭按钮、气象代站窗共享头部的关闭按钮、两种窗的起报时次选择触发器及其选项——达到 44×44 / 高 44 / 字号 16 的下限；气象要素切换项高 ≥ 44px、可换行、不超出抽屉水平范围。桌面形态尺寸不变。新增 `e2e/m11-sheet-controls.mobile.mocked.spec.ts`。

  Depends on: 4.2

  Verify：390×664 下两种窗的关闭按钮 ≥ 44×44、起报时次触发器高 ≥ 44px 且字号 ≥ 16px、展开后每个选项高 ≥ 44px；气象代站窗的每个要素切换项在抽屉水平范围内且高 ≥ 44px。

  **Suggested fixture level:** compact - 三处控件的移动尺寸类名。

  **Minimal mergeable slice:** atomic - 三处落点共用同一条尺寸审计断言；按落点拆是三份几行的 PR，各自都要重复同一套夹具。

  Triage（#2810）：Issue type: feature ｜ Fixture level: compact（与上游建议一致；三处控件的移动形态尺寸类名，无状态 / 接口 / 共享入口变化；设计见 design.md D13，规格见 `specs/mobile-curve-sheet` 的「Sheet controls meet the mobile floors」与 `specs/mobile-viewport-shell` 的「Touch-target audit with a sheet open」「Form-font audit on the map page」）｜ Blast radius: 两种曲线窗的头部与工具条——尺寸类写错会让桌面窗里的控件变大、把 16:9 的窗内容挤掉；或让手机上气象代站抽屉的工具条吃掉图表的高度（改动前 390×664 下图表区高 165.89px，只比 160px 下限高 5.89px）。
  - Change surface：`apps/frontend/src/components/map/M11RiverForecastPanel.tsx`（头部内联的关闭按钮；起报时次条的间距如需调整）；`apps/frontend/src/components/map/M11PopupChrome.tsx`（`M11PopupHeader` 的关闭按钮；`M11IssueTimeSelect` 的触发器与选项的类）；`apps/frontend/src/components/map/M11StationForcingPopup.tsx`（工具条 `m11-station-toolbar` 的移动形态间距、要素切换项的类）。新 spec `e2e/m11-sheet-controls.mobile.mocked.spec.ts`。不改共享基础组件 `src/components/ui/select.tsx`——这是对「约定」原文与 issue Out of Scope（“先合并者落地基础组件规则”）的**显式 override**：基础组件被 `AppShell` / `JobFilters` / `MonitoringPage` 共用，在这里改它会把影响面带出曲线窗；本 task 只在 `M11IssueTimeSelect` 自己的类上加移动前缀取值（触发器、选项，以及下拉内容 `SelectContent` 的最大高度——基础组件的 `max-h-96` 是 384px，高于 342px 的矮视口），基础组件的规则仍由 6.1 / #2813 落地，「约定」一节已同步改写；6.1 落地后这里的取值仍然成立，不需要回头删。新增 e2e 助手放新的 support 文件（见 (g)），不改既有 support 文件；不改 `M11DraggableCurveWindow.tsx`、`OverviewPage.tsx`、图表。
  - 现状（实现者先量，写进 PR）：两个关闭按钮 `h-7 w-7`（28×28）；起报时次触发器 `h-7`、字号 11px，选项字号 11px；要素切换项 `h-7`、字号 12px，没有最小宽度；气象代站工具条 `gap-3 px-4 py-2`：切换项容器是 `min-w-0 flex-1`，折行判定时主尺寸按 0 算，所以它**永远与起报时次条同一行**，五个切换项在右侧剩余宽度里自己折行——fixture 审核按真实类串的静态复刻估得 390×664 下剩余宽约 172px、切换项折 2 行、工具条高约 79px（回退字体下的估值，以实现者在真实页面上的实测为准）。`M11PopupSourceControls` 与 `M11PopupShell` 在产品代码里没有调用方（前者只被一个 vitest 直接渲染；清理已另立 #2848）（design.md D13 说的“气象代站窗的 source controls”实际是 `m11-station-toolbar`）：不改、不删，写进 PR 的范围外观察。
  - 口径（裁定）：
    - (1) 关闭按钮：两种窗在移动形态都是 ≥ 44×44（包围盒）；图标尺寸不变。头部不得因此变高（头部文字块已高于 44px）——以实测为准，变高了要报告。
    - (2) 起报时次触发器：移动形态高 ≥ 44px、计算字号 ≥ 16px，宽度仍受既有上限约束且不超出抽屉水平范围；选项每项高 ≥ 44px、字号 16px。下拉内容在 portal 里（抽屉之外）：展开后列表盒须在视口内，矮视口横屏（750×342）下每个选项仍可经列表自身的滚动够到并可选中。
    - (3) 要素切换项：移动形态每项包围盒高 ≥ 44px、宽 ≥ 44px（规格的“抽屉打开时的触控审计”要求 44×44；RH / Rn 这类短标签要靠移动前缀的最小宽度），字号不低于现值；可换行，每项包围盒在抽屉水平范围内。移动形态下切换项容器**独占一行**（移动前缀的 `basis-full` 一类，DOM 顺序不变）：不这样做的话，16px 字号的触发器把起报时次条撑到约 217px，切换项只剩约 127px、折成 3 行，工具条约 161px。
    - (4) **气象代站图表高度的冲突（#2806 携带备注，本 task 裁定）**。预算（估值，实现者实测后在 PR 里给出真实数字）：切换项独占一行后工具条高 = 44 + 44 + 1（下边线）+ 上下内边距 + 行距 = 89 + x，现状约 79；390×664 下图表区现有余量 5.89px，图表区下方留白 16px（卡片下内边距 8 + 已加载容器下内边距 8）。所以图表区必然被钉在 160px 下限，主体溢出量 ≈ 10 + x − 5.89，要让图表区不滚动就完整落在抽屉盒内须 x ≤ 约 11.9。裁定：(i) 规格场景「Station chart height on a portrait phone」原样保持——390×664 下气象代站图表区高 ≥ 160、**不滚动**就完整落在抽屉盒内、canvas 同尺寸，既有 `e2e/m11-station-sheet-chart.mobile.mocked.spec.ts` 的 (a) 不改通过，且图表区底边在主体可视底边之上 ≥ 2px（量取容差之外的余量）；(ii) 可用的旋钮，按顺序用、全部带移动前缀、只动间距不动内容与顺序：先是工具条自己的纵向内边距与行距，再是已加载容器的上内边距（现 10px）与图例行的下内边距（现 6px）；两组合计可让出十几 px，预期够用；(iii) 390×664 下不再有“剩余高度足够”的余量，既有 (e) 在该视口不再成立——**预先许可**把 (e) 的视口换成更高的移动竖屏视口（390×800 一类，须实测确认该视口下图表区 > 160 且主体不溢出），断言一条不改，只改视口常量与标题，写进偏离记录（#2806 的备注自己提的做法，4.5 的 Non-goals 也预告了）；(iv) 退路：(ii) 的旋钮用尽仍达不到 (i)，就停下并报告实测数字——那时要改的是规格场景本身（改成“可完整滚入视野”），由编排者改 spec delta 后再继续；实现者不得自行放宽 (a)，也不得改头部、图例行、卡片的内容与顺序或 4.5 定下的图表区结构。320×480 等更矮的视口下图表区本来就靠滚动够到（4.5 的 (c)），不受本条约束。
    - (5) 河段抽屉同理要量：起报时次条长高后，既有 `e2e/m11-river-sheet-chart.mobile.mocked.spec.ts` 全部用例须不改通过（预期 390×664 余量充足）；不通过就停下报告。
    - (6) 新增取值全部带 `mobile:` 前缀（矮视口横屏需要不同取值时再叠 `mobile-landscape:`）；桌面形态下这些控件的包围盒与计算字号和改动前逐像素相同。
  - 实现期修订（实测后同步）：(1) `src/index.css` 里未分层的 `button, input, select, textarea { font: inherit }` 压过全部工具类，`<button>` 上的 Tailwind 字号类不生效——触发器现值实测 11px（继承自父级）、切换项现值 11px（不是上文写的 12px）；所以触发器的移动字号用了带 `!` 的写法，切换项字号保持 11px；这条规则本身不在本 task 范围，已转告 6.1 / #2813。(2) 页面 `min-width: 320px`，比 320 更窄的视口照样渲染成 320 宽，(d) 的折行改在 568×320 的右侧抽屉（宽 284px）上证明：前四项一行、Rn 落第二行。(3) 真实预算：工具条 79 → 101px（x = 12），已加载容器上内边距 10 → 4px，390×664 下主体溢出 11px，图表区底边在主体可视底边之上 5.5px；4.5 的 (e) 换到 390×800。(4) 河段起报时次条也收紧了移动间距（矮视口横屏另有一档）：不收紧时 750×342 的河段图表区掉到 119.5px、主体出现 1px 溢出，既有河段 spec 只靠 0.5px 容差通过；为此多加一条河段用例（750×342 图表区 > 120、主体不溢出）。(5) (g) 的滚动是程序滚动并带重试：选择器库的“向上滚动”按钮挂载时会把焦点项拉回视野一次。
  - Must preserve：桌面形态——1280×900 下两种窗的关闭按钮 28×28、触发器高 28 / 字号 11px、切换项高 28（以实现者在未改动 `src/` 上的实测为准，新 spec 的桌面对照用例钉字面值）；`e2e/m11-curve-window-desktop.mocked.spec.ts`、`m11-curve-window-min-size.mocked.spec.ts`、`m11-overlay-collision.mocked.spec.ts` 的 1920 / 1440 / 1280 / 800 不改通过；移动形态——`m11-curve-sheet.mobile`（抽屉几何与“尺寸与内容状态无关”）、`m11-sheet-yields-chrome.mobile`、4.7 / 4.8 的四个触屏 spec、`m11-river-sheet-chart.mobile` 全部、`m11-station-sheet-chart.mobile` 除 (e) 的视口外全部，均不改通过；控件的 testid、aria 属性、DOM 顺序不变；河段名 / 站点 ID 行保持单行截断；全部既有 vitest 不改通过。#2803 的携带备注（`m11-curve-sheet.mobile` (d) 的启动器列与抽屉不相交）：本 task 不改启动器列，预期不受影响。
  - Risk pack「Resource limits / 溢出」selected -> 新移动 spec（三个移动 project 都跑、无按 project 跳过；每条用例内 `setViewportSize`，不用大于 1280×900 的视口，单用例最多两个页面；量具用包围盒与计算样式，不用文档滚动宽度；都在曲线已加载之后量）：
    - (a) 390×664，河段窗与气象代站窗各一条：关闭按钮包围盒 ≥ 44×44，且在抽屉盒内；
    - (b) 390×664，两种窗各一条：起报时次触发器高 ≥ 44、计算字号 ≥ 16px、包围盒在抽屉水平范围内；展开后每个选项（portal 里的 `[role=option]`）高 ≥ 44、列表盒在视口内；选中另一个时次后触发器显示该时次（控件仍可用）。基线 mock 只有一个起报时次：多时次用既有的 `installMultipleIssueTimes`（`e2e/support/curveSheet.mocked.ts`，按它在 `m11-curve-sheet.mobile` 里的用法与路由注册顺序）；
    - (c) 390×664 气象代站窗：五个要素切换项各自高 ≥ 44、宽 ≥ 44、包围盒在抽屉水平范围内，彼此不重叠；轻触另一个要素后它成为选中项；
    - (d) 换行：切换项独占一行后，390×664 与 320×480 下五项预期排成一行（估值：五项合计约 270px）；折行要在更窄的移动视口上证明——实现者实测选一个确实折成两行的视口（预期 280×653 一类；硬前提：至少出现两个不同的行顶坐标），断言每项高 ≥ 44、宽 ≥ 44、在抽屉水平范围内、任意两项不重叠。找不到会折行的移动视口就如实报告并保留“不重叠 + 在水平范围内”的断言；
    - (e) 触控审计：390×664 与 750×342，河段窗与气象代站窗——抽屉内每个可见的可交互控件（`button`、`a[href]`、`[role=tab]`、`[role=combobox]`、`select`、`input`）包围盒 ≥ 44×44；审计到的控件数须 > 0 并在失败信息里列出不达标者；
    - (f) 表单字号审计：同上两个视口，抽屉内每个可见的 `select` 与选择触发器计算字号 ≥ 16px；
    - (g) 750×342：时次足够多（≥ 8 个，mock 放新的 support 文件；硬前提：列表 `scrollHeight > clientHeight`）时展开起报时次，列表盒在视口内，最后一个选项可滚入列表可视区并可选中；
    - (h) 口径 (4)：390×664 气象代站窗——图表区高 ≥ 160、不滚动就在抽屉盒内，且图表区底边在主体可视底边之上 ≥ 2px；报告主体的 `scrollHeight − clientHeight`。
  - Risk pack「Legacy compatibility」selected -> 同一 spec 文件里不能放桌面用例（`.mobile.` 文件只在移动 project 跑）：桌面对照放新文件 `e2e/m11-sheet-controls-desktop.mocked.spec.ts`（桌面 project，1280×900）：(i) 两种窗的关闭按钮、起报时次触发器（高与字号）、要素切换项的包围盒与字号等于改动前实测的字面值。
  - 未选：Public API（无接口 / 契约面变化）、Concurrency、Error handling、Auth、File IO、Schema、Config、Documentation（design.md D13 与规格已写明）。
  - Non-goals：共享选择器基础组件（6.1）；320 宽及更窄的抽屉里 16px 触发器被压窄后时次文本是否被裁（规格只钉 390；实现者量一下写进范围外观察）；`M11PopupSourceControls` / `M11PopupShell`（无调用方）；抽屉头部的排版；图表；加载中 / 空态分支里的控件；自动平移（4.10）；桌面形态的任何变化；真机手感（#2818）。
  - Review focus：(1) 新类全部带移动前缀，桌面字面值不变；(2) 审计用例确实枚举到控件（数量 > 0）且没有豁免名单；(3) 口径 (4) 没有靠放宽 4.5 的 (a) 达成，(e) 只换了视口；(4) 选项的尺寸量的是展开后的真实选项节点；(5) 三个移动 project 都断言、无跳过、无 `force`；(6) testid / aria / DOM 顺序不变。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；新 spec 的 strict `tsc --noEmit` exit 0；改动前基线表（未改动的 `src/`）：390×664 / 750×342 / 1280×900 下两种窗的关闭按钮、触发器、选项、切换项的包围盒与字号，390×664 气象代站抽屉的工具条高、图表区高、主体 client / scroll；改动后同一张表；先红后绿——(a)(b)(c)(e)(f) 改动前红，(h)(i) 改动前可能已绿，须点名；`--repeat-each=5` 无 flaky（`--workers=1`）；变异：去掉关闭按钮的移动类 -> (a)(e) 红；去掉触发器的移动高度 / 字号 -> (b)(e)(f) 红；去掉选项的移动高度 -> (b) 红；去掉切换项的移动高度 -> (c)(e) 红；去掉切换项容器的独占一行 -> (h) 或既有 4.5 的 (a) 红；把移动类去掉前缀（桌面也变大）-> (i) 红；工具条不收紧间距 -> (h) 或既有 4.5 的 (a) 红。`git diff --stat --diff-filter=MDR origin/master -- apps/frontend/e2e 'apps/frontend/src/**/__tests__/**'` 期望至多 `m11-station-sheet-chart.mobile.mocked.spec.ts`（(e) 的视口；实测 (e) 仍成立就不改）。改展示端运行时代码，合并前出 node-27 live receipt（PR 的生产构建对 live API）：桌面布局 oracle exit 0；390×664 与 750×342 经门控钩子打开河段窗与气象代站窗，抽屉内可交互控件全部 ≥ 44×44、触发器字号 ≥ 16px；390×664 气象代站图表区 ≥ 160 且不滚动就在抽屉盒内；1280×900 下控件尺寸与改动前相同。

- [x] 4.10 抽屉打开时自动平移地图：移动形态下曲线抽屉打开、一种窗替换另一种窗、抽屉开着时在底部抽屉与右侧抽屉布局之间切换时，地图平滑移动（缩放不变），做一次带像素偏移的相机移动（向上偏移半个抽屉高度，或向左偏移半个抽屉宽度；不使用持久的相机内边距），使锚点落在未被抽屉遮住的地图区内。平移只在这三个触发点各发生一次：用户在抽屉打开期间手动移动地图后不拉回，但之后的替换或布局切换仍会再平移一次。关闭抽屉、离开移动形态都不移动地图；桌面形态不平移。地图容器暴露 `data-selected-anchor-x` / `data-selected-anchor-y`（两种形态都暴露；桌面双窗并存时取活动窗的锚点；无选中要素时不带）。页面的气象代站弹窗状态需要多记站点要素的经纬度作为锚点；河段沿用已有的弹窗锚点。新增 `e2e/m11-sheet-auto-pan.mobile.mocked.spec.ts` 与 `e2e/m11-curve-anchor-desktop.mocked.spec.ts`（桌面 project）。

  Depends on: 4.2

  Verify：移动 spec 断言三个移动 project 下河段窗与气象代站窗打开并等相机静止后，锚点坐标在地图区内且在抽屉之外（底部抽屉之上 / 右侧抽屉之左）；390×664 → 750×342 后锚点在右侧抽屉之左；关闭抽屉后地图容器不带两个锚点属性。桌面 spec 断言 1280×900 下打开河段窗后锚点坐标与点击点相差不超过 1px，双窗都开且气象代站窗为活动窗时属性给出站点锚点。vitest 断言：平移不改变缩放级别；抽屉打开期间用户移动地图后不触发平移；手动移动后再切换布局会平移一次；关闭抽屉与离开移动形态都不触发相机移动；河段 → 站点替换时以新锚点再平移一次。

  **Suggested fixture level:** expanded - 改动共享地图组件的相机行为与页面级弹窗状态，并在地图容器上新增属性。

  **Minimal mergeable slice:** atomic - 平移、触发条件与可观测的锚点属性互为前提：没有属性无法在真实布局下断言平移结果，没有平移属性就只是点击点的回显。

  Triage（#2811）：Issue type: feature ｜ Fixture level: expanded（与上游建议一致：改共享地图组件的相机行为与页面级弹窗状态，在地图容器上新增只读属性；触发条件是一个小状态机。设计见 design.md D17（含本 task 补的「实现期裁定」），规格见 `specs/mobile-curve-sheet` 的「Opening a sheet pans the selected feature into view」）｜ Blast radius: 单图展示页的地图相机——触发条件写错会让地图在用户没要求时移动（桌面开窗时、关抽屉时、手动拖动后被拉回、每次重渲染都平移），或改掉缩放级别；锚点取错会把地图移到被替换掉的那个要素上。
  - **⚠ 未经用户确认的优先级**（issue 的 needs-follow-up）：「平移只在触发点各发生一次；手动移动后不拉回，但之后换选要素或切换布局仍会再平移一次」是上游编排者补定的，本 task 照此实现并在 PR 里显著标注；它在实现里只对应“触发键”一处，日后改口径成本很小。
  - Governing invariant：本功能对相机的全部影响 = **“触发键”从任意值变成另一个非空值时做一次 `easeTo`**。触发键为空（没有选中锚点、不在移动形态、曲线区域处于兜底）的任何转移都不产生相机调用；触发键不变的任何重渲染都不产生相机调用；本功能从不设置 zoom / bearing / pitch，也从不使用持久的相机内边距（`setPadding` / `padding` 选项）。第二道闸：平移时刻地图区里必须**真的渲染着抽屉**（DOM 实测），否则不平移——曲线面板首次渲染即崩溃时，兜底状态要到提交阶段才报给页面，那一次提交里触发键仍是非空的，挡住平移的只有这道闸。因为没有任何“跟随”状态，用户手动移动地图后既不会被拉回，也不需要去检测“用户动过地图”。
  - Change surface：
    - `apps/frontend/src/pages/OverviewPage.tsx`：`stationPopup` 多记锚点经纬度；`handleMapOverlayClick` 的气象代站分支取锚点；由**收敛后的状态**算出“选中锚点”与“触发键”并经 `M11FullscreenMap` 传给地图组件。
    - `apps/frontend/src/components/map/M11MapLibreSurface.tsx`（及 `m11MapRuntime.tsx` / `m11MapSelection.tsx` 里与相机、选中属性相邻的位置，文件划分由实现者定）：收新 prop、做一次性平移、订阅相机静止事件、渲染新属性。
    - 测试：新 spec `e2e/m11-sheet-auto-pan.mobile.mocked.spec.ts`、`e2e/m11-curve-anchor-desktop.mocked.spec.ts`；新 vitest（页面级与地图组件级各一个文件）；`src/test/maplibreStub.tsx` 的假地图没有 `easeTo` / `resize` / `project` 等方法，需要时在**新测试文件内**自建假地图，不改共享桩的既有行为（确需给共享桩加可选能力时只做加法并写进偏离记录）。既有页面级 vitest 里对地图组件的桩忽略未知 prop，预期不需要改；需要改就停下报告。
    - 不改 `M11DraggableCurveWindow.tsx`、两个曲线面板、`useMobileForm.ts`、e2e 的既有 support 文件与既有定位钩子（`src/lib/riverClickEvidence`、`src/lib/stationLocateEvidence`）。
  - 口径（裁定）：
    - (1) 锚点：河段 = 点击时已记录的弹窗锚点（`riverPopup.lngLat`，即点击点的经纬度，不是河段中点）。气象代站 = 被点中的站点要素的 Point 几何坐标；要素没有 Point 几何时退回点击事件的经纬度；两者都没有时窗照常打开，但没有锚点（不平移、不带锚点属性）。
    - (2) 选中锚点（两种形态同一规则）：只有一种窗开着 → 它的锚点；两种都开着 → 活动窗（`activeCurveWindow`）的锚点。移动形态下“一种窗替换另一种窗”有一个两窗同时非空的未绘制中间提交（#2802 的携带备注），按本规则该提交里选中锚点已经是存活的那个窗的，所以替换只产生一次触发键变化——不得出现先朝被替换者平移、再朝存活者平移。
    - (3) 触发键 = 移动形态 && 有选中锚点 && 曲线区域不在兜底（`curveRegionInFallback` 为假）时，由「遮盖侧（底部 / 右侧）+ 选中锚点的身份（窗种类 + 要素 id + 锚点经纬度）」组成的字符串；否则为空。形态与遮盖侧一律用**页面那一份** `useMobileForm()` 的值（各组件各有一份订阅，可能不在同一个提交里翻转）。由此得到的触发点：打开抽屉；替换（含同一种窗内换选另一个要素、同一要素换了点击点）；竖屏 ↔ 矮视口横屏；窗开着时从桌面形态进入移动形态（窗变成了抽屉）；曲线区域兜底后「重试」成功（抽屉重新出现）。后三类原规格没有单列（原文是“只在三个时刻移动”），编排者裁定按“抽屉出现 / 选中锚点变化”处理，**规格增量已同步改写**（本 task 的 PR 一并提交）：SHALL 改为“抽屉出现或重新出现、抽屉开着时选中锚点变化、遮盖侧切换”，并各补一条 Scenario。不触发：关闭、离开移动形态、进入兜底、任何键不变的重渲染、用户手动移动地图。
    - (4) 平移：`easeTo`，目标是选中锚点，带像素偏移，使锚点落在**未被抽屉遮住的地图区的中心**——底部抽屉：向上偏移半个抽屉高度；右侧抽屉：向左偏移半个抽屉宽度。时长不超过既有相机动画的 450ms。抽屉尺寸只取真实渲染出来的抽屉的包围盒（DOM 实测），不按尺寸规则推算（推算值在兜底时也非零，会绕过上面的第二道闸），不得另抄一份数字；平移时刻没有渲染出抽屉就不平移。布局切换与进入移动形态时地图画布自己也在变尺寸，而地图库的 `resize` 经 ResizeObserver 异步触发且有约 50ms 节流；`easeTo` 在起点就把目标屏幕点冻结为“当时的画布中心 + 偏移”，动画中途的 `resize` 既不停止动画也不重算这个点。所以默认机制：**平移前先同步调一次 `map.resize()`**（空闲时幂等），再量抽屉、再 `easeTo`；实现者实测后可换等价机制，判据是 (e)(f)(g2) 的“位于未遮盖区中心”稳定通过。
    - (5) 锚点属性：`data-selected-anchor-x` / `data-selected-anchor-y` 加在 `m11-map-surface` 上，值为选中锚点在当前相机下的**视口** CSS px（地图画布的视口原点 + `project` 结果，至多两位小数），两种形态都带；选中锚点变化时立即更新，相机静止（`moveend`，含画布尺寸变化引起的）后更新；没有选中锚点时两个属性都不存在。地图实例尚未就绪时不带、不报错。订阅机制（裁定）：经 react-map-gl `<Map>` 的 `onMoveEnd` / `onLoad` 这类 prop，不直接在地图实例上 `on` / `off`；对实例方法（`project`、`easeTo`、`resize`、`getCenter`、`getZoom`、`getCanvas().getBoundingClientRect`）一律做存在性判断，缺了就跳过——既有约 15 个 vitest 文件渲染真实地图组件并配共享桩的假地图（没有这些方法，`getCanvas()` 多数只返回 `{ style }`，其中几个还会真的开窗），它们必须不改通过。
    - (6) 为可测性新增（规格属性清单之外，写进偏离记录）：`m11-map-surface` 上的 `data-camera-center`（`"lng,lat"`，6 位小数）与 `data-camera-zoom`（4 位小数），相机静止后更新、两种形态都带，**只在 `window.__NHMS_E2E_HOOKS__ === true` 时输出**（mocked 车道与 node-27 receipt 都开着这个门；生产 DOM 不增属性）。理由：规格的「Zoom is unchanged」「Closing does not move the map」「Leaving mobile form does not move the map」要在真实地图库上证明（假地图证明不了尺寸变化、内边距残留这类问题），而现有两个门控钩子按设计不暴露地图实例。不新增任何可写的钩子。
    - (7) 桌面形态：触发键恒为空，不平移；锚点属性照常暴露。
  - 实现期同步（以此为准，上文与此冲突处作废）：
    - 平移不在键变化的那次 effect 里同步做，而是**推迟到下一帧**（`requestAnimationFrame`；口径 (4) 允许的等价机制）。实测同步做时 (e) 两个方向、(f) 后半、(g2) 稳定红：键变化的那次提交里抽屉还是旧形态（390×664 → 750×342 量到全宽的底部抽屉，1280×900 → 390×664 量到桌面浮窗）——页面与抽屉各有一份 `useMobileForm()` 订阅，页面那份先翻。帧回调里的顺序：键仍是当前值 → DOM 里找到抽屉 → 同步 `resize()` → 量抽屉（零尺寸不平移）→ `easeTo`。
    - 由此多出一道闸：**帧回调时键必须仍是排这次平移时的键**。曲线面板首次渲染即崩溃时，挡住平移的实际是这道闸（到帧回调时键已变空）；“必须渲染着抽屉”的 DOM 闸仍保留作第二层，只由 (s7) 钉住——上文 Governing invariant 与 Review focus (3) 里“挡住平移的只有这道闸”不再成立，变异表里“去掉 DOM 闸 -> (h) 的前半红”也不成立（实测 (h) 不红，(s7) 红）。
    - 一帧之内键连续变化两次只平移一次，目标是最后一个键。标签页隐藏时帧回调不触发，平移要等页面可见。
    - 抽屉在 `ownerDocument` 里按 `data-m11-curve-window-kind` 查找，没有限定在地图区子树内（全页只有一个曲线窗容器）。
    - (c) 比上文多一条硬前提：相机中心确实变过。实测 (c) 的 750×342 河段在“不做平移”变异下也红。
    - 曲线区域兜底时选中要素仍在，锚点属性照常输出（规格只要求“无选中要素时不带”）。
  - Sibling surfaces（逐个核对，不只看改到的行）：`useM11MapCamera` 的 `fitTo`（流域边界点击的 `fitBounds`，450ms）与本功能的 `easeTo` 可能前后脚——后者打断前者是可接受的，但不得反过来被 `fitTo` 的 effect 因重渲染重放；站点聚合点击的 `flyTo`（会改缩放，不属于本功能，行为不变）；两个门控定位钩子在定位时 `fitBounds(duration: 0)`（e2e 里相机的起点是定位之后的状态）；`m11MapSelection.tsx` 的选中属性；单窗收敛的 layout effect 与 `chromeYielded`（4.6）——都读同一份页面形态；地图区域的 `RegionErrorBoundary`（地图崩溃重挂后相机回到初始视图；重挂后是否再平移一次取决于实现，两种结果都接受——Non-goal）；`src/main.tsx` 的 `StrictMode`（开发模式下 effect 双跑，平移须幂等：一次触发键变化只能有一次 `easeTo`；不在地图实例上 `on` / `off`）；既有页面级 vitest 对地图组件的七处桩；以及渲染真实地图组件 + 共享桩假地图的既有 vitest（组件级约 6 个文件、页面级约 9 个文件，如 `M11MapLibreSurfaceHook.test.tsx`、`OverviewPageLayerBasemapLaunchers.test.tsx`）；`handleMapOverlayClick` 里 `setActiveCurveWindow` 与 `setRiverPopup` / `setStationPopup` 在同一个回调里批处理——“替换只产生一次键变化”依赖这个耦合，不要拆开。站点锚点直接复用既有的 `popupAnchorFromInteraction`（`m11MapInteractions.ts`，已是口径 (1) 的实现）。
  - Must preserve：桌面形态开窗不移动地图（`m11-curve-window-desktop`、`m11-curve-window-min-size`、`m11-overlay-collision` 的 1920 / 1440 / 1280 / 800 不改通过）；全部既有移动 spec 不改通过——尤其 `m11-curve-sheet.mobile`（抽屉几何）、`m11-sheet-yields-chrome.mobile`（让位、暂停、兜底）、4.4 / 4.5 的图表 spec、4.7 / 4.8 的触屏 spec、4.9 的控件 spec（它们都在开窗后量抽屉内的东西，地图多平移一次不应影响；受影响就停下报告）；既有 vitest 全部不改通过；`m11-map-surface` 既有的 `data-*` 属性与取值不变；流域点击的 `fitBounds`、聚合点击的 `flyTo` 行为不变；两个门控钩子的签名与行为不变。
  - Risk pack「Concurrency / 状态机」selected -> vitest：
    - 页面级（新文件，照 `OverviewPageSingleCurveWindow.test.tsx` 的写法，在本文件里把地图组件桩成会渲染所收 prop 的元素；用 `installMobileFormMatchMedia` 驱动形态）：
      - (p1) 移动形态点河段 → 地图组件收到非空触发键与河段锚点；
      - (p2) 河段窗开着点气象代站（替换）→ 地图组件收到的触发键序列里**没有**任何一个由“被替换者的锚点”构成的新键，最终键对应站点锚点，恰好变化一次（记录每次渲染收到的键）；
      - (p3) 关闭 → 触发键为空、选中锚点为空；
      - (p4) 抽屉开着离开移动形态 → 触发键为空，选中锚点仍在；
      - (p5) 抽屉开着竖屏 → 矮视口横屏 → 触发键变化一次，遮盖侧为右侧；
      - (p6) 桌面形态：触发键恒为空；两窗都开时选中锚点 = 活动窗的锚点，激活另一个窗后换成另一个；
      - (p7) 桌面形态开着窗进入移动形态 → 触发键从空变为非空（一次）；两窗都开着进入移动形态 → 键只对应存活的活动窗；
      - (p8) 气象代站锚点来源：有 Point 几何用几何；没有几何退回事件经纬度；两者都没有 → 窗打开、无锚点、键为空；
      - (p9) 曲线区域兜底：断言**键序列**——首次渲染即崩溃时为 空 → K → 空（兜底状态在提交阶段才到页面，中间那个 K 是真实存在的），重试成功后 → K；
      - (p10) 移动形态河段窗开着，再点另一条河段（同一种窗内换选）→ 触发键变化恰一次，对应新锚点。
    - 地图组件级（新文件，假地图自建：`easeTo`、`resize`、`project`、`getCenter`、`getZoom`、`getCanvas`，并记录全部相机方法调用；`moveend` 经桩 `<Map>` 收到的 `onMoveEnd` prop 触发）：
      - (s1) 触发键从空变为非空 → `easeTo` 恰一次：`center` = 锚点、`offset` 符合遮盖侧与假抽屉尺寸，选项里**没有** `zoom` / `bearing` / `pitch` / `padding` 键；
      - (s2) 键不变的重渲染（其他 prop 变化）→ 无新增相机调用；
      - (s3) 键变为空（关闭 / 离开移动形态）→ 任何相机方法都没有被调用（`easeTo` / `flyTo` / `jumpTo` / `panTo` / `fitBounds` / `setCenter` / `setPadding` 全部为零新增）；
      - (s4) 键换成另一个非空值（替换 / 布局切换）→ 再恰一次，目标是新锚点 / 新偏移；
      - (s5) 假地图发出用户拖动后的 `moveend` → 无 `easeTo`；其后键变化 → 恰一次；
      - (s6) `StrictMode` 下挂载并走一遍 (s1) → `easeTo` 仍恰一次；
      - (s9) 假地图缺方法（只有共享桩那几样）且有选中锚点、键非空 → 不抛错、不带锚点属性；
      - (s10) 平移前调用了 `resize`（调用顺序：`resize` 先于 `easeTo`）；
      - (s7) 没有渲染出抽屉时键变为非空 → 不平移；
      - (s8) 属性：有锚点 → 两个锚点属性等于“画布视口原点 + `project`”；`moveend` 后按新的 `project` 更新；锚点为空 → 两个属性都不存在；测试门打开时 `data-camera-center` / `data-camera-zoom` 在 `moveend` 后等于假地图的值，门关着时两个相机属性都不存在。
  - Risk pack「Public API / entry」selected：用户可见的相机行为与只读属性 -> 新移动 spec（三个移动 project 都跑、无按 project 跳过、无 `force`；每条用例内 `setViewportSize`，不用大于 1280×900 的视口，单用例最多两个页面；读数一律轮询到满足，“不应变化”的判据在等够动画时长的数倍后再读；量具用包围盒，地图区 = `m11-fullscreen-map`，抽屉 = 曲线窗 frame）：
    - (a) 390×664 河段窗：相机静止后锚点在地图区内、在抽屉顶边之上，且位于未遮盖区的中心（横向 = 地图区中线，纵向 = 未遮盖区中线，各 ±4px）；`data-camera-zoom` 等于开窗前的值；
    - (b) 750×342 气象代站窗：锚点在地图区内、在抽屉左边之左，且位于未遮盖区中心（±4px）；缩放不变；
    - (c) 390×664 气象代站窗、750×342 河段窗：同上两条的对称组合（在未遮盖区内——与抽屉边的比较用严格不等号——且缩放不变）；
    - (d) 关闭：抽屉开着并静止后记下 `data-camera-center` / `data-camera-zoom`，关闭，等待后两者与关闭前相同，两个锚点属性都不存在；
    - (e) 旋转：390×664 开着河段窗 → 750×342，相机静止后锚点在地图区内、在右侧抽屉之左，且位于未遮盖区中心（±4px），缩放不变；反向（750×342 → 390×664）锚点在底部抽屉之上且位于未遮盖区中心（±4px）；
    - (f) 手动移动：390×664 抽屉开着，用触摸（`e2e/support/touchGestures.ts` 的 `dragOneFinger`；起点须满足 `elementFromPoint` 是地图画布）把地图向下拖到锚点落到抽屉顶边之下（硬前提：拖动后锚点的 y 大于抽屉顶边、`data-camera-center` 已改变），等待后锚点仍在抽屉之下（没有被拉回）；随后转到 750×342，相机静止后锚点在右侧抽屉之左且位于未遮盖区中心（±4px）；
    - (g) 离开移动形态：390×664 抽屉开着并静止后 → 1280×900，`data-camera-center` / `data-camera-zoom` 与切换前相同（地图库在画布尺寸变化时不改中心），锚点属性仍在；
    - (g2) 进入移动形态：1280×900 开着河段窗 → 390×664，相机静止后锚点在抽屉顶边之上且位于未遮盖区中心（±4px），缩放不变；
    - (h) 兜底：390×664 预置曲线崩溃开关后轻触河段 → 兜底块出现，等待后 `data-camera-center` 与轻触前相同（没有平移）；点「重试」（先清掉开关）→ 抽屉出现，相机静止后锚点在抽屉之上。
    - 浏览器里做不了的：移动形态下“河段窗开着再点气象代站”（站点定位点在抽屉之下，#2807 已有同样结论）——替换路径由 (p2)(s4) 覆盖。
  - Risk pack「Legacy compatibility」selected -> 新桌面 spec `e2e/m11-curve-anchor-desktop.mocked.spec.ts`（桌面 project，1280×900）：(i) 开河段窗：两个锚点属性与点击点相差 ≤ 1px，`data-camera-center` / `data-camera-zoom` 与点击前相同；(j) 双窗（顺序固定）：开河段窗 → 定位站点（钩子会移动相机），相机静止后记下此时的河段锚点属性 R2 → 点站点，断言属性 = 站点钩子给出的视口坐标（≤ 1px）→ 点一下河段窗使其成为活动窗，断言属性 = R2（≤ 1px）；(k) 关掉全部窗后两个锚点属性都不存在。
  - Risk pack「Error handling」selected：锚点缺失、地图未就绪、兜底 -> (p8)(p9)(s7)(h)。
  - 未选：Auth、File IO、Schema、Config、Resource limits、Release / dependency compatibility（不改依赖，只用 `easeTo` / `resize` 的公开选项）、Documentation（design.md D17 与规格已写明；本 task 给 D17 补「实现期裁定」并改写规格增量）。
  - Non-goals：缩放级别变化；桌面形态平移；持久相机内边距；“用户动过地图”的检测与任何跟随；地图崩溃重挂后的再平移；选中要素的高亮样式；抽屉关闭后把地图移回去；`prefers-reduced-motion` 的处理；全国缩放级别下点不中河段（design.md Open Questions 1）；真机手感（#2818）。
  - Review focus：(1) 触发键只由收敛后的页面状态与页面那一份形态值构成，替换时不出现朝被替换者的平移；(2) `easeTo` 的选项里没有缩放 / 方位 / 俯仰 / 内边距，全仓没有新增 `setPadding`；(3) 键为空的转移零相机调用（关闭、离开移动形态）；进入兜底不平移（键为空，或首次崩溃那一提交里靠“必须渲染着抽屉”的闸）；(4) `StrictMode` 下一次键变化恰一次 `easeTo`，不在实例上 `on` / `off`；平移前的同步 `resize` 会在 effect 内部同步发一次 `moveend`（重入）——它不得被当成触发，也不得造成第二次 `easeTo`；(5) 属性是视口坐标、相机静止后更新、无锚点时不存在；(6) 布局切换时以新画布尺寸为准（(e) 的两个方向）；(7) 桌面形态零相机调用；(8) 新增的两个相机只读属性没有被产品逻辑读取；(9) 既有相机调用（流域 `fitBounds`、聚合 `flyTo`）与门控钩子不变；(10) 三个移动 project 都断言、无跳过。
  - Evidence floor：约定的本地验证命令 + `check:types` + 治理门测试全绿；新 spec 的 strict `tsc --noEmit` exit 0；先红后绿——对 `origin/master` 的 `src/`：移动 spec 与桌面 spec 全红（属性不存在），其中哪些断言在“属性已存在但不平移”时仍会红，由下面的变异给出；vitest (p1)–(p10)、(s1)–(s10) 改动前红（(s9) 是防御性用例，改动前可能已绿，须点名）；`--repeat-each=5` 无 flaky（`--workers=1`），(e)(f)(g2)(h) 另跑 `--repeat-each=8`；变异：不做平移 -> (a)(b)(e)(f 的后半)(g2)(h 的后半)、(s1)(s4) 红（(c) 的 750×342 河段在这个变异下只差一个边界值，是否变红须点名）；偏移取反或取整个抽屉尺寸 -> (a)(b)、(s1) 红；触发键里去掉遮盖侧 -> (e)(f)、(p5)(s4) 红；触发键不看形态（桌面也平移）-> (i)、(p6) 红；关闭时把地图移回 -> (d)、(s3) 红；`easeTo` 带上 `zoom` -> (s1) 红（浏览器里若缩放值恰好相同则 e2e 不红，须点名）；选中锚点在两窗并存时取“非活动窗” -> (j)、(p2)(p6) 红；键不排除兜底 -> (p9) 红；去掉“必须渲染着抽屉”的闸，或抽屉尺寸改按规则推算 -> (s7)、(h) 的前半红；平移前不 `resize` -> (s10) 红（e2e 的 (e)(g2) 是否变红取决于时序，须如实报告）；属性不在 `moveend` 后更新 -> (a)–(f)、(s8) 红；每次渲染都平移（键比较失效）-> (f 的前半)、(s2)(s5) 红。`git diff --stat --diff-filter=MDR origin/master -- apps/frontend/e2e 'apps/frontend/src/**/__tests__/**' apps/frontend/src/test` 期望为空（既有 vitest 与共享桩一个不改）。改展示端运行时代码，合并前出 node-27 live receipt（PR 的生产构建对 live API）：桌面布局 oracle exit 0；390×664 与 750×342 经门控钩子打开河段窗与气象代站窗，相机静止后锚点在未遮盖区内、缩放不变；关闭后中心与缩放不变、锚点属性消失；390×664 → 750×342 后锚点在右侧抽屉之左；1280×900 开河段窗后锚点与点击点相差 ≤ 1px、相机不动。

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

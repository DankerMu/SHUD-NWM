# Tasks

- [x] 1.1 三个组件接上尺寸跟随：`QueueDonut` 恒渲染，传 `true`；`TrendLine` 与 `StageDurationBar` 在空数据时提前 return——hook 调用必须放在该 return 之前（Hooks 规则），入参就是同一个“有数据”条件，使空数据时观察器断开。函数型 `onChartReady` 交给 `ReactEChartsCore`。
- [x] 1.2 vitest（仿 `src/components/charts/__tests__/ForecastChartFillResize.test.tsx` 与 `src/components/map/__tests__/M11StationForcingPopupChartResize.test.tsx` 的做法：mock 封装库抓 props，调用 `onChartReady(假实例)`，用可控 `ResizeObserver`）：三个组件各自——画布宽 ≠ 容器宽时触发 -> `resize({ width: 'auto', height: 'auto' })` 被调用；尺寸一致 -> 不调用；卸载 -> `disconnect`；`TrendLine` / `StageDurationBar` 从有数据变为空数据 -> 不渲染图表且观察器已断开。
- [x] 1.3 mocked e2e（新 spec，文件名带 `mocked`、不带 `.mobile.`，只进桌面 project；只测 `/ops`，不对 `/monitoring` 参数化——同一个页面组件）。**这是守卫，不是保证能红的测试**：竞态窗口（图表初始化完成到绑定后 60ms）从页面外部无法确定性命中，改动前 0 红是可接受结果，判别力来自 1.2 与变异。
  - 两条各自重新加载的用例：一条“变窄”、一条“变宽”，改视口是页面加载完成后的第一个动作（只有第一次尺寸回调会被吞，同一用例里的第二次改视口恒绿）。
  - 视口对写死为同属单列布局的 750×342 与 390×664（变窄：750×342 加载 -> 390×664；变宽：反向）。不用默认的 1280：宽 ≥ 1200 时阶段列 / 趋势列是固定宽，容器不随视口变；也避开桌面 768–899 的单列带（见 Non-goals）。
  - 受控 mock 下页面恰好 4 张图（环图 1、趋势 2、阶段时长 1，阶段时长图恒渲染、不需要展开），数量写死为 4。容器取 `.echarts-for-react` 根节点，canvas 宽取 CSS 宽（`getBoundingClientRect`），不取 `canvas.width` 属性（那个乘了 DPR）。
  - 改视口前断言：4 张 canvas 已存在且各自宽等于容器 `clientWidth`，记下各容器宽；改视口后轮询到每张 canvas 宽等于其容器 `clientWidth`，并断言每个容器的 `clientWidth` 都与改前不同（否则不需要任何重排断言也成立），页面滚动容器 `scrollWidth <= clientWidth`（元素级，不用文档级）。
  - `e2e/support/opsFallback.mocked.ts` 只有滚动容器的量具，没有 canvas 量具：新增的量具放进该文件或新 spec 内；放进 `support/` 会被 `src/__tests__/mobileSpecOracles.test.ts` 扫描（元素级 `scrollWidth` 不触发它）。
- [x] 1.4 `visitOpsFallbackPage` 的注释：不删除，改写为“#2860 已让三类图表自己跟随容器；既有 spec 仍重新加载，是为了不依赖改视口的时序”。既有 spec 的访问方式不动；`e2e/ops.mobile.mocked.spec.ts` 里指向该说明的那一行注释允许同步改措辞，断言与结构不动。

## 约定

- Risk pack「Concurrency / shared state / ordering」selected：这是时序竞态（首个尺寸回调被吞）-> 1.2 的可控 `ResizeObserver` 用例钉住机制；1.3 钉住真实浏览器里的结果。改动前 1.3 可能 0 红或偶现红——如实报告红的次数（在未改的 `src/` 上跑 `--repeat-each=10`），0 红可接受；不为制造红改产品代码或给测试加人为延迟。
- 未选：Public API、Config、File IO、Schema、Auth、Resource limits、Legacy compatibility、Error handling、Release / dependency、Documentation。
- Must preserve：`ForecastChartFillResize.test.tsx`、`M11StationForcingPopupChartResize.test.tsx`、`e2e/ops.mobile.mocked.spec.ts`、`e2e/ops-fallback-desktop.mocked.spec.ts`、`e2e/monitoring.mocked.spec.ts` 不改通过；三张图的固定像素高度、配置与 DOM 结构不变（hook 同时比较宽高，固定高度下只有宽度不一致会触发重排）。
- Non-goals：桌面 768–899 宽的单列带里卡片可能被旧画布撑住不缩（隐式 auto 列、卡片无 `min-w-0`，属布局问题，未在浏览器验证——写进 PR 的范围外观察）；hook 内部逻辑；`ForecastChart` 的非 `fill` 分支（唯一调用方 `ForecastPanel` 运行时不可达）；`/ops` 移动形态的其他遗留项；开发用角色切换器的遮挡（#2862）。
- Evidence floor：
  - 1.2 先红后绿：对未改的三个组件，新用例因 `onChartReady` 为 undefined 而红；改后绿。
  - 变异（按 sha256 还原）：三个组件各自不传 `onChartReady` -> 该组件的用例红；`TrendLine` 与 `StageDurationBar` 各自把 hook 入参写死 `true` -> 各自的“空数据断开”用例红（用例须用 rerender 到空数据，而不是 unmount）。
  - 1.3 在未改 `src/` 上 `--repeat-each=10` 的红 / 绿次数；改后 `--repeat-each=5` 全绿（桌面 project）。
  - `cd apps/frontend && pnpm test && pnpm typecheck && pnpm check:types && pnpm build`；新 spec 由 Playwright 运行时加载通过（仓库脚本的 tsc 配置不含 e2e spec，不另造命令）；完整 mocked 车道；`src/__tests__/mobileSpecOracles.test.ts` 仍绿（新 spec 用的 `scrollWidth` 是元素级滚动容器，不是文档级）。
  - `openspec validate ops-charts-follow-container --strict --no-interactive`。
  - node-27（编排者执行，PR 构建对 live API）：桌面 oracle exit 0；`/ops` 在只读身份下可达时，加载后改视口（变窄 / 变宽），各图表 canvas 宽等于容器宽、页面不横向溢出；不可达则如实记为未覆盖。

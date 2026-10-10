# Tasks

按 1 -> 4 的顺序做，每一项做完单独可验证。issue 的第 (5) 项（图层目录 mock 收敛）已在 fixture review 后事前拆出、另行立单（#2879），不在本 change 内。

- [x] 1.1 `e2e/m11-control-bar-landscape.mobile.mocked.spec.ts` 的旋转用例（“同一页面不重新导航”的那条）：`setViewportSize(LANDSCAPE)` 之后、任何几何量测之前，加 `await expect(controlBarParts(page).timeline.getByLabel('播放速度')).toHaveCount(1)`——竖屏结构下该选择器是控制条 section 的直接子节点、不在时间轴里，只有 `portrait` 翻成 false 并提交后才成立（旋转前为假已由用例里现有的 `expect(portrait.speedInsideTimeline).toBe(false)` 钉住，同一判据）。保留现有的 CSS `min-width` poll。不用 `toPass()` 包整段量测，不加 `waitForTimeout`。
- [x] 2.1 `src/__tests__/c4DisplayLane.test.tsx` 的 `does not accept headers-only, failed completion, or stalled jobs bodies`：注入 `clock`（`createRiverClickDeadline(<预算>, () => clock.now)`，假页面的 `waitForTimeout` 推进 `clock.now`，照同文件已有写法）。**预算必须大于注入时钟下完整成功路径的耗时**（home quiet 500 + GFS ops quiet 500 + IFS ops quiet 500 + 若干 50ms 轮询，读码推算约 1.6–1.7k，以实跑为准）——注入时钟后预算 25 三个用例都会落成 `STEP_TIMEOUT` / `home`；预算只够到 ops 故障点时，撤掉故障得到的也是 `STEP_TIMEOUT` / `ops`，分不清超时来自故障还是预算。判据：同一预算下撤掉注入的故障得到 `ok === true`。不必取 180000 的整轮上限。三个用例各 `toMatchObject({ code, stage })`，读码推出的预期（实现时先跑一次确认；与下表不符则停下报告，不迁就）：headers-only status -> `STEP_TIMEOUT` / `ops`；post-header logs failure -> `OPS_UNAVAILABLE` / `ops`；stalled jobs body -> `STEP_TIMEOUT` / `ops`。headers-only 与 stalled 的码和阶段相同，车道输出本身区分不了这两个用例——各加一条断言证明“带故障的那个响应在 ops 阶段被车道观察到”（stalled 用假页面现成的 `textCallCount`，headers-only 把 `finished` 包成 `vi.fn` 断言被调用）；这两条断言在成功路径上同样成立，只是到达证明，不是区分证明，测试里照此注明。不改车道代码。
- [x] 3.1 新增 `apps/frontend/playwright.m15-visual.config.ts`：`testDir` + `testMatch` 只匹配 `e2e/m15-visual-conformance.spec.ts`，`use.baseURL` + Desktop Chrome、`webServer` 与默认配置一致（含 `PLAYWRIGHT_TEST_BASE_URL` 分支；复用默认配置的命名导出，不复制一份会漂移的 webServer 块——默认配置没有可复用的导出就给它加，行为不变）。`package.json` 的 `test:e2e:m15-visual` 改为 `--config playwright.m15-visual.config.ts --workers=1`，**去掉 `--project=mocked-regression-chromium`**（新配置没有这个 project）。默认配置的两处 `testIgnore` 不动；`playwright.config.ts` 里“它有独立 runner”的注释改到与事实一致。`e2e/m15-visual-evidence.md`、`.github/workflows/`、`docs/governance/` 不动（它们调用的就是这条 script，script 名不变）。
- [x] 4.1 `e2e/monitoring.mocked.spec.ts`：`selectRole` 改为调用 `e2e/support/setRole.ts` 的 `setRole`（它收角色值键而不是显示名——调用点改传值键或在 spec 内加一个显示名到值键的映射；不改 `setRole.ts`）；`wheelPageToBottom` 里两个不使用 `node` 的回调改成无参。去掉 `force` 后若点击因遮挡失败：那是真实缺陷，停下报告，不把 `force` 加回去、不加滚动绕过。

## 约定

- Risk pack「Concurrency / shared state / ordering」selected：(1) CSS 与 React 提交的先后 -> 1.1 的结构判据；(2) 墙钟 -> 注入时钟。
- Risk pack「Legacy compatibility」selected：(3) script 名、默认车道的排除不变；(4) 切角色的可观察结果不变。
- 未选：Public API、Config（仅测试配置）、File IO、Schema、Auth、Resource limits、Error handling、Release / dependency、Documentation。
- Must preserve：除上面点名的改动外，所有既有断言与期望值不动；`pnpm run test:e2e:mocked-regression --list` 的测试集合在改动前后相同——`--list` 每行带 `file:line:col`，剥掉 `:line:col` 后比对（数量 + project + 标题），基线取本分支的 merge-base；`mobileSpecOracles.test.ts` 的下限常量不动。
- Non-goals：图层目录 mock 收敛（issue 第 (5) 项，#2879）；#2862 留下的“避让角色切换器的滚动步骤”清理（不在本 change 内，见另单）；产品代码；退役 M15 车道及其文档 / workflow；`curveSheet` / `sheetControls` 的 latest-product 响应体重复；issue-time 三个夹具的重复；lint / `noUnusedLocals`（#2863）。
- Evidence floor：
  - (1) 判别力：把新加的等待改成等一个竖屏、横屏结构下都成立的东西（或删掉它）不要求必红（原窗口未能复现）；要给出的是静态证明——该判据在竖屏结构下不成立（在旋转前断言其计数为 0 / 不可见，写进用例本身）。`pnpm exec playwright test e2e/m11-control-bar-landscape.mobile.mocked.spec.ts -g "<该用例标题>" --repeat-each=20` 三个移动 project 全绿。
  - (2) 变异（sha256 还原）：三个用例各把注入的故障撤掉（走成功路径 -> `ok === true`）-> 该用例红；三条都如此（预算大于成功路径耗时是这条变异成立的前提）。改前后都跑 `pnpm exec vitest run src/__tests__/c4DisplayLane.test.tsx`。
  - (3) `pnpm run test:e2e:m15-visual --list` ≥ 1 个测试且 exit 0；`pnpm run test:e2e:m15-visual` 通过；默认车道 `--list` 不含 `m15-visual-conformance`。PR 的 CI 不跑这条 script（手动 workflow），这项证据只有本地的，报告里写明。
  - (4) `git grep -n "force: true" apps/frontend/e2e` 无命中；`pnpm exec playwright test e2e/monitoring.mocked.spec.ts` 全绿。
  - 回归：`cd apps/frontend && pnpm test && pnpm typecheck && pnpm check:types && pnpm build`；完整 mocked 车道；仓库根 `uv run python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json` 的 `hard_gate_failing_count` 为 0；`openspec validate frontend-test-timing-and-mock-cleanup --strict --no-interactive`。
  - node-27：不适用（只改测试代码与测试配置）。“生产构建产物不变”不作先验结论，以实证为准：改动前（merge-base）与改动后各 `pnpm build`，比对 `dist` 下全部文件的内容 sha256（含 `index.html`）。Tailwind v4 会扫描 `apps/frontend` 下的测试文件，若仅 CSS 有差异，贴出差异并说明来源，由编排者判断是否需要 node-27 receipt。

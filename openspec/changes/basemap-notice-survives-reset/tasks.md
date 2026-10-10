# Tasks

## 1. 判别与复现（纯 e2e，独立可合）

- [x] 1.1 `m11-notices.mobile.mocked.spec.ts` 的 `openWithNoticeAndStatus`：保留 `installNoticeMocks` 的返回值，在状态条断言之前 `await expect.poll(() => mocks.failedBasemapTiles(), '<说明>').toBeGreaterThan(0)`；状态条断言的 message 带上当时的计数值。之后再红，日志直接区分“瓦片没被请求”与“请求了但状态条没亮”。`m11-control-bar-portrait.mobile.mocked.spec.ts:305-313` 已在状态条之后断言计数：把计数检查挪到状态条断言之前（同样用 `expect.poll`），其余不动。
- [x] 1.2 确定性序列用例（加在 notices spec，三个移动 project 都会跑，不另铺）：
  - 安装 `installNoticeMocks(page, { failBasemapTiles: true, holdBootstrap: true })`——沿用现有 `holdBootstrap`（挂 `/api/v1/basins`），不新增挂起选项。它有效的原因：bootstrap 第一阶段是 `Promise.allSettled([fetchBasins(), fetchLayers(null)])`，basins 不回，图层状态就不落；放行后 `resetKey` 变两次——`overlay.sourceId` 从空变为径流 source，随后 `validTime` 从 null 校正为具体时刻。
  - `openMap`；等 `failedBasemapTiles() > 0` 且状态条已亮；记 N0 = `failedBasemapTiles()`。
  - 放行前在页面里装一个 `MutationObserver`，记录 `m11-map-source-error` 此后是否缺席过（只看终态会被“清掉后又被父级瓦片的新 503 重新点亮”骗过）。
  - `releaseBootstrap()`；等“放行已生效”的两个独立信号都到：`[data-testid=m11-map-surface]` 的 `data-registered-overlays` 含径流叠加层，且 URL 出现 `validTime=`（最后一次 key 变化）；再让出至少两帧。记 N1 = `failedBasemapTiles()`。
  - 断言：状态条从未缺席，且终态为“底图服务暂时不可用”。N0 / N1 写进断言 message 或 `console.log`，不对它们做断言。
  - 机制判定：在**未修复**的产品代码上跑这条用例（三个移动 project，`--repeat-each=5`），按观察记录三分：
    - 曾缺席、终态不在（通常 N1 == N0）= 机制确认，进第 2 节。
    - 曾缺席、终态在（N1 > N0，被父级回退重新点亮）= 清空确实发生，仍进第 2 节。
    - 从未缺席 = 机制被否定：停在第 1 节，留下这条用例（此时是绿的回归用例），不动产品代码，报告结论。
    - 同一 project 内结果不一致 = 用例本身不确定，先修成确定的再判。
  - 清空语义本身的确定性 red-before 判据是 2.2 的 hook 单测；这条序列用例是集成证据。

## 2. 修复（仅当 1.2 判定机制确认）

- [x] 2.1 `useM11MapSourceError`（`m11MapRuntime.tsx`）改为两份状态：
  - 底图提示（布尔）：底图 source 的 error 置真；只在 `basemap` 变化时清空。
  - 业务图层错误（字符串或 null）：非底图、非 glyphs 的 error 置值；随 `resetKey` 变化清空（现状）。
  - 对外仍返回 `{ mapSourceError, handleMapError }`，`mapSourceError` = 业务错误 ?? （底图提示 ? `M11_BASEMAP_UNAVAILABLE_NOTICE` : null）。业务错误清空后若底图提示仍为真，重新露出底图提示（现状是两者都丢）。
  - hook 需要 `basemap` 入参：签名改为接收 `resetKey` 与 `basemap`（或等价的最小改法）；`M11MapLibreSurface.tsx` 的调用点同步。`m11MapSourceErrorResetKey` 的组成不变。
  - glyphs 降级为 `console.warn` 的分支不变。
- [x] 2.2 hook 单测（`m11MapRuntime.test.tsx`）：
  - 底图 error -> `resetKey` 变化（`basemap` 不变）-> 提示仍在。
  - 底图 error -> `basemap` 变化 -> 提示清空。
  - 业务错误 -> `resetKey` 变化 -> 清空（现状，钉住）。
  - 底图 error + 业务错误 -> `resetKey` 变化 -> 露出底图提示。
  - 前两条与第四条在修复前红；现有三条用例只允许改 `renderHook` 的调用形状（因签名变化），断言不改。

## 约定

- Risk pack「Behavior regression」selected：业务图层错误的清空时机与“业务错误优先于底图提示”不变 -> 2.2 的第三条与现有两条优先级用例。
- Must preserve：状态条的 testid / 文案 / role / 位置与样式；`M11_BASEMAP_UNAVAILABLE_NOTICE` 文本；glyphs 分支；`m11MapSourceErrorResetKey` 的组成；mocked 车道 `retries: 0`；所有既有断言的期望值与超时数字。
- Non-goals：放大超时或加重试；业务图层错误的展示策略；天地图代理的限流 / 缓存；失败时上传 trace / `error-context.md`（报告，不做）；`m11-curve-sheet.mobile` (k)/(l) 的负载敏感性。
- Evidence floor：
  - 1.2 的判定记录（未修复代码上的结果，逐 project）。
  - 机制确认时：2.2 新用例与序列用例“修复前红、修复后绿”的输出；`pnpm exec vitest run src/components/map/__tests__/m11MapRuntime.test.tsx`。
  - `cd apps/frontend && pnpm typecheck && pnpm test && pnpm build`。
  - `pnpm exec playwright test e2e/m11-notices.mobile.mocked.spec.ts e2e/m11-control-bar-portrait.mobile.mocked.spec.ts --repeat-each=20`（三个移动 project）全绿；完整 `PLAYWRIGHT_WORKERS=4 pnpm run test:e2e:mocked-regression` 全绿；`pnpm run test:e2e:mocked-regression --list` 只比 merge-base 多出新增用例。
  - 治理 hard gate `hard_gate_failing_count` 为 0（`uv run python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json`）。
  - `openspec validate basemap-notice-survives-reset --strict --no-interactive`。
  - node-27：改了产品代码时，PR head 构建经校验 checkout 的预览端口对 live API 跑证据脚本两个移动预设 + 桌面 oracle（编排者执行）；只交付第 1 节时不适用。

# Tasks

盘点基于 fixture review 的读码结果（master @ `d315b1e87` 前后）；实现时先 `git grep` 复核，与下表不符则报告。

## 盘点（`apps/frontend/e2e`）

| 位置 | 条目形状 | 处理器 | 处置 |
|---|---|---|---|
| `support/zoomControl.mocked.ts` | 最小径流 | layers 回目录，其余 `[]` | 组 1（安装函数的原型） |
| `m11-baseline.mobile`、`m11-routes`、`m11-overlay-collision` | 最小径流 | 同上，外加显式 `basins -> []`（与兜底体相同） | 组 1 |
| `m11-shell-safe-area`、`m11-shell-viewport`、`m11-viewport-form` | 最小径流 | layers / 其余 `[]` | 组 1 |
| `support/siteHeader.mocked.ts` | 最小径流 | 另有 `runtime/config` 对象 | 组 2 |
| `m11-role-selector.mobile` | 最小径流 | 另有 `runtime/config`、计数副作用、本地 `json()` | 组 2 |
| `monitoring.mocked.spec.ts` | 最小径流 | `runtime/config` 走 `support/monitoring.mocked.ts` 的 `fulfill` | 组 2 |
| `support/legendLauncher.mocked.ts`、`support/opsEntry.mocked.ts` | 扩展径流 + 降水（`MOCK_LAYERS`） | 各自的处理器（计数 / `runtime/config`） | 组 2 |
| `support/controlBar.mocked.ts` | 降水条目 | 只有 `installFailClosedDischargeWithPrecipLegend` 的那条窄路由用到降水条目（`installFailClosedDischargeLayer` 不动） | 只换常量 |
| `support/riverWindow.mocked.ts` 的 `mockDischargeLayer`、`controlBar.mocked.ts` 由它派生的 `failClosedDischargeLayer` | 第三种径流（带瓦片模板与有效时刻）及其派生 | 自有路由 | **范围外，不动** |
| `m11-role-selector-desktop`（layers 回 `[]`）、`m11-role-selector-scrolling-pages.mobile`（复用支撑函数）、`support/monitoring.mocked.ts`、`preview-deeplink.spec.ts` | 无目录字面量 | — | 不动 |
| `m15-visual-conformance.spec.ts` | 最小径流 | — | 豁免（保持自包含；它本就被 mocked 车道排除） |

组 1 / 组 2 的所有响应都是 status 200、`contentType: 'application/json'`、信封 `{ status: 'ok', data }`，无额外 header。

## 任务

- [x] 1.1 复核上表，并为组 1 的每个调用点确认：它恰好只注册这一条 `page.route`（Playwright 后注册的路由先匹配——只有单路由的调用点换成共享安装函数才与顺序无关）。复核结果（含与上表的出入）进 PR。
- [x] 1.2 新建 `e2e/support/layerCatalog.mocked.ts`（文件名带独立 token `mocked`；只用相对 import，类型按现有支撑文件的写法 `import type { components } from '../../src/api/types'`），导出：
  - 最小径流条目——**故意不合 `Schemas['Layer']`**（缺 `tile_format`、`fallback_available`、`release_blocking` 等若干必填字段）：不加 `satisfies`、不补字段，注释一句说明它是“只够让页面起来”的最小目录；补字段会改变十余个 spec 收到的目录。
  - 扩展径流条目（带 `tile_format`、`fallback_available`、`release_blocking` 等，逐字段逐键序照搬）、降水条目、`MOCK_LAYERS`、`MOCK_PRECIP_LEGEND`（从 `legendLauncher.mocked.ts` 搬入，原文件转导出；新文件不反向 import `legendLauncher`）——这几项保留 / 加 `satisfies`。
  - 宽路由安装函数：`zoomControl.mocked.ts` 现有处理器的逐字搬移（layers 回最小径流目录，其余 `/api/v1/**` 回 `[]`）。
- [x] 1.3 替换：
  - 组 1：改为 `await` 调用共享安装函数（`mockZoomControlApi` 保留导出名，内部委托或转导出）；显式 `basins -> []` 分支随之消失（与兜底体相同）。
  - 组 2 与 `controlBar`：只把条目字面量换成 import 的常量，处理器、副作用、注册位置与顺序原样保留；不得改成“共享安装函数 + 叠加一条窄路由”。
  - 各支撑文件对外的导出名保留，调用方不改名。
  - 有任何字段差异、拿不准是否等价的那一处：保留原样并在报告里列出。
- [x] 1.4 等价证据（脚本放 scratchpad，不进仓库）：
  - 字节比对：对上表每一处，改前字面量与改后 import 值（条目与整个响应信封）`JSON.stringify` 相同。改前的值从 `git show origin/master:<path>` 取。
  - 类型证据：scratchpad 里一次性 tsconfig（include 新文件与被改的支撑文件 / spec，`tsc --noEmit`）——仓库的 `typecheck` / `check:types` 不覆盖 `e2e/support/**`，不能当证据。
  - 可选加强（头 5 分钟确认可行再做，不可行就跳过并写明）：用 `--trace on --output <scratchpad>` 跑组 1 的 spec，改前改后比对 trace 里 `/api/v1/**` 响应的 pathname / status / body。

## 约定

- Risk pack「Legacy compatibility」selected：每个 spec 收到的响应不变 -> 盘点 + 单路由确认 + 字节比对 + 完整车道。
- Must preserve：所有断言与期望值；`e2e/m15-visual-conformance.spec.ts` 不动；不删支撑文件；`src/__tests__/mobileSpecOracles.test.ts` 的下限常量不动；`pnpm run test:e2e:mocked-regression --list` 剥掉 `:line:col` 后的测试集合与 merge-base 相同。
- Non-goals：`riverWindow.mocked.ts` 的径流条目及其派生；四处重复的 runtime-config 对象；`curveSheet` / `sheetControls` 的 latest-product 响应体、issue-time 夹具的重复；产品代码；为减少 `broad-e2e-api-mock` 条数做任何额外的事（条数变化是副产物，如实报告）。
- Evidence floor：
  - 1.1 复核表、1.4 字节比对与类型证据。
  - `git grep -c "metadata: { layer_id: 'discharge', valid_times: \[\] }" apps/frontend/e2e` 只剩 `layerCatalog.mocked.ts` 与 `m15-visual-conformance.spec.ts`（外加列明的例外）；`MOCK_LAYERS`、扩展径流、降水条目各只定义一次。
  - `cd apps/frontend && pnpm typecheck && pnpm check:types`（照跑，但不作为新文件的类型证据）；`pnpm test`（含 `mobileSpecOracles`）；完整 mocked 车道 `PLAYWRIGHT_WORKERS=4 pnpm run test:e2e:mocked-regression`；`--list` 比对。
  - 仓库根：`uv run python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json` 的 `hard_gate_failing_count` 为 0，并报告 `broad-e2e-api-mock` 条数改前 / 改后（静态预计 20 -> 14）；`uv run pytest -q tests/test_entropy_audit_baseline_writer_summary.py` 通过。
  - `openspec validate e2e-layer-catalog-single-source --strict --no-interactive`。
  - node-27 不适用（只改 e2e 支撑代码，生产产物不受影响；dist 比对可选）。

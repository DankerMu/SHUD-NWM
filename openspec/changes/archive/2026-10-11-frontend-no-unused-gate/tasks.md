# Tasks

- [x] 1.1 先在未改配置的树上各跑一次并存下清单（scratchpad）：`pnpm exec tsc --noEmit -p tsconfig.app.json --noUnusedLocals --noUnusedParameters`、`pnpm exec tsc -p tsconfig.node-playwright.json --noUnusedLocals --noUnusedParameters`（后者按 `check:types` 脚本的实际参数形态）。
- [x] 1.2 逐条清零。规则（按 TS 5.9 实测行为）：
  - 未用的 import 说明符（含 `import type`）、非导出的局部变量 / 函数 / 类型 / 接口：删。导出的声明不在本门覆盖范围内，不动。只删 import 说明符时，同文件独立的 `export { … } from` 再导出保留。
  - 右值有副作用的未用绑定：保留调用、只去掉绑定（`f()` 或 `void f()`）。普通局部变量的下划线前缀**不**豁免（`const _x = f()` 仍报）。
  - 未用的解构成员：从解构模式里去掉。它是 rest 元素的兄弟（`const { a, ...rest } = obj` 且 `rest` 被使用）时 TS 本就不报，保持原样。确需保留一个不读的对象解构成员时，唯一豁免写法是重命名 `{ a: _a }`（简写 `{ _a }` 仍报）；数组解构用 `[_x, y]` 或留空位。
  - 未用的函数参数，分三类：
    - 导出函数，或签名由接口决定：改 `_` 前缀，位置不动，调用方不动。
    - 非导出函数且调用方全在同文件：删参数并同步实参，不论位置。
    - 回调 / mock 的参数：末尾未用的直接删；非末尾的改 `_` 前缀；删掉后 tsc 因推断签名变化而红的，改回 `_` 前缀。
  - 不用 `// @ts-ignore` / `@ts-expect-error` / 关掉某个文件的检查来消违规。
  - 点名条目（fixture review 逐条读码后的处置，行号以实现时为准）：

    | 位置 | 处置 |
    |---|---|
    | `playwright.c4-display-lane.ts:338` `capture` | 改成 `void <原表达式>`，**不得删语句**——右值是 fire-and-forget 的 promise 链，回写 `observed.bodyState` / `state.jsonBodies` |
    | `src/lib/riverClickEvidence/preflight.ts:468` `basinVersionId` | `_basinVersionId`（导出函数的末尾参数，约 30 处调用方都在传；不是漏掉的校验） |
    | `playwright.c4-display-terminal.ts:95`、`playwright.river-click-terminal.ts:153` `terminal` | `_terminal`（导出函数首参） |
    | `src/stores/overviewDataRequestPlan.ts:119` `query` | `_query`（导出函数首参） |
    | `src/lib/riverClickEvidence/hook.ts:241,242,299`（`startedAt`、`now`） | 删参数并同步同文件实参（`deadline` 已含这两个值） |
    | `src/lib/riverClickEvidence/config.ts:66` `name` | 删参数并同步同文件两处调用 |
    | `src/__tests__/c4DisplayOwner.test.ts:17` `parentDir` | 删参数并同步同文件 4 处调用 |
    | `src/__tests__/riverClickEvidenceOwner.test.ts:139` `malformed` | 删整条语句（与其后几行的调用逐字重复，不是“保留调用”的情形） |
    | `src/lib/riverClickEvidence/receipt.ts:461` `isUrlUserinfoFree` | 删（自引入起从未被调用；在用的 userinfo 拒绝在别处），报告里列出 |
    | `playwright.river-click-lane.ts:20,22`（`boundedMessage`、`TIMEOUT_SENTINEL`） | 只删 import 说明符；`export { … } from` 再导出保留 |
    | `playwright.river-click-lane-attempt.ts:335` | `const { page } = env` |
    | `riverClickEvidencePublisher.test.ts:292,305,545`、`riverClickLane.test.ts:89`、`riverClickTerminalPublisher.test.ts:246` | 按“回调 / mock”类处理（`:305` 的 `oldPath` 非末尾 -> `_oldPath`） |
    | `e2e/live-display.spec.ts:2,17` 三个未用的 type import（只在 node-playwright 配置下出现） | 删说明符 |

- [x] 1.3 两个 tsconfig 的 `compilerOptions` 加 `"noUnusedLocals": true, "noUnusedParameters": true`。
- [x] 1.4 反例证明门在拦：临时在一个 `src` 文件里加一个未用局部，`pnpm typecheck` 应以 TS6133 失败；撤掉后通过（不提交反例）。对 `check:types` 同样做一次（在一个 `playwright.*.ts` 里加未用局部）。

## 约定

- Risk pack「Behavior regression」selected：删声明不改行为 -> 1.2 的副作用规则 + dist 比对 + 全量 vitest + mocked 车道。
- Must preserve：所有测试断言；被删参数对应的调用方行为；`playwright.*.ts` 车道脚本的导出面（它们被 `src/__tests__` 的测试 import）；`tsconfig.json`（根，只有 references）不动；CI workflow 不动；开关写进两个 tsconfig 的 `compilerOptions`，`package.json` 的 `typecheck` / `check:types` 脚本字符串不改（有测试把它钉成精确字符串）。
- Non-goals：knip / eslint / biome；`e2e/**` 的类型门覆盖面（`tsconfig.node-playwright.json` 的 include 不扩）；为了“顺手”重构被触及的文件。
- Evidence floor：
  - 1.1 的改前清单与改后两条 tsc 命令 0 错；`pnpm typecheck`、`pnpm run check:types` exit 0；1.4 的反例输出。
  - `pnpm test` 全绿（用例数不少于 merge-base）；`pnpm build`。
  - merge-base 与改后 `dist` 按内容比对，逐条解释差异。预期有 JS 差异：`hook.ts`、`config.ts`、`receipt.ts` 在生产包内，删参数 / 删死函数会让含它们的块变小；`overviewDataRequestPlan.ts` 与 `preflight.ts` 只改参数名，压缩后应无差异。
  - 完整 mocked 车道 `PLAYWRIGHT_WORKERS=4 pnpm run test:e2e:mocked-regression`（单独跑，不并行其它重任务）；`--list` 集合与 merge-base 相同。
  - 被触及的 `playwright.*.ts` 车道脚本：对应的 `src/__tests__/*` 测试（riverClick* / c4Display*）通过即为证据，不需要真跑 live 车道。`e2e/live-display.spec.ts` 被 mocked 车道排除，它的证据是 `check:types` exit 0 加 `riverClickPhase2Closure.test.ts`、`riverClickLiveDisplayContract.test.ts` 的文本断言仍通过。
  - `openspec validate frontend-no-unused-gate --strict --no-interactive`。
  - node-27：预期有 JS 差异，编排者跑 live 守卫（两个移动预设 + 桌面 oracle；桌面 oracle 走河段点击取证路径，正好覆盖 `hook.ts`）。

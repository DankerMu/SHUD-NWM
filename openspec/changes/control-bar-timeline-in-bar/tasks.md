# Tasks

- [x] 1.1 滑块行行盒 = 16px。滑块行是 `m11-timeline-rows` 的第 2 个子节点（slider 的父节点，现为 `relative mt-2`，无 testid）；桌面形态下它现在高 21px，改后 16px，所有形态一起生效，`m11-timeline` 流高回到 64px。结构约束：input 仍是滑块行的直接子节点、轨道仍是滑块行的首子节点，不套包裹层（`e2e/support/touchAudit.mocked.ts` 用 `input.parentElement.firstElementChild` 取轨道）。做法由实现者实测择一；已知的推算（需实测确认）：行容器改 flex 可行（移动形态 input 相对行顶仍是 −14..30，中心差约 0）；input 改块级会让 `-mt-7` 与行的 `mt-2` 折叠、行盒变 44px；只改 `vertical-align` 行盒是 20 而不是 16。
- [x] 1.2 不传 cycle 的底行：桌面形态不折行；**竖屏移动形态保持现状的折行**（`m11MapRuntime.tsx` 里 320 宽 fail-closed 条高 187px 的推导与 `mobile:bottom-[15rem]` 依赖它），矮视口横屏保持不折行。写法即“默认不折行，`mobile:` 改回可折行，`mobile-landscape:` 不折行”，以三种形态的计算样式为准。桌面 768 宽时列很窄（推算约 82px，而不折行的 `Analysis / Forecast` 加间距约需 117px）：允许给底行的 span 加可截断（`min-w-0` / `truncate` 一类），不动禁用原因的 `max-w-48`；若实测列宽足够则不加，报告里给出数字。
- [x] 1.3 桌面 e2e（`e2e/m11-control-bar-desktop.mocked.spec.ts`）：
  - 1280×900 与 768×1024、有周期：`m11-timeline` 高 ≤ 64（容差 0.5）、顶边 ≥ 条顶边、底边 ≤ 条底边（容差 0.5）；滑块行（`m11-timeline-rows` 第 2 个子节点）高 16（容差 0.5）。不要量 input 自身（桌面下它本来就是 16px，恒真）。
  - 768×1024 + 无周期（fail-closed）：同样在条内；时间轴包围盒不与版权归属相交；底行两个 span 的右边不超过 `m11-timeline-rows` 的右边（容差 0.5），且都不与 `m11-control-bar-disabled-reason` 相交。
- [x] 1.4 移动 spec 按新实测收紧（硬要求，不是“如果允许”）：`e2e/m11-control-bar-landscape.mobile.mocked.spec.ts` 的 `MAX_TIMELINE_HEIGHT`（现 70）收到 64 加容差、`MAX_TIMELINE_OVERHANG`（现 3）收到 0 加容差；`e2e/m11-touch-audit.mobile.mocked.spec.ts` 的 `MAX_SLIDER_CENTRE_DELTA_PX`（现 3）收到 0.5 以内。没有任何既有上限需要放宽；若实测做不到上述收紧，按回退条件处理。
- [x] 1.5 订正会变假的注释，写成与实测一致：`M11Controls.tsx` 里“流高 64px 整”那段与命中区算术那段（依赖“行内替换元素的基线在边框盒底边”，1.1 拿掉了这个事实）；`M11BottomControlBar.test.tsx` 里的流高那句；`landscape` spec 里“约 69px / 既有的约 2.5px 行盒溢出”两处；`touch-audit` spec 里“实测恒为 −2.5px”。`m11MapRuntime.tsx` 的 187 / 227 推导：竖屏底行保持折行，滑块行矮 5px 后这两个数会变（预计 182 / 222，以实测为准），订正数字与余量说明，`mobile:bottom-[15rem]` 不改。

## 约定

- Risk pack「Legacy compatibility」selected。不动的字面量（一条不改通过）：`e2e/m11-control-bar-desktop.mocked.spec.ts` 既有断言（条高 64、宽 min(1024, 地图区宽 − 32)、居中、底距 40、条不压版权归属、控件 32px、字号 16 / 12、速度选择器在时间轴内且恰一个）；`e2e/m11-overlay-collision.mocked.spec.ts` 的条高 `toBe(64)`；`src/pages/m11/__tests__/M11BottomControlBar.test.tsx` 的 `h-16` 耦合、行数 3 / 3 / 3 / 2 与缺省外壳类名串；`M11BottomControlBarMobileForm.test.tsx`；`scripts/node27_display_v2_browser_evidence.mjs` 的 `CONTROL_BAR_HEIGHT = 64`。
- 会动的字面量（全部列在这里，别处不许动）：`landscape` spec 的 70 与 3；`touch-audit` spec 的上限 3 与 −2.5 注释；`m11MapRuntime.tsx` 注释里的 187 / 227。竖屏条高在 e2e 里没有等值字面量，只有下限 `> 64 + 0.5`（`portrait` spec 两处），改后仍成立。
- 移动形态必须不改而通过的断言：`e2e/m11-touch-audit.mobile.mocked.spec.ts` 的滑块段（中心差、`topHit` / `bottomHit` 为 `'self'`、44×44 审计）；`e2e/m11-control-bar-landscape.mobile.mocked.spec.ts` 的 `contains(timeline, slider)`；`e2e/m11-control-bar-portrait.mobile.mocked.spec.ts` 的“禁用原因在时间轴之下”；三个 spec 在三个移动 project 全绿。
- 回退条件：1.1 的修法使上面任一条移动断言变红，且无法用带移动前缀的小调整恢复——停下报告实测数字，不自行改成“仅桌面”分叉，也不改这些断言。
- 未选：Public API、Config、File IO、Schema、Auth、Concurrency、Resource limits、Error handling、Release / dependency、Documentation。
- Non-goals：条高 token、`overflow-hidden`、移动形态结构重排、竖屏底行的折行行为、禁用原因文案的宽度上限、版权归属的位置、`defaultM11TimelineClassName`（今天无调用点，无像素断言）、node-27 证据脚本。
- Evidence floor：
  - 先红后绿：1.3 的新断言对未改的 `src/` 为红（预期有周期 69 / ±2.5、滑块行 21；无周期 101 / ±18.5——报告实测值）；改后绿。
  - 改动前后实测表：1280×900 有周期、768×1024 有周期、768×1024 无周期、390×664 有周期、390×664 无周期、320×568 无周期、750×342 有周期——条盒、时间轴盒、滑块行高、滑块与轨道中心差；768×1024 无周期另给列宽与底行两个 span 的盒。
  - 变异：去掉 1.1 的类 -> 桌面“在条内”与“滑块行 16”红；去掉 1.2 的桌面不折行 -> 无周期“在条内”红；（若加了截断）去掉截断 -> 横向断言红；把 1.2 写成全形态不折行 -> 竖屏条高相关的实测与 `m11MapRuntime` 推导不符（给出实测数字，证明竖屏确实保持折行）。
  - `cd apps/frontend && pnpm test && pnpm exec tsc --noEmit && pnpm check:types && pnpm build`；改动 spec 的 strict tsc；`pnpm exec playwright test` 跑五个 spec（`m11-control-bar-desktop`、`m11-control-bar-landscape.mobile`、`m11-control-bar-portrait.mobile`、`m11-touch-audit.mobile`、`m11-overlay-collision`；桌面 project 加三个移动 project），再跑一遍完整 mocked 车道（其余 spec 按条的包围盒做相交判定）；改动的 spec `--repeat-each=5` 无 flaky。
  - `openspec validate control-bar-timeline-in-bar --strict --no-interactive`。
  - node-27（编排者执行，PR 构建对 live API）：桌面 oracle exit 0、控制条高 64；两个移动预设通过；另用一段临时探针在 1280×900 的 live 数据页面上读 `m11-bottom-control-bar` 与 `m11-timeline` 的包围盒，贴出输出。

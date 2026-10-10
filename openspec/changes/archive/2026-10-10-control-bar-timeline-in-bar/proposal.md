# control-bar-timeline-in-bar

## Why

桌面形态下 `/` 底部控制条高 64px（规格值），但条内时间轴 `m11-timeline` 实测高 69px，上下各溢出 2.5px；条不裁剪，文字直接画在地图上（#2864）。根因是滑块行的行盒是 21px 而不是设计的 16px：`<input type="range">` 是行内块、坐在基线上，继承了 20px 行高。源码与单测里两处注释把流高写成“64px 整”，与实测不符。768×1024 桌面形态且无可用周期时，底行 `Analysis / Forecast` 折成三行，时间轴变成 101px、上下各溢出 18.5px——不折行的类只挂在 `mobile-landscape:` 变体上。自 #2014 引入控制条起就存在，早于移动端适配。

## What Changes

- `apps/frontend/src/pages/m11/M11Controls.tsx` 的 `M11Timeline`：滑块行的行盒回到 16px（所有形态一起改，不做“仅桌面”分叉），流高回到 20 + 24 + 20 = 64px；不传 cycle 的底行在桌面形态也不折行（竖屏移动形态保持现状的折行，矮视口横屏保持不折行）；桌面窄宽下文字放不下时截断，不横向伸出列。
- 订正两处流高注释。
- `e2e/m11-control-bar-desktop.mocked.spec.ts` 增加“时间轴在条内”的像素断言（有周期 1280×900 与 768×1024；无周期 768×1024）。
- 移动 spec 里为这 2.5px 留的上限与“既有偏差”注释按新实测收紧 / 订正。

不改条高 token / `h-16` / 底距 / 条宽与居中；不给条加 `overflow-hidden`；不改移动形态的结构与触控命中区尺寸。design.md 省略（compact）。

## Triage

```text
Issue type: bugfix
Fixture level: compact
Upstream suggested level: compact (agree；合同里 legacy compatibility 是 expanded 触发词，这里维持 compact 的理由：要保的是单个组件内的测试字面量，没有外部调用方、没有格式或入口变化)
Blast radius: 桌面与移动两种形态的控制条版式——滑块触控命中区、桌面 64px 条高、竖屏无周期时的条高（上方提示条的避让推导依赖它）、768 宽底行的横向溢出；node-27 桌面 oracle（控制条高 64）
Selected risk packs: Legacy compatibility
Evidence floor: 新桌面断言先红后绿；issue 列出的桌面与移动 spec 在各自 project 全绿；本地全套前端验证；node-27 桌面 oracle exit 0 且两个移动预设通过
```

## Impact

- 受影响文件：`apps/frontend/src/pages/m11/M11Controls.tsx`（可能含 `M11BottomControlBar.tsx` 的注释）、`apps/frontend/src/components/map/m11MapRuntime.tsx`（仅注释里的数字）、`apps/frontend/src/pages/m11/__tests__/M11BottomControlBar.test.tsx`（注释）、`apps/frontend/e2e/m11-control-bar-desktop.mocked.spec.ts`、`e2e/m11-control-bar-landscape.mobile.mocked.spec.ts`、`e2e/m11-touch-audit.mobile.mocked.spec.ts`（常量 / 注释），必要时 `e2e/support/controlBar.mocked.ts`（量具）。
- 受影响规格：`map-first-layout-conformance`。

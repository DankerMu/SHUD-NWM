# curve-window-default-stagger

## Why

桌面形态下，地图区宽 W 在 900–1090px 时，河段曲线窗与气象代站曲线窗的默认位置横向重叠、纵向同高（#2840）：#2804 把桌面窗宽改成 `min(44rem, max(42vw, 30rem))` 后，窗宽在这段是 480px，而两窗左缘恒相距 0.44·W。站点窗在右，盖住河段窗的右缘——关闭按钮所在的位置；站点窗后开（在上层）时，河段窗的关闭按钮被遮住（按类推算：W ≤ 约 990 完全遮住，≤ 约 1054 部分遮住）。规格「Dual windows initially avoid perfect overlap」要求窗落到头部与关闭控件够得着的位置。不是功能丢失：点一下被盖的窗即可提到上层。

## What Changes

- `apps/frontend/src/components/map/M11DraggableCurveWindow.tsx` 的桌面摆位分支（W ≥ 900）：当两窗默认位置会横向重叠（窗宽 > 0.44·W）时，错开两窗的默认 y（河段窗在上、站点窗在下），使河段窗的关闭按钮完整露在站点窗之外。不重叠时（W ≥ 1091）默认位置逐像素不变。站点窗恒在右侧，它自己的关闭按钮在右端、永远在河段窗右缘之外，所以要解决的只有“站点窗后开时河段窗的关闭按钮被盖”这一个方向。
- 新增桌面 project 的 mocked Playwright 用例：站点窗后开时对河段窗关闭按钮做命中测试（改前红）；反向顺序与不重叠区间各一条回归护栏。

不改 D16 的宽度规则、拖拽与夹回视口的规则、移动形态；768–899 分支（两窗居中 ±18px、y 64 / 88）的既有行为不动。design.md 省略（compact）。

## Triage

```text
Issue type: bugfix
Fixture level: compact
Upstream suggested level: absent（合同里 legacy compatibility 是 expanded 触发词，这里定 compact 的理由：改的是单个函数里的默认位置计算，无外部调用方、无格式或入口变化，要保的是测试字面量）
Blast radius: 桌面形态曲线窗的默认位置；改坏时既有桌面窗位置的字面量断言（1280×900 等）会红，或窗在矮视口被夹回后仍重叠
Selected risk packs: Legacy compatibility
Evidence floor: 新用例先红后绿（W ≤ 990 的视口、站点窗后开）；既有三个桌面窗 spec 不改期望值通过；W ≥ 1091 的默认位置前后一致
```

## Impact

- 受影响文件：`apps/frontend/src/components/map/M11DraggableCurveWindow.tsx`、一个新的 e2e spec（及必要的 `e2e/support/` 量具）、涉及默认位置的既有 vitest（如有断言该分支的字面量）。
- 受影响规格：`map-feature-popups`。

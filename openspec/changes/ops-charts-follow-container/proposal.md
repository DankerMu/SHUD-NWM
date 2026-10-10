# ops-charts-follow-container

## Why

`/ops`（与共用同一页面的 `/monitoring`）上的三类图表——`QueueDonut`、`TrendLine`（两张）、`StageDurationBar`——用 `echarts-for-react` 的自动重排，而该封装库会吞掉绑定后的第一次尺寸回调（与其后 60ms 防抖窗口内的变化合并），容器恰在那时变宽度，画布就停在旧宽度，直到下一次尺寸变化（#2860）。症状是图表错位、页面横向溢出；`e2e/support/opsFallback.mocked.ts` 的注释已记录这一点并靠重新导航绕开。两个曲线图（`ForecastChart` 的 `fill` 分支、气象代站图表）在 #2806 已用 `useChartFollowsContainer` 保护，这三张图当时被列为 Non-goal。

## What Changes

- `apps/frontend/src/components/charts/{QueueDonut,TrendLine,StageDurationBar}.tsx`：各自调用现成的 `useChartFollowsContainer(<此刻是否渲染图表>)`，返回值交给 `ReactEChartsCore` 的 `onChartReady`。不新增抽象，不改 hook 内部。
- 同目录新增 vitest；新增一条 mocked e2e：在已加载的 `/ops` 上直接改视口，图表画布宽跟上容器宽。

不改 `useChartFollowsContainer.ts`、`ForecastChart.tsx`、`M11StationForcingPopup.tsx`、图表配置与卡片布局；不给封装库打补丁或升级。design.md 省略（compact）。

## Triage

```text
Issue type: bugfix
Fixture level: compact
Upstream suggested level: compact (agree)
Blast radius: /ops 与 /monitoring 的三类图表；改坏时图表不渲染或卸载后观察器泄漏
Selected risk packs: Concurrency / shared state / ordering
Evidence floor: 新 vitest 先红后绿；新 e2e 在 repeat-each=5 下稳定绿；既有图表与运维页测试不改通过；node-27 上 /ops 的 live receipt
```

## Impact

- 受影响文件：上述三个组件、`src/components/charts/__tests__/` 新测试、一个新 e2e spec，至多连带 `e2e/support/opsFallback.mocked.ts` 的注释 / 助手。
- 受影响规格：`pipeline-monitoring-frontend`。

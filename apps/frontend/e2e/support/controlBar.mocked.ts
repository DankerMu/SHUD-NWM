import { expect, type Page, type Route } from '@playwright/test'

import type { components } from '../../src/api/types'
import { MOCK_PRECIP_LAYER } from './layerCatalog.mocked'
import { contains, type Box } from './legendLauncher.mocked'
import { CONTROL_BAR, MAP_REGION, boxOf } from './overlayLaunchers.mocked'
import { mockDischargeLayer } from './riverWindow.mocked'
import type { ViewportSize } from './viewportForm'

/**
 * 底部控制条两个 spec（openspec mobile-responsive-display task 3.7，design.md D6）共用的选择器、
 * mock 与量具。文件名带 `mocked` token：只被 mocked 车道的 spec 引用，基线 mock 是
 * `installRiverWindowMocks`（带 9 个有效时刻与一个起报时次）。
 */
type Schemas = components['schemas']

export const TIMELINE = '[data-testid="m11-timeline"]'
export const DISABLED_REASON = '[data-testid="m11-control-bar-disabled-reason"]'
export const ATTRIBUTION = '.maplibregl-ctrl-attrib'
export const STEP_BUTTON_NAMES = ['上一个有效时刻', '播放时间轴', '下一个有效时刻'] as const
/** `src/pages/m11/M11BottomControlBar.tsx`：起报时次列表为空时的唯一选项。 */
export const EMPTY_CYCLE_OPTION_TEXT = '无可用起报时次'

export function controlBarParts(page: Page) {
  const bar = page.locator(CONTROL_BAR)
  return {
    bar,
    sourceGroup: bar.getByRole('group', { name: '预报源' }),
    sourceButtons: bar.getByRole('group', { name: '预报源' }).getByRole('button'),
    cycle: bar.getByLabel('起报时次'),
    /** 整页范围：任一形态下页面上恰有一个。 */
    speed: page.getByLabel('播放速度'),
    stepButtons: STEP_BUTTON_NAMES.map((name) => bar.getByRole('button', { name })),
    slider: bar.getByLabel('有效时间滑块'),
    timeline: bar.locator(TIMELINE),
    reason: bar.locator(DISABLED_REASON),
  }
}

/** 全国径流目录的 fail-closed 形态：没有有效时刻、没有默认周期。 */
const failClosedDischargeLayer = {
  ...mockDischargeLayer,
  metadata: { ...mockDischargeLayer.metadata, default_cycle: null, valid_times: [] },
} satisfies Schemas['Layer']

/**
 * 让禁用原因出现：在 `installRiverWindowMocks` **之后**叠一条 `/api/v1/layers` 窄路由（后注册者先匹配），
 * 目录回 fail-closed 的径流图层——控制条显示 fail-closed 文案、起报时次只剩空选项、全部控件禁用。
 * 这是竖屏下最高的控制条（两行 + 禁用原因行）。须在 `page.goto` 之前调用。
 */
export async function installFailClosedDischargeLayer(page: Page) {
  await page.route(
    (url) => url.pathname === '/api/v1/layers',
    (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'ok', data: [failClosedDischargeLayer] satisfies Schemas['Layer'][] }),
      }),
  )
}

/**
 * 最高的控制条 + 最长的图例：fail-closed 的径流图层（两行 + 禁用原因行）再加一条降水目录条目，
 * 图例面板因此同时列出径流分级与六级降水——竖屏窄视口下内容超出面板限高。
 * 用法同 `installFailClosedDischargeLayer`：叠在 `installRiverWindowMocks` 之后、`page.goto` 之前。
 */
export async function installFailClosedDischargeWithPrecipLegend(page: Page) {
  await page.route(
    (url) => url.pathname === '/api/v1/layers',
    (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'ok', data: [failClosedDischargeLayer, MOCK_PRECIP_LAYER] satisfies Schemas['Layer'][] }),
      }),
  )
}

export interface ControlBarMeasure {
  viewport: ViewportSize
  map: Box
  bar: Box
  sourceGroup: Box
  sourceButtons: Box[]
  cycle: Box
  speed: Box
  stepButtons: Box[]
  slider: Box
  timeline: Box
  /** 禁用原因不在 DOM 时为 null。 */
  reason: Box | null
  attribution: Box
  /** 页面上「播放速度」选择器的个数，以及它是否在 `m11-timeline` 里。 */
  speedCount: number
  speedInsideTimeline: boolean
  cycleFontSizePx: number
  speedFontSizePx: number
  timelineFlexBasis: string
}

/** 量控制条与它的每个控件；每个控件都必须可见（不可见即失败，而不是量出一个空盒子）。 */
export async function measureControlBar(page: Page, viewport: ViewportSize): Promise<ControlBarMeasure> {
  const parts = controlBarParts(page)
  await expect(parts.bar).toBeVisible()
  await expect(parts.speed, '页面上「播放速度」选择器应恰一个').toHaveCount(1)
  await expect(parts.sourceButtons, '预报源分段应有 GFS / IFS 两个按钮').toHaveCount(2)
  for (const control of [parts.cycle, parts.speed, parts.slider, ...parts.stepButtons]) {
    await expect(control).toBeVisible()
  }
  const fontSize = (element: Element) => Number.parseFloat(getComputedStyle(element).fontSize)
  const reasonCount = await parts.reason.count()
  return {
    viewport,
    map: await boxOf(page.locator(MAP_REGION), '地图区'),
    bar: await boxOf(parts.bar, '控制条'),
    sourceGroup: await boxOf(parts.sourceGroup, '预报源分段'),
    sourceButtons: [
      await boxOf(parts.sourceButtons.nth(0), '预报源按钮 1'),
      await boxOf(parts.sourceButtons.nth(1), '预报源按钮 2'),
    ],
    cycle: await boxOf(parts.cycle, '起报时次'),
    speed: await boxOf(parts.speed, '播放速度'),
    stepButtons: await Promise.all(parts.stepButtons.map((button, index) => boxOf(button, STEP_BUTTON_NAMES[index]))),
    slider: await boxOf(parts.slider, '有效时间滑块'),
    timeline: await boxOf(parts.timeline, '时间轴'),
    reason: reasonCount > 0 ? await boxOf(parts.reason, '禁用原因') : null,
    attribution: await boxOf(page.locator(ATTRIBUTION).first(), '版权归属'),
    speedCount: await parts.speed.count(),
    speedInsideTimeline: (await parts.timeline.getByLabel('播放速度').count()) === 1,
    cycleFontSizePx: await parts.cycle.evaluate(fontSize),
    speedFontSizePx: await parts.speed.evaluate(fontSize),
    timelineFlexBasis: await parts.timeline.evaluate((element) => getComputedStyle(element).flexBasis),
  }
}

/** 五类控件各自的包围盒（带名字），供“在视口内”逐个断言。 */
export function namedControlBoxes(measure: ControlBarMeasure): Array<{ name: string; box: Box }> {
  return [
    ...measure.sourceButtons.map((box, index) => ({ name: `预报源按钮 ${index + 1}`, box })),
    { name: '起报时次', box: measure.cycle },
    { name: '播放速度', box: measure.speed },
    ...measure.stepButtons.map((box, index) => ({ name: STEP_BUTTON_NAMES[index], box })),
    { name: '有效时间滑块', box: measure.slider },
  ]
}

/** 溢出 oracle：每个控件的包围盒都在视口内，且有面积。 */
export function expectControlsInViewport(measure: ControlBarMeasure, where: string) {
  const viewportBox: Box = { x: 0, y: 0, width: measure.viewport.width, height: measure.viewport.height }
  for (const { name, box } of namedControlBoxes(measure)) {
    expect.soft(box.width, `${name}宽应 > 0 @ ${where}`).toBeGreaterThan(0)
    expect.soft(box.height, `${name}高应 > 0 @ ${where}`).toBeGreaterThan(0)
    expect
      .soft(contains(viewportBox, box), `${name} ${JSON.stringify(box)} 不在视口 ${JSON.stringify(viewportBox)} 内 @ ${where}`)
      .toBe(true)
  }
}

/** 两个盒子在垂直方向有正的重叠（同一行）。 */
export function overlapsVertically(a: Box, b: Box): boolean {
  return a.y < b.y + b.height && b.y < a.y + a.height
}

export interface TimelineRowsMeasure {
  /** `m11-timeline-rows`：时间轴右侧的行列。 */
  rows: Box
  /** 滑块行 = 行列的第 2 个子节点（滑块的父节点）。 */
  sliderRow: Box
  /** 末行（第 3 个子节点：有周期时是刻度行，无周期时是底行）里的 `<span>`，按文档顺序。 */
  lastRowSpans: Box[]
  /** 滑块与轨道（滑块行的首子节点）的纵向中心差。 */
  sliderCentreDeltaY: number
}

/**
 * 量时间轴行列的内部几何（#2864）。按结构取节点：滑块行没有 testid，它是 `m11-timeline-rows` 的
 * 第 2 个子节点。量的是**行**而不是 input——桌面形态下 input 自身恒为 16px，量它恒真。
 */
export async function measureTimelineRows(page: Page): Promise<TimelineRowsMeasure> {
  return page.getByTestId('m11-timeline-rows').evaluate((rows) => {
    const boxOfRect = (rect: DOMRect) => ({ x: rect.left, y: rect.top, width: rect.width, height: rect.height })
    const sliderRow = rows.children[1]
    const lastRow = rows.children[2]
    const input = sliderRow?.querySelector(':scope > input[type="range"]')
    const track = sliderRow?.firstElementChild
    if (!sliderRow || !lastRow || !input || !track || track === input) {
      throw new Error('m11-timeline-rows 的结构不是「当前时次行 / 滑块行（轨道在前、滑块为直接子节点）/ 末行」')
    }
    const own = input.getBoundingClientRect()
    const trackRect = track.getBoundingClientRect()
    return {
      rows: boxOfRect(rows.getBoundingClientRect()),
      sliderRow: boxOfRect(sliderRow.getBoundingClientRect()),
      lastRowSpans: Array.from(lastRow.querySelectorAll(':scope > span')).map((span) => boxOfRect(span.getBoundingClientRect())),
      sliderCentreDeltaY: own.top + own.height / 2 - (trackRect.top + trackRect.height / 2),
    }
  })
}

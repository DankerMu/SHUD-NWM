import { expect, type Page } from '@playwright/test'

import { CURVE_WINDOWS, openCurveWindow, type CurveWindowKind } from './curveSheet.mocked'
import type { Box } from './legendLauncher.mocked'
import { installRiverWindowMocks } from './riverWindow.mocked'
import { isShortLandscape, type ViewportSize } from './viewportForm'

/**
 * 抽屉内图表区的量具（openspec mobile-responsive-display task 4.4 / 4.5，design.md D12 前两条）。
 * 文件名带 `mocked` token：只被 mocked 车道的 spec 引用。
 *
 * 与面板无关：窗、主体容器、图表区都由 `CurveWindowKind`（或显式给的图表区 testid）定位，
 * 河段窗与气象代站窗共用同一套量法。所有判据都取自包围盒与计算样式，不看类名。
 */

/** 规格下限：非矮视口横屏 160px、矮视口横屏 120px。 */
export const CHART_FLOOR_PX = 160
export const CHART_FLOOR_SHORT_LANDSCAPE_PX = 120
/** 已加载容器的下内边距：主体滚到底时图表区底边到主体可视底边至少留这么多。 */
export const CHART_BOTTOM_GAP_PX = 8
/** 下限 / 间距这类“应恰好等于”的判据的容差。 */
export const CHART_EXACT_TOLERANCE_PX = 0.5
/** 画布与图表区、两次打开之间这类“同尺寸”判据的容差。 */
export const CHART_SIZE_TOLERANCE_PX = 1

export function chartFloorPx(viewport: ViewportSize): number {
  return isShortLandscape(viewport) ? CHART_FLOOR_SHORT_LANDSCAPE_PX : CHART_FLOOR_PX
}

export interface SheetChartTarget {
  kind: CurveWindowKind
  /** 图表区的 testid；缺省取该窗在 `CURVE_WINDOWS` 里登记的那一个。 */
  chartTestId?: string
}

function resolveTarget(target: SheetChartTarget) {
  const spec = CURVE_WINDOWS[target.kind]
  return { name: spec.name, testId: spec.testId, chartTestId: target.chartTestId ?? spec.chart }
}

export interface SheetChartMeasure {
  /** 窗（抽屉）的包围盒。 */
  frame: Box
  /** 主体容器的可视盒（内边距盒：不含边框与滚动条）与滚动量。 */
  body: { visible: Box; overflowY: string; scrollHeight: number; clientHeight: number; scrollTop: number }
  /** 图表区。 */
  chart: Box
  /** 图表区计算后的 `min-height`。 */
  chartMinHeight: string
  /** 图表区的父容器（已加载容器）及其计算后的下内边距（px）。 */
  parent: Box
  parentPaddingBottom: number
  /** 图表区内第一个 canvas 的 CSS 盒；还没画出来时为 null。 */
  canvas: Box | null
  /** 主体容器内部（不含主体自己）计算 `overflow-y` 为 auto / scroll 的元素个数：不得新增第二个滚动容器。 */
  nestedScrollerCount: number
}

export async function measureSheetChart(page: Page, target: SheetChartTarget): Promise<SheetChartMeasure> {
  const resolved = resolveTarget(target)
  const measured = await page.evaluate(({ testId, chartTestId }) => {
    const find = (id: string) => document.querySelector<HTMLElement>(`[data-testid="${id}"]`)
    const frame = find(testId)
    const body = find(`${testId}-body`)
    const chart = frame?.querySelector<HTMLElement>(`[data-testid="${chartTestId}"]`) ?? null
    const parent = chart?.parentElement ?? null
    if (!frame || !body || !chart || !parent) return null
    const box = (element: Element) => {
      const rect = element.getBoundingClientRect()
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
    }
    const bodyRect = body.getBoundingClientRect()
    const canvas = chart.querySelector('canvas')
    const scrolls = (element: Element) => {
      const overflowY = getComputedStyle(element).overflowY
      return overflowY === 'auto' || overflowY === 'scroll'
    }
    return {
      frame: box(frame),
      body: {
        visible: { x: bodyRect.x + body.clientLeft, y: bodyRect.y + body.clientTop, width: body.clientWidth, height: body.clientHeight },
        overflowY: getComputedStyle(body).overflowY,
        scrollHeight: body.scrollHeight,
        clientHeight: body.clientHeight,
        scrollTop: body.scrollTop,
      },
      chart: box(chart),
      chartMinHeight: getComputedStyle(chart).minHeight,
      parent: box(parent),
      parentPaddingBottom: Number.parseFloat(getComputedStyle(parent).paddingBottom),
      canvas: canvas ? box(canvas) : null,
      nestedScrollerCount: Array.from(body.querySelectorAll('*')).filter(scrolls).length,
    }
  }, resolved)
  if (!measured) {
    throw new Error(`measureSheetChart: the ${target.kind} window, its body container or its chart area (${resolved.chartTestId}) is not in the DOM`)
  }
  return measured
}

/** 设视口、装基线 mock、经真实指针输入开窗，并等到“曲线已加载”（图表区内 canvas 可见）。 */
export async function openLoadedCurveWindow(page: Page, viewport: ViewportSize, target: SheetChartTarget) {
  await page.setViewportSize(viewport)
  await installRiverWindowMocks(page)
  await openCurveWindow(page, target.kind)
  await expect(sheetChartCanvas(page, target), '曲线应已加载（图表 canvas 可见）').toBeVisible()
}

export function sheetChartCanvas(page: Page, target: SheetChartTarget) {
  const resolved = resolveTarget(target)
  return page.getByTestId(resolved.testId).getByTestId(resolved.chartTestId).locator('canvas').first()
}

/** 把主体容器滚到底。 */
export async function scrollSheetBodyToEnd(page: Page, target: SheetChartTarget) {
  await page.getByTestId(`${resolveTarget(target).testId}-body`).evaluate((element) => {
    element.scrollTop = element.scrollHeight
  })
}

/** `inner` 相对 `outer` 四边各自的越界量（> 0 表示越出），供带数值的失败信息用。 */
export function overflowOf(outer: Box, inner: Box) {
  return {
    left: outer.x - inner.x,
    top: outer.y - inner.y,
    right: inner.x + inner.width - (outer.x + outer.width),
    bottom: inner.y + inner.height - (outer.y + outer.height),
  }
}

/** canvas 与图表区宽高之差的清单；空 = 同尺寸（±1px）。canvas 不存在也算偏差。 */
export function canvasSizeMismatches(measure: SheetChartMeasure): string[] {
  const { canvas, chart } = measure
  if (!canvas) return ['图表区内没有 canvas']
  const mismatches: string[] = []
  for (const key of ['width', 'height'] as const) {
    if (Math.abs(canvas[key] - chart[key]) > CHART_SIZE_TOLERANCE_PX) mismatches.push(`canvas.${key} ${canvas[key]} != 图表区.${key} ${chart[key]}`)
  }
  return mismatches
}

/**
 * 量到“画布已跟上图表区”为止并返回这次测量。图表库经尺寸观察异步重排，视口刚变过时
 * 画布会滞后一两帧，所以用 `toPass` 轮询；超时即画布没有跟上，失败信息带两者的实测尺寸。
 */
export async function measureSettledSheetChart(page: Page, target: SheetChartTarget, where: string): Promise<SheetChartMeasure> {
  let last: SheetChartMeasure | undefined
  await expect(async () => {
    last = await measureSheetChart(page, target)
    expect(canvasSizeMismatches(last), `canvas 应与图表区同尺寸 @ ${where}`).toEqual([])
  }).toPass({ timeout: 4_000 })
  return last!
}

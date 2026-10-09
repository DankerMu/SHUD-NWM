import { expect, test, type Page, type TestInfo } from '@playwright/test'

import { CURVE_WINDOWS, curveWindowParts, expectSheet } from './support/curveSheet.mocked'
import { contains, type Box } from './support/legendLauncher.mocked'
import { boxOf } from './support/overlayLaunchers.mocked'
import {
  CHART_EXACT_TOLERANCE_PX,
  CHART_FLOOR_PX,
  CHART_FLOOR_SHORT_LANDSCAPE_PX,
  CHART_SIZE_TOLERANCE_PX,
  chartFloorPx,
  measureSettledSheetChart,
  measureSheetChart,
  openLoadedCurveWindow,
  overflowOf,
  type SheetChartMeasure,
  type SheetChartTarget,
} from './support/sheetChart.mocked'
import { isMobileForm, isShortLandscape, requireViewport, type ViewportSize } from './support/viewportForm'

/**
 * 气象代站抽屉内图表区的可读高度（openspec mobile-responsive-display task 4.5，design.md D12 前两条）。
 *
 * 用例 (a)–(h) 对应 tasks.md 里 #2806 的 Triage。每条用例在用例内 `setViewportSize`，所以三个移动
 * project 下执行的是同一组断言，不按 project 跳过。全部在“曲线已加载”（图表 canvas 可见）之后量，
 * 默认要素（PRCP），判据取自包围盒与计算样式。
 *
 * “图表区”是图表卡片里直接包住图表的那一层（`m11-station-panel-chart`）；卡片
 * （`CURVE_WINDOWS.station.chart`，含要素标题与徽标行）是它的父元素，再外一层是已加载容器。
 */

const CHART_AREA_TEST_ID = 'm11-station-panel-chart'
const LOADED_TEST_ID = 'm11-station-popup-loaded'
const CARD_TEST_ID = CURVE_WINDOWS.station.chart
const STATION: SheetChartTarget = { kind: 'station', chartTestId: CHART_AREA_TEST_ID }

const PORTRAIT: ViewportSize = { width: 390, height: 664 }
/** 剩余高度足够（不咬下限）的移动竖屏视口：task 4.9 把要素切换项加到 44px 高之后 390×664 已咬住下限，这里实测图表区 231.5px、主体不溢出。 */
const TALL_PORTRAIT: ViewportSize = { width: 390, height: 800 }
/** 下限咬住的矮视口横屏：只加 testid 的改动前 `src/` 上图表区实测 61.5px（主体 213px，不溢出）。 */
const LANDSCAPE: ViewportSize = { width: 750, height: 342 }
/** 改动前图表区实测 109.5px（主体 261px）。 */
const LANDSCAPE_WIDE: ViewportSize = { width: 844, height: 390 }
/** 下限咬住的竖向矮视口：改动前图表区实测 5px（主体 191px，不溢出）。 */
const SHORT_PORTRAIT: ViewportSize = { width: 320, height: 480 }
const DESKTOP: ViewportSize = { width: 1280, height: 900 }

function label(page: Page, testInfo: TestInfo) {
  const viewport = requireViewport(page)
  return `${testInfo.project.name} ${viewport.width}x${viewport.height}`
}

function report(name: string, where: string, measure: unknown) {
  console.log(`station-sheet-chart ${name} @ ${where}`, JSON.stringify(measure))
}

/** 已加载容器与图表卡片的包围盒（量具返回的 `parent` 只到卡片这一级）。 */
async function measureStationContainers(page: Page): Promise<{ loaded: Box; card: Box }> {
  const measured = await page.evaluate(
    ({ loadedTestId, cardTestId }) => {
      const box = (id: string) => {
        const rect = document.querySelector(`[data-testid="${id}"]`)?.getBoundingClientRect()
        return rect ? { x: rect.x, y: rect.y, width: rect.width, height: rect.height } : null
      }
      return { loaded: box(loadedTestId), card: box(cardTestId) }
    },
    { loadedTestId: LOADED_TEST_ID, cardTestId: CARD_TEST_ID },
  )
  if (!measured.loaded || !measured.card) throw new Error('measureStationContainers: the loaded container or the chart card is not in the DOM')
  return { loaded: measured.loaded, card: measured.card }
}

/** 把图表区滚入视野（就近对齐），返回滚动后的测量。 */
async function scrollChartAreaIntoView(page: Page): Promise<SheetChartMeasure> {
  await page
    .getByTestId(CURVE_WINDOWS.station.testId)
    .getByTestId(CHART_AREA_TEST_ID)
    .evaluate((element) => element.scrollIntoView({ block: 'nearest' }))
  return measureSheetChart(page, STATION)
}

/** 滚入视野后：图表区完整落在主体可视盒与抽屉盒内，抽屉盒不变，标题与关闭按钮仍可见。 */
async function expectChartReachable(page: Page, sheetFrame: Box, where: string) {
  const parts = curveWindowParts(page, 'station')
  const scrolled = await scrollChartAreaIntoView(page)
  report('scrolled into view', where, scrolled)
  expect(scrolled.frame, '主体滚动不改变抽屉盒').toEqual(sheetFrame)
  expect(
    contains(scrolled.body.visible, scrolled.chart),
    `滚入视野后图表区应完整落在主体可视盒内，越界量 ${JSON.stringify(overflowOf(scrolled.body.visible, scrolled.chart))} @ ${where}`,
  ).toBe(true)
  expect(contains(scrolled.frame, scrolled.chart), `滚入视野后图表区应在抽屉盒内 @ ${where}`).toBe(true)
  for (const [partName, locator] of [['标题', parts.title], ['关闭按钮', parts.close]] as const) {
    await expect(locator, `${partName}应可见 @ ${where}`).toBeVisible()
    expect(contains(scrolled.frame, await boxOf(locator, partName)), `主体滚动后${partName}应在抽屉盒内 @ ${where}`).toBe(true)
  }
  return scrolled
}

/** 图表区在卡片内、卡片在已加载容器内（已加载容器与卡片都随内容长高）。 */
async function expectContainment(page: Page, measure: SheetChartMeasure, where: string) {
  const { loaded, card } = await measureStationContainers(page)
  report('containers', where, { loaded, card })
  expect(measure.parent, '图表区的父元素应是图表卡片').toEqual(card)
  expect(
    contains(card, measure.chart),
    `图表区应完整在卡片盒内，越界量 ${JSON.stringify(overflowOf(card, measure.chart))} @ ${where}`,
  ).toBe(true)
  expect(
    contains(loaded, card),
    `卡片应完整在已加载容器盒内，越界量 ${JSON.stringify(overflowOf(loaded, card))} @ ${where}`,
  ).toBe(true)
  return { loaded, card }
}

/** 下限咬住的抽屉：图表区恰为下限、主体溢出并由它滚动、各级容器随内容长高、图表区可完整滚入视野。 */
async function expectFloorBites(page: Page, viewport: ViewportSize, where: string) {
  const floor = chartFloorPx(viewport)
  const sheet = await expectSheet(page, 'station', where)
  const before = await measureSettledSheetChart(page, STATION, where)
  report('floor bites', where, before)

  // 硬前提：这个视口里主体确实溢出——否则下面的“滚入视野”是空转。
  expect(before.body.scrollHeight, `前提：主体应溢出（scrollHeight ${before.body.scrollHeight} > clientHeight ${before.body.clientHeight}）@ ${where}`).toBeGreaterThan(
    before.body.clientHeight,
  )
  expect(before.body.overflowY).toBe('auto')
  expect(before.nestedScrollerCount, '主体内部不应有第二个滚动容器').toBe(0)

  expect(Math.abs(before.chart.height - floor), `图表区高 ${before.chart.height} 应为下限 ${floor} @ ${where}`).toBeLessThanOrEqual(CHART_EXACT_TOLERANCE_PX)
  await expectContainment(page, before, where)
  expect(
    contains(before.body.visible, before.chart),
    `前提：滚动前图表区不应已完整可见（否则“滚入视野”无从验证）@ ${where}`,
  ).toBe(false)

  const scrolled = await expectChartReachable(page, sheet.frame, where)
  expect(scrolled.body.scrollTop, '主体应真的滚动了').toBeGreaterThan(0)
}

/**
 * 同一气象代站窗（不重开）从 `from` 转到 `to`：canvas 跟上新图表区、尺寸与旋转前不同，
 * 且图表区高度与主体内容高等于“在 `to` 新开页面、新打开的气象代站窗”的值（防高度棘轮）。
 */
async function expectChartFollowsRotation(page: Page, testInfo: TestInfo, from: ViewportSize, to: ViewportSize) {
  await openLoadedCurveWindow(page, from, STATION)
  const frame = curveWindowParts(page, 'station').frame
  await expectSheet(page, 'station', label(page, testInfo))
  const before = await measureSettledSheetChart(page, STATION, label(page, testInfo))
  await frame.evaluate((element) => {
    ;(element as HTMLElement & { __sheetChartMark?: string }).__sheetChartMark = 'before-rotation'
  })

  await page.setViewportSize(to)
  const where = label(page, testInfo)
  // 形态经 matchMedia 订阅异步落定：先等抽屉换成新视口的那一种，再等画布跟上。
  await expectSheet(page, 'station', where)
  const after = await measureSettledSheetChart(page, STATION, where)
  expect(
    await frame.evaluate((element) => (element as HTMLElement & { __sheetChartMark?: string }).__sheetChartMark),
    '旋转后应是同一个窗节点（没有重开）',
  ).toBe('before-rotation')
  const floor = chartFloorPx(to)
  expect(after.chart.height, `图表区高 ${after.chart.height} 应 ≥ ${floor} @ ${where}`).toBeGreaterThanOrEqual(floor)

  const beforeCanvas = before.canvas!
  const afterCanvas = after.canvas!
  expect(Math.abs(afterCanvas.width - beforeCanvas.width), `旋转前后 canvas 宽应不同（${beforeCanvas.width} → ${afterCanvas.width}）`).toBeGreaterThan(CHART_SIZE_TOLERANCE_PX)
  expect(Math.abs(afterCanvas.height - beforeCanvas.height), `旋转前后 canvas 高应不同（${beforeCanvas.height} → ${afterCanvas.height}）`).toBeGreaterThan(CHART_SIZE_TOLERANCE_PX)

  // 对照：同一浏览器上下文里新开一个页面，在目标视口新打开气象代站窗。
  const freshPage = await page.context().newPage()
  await openLoadedCurveWindow(freshPage, to, STATION)
  await expectSheet(freshPage, 'station', `${where} fresh`)
  const fresh = await measureSettledSheetChart(freshPage, STATION, `${where} fresh`)
  await freshPage.close()

  report('rotation', where, {
    before: { chart: before.chart, canvas: before.canvas, body: before.body },
    after: { chart: after.chart, canvas: after.canvas, body: after.body },
    fresh: { chart: fresh.chart, canvas: fresh.canvas, body: fresh.body },
  })
  expect(
    Math.abs(after.chart.height - fresh.chart.height),
    `旋转后图表区高 ${after.chart.height} 应等于新开窗的图表区高 ${fresh.chart.height}（旋转前 ${before.chart.height}）@ ${where}`,
  ).toBeLessThanOrEqual(CHART_SIZE_TOLERANCE_PX)
  expect(Math.abs(after.chart.width - fresh.chart.width)).toBeLessThanOrEqual(CHART_SIZE_TOLERANCE_PX)
  expect(
    Math.abs(after.body.scrollHeight - fresh.body.scrollHeight),
    `旋转后主体内容高 ${after.body.scrollHeight} 应等于新开窗的 ${fresh.body.scrollHeight} @ ${where}`,
  ).toBeLessThanOrEqual(CHART_SIZE_TOLERANCE_PX)
}

test.describe('M11 气象代站抽屉图表可读高度', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
  })

  test('(a) 390x664：图表区高 ≥ 160、不滚动就在抽屉盒内，canvas 与图表区同尺寸', async ({ page }, testInfo) => {
    await openLoadedCurveWindow(page, PORTRAIT, STATION)
    const where = label(page, testInfo)
    expect(chartFloorPx(requireViewport(page))).toBe(CHART_FLOOR_PX)
    await expectSheet(page, 'station', where)

    const measure = await measureSettledSheetChart(page, STATION, where)
    report('(a)', where, measure)
    expect(measure.chart.height, `图表区高 ${measure.chart.height} 应 ≥ ${CHART_FLOOR_PX} @ ${where}`).toBeGreaterThanOrEqual(CHART_FLOOR_PX)
    expect(measure.body.scrollTop, '没有滚动过').toBe(0)
    expect(
      contains(measure.frame, measure.chart),
      `图表区 ${JSON.stringify(measure.chart)} 应在抽屉盒 ${JSON.stringify(measure.frame)} 内 @ ${where}`,
    ).toBe(true)
  })

  for (const viewport of [LANDSCAPE, LANDSCAPE_WIDE]) {
    test(`(b) ${viewport.width}x${viewport.height}：图表区高 ≥ 120，canvas 与图表区同尺寸，滚入视野后完整落在抽屉可视框内`, async ({ page }, testInfo) => {
      await openLoadedCurveWindow(page, viewport, STATION)
      const where = label(page, testInfo)
      expect(chartFloorPx(requireViewport(page))).toBe(CHART_FLOOR_SHORT_LANDSCAPE_PX)
      const sheet = await expectSheet(page, 'station', where)

      const measure = await measureSettledSheetChart(page, STATION, where)
      report('(b)', where, measure)
      expect(measure.chart.height, `图表区高 ${measure.chart.height} 应 ≥ ${CHART_FLOOR_SHORT_LANDSCAPE_PX} @ ${where}`).toBeGreaterThanOrEqual(
        CHART_FLOOR_SHORT_LANDSCAPE_PX,
      )
      await expectChartReachable(page, sheet.frame, where)
    })
  }

  test('(c) 320x480：下限咬住——图表区高 = 160，主体溢出并滚动，图表区在卡片内、卡片在已加载容器内，可完整滚入视野', async ({ page }, testInfo) => {
    await openLoadedCurveWindow(page, SHORT_PORTRAIT, STATION)
    expect(isShortLandscape(requireViewport(page))).toBe(false)
    expect(chartFloorPx(requireViewport(page))).toBe(CHART_FLOOR_PX)

    await expectFloorBites(page, SHORT_PORTRAIT, label(page, testInfo))
  })

  test('(d) 750x342：下限咬住——图表区高 = 120，主体溢出并滚动，图表区在卡片内、卡片在已加载容器内，可完整滚入视野', async ({ page }, testInfo) => {
    await openLoadedCurveWindow(page, LANDSCAPE, STATION)
    expect(isShortLandscape(requireViewport(page))).toBe(true)
    expect(chartFloorPx(requireViewport(page))).toBe(CHART_FLOOR_SHORT_LANDSCAPE_PX)

    await expectFloorBites(page, LANDSCAPE, label(page, testInfo))
  })

  test('(e) 390x800：剩余高度足够时图表区仍占满——高 > 160，主体不溢出，已加载容器底贴主体可视底边', async ({ page }, testInfo) => {
    await openLoadedCurveWindow(page, TALL_PORTRAIT, STATION)
    const where = label(page, testInfo)
    await expectSheet(page, 'station', where)

    const measure = await measureSettledSheetChart(page, STATION, where)
    report('(e)', where, measure)
    expect(measure.chart.height, `图表区高 ${measure.chart.height} 应 > ${CHART_FLOOR_PX}（没有被钉在下限）`).toBeGreaterThan(CHART_FLOOR_PX)
    expect(measure.body.scrollHeight, '主体不应溢出（含亚像素造成的 1px 假溢出）').toBe(measure.body.clientHeight)
    // 占满：图表区伸到卡片的下内边距处，卡片伸到已加载容器的下内边距处，容器底贴主体可视底边。
    const { loaded, card } = await expectContainment(page, measure, where)
    const bodyBottom = measure.body.visible.y + measure.body.visible.height
    // 主体的 clientHeight 是取整值，容器盒是亚像素值：容差取 1px。
    expect(Math.abs(loaded.y + loaded.height - bodyBottom), `已加载容器底边 ${loaded.y + loaded.height} 应贴主体可视底边 ${bodyBottom}`).toBeLessThanOrEqual(CHART_SIZE_TOLERANCE_PX)
    const chartToCardBottom = card.y + card.height - (measure.chart.y + measure.chart.height)
    expect(Math.abs(chartToCardBottom - measure.parentPaddingBottom), `图表区底边距卡片底边 ${chartToCardBottom} 应为卡片下内边距 ${measure.parentPaddingBottom}`).toBeLessThanOrEqual(
      CHART_EXACT_TOLERANCE_PX,
    )
  })

  for (const [viewport, expected] of [
    [PORTRAIT, `${CHART_FLOOR_PX}px`],
    [LANDSCAPE, `${CHART_FLOOR_SHORT_LANDSCAPE_PX}px`],
    // 桌面形态：改动前实测即 0px。
    [DESKTOP, '0px'],
  ] as const) {
    test(`(f) ${viewport.width}x${viewport.height}：图表区计算 min-height 为 ${expected}`, async ({ page }) => {
      await openLoadedCurveWindow(page, viewport, STATION)

      const measure = await measureSheetChart(page, STATION)
      expect(measure.chartMinHeight).toBe(expected)
    })
  }

  test('(g) 390x664 → 750x342：canvas 跟上新图表区，图表区高度与主体内容高等于在 750x342 新开窗的值', async ({ page }, testInfo) => {
    await expectChartFollowsRotation(page, testInfo, PORTRAIT, LANDSCAPE)
  })

  test('(h) 750x342 → 390x664：canvas 跟上新图表区，图表区高度与主体内容高等于在 390x664 新开窗的值', async ({ page }, testInfo) => {
    await expectChartFollowsRotation(page, testInfo, LANDSCAPE, PORTRAIT)
  })
})

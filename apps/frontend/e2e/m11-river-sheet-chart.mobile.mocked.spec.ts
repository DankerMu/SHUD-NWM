import { expect, test, type Page, type TestInfo } from '@playwright/test'

import { curveWindowParts, expectSheet } from './support/curveSheet.mocked'
import { contains } from './support/legendLauncher.mocked'
import { boxOf } from './support/overlayLaunchers.mocked'
import {
  CHART_BOTTOM_GAP_PX,
  CHART_EXACT_TOLERANCE_PX,
  CHART_FLOOR_PX,
  CHART_FLOOR_SHORT_LANDSCAPE_PX,
  CHART_SIZE_TOLERANCE_PX,
  chartFloorPx,
  measureSettledSheetChart,
  measureSheetChart,
  openLoadedCurveWindow,
  overflowOf,
  scrollSheetBodyToEnd,
  type SheetChartMeasure,
  type SheetChartTarget,
} from './support/sheetChart.mocked'
import { isMobileForm, isShortLandscape, requireViewport, type ViewportSize } from './support/viewportForm'

/**
 * 河段抽屉内图表区的可读高度（openspec mobile-responsive-display task 4.4，design.md D12 前两条）。
 *
 * 用例 (a)–(h) 对应 tasks.md 里 #2805 的 Triage。每条用例在用例内 `setViewportSize`，所以三个移动
 * project 下执行的是同一组断言，不按 project 跳过。全部在“曲线已加载”（图表 canvas 可见）之后量，
 * 判据取自包围盒与计算样式。
 */

const RIVER: SheetChartTarget = { kind: 'river' }

const PORTRAIT: ViewportSize = { width: 390, height: 664 }
const LANDSCAPE: ViewportSize = { width: 750, height: 342 }
const LANDSCAPE_WIDE: ViewportSize = { width: 844, height: 390 }
/** 下限咬住的竖向矮视口：改动前图表区实测 103px（主体 191px）。 */
const SHORT_PORTRAIT: ViewportSize = { width: 320, height: 480 }
/** 下限咬住的矮视口横屏：改动前图表区实测 87px（主体 175px）。 */
const SHORT_LANDSCAPE: ViewportSize = { width: 568, height: 320 }
const DESKTOP: ViewportSize = { width: 1280, height: 900 }

function label(page: Page, testInfo: TestInfo) {
  const viewport = requireViewport(page)
  return `${testInfo.project.name} ${viewport.width}x${viewport.height}`
}

function report(name: string, where: string, measure: SheetChartMeasure) {
  console.log(`river-sheet-chart ${name} @ ${where}`, JSON.stringify(measure))
}

/** 图表区高不低于该视口的下限、整盒在抽屉盒内、canvas 与图表区同尺寸（`measureSettledSheetChart` 已保证）。 */
function expectReadableChart(measure: SheetChartMeasure, viewport: ViewportSize, where: string) {
  const floor = chartFloorPx(viewport)
  expect(measure.chart.height, `图表区高 ${measure.chart.height} 应 ≥ ${floor} @ ${where}`).toBeGreaterThanOrEqual(floor)
  expect(
    contains(measure.frame, measure.chart),
    `图表区 ${JSON.stringify(measure.chart)} 应在抽屉盒 ${JSON.stringify(measure.frame)} 内 @ ${where}`,
  ).toBe(true)
}

/** 下限咬住的抽屉：图表区恰为下限、主体溢出并由它滚动、滚到底后图表区完整可见且底边留出容器的下内边距。 */
async function expectFloorBites(page: Page, viewport: ViewportSize, where: string) {
  const floor = chartFloorPx(viewport)
  const parts = curveWindowParts(page, 'river')
  const sheet = await expectSheet(page, 'river', where)
  const before = await measureSettledSheetChart(page, RIVER, where)
  report('floor bites', where, before)

  // 硬前提：这个视口里主体确实溢出——否则下面的“滚到底”是空转。
  expect(before.body.scrollHeight, `前提：主体应溢出（scrollHeight ${before.body.scrollHeight} > clientHeight ${before.body.clientHeight}）@ ${where}`).toBeGreaterThan(
    before.body.clientHeight,
  )
  expect(before.body.overflowY).toBe('auto')
  expect(before.nestedScrollerCount, '主体内部不应有第二个滚动容器').toBe(0)

  expect(Math.abs(before.chart.height - floor), `图表区高 ${before.chart.height} 应为下限 ${floor} @ ${where}`).toBeLessThanOrEqual(CHART_EXACT_TOLERANCE_PX)
  expect(
    contains(before.parent, before.chart),
    `图表区应完整在其父容器盒内，越界量 ${JSON.stringify(overflowOf(before.parent, before.chart))} @ ${where}`,
  ).toBe(true)

  await scrollSheetBodyToEnd(page, RIVER)
  const scrolled = await measureSheetChart(page, RIVER)
  report('floor bites, scrolled', where, scrolled)
  expect(scrolled.body.scrollTop, '主体应真的滚动了').toBeGreaterThan(0)
  expect(scrolled.frame, '主体滚动不改变抽屉盒').toEqual(sheet.frame)
  expect(
    contains(scrolled.body.visible, scrolled.chart),
    `滚到底后图表区应完整落在主体可视盒内，越界量 ${JSON.stringify(overflowOf(scrolled.body.visible, scrolled.chart))} @ ${where}`,
  ).toBe(true)
  expect(contains(scrolled.frame, scrolled.chart), `滚到底后图表区应在抽屉盒内 @ ${where}`).toBe(true)
  expect(scrolled.parentPaddingBottom, '已加载容器的下内边距').toBe(CHART_BOTTOM_GAP_PX)
  const bottomGap = scrolled.body.visible.y + scrolled.body.visible.height - (scrolled.chart.y + scrolled.chart.height)
  expect(bottomGap, `滚到底后图表区底边距主体可视底边 ${bottomGap} 应 ≥ ${CHART_BOTTOM_GAP_PX} @ ${where}`).toBeGreaterThanOrEqual(
    CHART_BOTTOM_GAP_PX - CHART_EXACT_TOLERANCE_PX,
  )

  for (const [partName, locator] of [['标题', parts.title], ['关闭按钮', parts.close]] as const) {
    await expect(locator, `${partName}应可见 @ ${where}`).toBeVisible()
    expect(contains(scrolled.frame, await boxOf(locator, partName)), `主体滚动后${partName}应在抽屉盒内 @ ${where}`).toBe(true)
  }
}

/**
 * 同一河段窗（不重开）从 `from` 转到 `to`：canvas 跟上新图表区、尺寸与旋转前不同、图表区仍在抽屉内，
 * 且图表区高度等于“在 `to` 新开页面、新打开的河段窗”的图表区高度（防高度棘轮）。
 */
async function expectChartFollowsRotation(page: Page, testInfo: TestInfo, from: ViewportSize, to: ViewportSize) {
  await openLoadedCurveWindow(page, from, RIVER)
  const frame = curveWindowParts(page, 'river').frame
  await expectSheet(page, 'river', label(page, testInfo))
  const before = await measureSettledSheetChart(page, RIVER, label(page, testInfo))
  await frame.evaluate((element) => {
    ;(element as HTMLElement & { __sheetChartMark?: string }).__sheetChartMark = 'before-rotation'
  })

  await page.setViewportSize(to)
  const where = label(page, testInfo)
  // 形态经 matchMedia 订阅异步落定：先等抽屉换成新视口的那一种，再等画布跟上。
  await expectSheet(page, 'river', where)
  const after = await measureSettledSheetChart(page, RIVER, where)
  expect(
    await frame.evaluate((element) => (element as HTMLElement & { __sheetChartMark?: string }).__sheetChartMark),
    '旋转后应是同一个窗节点（没有重开）',
  ).toBe('before-rotation')
  expectReadableChart(after, to, where)

  const beforeCanvas = before.canvas!
  const afterCanvas = after.canvas!
  expect(Math.abs(afterCanvas.width - beforeCanvas.width), `旋转前后 canvas 宽应不同（${beforeCanvas.width} → ${afterCanvas.width}）`).toBeGreaterThan(CHART_SIZE_TOLERANCE_PX)
  expect(Math.abs(afterCanvas.height - beforeCanvas.height), `旋转前后 canvas 高应不同（${beforeCanvas.height} → ${afterCanvas.height}）`).toBeGreaterThan(CHART_SIZE_TOLERANCE_PX)

  // 对照：同一浏览器上下文里新开一个页面，在目标视口新打开河段窗。
  const freshPage = await page.context().newPage()
  await openLoadedCurveWindow(freshPage, to, RIVER)
  await expectSheet(freshPage, 'river', `${where} fresh`)
  const fresh = await measureSettledSheetChart(freshPage, RIVER, `${where} fresh`)
  await freshPage.close()

  console.log(
    `river-sheet-chart rotation @ ${where}`,
    JSON.stringify({ before: { chart: before.chart, canvas: before.canvas }, after: { chart: after.chart, canvas: after.canvas, body: after.body }, fresh: { chart: fresh.chart, body: fresh.body } }),
  )
  expect(
    Math.abs(after.chart.height - fresh.chart.height),
    `旋转后图表区高 ${after.chart.height} 应等于新开窗的图表区高 ${fresh.chart.height}（旋转前 ${before.chart.height}）@ ${where}`,
  ).toBeLessThanOrEqual(CHART_SIZE_TOLERANCE_PX)
  expect(Math.abs(after.chart.width - fresh.chart.width)).toBeLessThanOrEqual(CHART_SIZE_TOLERANCE_PX)
  expect(after.body.scrollHeight, '旋转后主体的内容高应等于新开窗的').toBe(fresh.body.scrollHeight)
}

test.describe('M11 河段抽屉图表可读高度', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
  })

  test('(a) 390x664：图表区高 ≥ 160、在抽屉盒内，canvas 与图表区同尺寸', async ({ page }, testInfo) => {
    await openLoadedCurveWindow(page, PORTRAIT, RIVER)
    const where = label(page, testInfo)
    expect(chartFloorPx(requireViewport(page))).toBe(CHART_FLOOR_PX)
    await expectSheet(page, 'river', where)

    const measure = await measureSettledSheetChart(page, RIVER, where)
    report('(a)', where, measure)
    expectReadableChart(measure, PORTRAIT, where)
  })

  for (const viewport of [LANDSCAPE, LANDSCAPE_WIDE]) {
    test(`(b) ${viewport.width}x${viewport.height}：图表区高 ≥ 120、在抽屉盒内，canvas 与图表区同尺寸`, async ({ page }, testInfo) => {
      await openLoadedCurveWindow(page, viewport, RIVER)
      const where = label(page, testInfo)
      expect(chartFloorPx(requireViewport(page))).toBe(CHART_FLOOR_SHORT_LANDSCAPE_PX)
      await expectSheet(page, 'river', where)

      const measure = await measureSettledSheetChart(page, RIVER, where)
      report('(b)', where, measure)
      expectReadableChart(measure, viewport, where)
    })
  }

  test('(c) 320x480：下限咬住——图表区高 = 160，主体溢出并滚动，滚到底后图表区完整可见且底边留 ≥ 8px', async ({ page }, testInfo) => {
    await openLoadedCurveWindow(page, SHORT_PORTRAIT, RIVER)
    expect(isShortLandscape(requireViewport(page))).toBe(false)
    expect(chartFloorPx(requireViewport(page))).toBe(CHART_FLOOR_PX)

    await expectFloorBites(page, SHORT_PORTRAIT, label(page, testInfo))
  })

  test('(d) 568x320：下限咬住——图表区高 = 120，主体溢出并滚动，滚到底后图表区完整可见且底边留 ≥ 8px', async ({ page }, testInfo) => {
    await openLoadedCurveWindow(page, SHORT_LANDSCAPE, RIVER)
    expect(isShortLandscape(requireViewport(page))).toBe(true)
    expect(chartFloorPx(requireViewport(page))).toBe(CHART_FLOOR_SHORT_LANDSCAPE_PX)

    await expectFloorBites(page, SHORT_LANDSCAPE, label(page, testInfo))
  })

  test('(e) 390x664：剩余高度足够时图表区仍占满——高 > 160，主体不溢出，图表区底边距主体底边恰为下内边距', async ({ page }, testInfo) => {
    await openLoadedCurveWindow(page, PORTRAIT, RIVER)
    const where = label(page, testInfo)
    await expectSheet(page, 'river', where)

    const measure = await measureSettledSheetChart(page, RIVER, where)
    report('(e)', where, measure)
    expect(measure.chart.height, `图表区高 ${measure.chart.height} 应 > ${CHART_FLOOR_PX}（没有被钉在下限）`).toBeGreaterThan(CHART_FLOOR_PX)
    expect(measure.body.scrollHeight, '主体不应溢出').toBe(measure.body.clientHeight)
    // 占满：图表区一直伸到已加载容器的下内边距处，容器底贴主体可视底边。
    const bottomGap = measure.body.visible.y + measure.body.visible.height - (measure.chart.y + measure.chart.height)
    expect(Math.abs(bottomGap - CHART_BOTTOM_GAP_PX), `图表区底边距主体可视底边 ${bottomGap} 应为 ${CHART_BOTTOM_GAP_PX}`).toBeLessThanOrEqual(CHART_EXACT_TOLERANCE_PX)
  })

  for (const [viewport, expected] of [
    [PORTRAIT, `${CHART_FLOOR_PX}px`],
    [LANDSCAPE, `${CHART_FLOOR_SHORT_LANDSCAPE_PX}px`],
    // 桌面形态：改动前（origin/master）实测即 0px。
    [DESKTOP, '0px'],
  ] as const) {
    test(`(f) ${viewport.width}x${viewport.height}：图表区计算 min-height 为 ${expected}`, async ({ page }) => {
      await openLoadedCurveWindow(page, viewport, RIVER)

      const measure = await measureSheetChart(page, RIVER)
      expect(measure.chartMinHeight).toBe(expected)
    })
  }

  test('(g) 390x664 → 750x342：canvas 跟上新图表区，图表区高度等于在 750x342 新开窗的高度', async ({ page }, testInfo) => {
    await expectChartFollowsRotation(page, testInfo, PORTRAIT, LANDSCAPE)
  })

  test('(h) 750x342 → 390x664：canvas 跟上新图表区，图表区高度等于在 390x664 新开窗的高度', async ({ page }, testInfo) => {
    await expectChartFollowsRotation(page, testInfo, LANDSCAPE, PORTRAIT)
  })
})

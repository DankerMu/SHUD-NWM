import { expect, test, type Page, type TestInfo } from '@playwright/test'

import { curveWindowParts, expectSheet, measureCurveWindow } from './support/curveSheet.mocked'
import type { Box } from './support/legendLauncher.mocked'
import { boxOf } from './support/overlayLaunchers.mocked'
import { measureSettledSheetChart, measureSheetChart, openLoadedCurveWindow, type SheetChartTarget } from './support/sheetChart.mocked'
import { dragOneFinger, pinchHorizontally, tapWithTouch, type TouchPoint } from './support/touchGestures'
import { isMobileForm, requireViewport, type ViewportSize } from './support/viewportForm'

/**
 * 河段曲线的触屏缩放（openspec mobile-responsive-display task 4.7，design.md D12 后三条）。
 *
 * 用例 (a)–(g) 对应 tasks.md 里 #2808 的 Triage。每条用例在用例内 `setViewportSize`，所以三个移动
 * project 下执行的是同一组断言，不按 project 跳过。全部在“曲线已加载”（图表 canvas 可见）之后做；
 * 手势一律经 `support/touchGestures.ts` 的 CDP 合成。缩放 / 平移的派发有节流与约 100ms 动画，
 * 属性读数都用轮询。
 */

const RIVER: SheetChartTarget = { kind: 'river' }

const PORTRAIT: ViewportSize = { width: 390, height: 664 }
const LANDSCAPE: ViewportSize = { width: 750, height: 342 }
/** 4.4 的下限咬住、抽屉主体溢出的竖向矮视口。 */
const SHORT_PORTRAIT: ViewportSize = { width: 320, height: 480 }

/** 紧凑图表的绘图区（grid）相对图表区四边的内缩：独立抄自图表配置，不读产品常量。 */
const GRID_INSET_PX = { left: 48, right: 16, top: 16, bottom: 28 }
const SPAN_TOLERANCE = 0.5
/** 平移 / 捏合的手势幅度（CSS px）。 */
const PAN_PX = 40
const PINCH_FROM_PX = 40
const PINCH_TO_PX = 160
const SCROLL_DRAG_PX = 50
/** “不应发生变化”的判据在手势结束后等这么久再读（节流 + 动画的数倍）。 */
const SETTLE_MS = 400

type ZoomWindow = { start: number; end: number }

function label(page: Page, testInfo: TestInfo) {
  const viewport = requireViewport(page)
  return `${testInfo.project.name} ${viewport.width}x${viewport.height}`
}

function chartArea(page: Page) {
  return page.getByTestId('m11-river-forecast-panel').getByTestId('m11-river-panel-chart')
}

function sheetBody(page: Page) {
  return curveWindowParts(page, 'river').body
}

/** 图表区上的缩放窗口属性；属性缺失或不是数字时为 NaN（任何比较都不成立）。 */
async function readZoomWindow(page: Page): Promise<ZoomWindow> {
  const area = chartArea(page)
  const [start, end] = await Promise.all([area.getAttribute('data-zoom-start'), area.getAttribute('data-zoom-end')])
  const parse = (value: string | null) => (value === null || value.trim() === '' ? Number.NaN : Number(value))
  return { start: parse(start), end: parse(end) }
}

const spanOf = (zoomWindow: ZoomWindow) => zoomWindow.end - zoomWindow.start

/** 轮询到缩放窗口不再变化（相隔 150ms 的两次读数相同）并返回它。 */
async function settledZoomWindow(page: Page): Promise<ZoomWindow> {
  let last: ZoomWindow | undefined
  await expect(async () => {
    const first = await readZoomWindow(page)
    await page.waitForTimeout(150)
    last = await readZoomWindow(page)
    expect(Number.isFinite(last.start) && Number.isFinite(last.end), `缩放窗口属性应为数字，实际 ${JSON.stringify(last)}`).toBe(true)
    expect(last, '缩放窗口应已稳定').toEqual(first)
  }).toPass({ timeout: 4_000 })
  return last!
}

async function expectFullRange(page: Page, where: string) {
  await expect(chartArea(page), `data-zoom-start @ ${where}`).toHaveAttribute('data-zoom-start', '0')
  await expect(chartArea(page), `data-zoom-end @ ${where}`).toHaveAttribute('data-zoom-end', '100')
}

/** 绘图区（grid）的视口包围盒。 */
async function plotBox(page: Page): Promise<Box> {
  const { chart } = await measureSheetChart(page, RIVER)
  const plot = {
    x: chart.x + GRID_INSET_PX.left,
    y: chart.y + GRID_INSET_PX.top,
    width: chart.width - GRID_INSET_PX.left - GRID_INSET_PX.right,
    height: chart.height - GRID_INSET_PX.top - GRID_INSET_PX.bottom,
  }
  expect(plot.width, `绘图区宽 ${plot.width} 应容得下捏合手势`).toBeGreaterThan(PINCH_TO_PX)
  expect(plot.height, '绘图区应有高度').toBeGreaterThan(0)
  return plot
}

const centerOf = (box: Box): TouchPoint => ({ x: box.x + box.width / 2, y: box.y + box.height / 2 })

async function openRiverSheet(page: Page, viewport: ViewportSize, testInfo: TestInfo) {
  await openLoadedCurveWindow(page, viewport, RIVER)
  const where = label(page, testInfo)
  await expectSheet(page, 'river', where)
  // 画布跟上图表区之后绘图区的位置才是最终的。
  await measureSettledSheetChart(page, RIVER, where)
  return where
}

/**
 * 在绘图区中央做一次向外捏合，等缩放窗口落定并返回。断言 `0 ≤ start < end ≤ 100` 且跨度 < 100——
 * 后续的平移 / 旋转用例把它当硬前提。
 */
async function pinchOut(page: Page, where: string): Promise<{ plot: Box; zoomed: ZoomWindow }> {
  const plot = await plotBox(page)
  await pinchHorizontally(page, centerOf(plot), PINCH_FROM_PX, PINCH_TO_PX)
  await expect.poll(async () => spanOf(await readZoomWindow(page)), { message: `捏合后跨度应 < 100 @ ${where}` }).toBeLessThan(100)
  const zoomed = await settledZoomWindow(page)
  console.log(`river-chart-touch pinch @ ${where}`, JSON.stringify({ plot, zoomed }))
  expect(zoomed.start, `start ${zoomed.start} 应 ≥ 0`).toBeGreaterThanOrEqual(0)
  expect(zoomed.end, `end ${zoomed.end} 应 ≤ 100`).toBeLessThanOrEqual(100)
  expect(zoomed.start, `start ${zoomed.start} 应 < end ${zoomed.end}`).toBeLessThan(zoomed.end)
  expect(spanOf(zoomed), `跨度 ${spanOf(zoomed)} 应 < 100`).toBeLessThan(100)
  return { plot, zoomed }
}

test.describe('M11 河段曲线触屏缩放', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
  })

  for (const viewport of [PORTRAIT, LANDSCAPE]) {
    const size = `${viewport.width}x${viewport.height}`

    test(`(a) ${size} 初始：缩放窗口为 0 / 100，提示文案是双指手势`, async ({ page }, testInfo) => {
      const where = await openRiverSheet(page, viewport, testInfo)

      await expectFullRange(page, where)
      const hint = page.getByTestId('m11-river-panel-zoom-hint')
      await expect(hint).toBeVisible()
      await expect(hint).toContainText('双指')
      await expect(hint).not.toContainText('滚轮')
      // 手势归属的样式事实：浏览器只接管纵向滚动，横向拖动与捏合留给图表。行为由 (b)(c)(f) 断言，
      // 但合成手势下去掉这条声明它们仍然通过，所以这里另钉计算样式。
      expect(await chartArea(page).evaluate((element) => getComputedStyle(element).touchAction), `图表区的 touch-action @ ${where}`).toBe('pan-y')
    })

    test(`(b) ${size}：绘图区内双指向外捏合只缩放时间轴，不缩放页面、不动抽屉`, async ({ page }, testInfo) => {
      const where = await openRiverSheet(page, viewport, testInfo)
      const before = await measureCurveWindow(page, 'river')

      await pinchOut(page, where)

      expect(await page.evaluate(() => window.visualViewport?.scale), '捏合不应触发页面缩放').toBe(1)
      expect((await measureCurveWindow(page, 'river')).frame, '捏合不应改变抽屉包围盒').toEqual(before.frame)
    })

    test(`(c) ${size}：放大后单指横向拖动平移时间轴，跨度不变，反向拖动往回走`, async ({ page }, testInfo) => {
      const where = await openRiverSheet(page, viewport, testInfo)
      // 硬前提（pinchOut 内断言）：跨度 < 100，否则没有可平移的余地。
      const { plot, zoomed } = await pinchOut(page, where)
      const span = spanOf(zoomed)

      await dragOneFinger(page, centerOf(plot), -PAN_PX, 0)
      await expect
        .poll(async () => Math.abs((await readZoomWindow(page)).start - zoomed.start), { message: `单指横向拖动后 start 应改变 @ ${where}` })
        .toBeGreaterThan(SPAN_TOLERANCE)
      const panned = await settledZoomWindow(page)
      expect(Math.abs(spanOf(panned) - span), `平移后跨度 ${spanOf(panned)} 应仍为 ${span}`).toBeLessThanOrEqual(SPAN_TOLERANCE)

      const direction = Math.sign(panned.start - zoomed.start)
      await dragOneFinger(page, centerOf(plot), PAN_PX, 0)
      await expect
        .poll(async () => ((await readZoomWindow(page)).start - panned.start) * direction, { message: `反向拖动后 start 应朝反方向变化 @ ${where}` })
        .toBeLessThan(-SPAN_TOLERANCE)
      const back = await settledZoomWindow(page)
      console.log(`river-chart-touch pan @ ${where}`, JSON.stringify({ zoomed, panned, back }))
      expect(Math.abs(spanOf(back) - span), `拖回后跨度 ${spanOf(back)} 应仍为 ${span}`).toBeLessThanOrEqual(SPAN_TOLERANCE)
    })
  }

  test('(d) 390x664 未缩放时单指横向拖动：缩放窗口仍为 0 / 100', async ({ page }, testInfo) => {
    const where = await openRiverSheet(page, PORTRAIT, testInfo)
    await expectFullRange(page, where)

    await dragOneFinger(page, centerOf(await plotBox(page)), -PAN_PX, 0)
    // 手势确实落在图表上：按下即触发 tooltip。
    await expect(chartArea(page)).toHaveAttribute('data-tooltip-visible', 'true')
    await page.waitForTimeout(SETTLE_MS)

    await expectFullRange(page, where)
  })

  test('(e) 390x664：点按绘图区内一点后 tooltip 可见', async ({ page }, testInfo) => {
    await openRiverSheet(page, PORTRAIT, testInfo)
    // 硬前提：点按前不可见。
    await expect(chartArea(page)).toHaveAttribute('data-tooltip-visible', 'false')

    const plot = await plotBox(page)
    await tapWithTouch(page, { x: plot.x + plot.width * 0.6, y: plot.y + plot.height / 2 })

    await expect(chartArea(page)).toHaveAttribute('data-tooltip-visible', 'true')
  })

  test('(f) 320x480：绘图区内起手的单指纵向拖动仍滚动抽屉主体，不触发缩放', async ({ page }, testInfo) => {
    const where = await openRiverSheet(page, SHORT_PORTRAIT, testInfo)
    const body = sheetBody(page)
    const scrollTop = () => body.evaluate((element) => element.scrollTop)
    const measure = await measureSheetChart(page, RIVER)
    // 硬前提：主体确实溢出。
    expect(
      measure.body.scrollHeight,
      `前提：主体应溢出（scrollHeight ${measure.body.scrollHeight} > clientHeight ${measure.body.clientHeight}）@ ${where}`,
    ).toBeGreaterThan(measure.body.clientHeight)
    expect(measure.body.scrollTop).toBe(0)

    // 对照：在主体内、图表区之外（起报时次条的右端空白）起手——证明合成手势能驱动浏览器滚动。
    const cycleBar = await boxOf(page.getByTestId('m11-river-panel-cycle-bar'), '起报时次条')
    const control = { x: cycleBar.x + cycleBar.width - 24, y: cycleBar.y + cycleBar.height / 2 }
    expect(control.y, '对照起手点应在图表区之上').toBeLessThan(measure.chart.y)
    await dragOneFinger(page, control, 0, -SCROLL_DRAG_PX)
    await expect.poll(scrollTop, { message: `对照：图表区之外起手的纵向拖动应滚动主体 @ ${where}` }).toBeGreaterThan(0)
    const controlScrollTop = await scrollTop()

    await body.evaluate((element) => {
      element.scrollTop = 0
    })
    await expect.poll(scrollTop).toBe(0)

    // 绘图区内起手：取绘图区与主体可视盒的交集里靠下的一点，向上拖。
    const plot = await plotBox(page)
    const visibleBottom = measure.body.visible.y + measure.body.visible.height
    const start = { x: plot.x + plot.width / 2, y: Math.min(plot.y + plot.height, visibleBottom) - 12 }
    expect(start.y, '起手点应在绘图区内').toBeGreaterThan(plot.y)
    expect(start.y, '起手点应在绘图区内').toBeLessThan(plot.y + plot.height)
    await dragOneFinger(page, start, 0, -SCROLL_DRAG_PX)
    await expect.poll(scrollTop, { message: `绘图区内起手的纵向拖动应滚动主体 @ ${where}` }).toBeGreaterThan(0)
    console.log(`river-chart-touch scroll @ ${where}`, JSON.stringify({ control, controlScrollTop, start, scrollTop: await scrollTop(), plot, body: measure.body }))

    await page.waitForTimeout(SETTLE_MS)
    await expectFullRange(page, where)
  })

  test('(g) 390x664 捏合后转到 750x342：缩放窗口保持', async ({ page }, testInfo) => {
    const where = await openRiverSheet(page, PORTRAIT, testInfo)
    const { zoomed } = await pinchOut(page, where)

    await page.setViewportSize(LANDSCAPE)
    const rotatedWhere = label(page, testInfo)
    await expectSheet(page, 'river', rotatedWhere)
    await measureSettledSheetChart(page, RIVER, rotatedWhere)

    const rotated = await settledZoomWindow(page)
    console.log(`river-chart-touch rotation @ ${rotatedWhere}`, JSON.stringify({ zoomed, rotated }))
    expect(spanOf(rotated), `旋转后跨度 ${spanOf(rotated)} 应仍 < 100`).toBeLessThan(100)
    expect(Math.abs(spanOf(rotated) - spanOf(zoomed)), `旋转后跨度 ${spanOf(rotated)} 应等于旋转前的 ${spanOf(zoomed)}`).toBeLessThanOrEqual(SPAN_TOLERANCE)
    expect(Math.abs(rotated.start - zoomed.start), `旋转后 start ${rotated.start} 应等于旋转前的 ${zoomed.start}`).toBeLessThanOrEqual(SPAN_TOLERANCE)
  })
})

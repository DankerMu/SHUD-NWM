import { expect, test, type Page, type TestInfo } from '@playwright/test'

import type { components } from '../src/api/types'
import { curveWindowParts, expectSheet, measureCurveWindow, openCurveWindow } from './support/curveSheet.mocked'
import type { Box } from './support/legendLauncher.mocked'
import { boxOf } from './support/overlayLaunchers.mocked'
import { installRiverWindowMocks, MOCK_CYCLE, MOCK_VALID_TIMES, mockModel, mockRuns, mockStation } from './support/riverWindow.mocked'
import {
  measureSettledSheetChart,
  measureSheetChart,
  openLoadedCurveWindow,
  sheetChartCanvas,
  type SheetChartTarget,
} from './support/sheetChart.mocked'
import { dragOneFinger, pinchHorizontally, tapWithTouch, type TouchPoint } from './support/touchGestures'
import { isMobileForm, requireViewport, type ViewportSize } from './support/viewportForm'

/**
 * 气象代站曲线的触屏缩放（openspec mobile-responsive-display task 4.8，design.md D12 后三条）。
 *
 * 用例 (a)–(g) 对应 tasks.md 里 #2809 的 Triage；(l) 是口径 (3) 的销毁重建路径在浏览器里的那一条。每条用例在
 * 用例内 `setViewportSize`，所以三个移动 project 下执行的是同一组断言，不按 project 跳过。全部在
 * “曲线已加载”（图表 canvas 可见）之后做；手势一律经 `support/touchGestures.ts` 的 CDP 合成。
 * 缩放 / 平移的派发有节流与约 100ms 动画，属性读数都用轮询。
 */

type Schemas = components['schemas']

const CHART_AREA_TEST_ID = 'm11-station-panel-chart'
const STATION: SheetChartTarget = { kind: 'station', chartTestId: CHART_AREA_TEST_ID }

const PORTRAIT: ViewportSize = { width: 390, height: 664 }
const LANDSCAPE: ViewportSize = { width: 750, height: 342 }
/** 4.5 的下限咬住、抽屉主体溢出的竖向矮视口。 */
const SHORT_PORTRAIT: ViewportSize = { width: 320, height: 480 }

/** 图表的绘图区（grid）相对图表区四边的内缩：独立抄自图表配置，不读产品常量。 */
const GRID_INSET_PX = { left: 48, right: 16, top: 18, bottom: 28 }
const SPAN_TOLERANCE = 0.5
/** 平移 / 捏合的手势幅度（CSS px）。 */
const PAN_PX = 40
const PINCH_FROM_PX = 40
const PINCH_TO_PX = 160
const SCROLL_DRAG_PX = 50
/** (e) 的起手点到主体可视底边的距离。 */
const SCROLL_START_INSET_PX = 12
/** “不应发生变化”的判据在手势结束后等这么久再读（节流 + 动画的数倍）。 */
const SETTLE_MS = 400

type ZoomWindow = { start: number; end: number }

function label(page: Page, testInfo: TestInfo) {
  const viewport = requireViewport(page)
  return `${testInfo.project.name} ${viewport.width}x${viewport.height}`
}

function stationWindow(page: Page) {
  return curveWindowParts(page, 'station').frame
}

function chartArea(page: Page) {
  return stationWindow(page).getByTestId(CHART_AREA_TEST_ID)
}

function variableToggle(page: Page, variable: string) {
  return stationWindow(page).getByTestId(`m11-station-variable-toggle-${variable}`)
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
  const { chart } = await measureSheetChart(page, STATION)
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

async function settleStationSheet(page: Page, testInfo: TestInfo) {
  const where = label(page, testInfo)
  await expectSheet(page, 'station', where)
  // 画布跟上图表区之后绘图区的位置才是最终的。
  await measureSettledSheetChart(page, STATION, where)
  return where
}

async function openStationSheet(page: Page, viewport: ViewportSize, testInfo: TestInfo) {
  await openLoadedCurveWindow(page, viewport, STATION)
  return settleStationSheet(page, testInfo)
}

/**
 * 在绘图区中央做一次向外捏合，等缩放窗口落定并返回。断言 `0 ≤ start < end ≤ 100` 且跨度 < 100——
 * 后续的平移 / 旋转 / 复位用例把它当硬前提。
 */
async function pinchOut(page: Page, where: string): Promise<{ plot: Box; zoomed: ZoomWindow }> {
  const plot = await plotBox(page)
  await pinchHorizontally(page, centerOf(plot), PINCH_FROM_PX, PINCH_TO_PX)
  await expect.poll(async () => spanOf(await readZoomWindow(page)), { message: `捏合后跨度应 < 100 @ ${where}` }).toBeLessThan(100)
  const zoomed = await settledZoomWindow(page)
  console.log(`station-chart-touch pinch @ ${where}`, JSON.stringify({ plot, zoomed }))
  expect(zoomed.start, `start ${zoomed.start} 应 ≥ 0`).toBeGreaterThanOrEqual(0)
  expect(zoomed.end, `end ${zoomed.end} 应 ≤ 100`).toBeLessThanOrEqual(100)
  expect(zoomed.start, `start ${zoomed.start} 应 < end ${zoomed.end}`).toBeLessThan(zoomed.end)
  expect(spanOf(zoomed), `跨度 ${spanOf(zoomed)} 应 < 100`).toBeLessThan(100)
  return { plot, zoomed }
}

/** 基线 mock 里有曲线、而 `installStationSeriesWithoutDroppedVariable` 的响应里两个源都没有的要素。 */
const DROPPED_VARIABLE = 'RH'
const SERVED_VARIABLES = [
  { variable: 'PRCP', unit: 'mm', base: 0.4 },
  { variable: 'TEMP', unit: 'degC', base: 6 },
] as const

/**
 * 气象代站序列的路由覆盖：两个源都只带 PRCP 与 TEMP，其余要素切过去即进入“无可绘制序列”的空态
 * （图表实例被销毁）。须在 `installRiverWindowMocks` 之后、导航之前注册。响应由基线 mock 的导出常量拼出。
 */
async function installStationSeriesWithoutDroppedVariable(page: Page) {
  await page.route(
    (url) => url.pathname === `/api/v1/met/stations/${mockStation.station_id}/series`,
    (route) => {
      const source = new URL(route.request().url()).searchParams.get('source_id')?.toUpperCase() === 'IFS' ? 'IFS' : 'GFS'
      const returned = { from: MOCK_VALID_TIMES[0], to: MOCK_VALID_TIMES[MOCK_VALID_TIMES.length - 1] }
      const data = {
        station_id: mockStation.station_id,
        station: mockStation,
        forcing_version_id: mockRuns[source].forcing_version_id,
        model_id: mockModel.model_id,
        source_id: source,
        cycle_time: MOCK_CYCLE,
        valid_time_start: mockRuns[source].start_time,
        valid_time_end: mockRuns[source].end_time,
        limit: 480,
        requested_from: null,
        requested_to: null,
        series: SERVED_VARIABLES.map(({ variable, unit, base }) => ({
          variable,
          unit,
          native_resolution: 'PT3H',
          source_id: source,
          cycle_time: MOCK_CYCLE,
          points: MOCK_VALID_TIMES.map((validTime, index) => ({
            valid_time: validTime,
            value: base + index * 0.5,
            quality_flag: 'ok',
            source_id: source,
          })),
          truncated: false,
          metadata: {
            limit: 480,
            returned_points: MOCK_VALID_TIMES.length,
            requested_from: null,
            requested_to: null,
            returned_from: returned.from,
            returned_to: returned.to,
            truncated: false,
          },
        })),
      } satisfies Schemas['StationSeriesResponse']
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', data }) })
    },
  )
}

test.describe('M11 气象代站曲线触屏缩放', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
  })

  for (const viewport of [PORTRAIT, LANDSCAPE]) {
    const size = `${viewport.width}x${viewport.height}`

    test(`(a) ${size} 初始：缩放窗口为 0 / 100，tooltip 不可见，图表区只让浏览器接管纵向滚动`, async ({ page }, testInfo) => {
      const where = await openStationSheet(page, viewport, testInfo)

      await expectFullRange(page, where)
      await expect(chartArea(page)).toHaveAttribute('data-tooltip-visible', 'false')
      // 手势归属的样式事实：横向拖动与捏合留给图表。行为由 (b)(c)(e) 断言，但合成手势下去掉这条声明
      // 它们仍然通过，所以这里另钉计算样式。
      expect(await chartArea(page).evaluate((element) => getComputedStyle(element).touchAction), `图表区的 touch-action @ ${where}`).toBe('pan-y')
    })

    test(`(b) ${size}：绘图区内双指向外捏合只缩放时间轴，不缩放页面、不动抽屉`, async ({ page }, testInfo) => {
      const where = await openStationSheet(page, viewport, testInfo)
      const before = await measureCurveWindow(page, 'station')

      await pinchOut(page, where)

      expect(await page.evaluate(() => window.visualViewport?.scale), '捏合不应触发页面缩放').toBe(1)
      expect((await measureCurveWindow(page, 'station')).frame, '捏合不应改变抽屉包围盒').toEqual(before.frame)
    })

    test(`(c) ${size}：放大后单指横向拖动平移时间轴，跨度不变，反向拖动往回走`, async ({ page }, testInfo) => {
      const where = await openStationSheet(page, viewport, testInfo)
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
      console.log(`station-chart-touch pan @ ${where}`, JSON.stringify({ zoomed, panned, back }))
      expect(Math.abs(spanOf(back) - span), `拖回后跨度 ${spanOf(back)} 应仍为 ${span}`).toBeLessThanOrEqual(SPAN_TOLERANCE)
    })
  }

  test('(d) 390x664：点按绘图区内一点后 tooltip 可见', async ({ page }, testInfo) => {
    await openStationSheet(page, PORTRAIT, testInfo)
    // 硬前提：点按前不可见。
    await expect(chartArea(page)).toHaveAttribute('data-tooltip-visible', 'false')

    const plot = await plotBox(page)
    await tapWithTouch(page, { x: plot.x + plot.width * 0.6, y: plot.y + plot.height / 2 })

    await expect(chartArea(page)).toHaveAttribute('data-tooltip-visible', 'true')
  })

  test('(e) 320x480：绘图区内起手的单指纵向拖动仍滚动抽屉主体，不触发缩放', async ({ page }, testInfo) => {
    const where = await openStationSheet(page, SHORT_PORTRAIT, testInfo)
    const body = curveWindowParts(page, 'station').body
    const scrollTop = () => body.evaluate((element) => element.scrollTop)
    const setScrollTop = (top: number) =>
      body.evaluate((element, value) => {
        element.scrollTop = value
      }, top)
    const measure = await measureSheetChart(page, STATION)
    // 硬前提：主体确实溢出。
    expect(
      measure.body.scrollHeight,
      `前提：主体应溢出（scrollHeight ${measure.body.scrollHeight} > clientHeight ${measure.body.clientHeight}）@ ${where}`,
    ).toBeGreaterThan(measure.body.clientHeight)
    expect(measure.body.scrollTop).toBe(0)
    const visible = measure.body.visible
    const visibleBottom = visible.y + visible.height

    // 对照：在主体内、图表区之外（图例行右端的文字）起手——证明合成手势能驱动浏览器滚动。
    const control = centerOf(await boxOf(stationWindow(page).getByText('单要素双源同轴', { exact: true }), '图例行'))
    expect(control.y, '对照起手点应在图表区之上').toBeLessThan(measure.chart.y)
    expect(control.y, '对照起手点应在主体可视盒内').toBeGreaterThan(visible.y)
    await dragOneFinger(page, control, 0, -SCROLL_DRAG_PX)
    await expect.poll(scrollTop, { message: `对照：图表区之外起手的纵向拖动应滚动主体 @ ${where}` }).toBeGreaterThan(0)
    const controlScrollTop = await scrollTop()

    // 复位。未滚动时绘图区在主体可视盒内只露出几个像素（落不稳一个起手点），所以不复位到 0，而是复位到
    // “绘图区中线恰在可视底边之上 SCROLL_START_INSET_PX”的位置，再从那里起手。
    await setScrollTop(0)
    await expect.poll(scrollTop).toBe(0)
    const unscrolledPlot = await plotBox(page)
    await setScrollTop(Math.max(0, centerOf(unscrolledPlot).y - (visibleBottom - SCROLL_START_INSET_PX)))
    const resting = await scrollTop()
    expect(
      measure.body.scrollHeight - measure.body.clientHeight - resting,
      `前提：起手位置（scrollTop ${resting}）之后还应有不少于一次拖动的滚动余量 @ ${where}`,
    ).toBeGreaterThanOrEqual(SCROLL_DRAG_PX)

    // 绘图区内起手：起手点同时落在绘图区与主体可视盒内，向上拖。
    const plot = await plotBox(page)
    const start = centerOf(plot)
    expect(start.y, '起手点应在主体可视盒内').toBeGreaterThan(visible.y)
    expect(start.y, '起手点应在主体可视盒内').toBeLessThan(visibleBottom)
    expect(Math.min(start.y - plot.y, plot.y + plot.height - start.y), '起手点应在绘图区内部（离上下边都有余量）').toBeGreaterThan(SCROLL_START_INSET_PX)
    await dragOneFinger(page, start, 0, -SCROLL_DRAG_PX)
    await expect.poll(scrollTop, { message: `绘图区内起手的纵向拖动应滚动主体（起手时 scrollTop ${resting}）@ ${where}` }).toBeGreaterThan(resting)
    console.log(
      `station-chart-touch scroll @ ${where}`,
      JSON.stringify({ control, controlScrollTop, resting, start, scrollTop: await scrollTop(), unscrolledPlot, plot, body: measure.body }),
    )

    await page.waitForTimeout(SETTLE_MS)
    await expectFullRange(page, where)
  })

  test('(f) 390x664 捏合后转到 750x342：缩放窗口保持', async ({ page }, testInfo) => {
    const where = await openStationSheet(page, PORTRAIT, testInfo)
    const { zoomed } = await pinchOut(page, where)

    await page.setViewportSize(LANDSCAPE)
    const rotatedWhere = await settleStationSheet(page, testInfo)

    const rotated = await settledZoomWindow(page)
    console.log(`station-chart-touch rotation @ ${rotatedWhere}`, JSON.stringify({ zoomed, rotated }))
    expect(spanOf(rotated), `旋转后跨度 ${spanOf(rotated)} 应仍 < 100`).toBeLessThan(100)
    expect(Math.abs(spanOf(rotated) - spanOf(zoomed)), `旋转后跨度 ${spanOf(rotated)} 应等于旋转前的 ${spanOf(zoomed)}`).toBeLessThanOrEqual(SPAN_TOLERANCE)
    expect(Math.abs(rotated.start - zoomed.start), `旋转后 start ${rotated.start} 应等于旋转前的 ${zoomed.start}`).toBeLessThanOrEqual(SPAN_TOLERANCE)
  })

  test('(g) 390x664 捏合后切换到另一个有曲线的要素：缩放窗口回到 0 / 100', async ({ page }, testInfo) => {
    const where = await openStationSheet(page, PORTRAIT, testInfo)
    // 硬前提（pinchOut 内断言）：跨度 < 100，否则“回到 0 / 100”什么也没证明。
    await pinchOut(page, where)

    await variableToggle(page, 'TEMP').click()
    await expect(stationWindow(page).getByTestId('m11-station-variable-TEMP-chart')).toBeVisible()
    await expect(sheetChartCanvas(page, STATION)).toBeVisible()

    await expectFullRange(page, where)
    // 不是一闪而过的初值：落定后仍是全范围。
    expect(await settledZoomWindow(page)).toEqual({ start: 0, end: 100 })
  })

  test('(l) 390x664 切到没有曲线的要素再切回来（图表销毁重建）：属性从 0 / 100 / "false" 重新开始', async ({ page }, testInfo) => {
    await page.setViewportSize(PORTRAIT)
    await installRiverWindowMocks(page)
    await installStationSeriesWithoutDroppedVariable(page)
    await openCurveWindow(page, 'station')
    await expect(sheetChartCanvas(page, STATION), '曲线应已加载（图表 canvas 可见）').toBeVisible()
    const where = await settleStationSheet(page, testInfo)

    // 硬前提：销毁前窗口已缩放（pinchOut 内断言）、tooltip 可见。
    const { plot } = await pinchOut(page, where)
    await tapWithTouch(page, { x: plot.x + plot.width * 0.6, y: plot.y + plot.height / 2 })
    await expect(chartArea(page)).toHaveAttribute('data-tooltip-visible', 'true')

    await variableToggle(page, DROPPED_VARIABLE).click()
    await expect(stationWindow(page).getByTestId('m11-station-popup-empty')).toBeVisible()
    await expect(chartArea(page), '空态下图表区应已卸载').toHaveCount(0)

    await variableToggle(page, 'PRCP').click()
    await expect(sheetChartCanvas(page, STATION), '切回后曲线应重新画出').toBeVisible()
    await expectFullRange(page, where)
    await expect(chartArea(page)).toHaveAttribute('data-tooltip-visible', 'false')
    expect(await settledZoomWindow(page)).toEqual({ start: 0, end: 100 })
  })
})

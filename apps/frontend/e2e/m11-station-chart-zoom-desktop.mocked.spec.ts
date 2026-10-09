import { expect, test, type Page } from '@playwright/test'

import { curveWindowParts, dragWithMouse } from './support/curveSheet.mocked'
import { measureSheetChart, openLoadedCurveWindow, type SheetChartTarget } from './support/sheetChart.mocked'
import { isMobileForm, requireViewport, type ViewportSize } from './support/viewportForm'

/**
 * 气象代站曲线缩放的桌面不变量（openspec mobile-responsive-display task 4.8 的 Risk pack「Legacy compatibility」）：
 * 桌面形态同样暴露缩放窗口属性，滚轮仍缩放时间轴，按住鼠标拖动仍平移（图表库缺省，与改动前实测一致）。
 * 用例 (h)(i) 对应 tasks.md 里 #2809 的 Triage。
 */
const DESKTOP: ViewportSize = { width: 1280, height: 900 }
const CHART_AREA_TEST_ID = 'm11-station-panel-chart'
const STATION: SheetChartTarget = { kind: 'station', chartTestId: CHART_AREA_TEST_ID }

/** 图表的绘图区（grid）相对图表区四边的内缩：独立抄自图表配置，不读产品常量。 */
const GRID_INSET_PX = { left: 48, right: 16, top: 18, bottom: 28 }
const WHEEL_DELTA_PX = 240
const DRAG_PX = 80
const SPAN_TOLERANCE = 0.5
/** 缩放派发的节流 + 动画的数倍：等窗口落定再取基准。 */
const SETTLE_MS = 400

function chartArea(page: Page) {
  return curveWindowParts(page, 'station').frame.getByTestId(CHART_AREA_TEST_ID)
}

async function readZoomWindow(page: Page) {
  const area = chartArea(page)
  const [start, end] = await Promise.all([area.getAttribute('data-zoom-start'), area.getAttribute('data-zoom-end')])
  const parse = (value: string | null) => (value === null || value.trim() === '' ? Number.NaN : Number(value))
  return { start: parse(start), end: parse(end) }
}

async function openDesktopStationWindow(page: Page) {
  await openLoadedCurveWindow(page, DESKTOP, STATION)
  expect(isMobileForm(requireViewport(page))).toBe(false)
}

async function plotCenter(page: Page) {
  const { chart } = await measureSheetChart(page, STATION)
  return {
    x: chart.x + GRID_INSET_PX.left + (chart.width - GRID_INSET_PX.left - GRID_INSET_PX.right) / 2,
    y: chart.y + GRID_INSET_PX.top + (chart.height - GRID_INSET_PX.top - GRID_INSET_PX.bottom) / 2,
  }
}

test.describe('M11 气象代站曲线缩放桌面不变量', () => {
  test('(h) 1280x900：缩放窗口为 0 / 100，图表区不带移动专属的 touch-action', async ({ page }) => {
    await openDesktopStationWindow(page)

    await expect(chartArea(page)).toHaveAttribute('data-zoom-start', '0')
    await expect(chartArea(page)).toHaveAttribute('data-zoom-end', '100')
    await expect(chartArea(page)).toHaveAttribute('data-tooltip-visible', 'false')
    expect(await chartArea(page).evaluate((element) => getComputedStyle(element).touchAction)).toBe('auto')
  })

  test('(i) 1280x900：绘图区内滚轮放大后跨度 < 100，按住鼠标横向拖动平移而跨度不变', async ({ page }) => {
    await openDesktopStationWindow(page)
    const center = await plotCenter(page)

    await page.mouse.move(center.x, center.y)
    // 向上滚 = 放大；全范围时缩小是空操作。
    await page.mouse.wheel(0, -WHEEL_DELTA_PX)
    await expect
      .poll(async () => {
        const zoomWindow = await readZoomWindow(page)
        return zoomWindow.end - zoomWindow.start
      }, { message: '滚轮放大后跨度应 < 100' })
      .toBeLessThan(100)
    // 等缩放落定再取基准。
    await page.waitForTimeout(SETTLE_MS)
    const zoomed = await readZoomWindow(page)
    const span = zoomed.end - zoomed.start
    expect(span, `前提：跨度 ${span} 应 < 100`).toBeLessThan(100)

    // 改动前的桌面基线（未改动的 `src/` 上实测）：按住鼠标拖动平移时间轴，跨度不变。
    await dragWithMouse(page, center, -DRAG_PX, 0)
    await expect
      .poll(async () => Math.abs((await readZoomWindow(page)).start - zoomed.start), { message: '按住鼠标横向拖动后 start 应改变' })
      .toBeGreaterThan(SPAN_TOLERANCE)
    await page.waitForTimeout(SETTLE_MS)

    const afterDrag = await readZoomWindow(page)
    console.log('station-chart-zoom-desktop', JSON.stringify({ center, zoomed, afterDrag }))
    expect(Math.abs(afterDrag.end - afterDrag.start - span), `拖动后跨度 ${afterDrag.end - afterDrag.start} 应仍为 ${span}`).toBeLessThanOrEqual(SPAN_TOLERANCE)
  })
})

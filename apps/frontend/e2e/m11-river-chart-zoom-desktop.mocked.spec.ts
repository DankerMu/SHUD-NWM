import { expect, test, type Page } from '@playwright/test'

import { dragWithMouse } from './support/curveSheet.mocked'
import { measureSheetChart, openLoadedCurveWindow, type SheetChartTarget } from './support/sheetChart.mocked'
import { isMobileForm, requireViewport, type ViewportSize } from './support/viewportForm'

/**
 * 河段曲线缩放的桌面不变量（openspec mobile-responsive-display task 4.7 的 Risk pack「Legacy compatibility」）：
 * 桌面形态同样暴露缩放窗口属性，滚轮仍缩放时间轴，按住鼠标拖动仍不平移，提示文案不变。
 * 用例 (h)(i) 对应 tasks.md 里 #2808 的 Triage。
 */
const DESKTOP: ViewportSize = { width: 1280, height: 900 }
const RIVER: SheetChartTarget = { kind: 'river' }

/** 紧凑图表的绘图区（grid）相对图表区四边的内缩：独立抄自图表配置，不读产品常量。 */
const GRID_INSET_PX = { left: 48, right: 16, top: 16, bottom: 28 }
const WHEEL_DELTA_PX = 240
const DRAG_PX = 80
/** “不应发生变化”的判据在手势结束后等这么久再读（缩放派发的节流 + 动画的数倍）。 */
const SETTLE_MS = 400

function chartArea(page: Page) {
  return page.getByTestId('m11-river-forecast-panel').getByTestId('m11-river-panel-chart')
}

async function readZoomWindow(page: Page) {
  const area = chartArea(page)
  const [start, end] = await Promise.all([area.getAttribute('data-zoom-start'), area.getAttribute('data-zoom-end')])
  const parse = (value: string | null) => (value === null || value.trim() === '' ? Number.NaN : Number(value))
  return { start: parse(start), end: parse(end) }
}

async function openDesktopRiverWindow(page: Page) {
  await openLoadedCurveWindow(page, DESKTOP, RIVER)
  expect(isMobileForm(requireViewport(page))).toBe(false)
}

async function plotCenter(page: Page) {
  const { chart } = await measureSheetChart(page, RIVER)
  return {
    x: chart.x + GRID_INSET_PX.left + (chart.width - GRID_INSET_PX.left - GRID_INSET_PX.right) / 2,
    y: chart.y + GRID_INSET_PX.top + (chart.height - GRID_INSET_PX.top - GRID_INSET_PX.bottom) / 2,
  }
}

test.describe('M11 河段曲线缩放桌面不变量', () => {
  test('(h) 1280x900：缩放窗口为 0 / 100，提示文案为「滚轮缩放时间轴」', async ({ page }) => {
    await openDesktopRiverWindow(page)

    await expect(chartArea(page)).toHaveAttribute('data-zoom-start', '0')
    await expect(chartArea(page)).toHaveAttribute('data-zoom-end', '100')
    await expect(page.getByTestId('m11-river-panel-zoom-hint')).toHaveText('滚轮缩放时间轴')
    // 移动专属的 touch-action 不落到桌面形态。
    expect(await chartArea(page).evaluate((element) => getComputedStyle(element).touchAction)).toBe('auto')
  })

  test('(i) 1280x900：绘图区内滚轮放大后跨度 < 100，按住鼠标横向拖动不平移', async ({ page }) => {
    await openDesktopRiverWindow(page)
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
    expect(zoomed.end - zoomed.start, `前提：跨度 ${zoomed.end - zoomed.start} 应 < 100`).toBeLessThan(100)

    await dragWithMouse(page, center, -DRAG_PX, 0)
    await page.waitForTimeout(SETTLE_MS)

    const afterDrag = await readZoomWindow(page)
    console.log('river-chart-zoom-desktop', JSON.stringify({ center, zoomed, afterDrag }))
    expect(afterDrag, '桌面形态按住鼠标拖动不应平移时间轴').toEqual(zoomed)
  })
})

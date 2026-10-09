import { expect, test, type Page } from '@playwright/test'

import {
  CURVE_WINDOWS,
  CURVE_WINDOW_KINDS,
  MAP_REGION_TEST_ID,
  curveWindowParts,
  measureCurveWindow,
  openCurveWindow,
  type CurveWindowKind,
} from './support/curveSheet.mocked'
import type { Box } from './support/legendLauncher.mocked'
import { boxOf } from './support/overlayLaunchers.mocked'
import { installRiverWindowMocks } from './support/riverWindow.mocked'
import { isMobileForm, requireViewport, type ViewportSize } from './support/viewportForm'

/**
 * 曲线窗的桌面不变量（openspec mobile-responsive-display task 4.2 的 Risk pack「Legacy compatibility」）。
 *
 * 4.2 给窗加了移动形态分支与一层主体容器；桌面形态的 DOM 盒必须与改动前相同。下面的字面包围盒
 * 量自改动前的 `src/`（`origin/master` 3197ccefd，1280×900，Chromium）——“字面包围盒”那组用例
 * 改动前后都绿，是“加了主体容器后桌面几何不变”的钉子。
 * 768×1024 不写字面包围盒：task 4.3 会改该视口的默认宽度，那一行归 4.3 的 spec。
 */
const DESKTOP: ViewportSize = { width: 1280, height: 900 }
const TABLET_PORTRAIT: ViewportSize = { width: 768, height: 1024 }
const BOX_TOLERANCE_PX = 0.5

const MASTER_BOXES_1280x900: Record<CurveWindowKind, { frame: Box; chart: Box }> = {
  river: {
    frame: { x: 89.59375, y: 172, width: 537.59375, height: 302.390625 },
    chart: { x: 102.59375, y: 329.5, width: 511.59375, height: 135.890625 },
  },
  station: {
    frame: { x: 652.796875, y: 172, width: 537.59375, height: 302.390625 },
    chart: { x: 665.796875, y: 329.5, width: 511.59375, height: 135.890625 },
  },
}

function expectBoxNear(actual: Box, expected: Box, name: string) {
  for (const key of ['x', 'y', 'width', 'height'] as const) {
    expect(Math.abs(actual[key] - expected[key]), `${name}.${key} ${actual[key]} 应为 ${expected[key]}`).toBeLessThanOrEqual(BOX_TOLERANCE_PX)
  }
}

async function openAt(page: Page, viewport: ViewportSize, kind: CurveWindowKind) {
  await page.setViewportSize(viewport)
  expect(isMobileForm(requireViewport(page))).toBe(false)
  await installRiverWindowMocks(page)
  await openCurveWindow(page, kind)
  // 等到“已加载”：图表容器已挂上。768×1024 下改动前的图表画布高度就是 0（窗约 323×181，
  // 头部与工具行占满——最小宽度归 task 4.3），所以画布可见只在 1280×900 的用例里断言。
  await expect(page.getByTestId(CURVE_WINDOWS[kind].chart)).toBeAttached()
}

test.describe('M11 曲线窗桌面不变量', () => {
  for (const kind of CURVE_WINDOW_KINDS) {
    const name = CURVE_WINDOWS[kind].name

    test(`1280x900 ${name}：窗与图表容器的包围盒等于改动前实测的字面值，宽高比 16:9，可拖拽`, async ({ page }) => {
      await openAt(page, DESKTOP, kind)
      await expect(curveWindowParts(page, kind).chartCanvas).toBeVisible()

      const measure = await measureCurveWindow(page, kind)
      const chart = await boxOf(page.getByTestId(CURVE_WINDOWS[kind].chart), '图表容器')
      console.log(`curve-window desktop ${kind} @ 1280x900`, JSON.stringify({ ...measure, chart }))

      expectBoxNear(measure.frame, MASTER_BOXES_1280x900[kind].frame, name)
      expectBoxNear(chart, MASTER_BOXES_1280x900[kind].chart, `${name}图表容器`)
      expect(measure.aspectRatio).toBe('16 / 9')
      expect(measure.handleCursor).toBe('grab')
      expect(measure.offsetParentTestId).toBe(MAP_REGION_TEST_ID)
      expect(measure.inline.visibility).toBe('')
    })

    test(`768x1024 ${name}：桌面形态，宽高比 16:9，可拖拽`, async ({ page }) => {
      await openAt(page, TABLET_PORTRAIT, kind)

      const measure = await measureCurveWindow(page, kind)
      console.log(`curve-window desktop ${kind} @ 768x1024`, JSON.stringify(measure))
      expect(measure.aspectRatio).toBe('16 / 9')
      expect(measure.handleCursor).toBe('grab')
      expect(Math.abs(measure.frame.width / measure.frame.height - 16 / 9)).toBeLessThan(0.01)
    })

    for (const viewport of [DESKTOP, TABLET_PORTRAIT]) {
      test(`${viewport.width}x${viewport.height} ${name}：主体容器不产生布局盒（display: contents），头部在它之外`, async ({ page }) => {
        await openAt(page, viewport, kind)

        const measure = await measureCurveWindow(page, kind)
        expect(measure.body, '主体容器应存在').not.toBeNull()
        expect(measure.body!.display).toBe('contents')
        expect(measure.handleInsideBody).toBe(false)
      })
    }
  }
})

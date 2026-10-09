import { expect, test, type Page } from '@playwright/test'

import {
  CURVE_WINDOWS,
  CURVE_WINDOW_KINDS,
  centerOf,
  curveWindowParts,
  dragWithMouse,
  measureCurveWindow,
  openCurveWindow,
  type CurveWindowKind,
  type CurveWindowMeasure,
} from './support/curveSheet.mocked'
import { contains } from './support/legendLauncher.mocked'
import { boxOf } from './support/overlayLaunchers.mocked'
import { installRiverWindowMocks } from './support/riverWindow.mocked'
import { isMobileForm, requireViewport, type ViewportSize } from './support/viewportForm'

/**
 * 桌面形态曲线窗的最小宽度（openspec mobile-responsive-display task 4.3，design.md D16）：
 * 默认宽度 = min(44rem, max(42vw, 30rem))，16:9，仍被夹在地图区内。
 * 用例 (a)–(f) 对应 tasks.md 里 #2804 的 Triage。期望值由规则在这里独立算出，不读产品常量。
 */
const REM_PX = 16
const MIN_WIDTH_PX = 30 * REM_PX
const MAX_WIDTH_PX = 44 * REM_PX
const WIDTH_VIEWPORT_RATIO = 0.42
const SIZE_TOLERANCE_PX = 0.5
/** 窗被夹回地图区时与边缘保留的间距。 */
const WINDOW_MARGIN_PX = 12
const EDGE_TOLERANCE_PX = 1

const TABLET_PORTRAIT: ViewportSize = { width: 768, height: 1024 }

function ruleWidth(viewport: ViewportSize) {
  return Math.min(MAX_WIDTH_PX, Math.max(WIDTH_VIEWPORT_RATIO * viewport.width, MIN_WIDTH_PX))
}

async function openAt(page: Page, viewport: ViewportSize, kind: CurveWindowKind): Promise<CurveWindowMeasure> {
  await page.setViewportSize(viewport)
  expect(isMobileForm(requireViewport(page))).toBe(false)
  await installRiverWindowMocks(page)
  await openCurveWindow(page, kind)
  await expect(page.getByTestId(CURVE_WINDOWS[kind].chart)).toBeAttached()
  const measure = await measureCurveWindow(page, kind)
  console.log(`curve-window min-size ${kind} @ ${viewport.width}x${viewport.height}`, JSON.stringify({ frame: measure.frame, map: measure.map }))
  return measure
}

function expectSize(measure: CurveWindowMeasure, expected: { width: number; height?: number }, where: string) {
  const { frame } = measure
  expect(Math.abs(frame.width - expected.width), `窗宽 ${frame.width} 应为 ${expected.width} @ ${where}`).toBeLessThanOrEqual(SIZE_TOLERANCE_PX)
  if (expected.height !== undefined) {
    expect(Math.abs(frame.height - expected.height), `窗高 ${frame.height} 应为 ${expected.height} @ ${where}`).toBeLessThanOrEqual(SIZE_TOLERANCE_PX)
  }
  expect(measure.aspectRatio, `宽高比 @ ${where}`).toBe('16 / 9')
  expect(contains(measure.map, frame), `窗 ${JSON.stringify(frame)} 应在地图区 ${JSON.stringify(measure.map)} 内 @ ${where}`).toBe(true)
}

test.describe('M11 桌面形态曲线窗最小宽度', () => {
  for (const kind of CURVE_WINDOW_KINDS) {
    const name = CURVE_WINDOWS[kind].name

    test(`(a) 768x1024 ${name}：480x270，在地图区内，图表 canvas 可见`, async ({ page }) => {
      const measure = await openAt(page, TABLET_PORTRAIT, kind)

      expectSize(measure, { width: 480, height: 270 }, '768x1024')
      const { chartCanvas } = curveWindowParts(page, kind)
      await expect(chartCanvas, '图表 canvas 应可见').toBeVisible()
      const canvas = await boxOf(chartCanvas, '图表 canvas')
      console.log(`curve-window min-size ${kind} canvas @ 768x1024`, JSON.stringify(canvas))
      expect(canvas.width).toBeGreaterThan(0)
      expect(canvas.height).toBeGreaterThan(0)
      expect(contains(measure.frame, canvas), `图表 canvas ${JSON.stringify(canvas)} 应在窗内`).toBe(true)
    })
  }

  test('(b) 1280x900 河段窗：宽 = 视口宽的 42%（537.6）', async ({ page }) => {
    const viewport = { width: 1280, height: 900 }
    expect(ruleWidth(viewport)).toBeCloseTo(537.6)
    const measure = await openAt(page, viewport, 'river')

    expectSize(measure, { width: 537.6, height: 302.4 }, '1280x900')
  })

  test('(c) 1024x768 河段窗：480x270（30rem 一支）', async ({ page }) => {
    const measure = await openAt(page, { width: 1024, height: 768 }, 'river')

    expectSize(measure, { width: 480, height: 270 }, '1024x768')
  })

  test('(d) 1143x900 河段窗：宽 = 视口宽的 42%，不小于 30rem（两支相接处）', async ({ page }) => {
    const viewport = { width: 1143, height: 900 }
    const expected = WIDTH_VIEWPORT_RATIO * viewport.width
    expect(expected).toBeGreaterThanOrEqual(MIN_WIDTH_PX)
    expect(ruleWidth(viewport)).toBe(expected)
    const measure = await openAt(page, viewport, 'river')

    expectSize(measure, { width: expected }, '1143x900')
  })

  test('(e) 1920x1080 河段窗：宽 704（44rem 封顶）', async ({ page }) => {
    const viewport = { width: 1920, height: 1080 }
    expect(ruleWidth(viewport)).toBe(MAX_WIDTH_PX)
    const measure = await openAt(page, viewport, 'river')

    expectSize(measure, { width: 704, height: 396 }, '1920x1080')
  })

  test('(f) 768x1024 河段窗拖向右下角越界：右边与下边各距地图区边缘 12px', async ({ page }) => {
    const before = await openAt(page, TABLET_PORTRAIT, 'river')
    // 抓手左侧的标题区：避开关闭按钮等不触发拖拽的控件。
    const grab = { x: before.handle.x + 24, y: centerOf(before.handle).y }
    // 指针拖到视口右下角：窗的左上角随之越过可停留范围，clamp 把它钉在边距上。
    await dragWithMouse(page, grab, TABLET_PORTRAIT.width - 2 - grab.x, TABLET_PORTRAIT.height - 2 - grab.y)

    const after = await measureCurveWindow(page, 'river')
    console.log('curve-window min-size river dragged @ 768x1024', JSON.stringify({ before: before.frame, after: after.frame, map: after.map }))
    expect(after.frame.x, '窗应被拖动过').toBeGreaterThan(before.frame.x)
    expect(after.frame.y, '窗应被拖动过').toBeGreaterThan(before.frame.y)
    const right = after.frame.x + after.frame.width
    const bottom = after.frame.y + after.frame.height
    const wantedRight = after.map.x + after.map.width - WINDOW_MARGIN_PX
    const wantedBottom = after.map.y + after.map.height - WINDOW_MARGIN_PX
    expect(Math.abs(right - wantedRight), `窗右边 ${right} 应为 ${wantedRight}`).toBeLessThanOrEqual(EDGE_TOLERANCE_PX)
    expect(Math.abs(bottom - wantedBottom), `窗下边 ${bottom} 应为 ${wantedBottom}`).toBeLessThanOrEqual(EDGE_TOLERANCE_PX)
  })
})

import { expect, test, type Page, type TestInfo } from '@playwright/test'

import { CURVE_WINDOWS, curveWindowParts, expectSheet, measureCurveWindow, type CurveWindowKind } from './support/curveSheet.mocked'
import { gotoWithRiverHooks, locateRiverPoint, tapLocatedPoint } from './support/openRiverWindow'
import { gotoWithStationLayer, locateStationPoint } from './support/openStationWindow'
import { MAP_CANVAS, REGION_FALLBACK_TEST_IDS, clearCrashSwitch, installCrashSwitch, readCrashSwitch } from './support/regionFallbacks.mocked'
import { installRiverWindowMocks } from './support/riverWindow.mocked'
import {
  QUIET_MS,
  anchorPoint,
  expectAnchorClearOfSheet,
  readMapCamera,
  settledMapCamera,
  type AnchorMeasure,
  type MapCameraRead,
} from './support/sheetAutoPan.mocked'
import { dragOneFinger } from './support/touchGestures'
import { isMobileForm, requireViewport, type ViewportSize } from './support/viewportForm'

/**
 * 抽屉打开时自动平移地图（openspec mobile-responsive-display task 4.10，design.md D17）。
 *
 * 用例 (a)–(h) 对应 tasks.md 里 #2811 的 Triage。每条用例都在用例内 `setViewportSize`，所以三个移动 project
 * 下执行的是同一组断言，不按 project 跳过。开窗一律是定位钩子给出的点上的一次真实轻触：钩子先把要素移到
 * 地图区中央（`fitBounds(duration: 0)`），所以开窗前锚点在地图区中心——底部抽屉 / 右侧抽屉正好盖住它，
 * “锚点在未遮盖区中心”只能来自平移。
 *
 * 读数全是地图容器上的只读属性，只在相机静止后更新：期望成立的判据轮询到满足，“不应变化”的判据在等够
 * 动画时长的数倍（`QUIET_MS`）后再读。移动形态下“河段窗开着再点气象代站”在浏览器里做不了（站点定位点在
 * 抽屉之下），替换路径由 vitest 覆盖。
 */

const PORTRAIT: ViewportSize = { width: 390, height: 664 }
const LANDSCAPE: ViewportSize = { width: 750, height: 342 }
const DESKTOP: ViewportSize = { width: 1280, height: 900 }
const CURVE_FALLBACK = REGION_FALLBACK_TEST_IDS.curve

function label(page: Page, testInfo: TestInfo) {
  const viewport = requireViewport(page)
  return `${testInfo.project.name} ${viewport.width}x${viewport.height}`
}

/** 设视口、装基线 mock、开门控并导航到地图就绪；不开窗。气象代站一开始就带 `metStations=1`。 */
async function loadMap(page: Page, viewport: ViewportSize, kind: CurveWindowKind, options: { crashCurve?: boolean } = {}) {
  await page.setViewportSize(viewport)
  await installRiverWindowMocks(page)
  // 崩溃开关先于页面脚本设好：曲线探针只在有曲线面板渲染时挂载，所以此刻还不产生兜底。
  if (options.crashCurve) await installCrashSwitch(page, { gate: true, region: 'curve' })
  if (kind === 'river') await gotoWithRiverHooks(page)
  else await gotoWithStationLayer(page)
}

/** 定位要素并等相机静止：返回定位点与轻触前的相机读数（此时没有选中锚点）。 */
async function locate(page: Page, kind: CurveWindowKind) {
  const point = kind === 'river' ? await locateRiverPoint(page) : await locateStationPoint(page)
  const before = await settledMapCamera(page)
  expect(anchorPoint(before), '开窗前地图容器不应带锚点属性').toBeNull()
  return { point, before }
}

/** 在已定位的点上真实轻触，并等到该种曲线窗可见。 */
async function tapOpen(page: Page, kind: CurveWindowKind, point: { x: number; y: number }) {
  expect(await tapLocatedPoint(page, point), '触屏上下文').toBe('tap')
  await expect(curveWindowParts(page, kind).frame, `${CURVE_WINDOWS[kind].name}应在轻触后可见`).toBeVisible()
}

/** 设视口 -> 定位 -> 轻触开窗；返回轻触前的相机读数。 */
async function openAt(page: Page, viewport: ViewportSize, kind: CurveWindowKind) {
  await loadMap(page, viewport, kind)
  const { point, before } = await locate(page, kind)
  await tapOpen(page, kind, point)
  return before
}

function logMeasure(tag: string, where: string, measure: AnchorMeasure, before: MapCameraRead) {
  console.log(
    `sheet-auto-pan ${tag} @ ${where}`,
    JSON.stringify({
      map: measure.map,
      sheet: measure.sheet,
      uncoveredCentre: measure.uncoveredCentre,
      anchor: measure.anchor,
      zoomBefore: before.zoom,
      zoomAfter: measure.camera.zoom,
    }),
  )
}

test.describe('M11 抽屉打开时自动平移地图', () => {
  test.beforeEach(async ({ page }) => {
    expect(isMobileForm(requireViewport(page))).toBe(true)
  })

  for (const [tag, viewport, kind] of [
    ['(a)', PORTRAIT, 'river'],
    ['(b)', LANDSCAPE, 'station'],
  ] as const) {
    test(`${tag} ${viewport.width}x${viewport.height} ${CURVE_WINDOWS[kind].name}：相机静止后锚点在未遮盖区的中心，缩放不变`, async ({ page }, testInfo) => {
      const before = await openAt(page, viewport, kind)
      const where = label(page, testInfo)

      const measure = await expectAnchorClearOfSheet(page, kind, where, { centred: true })
      logMeasure(tag, where, measure, before)
      expect(measure.camera.zoom, `平移不改变缩放 @ ${where}`).toBe(before.zoom)
      expect(measure.camera.center, `硬前提：相机确实移动过 @ ${where}`).not.toBe(before.center)
    })
  }

  for (const [viewport, kind] of [
    [PORTRAIT, 'station'],
    [LANDSCAPE, 'river'],
  ] as const) {
    test(`(c) ${viewport.width}x${viewport.height} ${CURVE_WINDOWS[kind].name}：相机静止后锚点在未遮盖区内，缩放不变`, async ({ page }, testInfo) => {
      const before = await openAt(page, viewport, kind)
      const where = label(page, testInfo)

      const measure = await expectAnchorClearOfSheet(page, kind, where, { centred: false })
      logMeasure('(c)', where, measure, before)
      expect(measure.camera.zoom, `平移不改变缩放 @ ${where}`).toBe(before.zoom)
      // 750x342 河段的未平移位置只比抽屉左边差一个边界值：另外钉住“相机确实移动过”。
      expect(measure.camera.center, `硬前提：相机确实移动过 @ ${where}`).not.toBe(before.center)
    })
  }

  test('(d) 390x664：关闭抽屉后相机中心与缩放不变，地图容器不带两个锚点属性', async ({ page }, testInfo) => {
    await openAt(page, PORTRAIT, 'river')
    const where = label(page, testInfo)
    await expectAnchorClearOfSheet(page, 'river', where, { centred: true })
    const open = await settledMapCamera(page)
    expect(anchorPoint(open), `硬前提：关闭前有锚点属性 @ ${where}`).not.toBeNull()

    const parts = curveWindowParts(page, 'river')
    await parts.close.tap()
    await expect(parts.frame).toHaveCount(0)

    await page.waitForTimeout(QUIET_MS)
    const closed = await readMapCamera(page)
    expect({ center: closed.center, zoom: closed.zoom }, `关闭不移动地图 @ ${where}`).toEqual({ center: open.center, zoom: open.zoom })
    expect({ anchorX: closed.anchorX, anchorY: closed.anchorY }, `关闭后不带锚点属性 @ ${where}`).toEqual({ anchorX: null, anchorY: null })
  })

  for (const [from, to] of [
    [PORTRAIT, LANDSCAPE],
    [LANDSCAPE, PORTRAIT],
  ] as const) {
    test(`(e) 旋转 ${from.width}x${from.height} -> ${to.width}x${to.height}：河段窗开着，相机静止后锚点在新的未遮盖区的中心，缩放不变`, async ({ page }, testInfo) => {
      const before = await openAt(page, from, 'river')
      await expectAnchorClearOfSheet(page, 'river', label(page, testInfo), { centred: true })

      await page.setViewportSize(to)
      const where = label(page, testInfo)
      const measure = await expectAnchorClearOfSheet(page, 'river', where, { centred: true })
      logMeasure(`(e) from ${from.width}x${from.height}`, where, measure, before)
      expect(measure.camera.zoom, `旋转后的平移不改变缩放 @ ${where}`).toBe(before.zoom)
    })
  }

  test('(f) 390x664：手动把锚点拖到抽屉之下后不被拉回；随后转到 750x342 再平移一次', async ({ page }, testInfo) => {
    const before = await openAt(page, PORTRAIT, 'river')
    const portrait = label(page, testInfo)
    const opened = await expectAnchorClearOfSheet(page, 'river', portrait, { centred: true })
    const settled = await settledMapCamera(page)

    // 起点：未遮盖区里命中测试落在地图画布上的点（启动器列让位后不可见，但不写死坐标）。
    const dragDistance = opened.sheet.y - opened.anchor.y + 60
    const start = await page.evaluate(
      ({ map, sheetTop, canvasSelector }) => {
        const canvas = document.querySelector(canvasSelector)
        for (let y = map.y + 12; y < sheetTop - 12; y += 12) {
          for (let x = map.x + 24; x < map.x + map.width - 24; x += 24) {
            if (canvas !== null && document.elementFromPoint(x, y) === canvas) return { x, y }
          }
        }
        return null
      },
      { map: opened.map, sheetTop: opened.sheet.y, canvasSelector: MAP_CANVAS },
    )
    expect(start, `未遮盖区里应有落在地图画布上的点 @ ${portrait}`).not.toBeNull()
    expect(start!.y + dragDistance, '硬前提：拖动终点仍在视口内').toBeLessThan(PORTRAIT.height)

    await dragOneFinger(page, start!, 0, dragDistance)

    // 硬前提：拖动（含惯性）静止后锚点已在抽屉顶边之下，相机中心已改变。
    const dragged = await settledMapCamera(page)
    console.log(`sheet-auto-pan (f) drag @ ${portrait}`, JSON.stringify({ start, dragDistance, sheetTop: opened.sheet.y, settled, dragged }))
    expect(dragged.center, `硬前提：拖动改变了相机中心 @ ${portrait}`).not.toBe(settled.center)
    expect(anchorPoint(dragged)!.y, `硬前提：拖动后锚点在抽屉顶边之下 @ ${portrait}`).toBeGreaterThan(opened.sheet.y)

    await page.waitForTimeout(QUIET_MS)
    const later = await readMapCamera(page)
    expect(later, `手动移动后地图不应被拉回 @ ${portrait}`).toEqual(dragged)

    await page.setViewportSize(LANDSCAPE)
    const landscape = label(page, testInfo)
    const measure = await expectAnchorClearOfSheet(page, 'river', landscape, { centred: true })
    logMeasure('(f) after rotation', landscape, measure, before)
  })

  test('(g) 离开移动形态 390x664 -> 1280x900：相机中心与缩放不变，锚点属性仍在', async ({ page }, testInfo) => {
    await openAt(page, PORTRAIT, 'river')
    const where = label(page, testInfo)
    await expectAnchorClearOfSheet(page, 'river', where, { centred: true })
    const open = await settledMapCamera(page)

    await page.setViewportSize(DESKTOP)
    expect(isMobileForm(requireViewport(page))).toBe(false)
    // 窗回到桌面形态：有内联定位（抽屉没有）。
    await expect.poll(async () => (await measureCurveWindow(page, 'river')).inline.left, '窗应回到桌面形态').not.toBe('')

    await page.waitForTimeout(QUIET_MS)
    const desktop = await readMapCamera(page)
    console.log(`sheet-auto-pan (g) @ ${where} -> 1280x900`, JSON.stringify({ open, desktop }))
    expect({ center: desktop.center, zoom: desktop.zoom }, '离开移动形态不移动地图').toEqual({ center: open.center, zoom: open.zoom })
    expect(anchorPoint(desktop), '桌面形态仍带锚点属性').not.toBeNull()
  })

  test('(g2) 进入移动形态 1280x900 -> 390x664：河段窗开着，相机静止后锚点在未遮盖区的中心，缩放不变', async ({ page }, testInfo) => {
    const before = await openAt(page, DESKTOP, 'river')
    expect(isMobileForm(requireViewport(page))).toBe(false)
    // 桌面形态开窗不移动地图。
    await page.waitForTimeout(QUIET_MS)
    const desktop = await readMapCamera(page)
    expect({ center: desktop.center, zoom: desktop.zoom }, '硬前提：桌面形态开窗没有移动地图').toEqual({ center: before.center, zoom: before.zoom })

    await page.setViewportSize(PORTRAIT)
    const where = label(page, testInfo)
    const measure = await expectAnchorClearOfSheet(page, 'river', where, { centred: true })
    logMeasure('(g2)', where, measure, before)
    expect(measure.camera.zoom, `进入移动形态的平移不改变缩放 @ ${where}`).toBe(before.zoom)
  })

  test('(h) 390x664：曲线区域兜底时不平移；重试成功后抽屉出现并平移一次', async ({ page }, testInfo) => {
    await loadMap(page, PORTRAIT, 'river', { crashCurve: true })
    const where = label(page, testInfo)
    expect(await readCrashSwitch(page)).toEqual({ gate: true, region: 'curve' })
    const fallback = page.getByTestId(CURVE_FALLBACK)
    const river = curveWindowParts(page, 'river')

    const { point, before } = await locate(page, 'river')
    expect(await tapLocatedPoint(page, point)).toBe('tap')
    await expect(fallback).toBeVisible()
    await expect(river.frame).toHaveCount(0)

    await page.waitForTimeout(QUIET_MS)
    const inFallback = await readMapCamera(page)
    console.log(`sheet-auto-pan (h) fallback @ ${where}`, JSON.stringify({ before, inFallback }))
    expect({ center: inFallback.center, zoom: inFallback.zoom }, `兜底时不平移 @ ${where}`).toEqual({ center: before.center, zoom: before.zoom })

    await clearCrashSwitch(page)
    await fallback.getByRole('button', { name: '重试' }).tap()
    await expect(river.frame).toBeVisible()
    await expect(fallback).toHaveCount(0)
    await expectSheet(page, 'river', where)

    const measure = await expectAnchorClearOfSheet(page, 'river', where, { centred: true })
    logMeasure('(h) after retry', where, measure, before)
    expect(measure.camera.zoom, `重试后的平移不改变缩放 @ ${where}`).toBe(before.zoom)
  })
})

import { expect, test, type Page } from '@playwright/test'

import { curveWindowParts } from './support/curveSheet.mocked'
import { gotoWithRiverHooks, locateRiverPoint, tapLocatedPoint } from './support/openRiverWindow'
import { STATION_LAYER_URL, locateStationPoint } from './support/openStationWindow'
import { installRiverWindowMocks } from './support/riverWindow.mocked'
import { MAP_SURFACE, QUIET_MS, anchorPoint, readMapCamera, settledMapCamera, type MapCameraRead } from './support/sheetAutoPan.mocked'
import { isMobileForm, requireViewport, type ViewportSize } from './support/viewportForm'

/**
 * 选中锚点属性的桌面不变量（openspec mobile-responsive-display task 4.10 的 Risk pack「Legacy compatibility」，
 * design.md D17）：桌面形态开窗不移动地图，`data-selected-anchor-x / -y` 照常给出选中锚点的视口坐标；
 * 两窗并存时给活动窗的。用例 (i)(j)(k) 对应 tasks.md 里 #2811 的 Triage。
 */
const DESKTOP: ViewportSize = { width: 1280, height: 900 }
const ANCHOR_TOLERANCE_PX = 1
/** 代站图层已打开且有要素（`openStationWindow.ts` 里的同一个判据；那里没有导出）。 */
const MAP_WITH_STATIONS = `${MAP_SURFACE}:not([data-met-station-feature-count="0"])`

type Point = { x: number; y: number }

/** 轮询到锚点属性与期望点相差不超过 1px；返回成立时的读数。 */
async function expectAnchorNear(page: Page, expected: Point, what: string): Promise<MapCameraRead> {
  let read: MapCameraRead | undefined
  await expect(async () => {
    read = await readMapCamera(page)
    const anchor = anchorPoint(read)
    expect(anchor, `${what}：地图容器应带两个锚点属性`).not.toBeNull()
    const detail = JSON.stringify({ anchor, expected })
    expect(Math.abs(anchor!.x - expected.x), `${what}：锚点 x ${detail}`).toBeLessThanOrEqual(ANCHOR_TOLERANCE_PX)
    expect(Math.abs(anchor!.y - expected.y), `${what}：锚点 y ${detail}`).toBeLessThanOrEqual(ANCHOR_TOLERANCE_PX)
  }).toPass({ timeout: 6_000 })
  return read!
}

/**
 * 1280×900 带代站图层：开河段窗 -> 定位站点（钩子会移动相机）-> 相机静止后记下河段锚点 R2 -> 点站点。
 * 返回 R2 与站点钩子给出的视口点。顺序固定：站点定位点在地图区中央，不在河段窗之下。
 */
async function openBothWindows(page: Page) {
  await page.setViewportSize(DESKTOP)
  expect(isMobileForm(requireViewport(page))).toBe(false)
  await installRiverWindowMocks(page)
  await gotoWithRiverHooks(page, { url: STATION_LAYER_URL })
  await page.locator(MAP_WITH_STATIONS).waitFor({ state: 'attached' })

  const riverPoint = await locateRiverPoint(page)
  expect(await tapLocatedPoint(page, riverPoint)).toBe('click')
  await expect(curveWindowParts(page, 'river').frame).toBeVisible()
  await expectAnchorNear(page, riverPoint, '开河段窗后')

  const stationPoint = await locateStationPoint(page)
  const afterLocate = await settledMapCamera(page)
  const riverAnchor = anchorPoint(afterLocate)
  expect(riverAnchor, '站点定位后河段锚点属性仍在').not.toBeNull()

  expect(await tapLocatedPoint(page, stationPoint)).toBe('click')
  await expect(curveWindowParts(page, 'station').frame).toBeVisible()
  await expect(curveWindowParts(page, 'river').frame, '桌面形态两窗并存').toBeVisible()
  return { riverPoint, riverAnchor: riverAnchor!, stationPoint, afterLocate }
}

test.describe('M11 选中锚点属性的桌面不变量', () => {
  test('(i) 1280x900 开河段窗：锚点属性与点击点相差 ≤ 1px，相机中心与缩放不变', async ({ page }) => {
    await page.setViewportSize(DESKTOP)
    expect(isMobileForm(requireViewport(page))).toBe(false)
    await installRiverWindowMocks(page)
    await gotoWithRiverHooks(page)
    const point = await locateRiverPoint(page)
    const before = await settledMapCamera(page)
    expect(anchorPoint(before), '开窗前不带锚点属性').toBeNull()

    expect(await tapLocatedPoint(page, point)).toBe('click')
    await expect(curveWindowParts(page, 'river').frame).toBeVisible()

    const opened = await expectAnchorNear(page, point, '开河段窗后')
    await page.waitForTimeout(QUIET_MS)
    const later = await readMapCamera(page)
    console.log('curve-anchor desktop (i) @ 1280x900', JSON.stringify({ point, before, opened, later }))
    expect({ center: later.center, zoom: later.zoom }, '桌面形态开窗不移动地图').toEqual({ center: before.center, zoom: before.zoom })
    expect(later, '等待后锚点属性不变').toEqual(opened)
  })

  test('(j) 1280x900 双窗：气象代站窗为活动窗时属性给站点锚点；点河段窗使其成为活动窗后给回河段锚点', async ({ page }) => {
    const { riverAnchor, stationPoint, afterLocate } = await openBothWindows(page)
    const river = curveWindowParts(page, 'river')
    const station = curveWindowParts(page, 'station')
    await expect(station.frame).toHaveAttribute('data-m11-curve-window-active', 'true')
    await expect(river.frame).toHaveAttribute('data-m11-curve-window-active', 'false')

    const stationActive = await expectAnchorNear(page, stationPoint, '气象代站窗为活动窗')
    // 硬前提：两个锚点相距足够远，“给的是哪一个”才分得清。
    expect(Math.hypot(stationPoint.x - riverAnchor.x, stationPoint.y - riverAnchor.y), '硬前提：河段锚点与站点锚点相距 > 10px').toBeGreaterThan(10)

    await river.title.click()
    await expect(river.frame).toHaveAttribute('data-m11-curve-window-active', 'true')
    const riverActive = await expectAnchorNear(page, riverAnchor, '河段窗成为活动窗后')

    console.log('curve-anchor desktop (j) @ 1280x900', JSON.stringify({ riverAnchor, stationPoint, stationActive, riverActive }))
    expect({ center: riverActive.center, zoom: riverActive.zoom }, '开站点窗、切换活动窗都不移动地图').toEqual({
      center: afterLocate.center,
      zoom: afterLocate.zoom,
    })
  })

  test('(k) 1280x900：关掉活动窗后属性给仍开着的窗的锚点；关掉全部窗后两个锚点属性都不存在', async ({ page }) => {
    const { riverAnchor, stationPoint } = await openBothWindows(page)
    const river = curveWindowParts(page, 'river')
    const station = curveWindowParts(page, 'station')
    await expectAnchorNear(page, stationPoint, '气象代站窗为活动窗')

    await station.close.click()
    await expect(station.frame).toHaveCount(0)
    await expectAnchorNear(page, riverAnchor, '只剩河段窗')

    await river.close.click()
    await expect(river.frame).toHaveCount(0)
    await expect
      .poll(async () => {
        const closed = await readMapCamera(page)
        return { anchorX: closed.anchorX, anchorY: closed.anchorY }
      }, '关掉全部窗后不带锚点属性')
      .toEqual({ anchorX: null, anchorY: null })
  })
})

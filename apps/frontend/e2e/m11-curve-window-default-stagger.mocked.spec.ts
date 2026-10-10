import { expect, test, type Page } from '@playwright/test'

import {
  CURVE_WINDOWS,
  CURVE_WINDOW_KINDS,
  centerOf,
  curveWindowParts,
  hitsCurveWindow,
  intersectionOf,
  measureCurveWindow,
  openCurveWindow,
  type CurveWindowKind,
} from './support/curveSheet.mocked'
import { contains, type Box } from './support/legendLauncher.mocked'
import { gotoWithRiverHooks, locateRiverPoint, tapLocatedPoint } from './support/openRiverWindow'
import { STATION_LAYER_URL, locateStationPoint } from './support/openStationWindow'
import { boxOf } from './support/overlayLaunchers.mocked'
import { installRiverWindowMocks } from './support/riverWindow.mocked'
import { MAP_SURFACE } from './support/sheetAutoPan.mocked'
import { isMobileForm, requireViewport, type ViewportSize } from './support/viewportForm'

/**
 * 桌面形态两个曲线窗的默认位置在横向重叠时纵向错开（openspec curve-window-default-stagger，#2840）。
 *
 * 地图区宽 900–1090 时两窗默认的横向范围重叠（窗宽 480 > 两窗锚点间距 0.44 × 地图区宽），气象代站窗在右、
 * 盖住河段窗右端的关闭按钮。改动后重叠时气象代站窗的默认位置下移，河段窗的关闭按钮整个露在它之外。
 * 用例 (a)(b)(c) 对应 tasks.md 1.3；(d) 是桌面形态最矮视口的补充护栏。
 *
 * 视口的取法：两个定位钩子的目标点是地图区中央，被已开的窗盖住时钩子拒绝定位，所以双窗用例的视口
 * 高度取 1000 / 900（地图区中央在两窗下方）。900×500 开不出第二个窗——默认位置不依赖另一窗是否打开，
 * (d) 把两窗各自单开量到的盒拼起来比较。
 */
const OVERLAPPING: ViewportSize[] = [
  { width: 900, height: 1000 },
  { width: 960, height: 1000 },
]
/** 关闭按钮被气象代站窗完全盖住的地图区宽上限（#2840 的推算，改动前实测 900 / 960 均全盖）。 */
const FULLY_COVERED_MAP_WIDTH = { min: 900, max: 990 }
/** 窗宽仍是 480（30rem 一支，到约 1143 为止），但两窗默认的横向范围已不重叠（≥ 1091）。 */
const NOT_OVERLAPPING: ViewportSize = { width: 1100, height: 900 }
const NOT_OVERLAPPING_MAP_WIDTH = { min: 1091, max: 1142 }
/**
 * 1100×900 两窗默认位置相对地图区左上角的字面值，量自改动前的 `src/`（origin/master 336641032，Chromium）：
 * 河段窗 (68, 88)、气象代站窗 (552, 88)，窗 480×270。
 */
const MASTER_OFFSETS_1100x900: Record<CurveWindowKind, { x: number; y: number }> = {
  river: { x: 68, y: 88 },
  station: { x: 552, y: 88 },
}
/** 桌面形态最矮的视口（高 < 500 即移动形态）；气象代站窗下移后在这里被夹回地图区。 */
const SHORTEST_DESKTOP: ViewportSize = { width: 900, height: 500 }
const BOX_TOLERANCE_PX = 0.5
/** 代站图层已打开且有要素（`openStationWindow.ts` 里的同一个判据；那里没有导出）。 */
const MAP_WITH_STATIONS = `${MAP_SURFACE}:not([data-met-station-feature-count="0"])`

interface BothWindows {
  map: Box
  frames: Record<CurveWindowKind, Box>
  closes: Record<CurveWindowKind, Box>
}

/** 在带代站图层的页面上按 `order` 依次经真实点击打开两个窗，等两窗的图表容器挂上后量盒。 */
async function openBothWindows(page: Page, viewport: ViewportSize, order: readonly CurveWindowKind[]): Promise<BothWindows> {
  await page.setViewportSize(viewport)
  expect(isMobileForm(requireViewport(page))).toBe(false)
  await installRiverWindowMocks(page)
  await gotoWithRiverHooks(page, { url: STATION_LAYER_URL })
  await page.locator(MAP_WITH_STATIONS).waitFor({ state: 'attached' })

  for (const kind of order) {
    const point = kind === 'river' ? await locateRiverPoint(page) : await locateStationPoint(page)
    expect(await tapLocatedPoint(page, point)).toBe('click')
    await expect(curveWindowParts(page, kind).frame, `${CURVE_WINDOWS[kind].name}应打开`).toBeVisible()
  }
  for (const kind of CURVE_WINDOW_KINDS) {
    await expect(curveWindowParts(page, kind).frame, '桌面形态两窗并存').toBeVisible()
    await expect(page.getByTestId(CURVE_WINDOWS[kind].chart)).toBeAttached()
  }
  // 后开的窗是活动窗（在上层）。
  await expect(curveWindowParts(page, order[order.length - 1]).frame).toHaveAttribute('data-m11-curve-window-active', 'true')

  const river = await measureCurveWindow(page, 'river')
  const station = await measureCurveWindow(page, 'station')
  const measured: BothWindows = {
    map: river.map,
    frames: { river: river.frame, station: station.frame },
    closes: {
      river: await boxOf(curveWindowParts(page, 'river').close, '河段窗关闭按钮'),
      station: await boxOf(curveWindowParts(page, 'station').close, '气象代站窗关闭按钮'),
    },
  }
  console.log(`curve-window stagger ${order.join('->')} @ ${viewport.width}x${viewport.height}`, JSON.stringify(measured))
  return measured
}

/** 关闭按钮盒四角各内缩 1px 的四个点与中心。 */
function probePoints(close: Box) {
  const right = close.x + close.width - 1
  const bottom = close.y + close.height - 1
  return [
    { name: '左上', x: close.x + 1, y: close.y + 1 },
    { name: '右上', x: right, y: close.y + 1 },
    { name: '左下', x: close.x + 1, y: bottom },
    { name: '右下', x: right, y: bottom },
    { name: '中心', ...centerOf(close) },
  ]
}

/** 关闭按钮的五个探测点的命中测试都落在它自己的窗里。 */
async function expectCloseReachable(page: Page, kind: CurveWindowKind, close: Box, where: string) {
  for (const point of probePoints(close)) {
    const hit = await hitsCurveWindow(page, kind, point)
    expect(
      hit.inside,
      `${CURVE_WINDOWS[kind].name}关闭按钮${point.name}点 (${point.x}, ${point.y}) 应命中自己的窗，实际命中 ${hit.top} @ ${where}`,
    ).toBe(true)
  }
}

function expectMapWidthWithin(map: Box, range: { min: number; max: number }, where: string) {
  expect(map.width, `地图区宽 @ ${where}`).toBeGreaterThanOrEqual(range.min)
  expect(map.width, `地图区宽 @ ${where}`).toBeLessThanOrEqual(range.max)
}

test.describe('M11 桌面形态两个曲线窗默认位置的纵向错开', () => {
  for (const viewport of OVERLAPPING) {
    const where = `${viewport.width}x${viewport.height}`

    test(`(a) ${where} 先开河段窗再开气象代站窗：河段窗的关闭按钮露在气象代站窗之外，点它只关掉河段窗`, async ({ page }) => {
      const { map, frames, closes } = await openBothWindows(page, viewport, ['river', 'station'])
      expectMapWidthWithin(map, FULLY_COVERED_MAP_WIDTH, where)
      // 硬前提：两窗默认的横向范围确实重叠，否则“露在外面”不需要错开也成立。
      expect(frames.river.x + frames.river.width, '硬前提：河段窗右缘越过气象代站窗左缘').toBeGreaterThan(frames.station.x)

      await expectCloseReachable(page, 'river', closes.river, where)
      expect(
        intersectionOf(closes.river, frames.station),
        `河段窗关闭按钮 ${JSON.stringify(closes.river)} 应整个在气象代站窗 ${JSON.stringify(frames.station)} 之外`,
      ).toBeNull()
      expect(frames.river.y, '河段窗在上、气象代站窗在下').toBeLessThan(frames.station.y)

      const river = curveWindowParts(page, 'river')
      await river.close.click()
      await expect(river.frame, '河段窗应被关掉').toHaveCount(0)
      await expect(curveWindowParts(page, 'station').frame, '气象代站窗仍在').toBeVisible()
    })

    test(`(b) ${where} 先开气象代站窗再开河段窗：两窗的关闭按钮都可命中`, async ({ page }) => {
      const { map, closes } = await openBothWindows(page, viewport, ['station', 'river'])
      expectMapWidthWithin(map, FULLY_COVERED_MAP_WIDTH, where)

      for (const kind of CURVE_WINDOW_KINDS) {
        await expectCloseReachable(page, kind, closes[kind], where)
      }
    })
  }

  test('(c) 1100x900 窗宽 480 但两窗不重叠：两窗默认 top 相等，位置等于改动前实测的字面值', async ({ page }) => {
    const where = `${NOT_OVERLAPPING.width}x${NOT_OVERLAPPING.height}`
    const { map, frames } = await openBothWindows(page, NOT_OVERLAPPING, ['river', 'station'])
    expectMapWidthWithin(map, NOT_OVERLAPPING_MAP_WIDTH, where)
    // 硬前提：仍在 30rem 一支（窗宽 480），所以“窗宽是 480 就错开”的写法在这里会被抓住。
    expect(Math.abs(frames.river.width - 480), `河段窗宽 ${frames.river.width} 应为 480`).toBeLessThanOrEqual(BOX_TOLERANCE_PX)
    expect(frames.river.x + frames.river.width, '硬前提：两窗默认的横向范围不重叠').toBeLessThanOrEqual(frames.station.x)

    expect(frames.station.y, '不重叠时两窗默认 top 相等').toBe(frames.river.y)
    for (const kind of CURVE_WINDOW_KINDS) {
      const offset = { x: frames[kind].x - map.x, y: frames[kind].y - map.y }
      const expected = MASTER_OFFSETS_1100x900[kind]
      for (const axis of ['x', 'y'] as const) {
        expect(
          Math.abs(offset[axis] - expected[axis]),
          `${CURVE_WINDOWS[kind].name}相对地图区的 ${axis} ${offset[axis]} 应为 ${expected[axis]}`,
        ).toBeLessThanOrEqual(BOX_TOLERANCE_PX)
      }
    }
  })

  test('(d) 900x500 两窗各自单开：气象代站窗被夹回地图区后，河段窗的关闭按钮仍在它的盒之外', async ({ page }) => {
    await page.setViewportSize(SHORTEST_DESKTOP)
    expect(isMobileForm(requireViewport(page))).toBe(false)
    await installRiverWindowMocks(page)

    // 默认位置只依赖窗的 kind 与地图区尺寸；每次开窗助手都重新导航，所以第二次是从无窗状态开。
    await openCurveWindow(page, 'station')
    await expect(page.getByTestId(CURVE_WINDOWS.station.chart)).toBeAttached()
    const station = await measureCurveWindow(page, 'station')

    await openCurveWindow(page, 'river')
    await expect(page.getByTestId(CURVE_WINDOWS.river.chart)).toBeAttached()
    await expect(curveWindowParts(page, 'station').frame, '重新导航后只有河段窗').toHaveCount(0)
    const river = await measureCurveWindow(page, 'river')
    const riverClose = await boxOf(curveWindowParts(page, 'river').close, '河段窗关闭按钮')
    console.log('curve-window stagger single-open @ 900x500', JSON.stringify({ map: river.map, river: river.frame, station: station.frame, riverClose }))

    expect(station.map, '两次量到的地图区相同').toEqual(river.map)
    expect(river.frame.x + river.frame.width, '硬前提：两窗默认的横向范围重叠').toBeGreaterThan(station.frame.x)
    expect(contains(station.map, station.frame), `气象代站窗 ${JSON.stringify(station.frame)} 应在地图区 ${JSON.stringify(station.map)} 内`).toBe(true)
    expect(
      intersectionOf(riverClose, station.frame),
      `河段窗关闭按钮 ${JSON.stringify(riverClose)} 应整个在气象代站窗 ${JSON.stringify(station.frame)} 之外`,
    ).toBeNull()
  })
})

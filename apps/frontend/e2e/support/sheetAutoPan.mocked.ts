import { expect, type Page } from '@playwright/test'

import {
  CURVE_WINDOWS,
  expectSheet,
  expectedSheetAnchor,
  measureCurveWindow,
  type CurveWindowKind,
  type SheetAnchor,
} from './curveSheet.mocked'
import type { Box } from './legendLauncher.mocked'
import { requireViewport } from './viewportForm'

/**
 * 抽屉自动平移两个 spec（openspec mobile-responsive-display task 4.10，design.md D17）共用的读数与量具。
 * 文件名带 `mocked` token：只被 mocked 车道的 spec 引用。
 *
 * 读的全是 `m11-map-surface` 上的只读属性：`data-selected-anchor-x / -y`（选中锚点的视口 CSS px）与测试门
 * 打开时才有的 `data-camera-center` / `data-camera-zoom`。它们只在相机静止（`moveend`）后更新，所以一次
 * 平移进行中读到的仍是平移前的值——“已静止”的判据要么轮询到期望成立，要么隔一个大于动画时长的间隔读两次。
 */
export const MAP_SURFACE = '[data-testid="m11-map-surface"]'
/** 产品里相机动画的时长上限。 */
export const PAN_DURATION_MS = 450
/** “不应变化”的观察窗：动画时长的 4 倍。 */
export const QUIET_MS = 4 * PAN_DURATION_MS
/** 判“已静止”时两次读数的间隔：大于一次动画的时长。 */
const SETTLE_PROBE_MS = PAN_DURATION_MS + 200
/** “位于未遮盖区中心”的容差。 */
export const CENTRE_TOLERANCE_PX = 4

export interface MapCameraRead {
  /** 两个锚点属性的原文；属性不存在为 null。 */
  anchorX: string | null
  anchorY: string | null
  /** `"lng,lat"`，6 位小数。 */
  center: string | null
  /** 4 位小数。 */
  zoom: string | null
}

export async function readMapCamera(page: Page): Promise<MapCameraRead> {
  return page.locator(MAP_SURFACE).evaluate((surface) => ({
    anchorX: surface.getAttribute('data-selected-anchor-x'),
    anchorY: surface.getAttribute('data-selected-anchor-y'),
    center: surface.getAttribute('data-camera-center'),
    zoom: surface.getAttribute('data-camera-zoom'),
  }))
}

/** 锚点的视口坐标；两个属性缺任何一个都算没有锚点。 */
export function anchorPoint(camera: MapCameraRead): { x: number; y: number } | null {
  if (camera.anchorX === null || camera.anchorY === null) return null
  return { x: Number(camera.anchorX), y: Number(camera.anchorY) }
}

/** 等相机静止并返回读数：隔一个大于动画时长的间隔读两次，四个属性都相同，且相机属性已输出。 */
export async function settledMapCamera(page: Page): Promise<MapCameraRead> {
  let settled: MapCameraRead | undefined
  await expect(async () => {
    const first = await readMapCamera(page)
    await page.waitForTimeout(SETTLE_PROBE_MS)
    const second = await readMapCamera(page)
    expect(second.center, '测试门打开时地图容器应带 data-camera-center').not.toBeNull()
    expect(second.zoom, '测试门打开时地图容器应带 data-camera-zoom').not.toBeNull()
    expect(second).toEqual(first)
    settled = second
  }).toPass({ timeout: 8_000 })
  return settled!
}

export interface AnchorMeasure {
  side: SheetAnchor
  map: Box
  sheet: Box
  /** 未被抽屉遮住的地图区的中心。 */
  uncoveredCentre: { x: number; y: number }
  anchor: { x: number; y: number }
  camera: MapCameraRead
}

/** 未被抽屉遮住的地图区的中心：底部抽屉取抽屉顶边之上那一块，右侧抽屉取抽屉左边之左那一块。 */
export function uncoveredCentreOf(map: Box, sheet: Box, side: SheetAnchor): { x: number; y: number } {
  return side === 'right'
    ? { x: (map.x + sheet.x) / 2, y: map.y + map.height / 2 }
    : { x: map.x + map.width / 2, y: (map.y + sheet.y) / 2 }
}

/**
 * 断言相机静止后选中锚点在地图区内、在抽屉之外（底部抽屉之上 / 右侧抽屉之左，严格不等号）；
 * `centred` 为真时另断言它位于未遮盖区的中心（±4px）。轮询到成立为止，返回成立时的那次测量。
 */
export async function expectAnchorClearOfSheet(
  page: Page,
  kind: CurveWindowKind,
  where: string,
  options: { centred: boolean },
): Promise<AnchorMeasure> {
  const side = expectedSheetAnchor(requireViewport(page))
  // 视口刚变过时抽屉的形态异步落定：先等它成为当前视口应有的那种抽屉，再量锚点。
  await expectSheet(page, kind, where)
  let measure: AnchorMeasure | undefined
  await expect(async () => {
    const { map, frame: sheet } = await measureCurveWindow(page, kind)
    const camera = await readMapCamera(page)
    const anchor = anchorPoint(camera)
    const name = CURVE_WINDOWS[kind].name
    expect(anchor, `${name}打开时地图容器应带两个锚点属性 @ ${where}`).not.toBeNull()
    const uncoveredCentre = uncoveredCentreOf(map, sheet, side)
    const detail = JSON.stringify({ side, map, sheet, uncoveredCentre, anchor })
    expect(anchor!.x, `锚点应在地图区内 ${detail} @ ${where}`).toBeGreaterThan(map.x)
    expect(anchor!.x, `锚点应在地图区内 ${detail} @ ${where}`).toBeLessThan(map.x + map.width)
    expect(anchor!.y, `锚点应在地图区内 ${detail} @ ${where}`).toBeGreaterThan(map.y)
    expect(anchor!.y, `锚点应在地图区内 ${detail} @ ${where}`).toBeLessThan(map.y + map.height)
    if (side === 'right') expect(anchor!.x, `锚点应在右侧抽屉左边之左 ${detail} @ ${where}`).toBeLessThan(sheet.x)
    else expect(anchor!.y, `锚点应在底部抽屉顶边之上 ${detail} @ ${where}`).toBeLessThan(sheet.y)
    if (options.centred) {
      expect(Math.abs(anchor!.x - uncoveredCentre.x), `锚点应在未遮盖区中心（横向）${detail} @ ${where}`).toBeLessThanOrEqual(CENTRE_TOLERANCE_PX)
      expect(Math.abs(anchor!.y - uncoveredCentre.y), `锚点应在未遮盖区中心（纵向）${detail} @ ${where}`).toBeLessThanOrEqual(CENTRE_TOLERANCE_PX)
    }
    measure = { side, map, sheet, uncoveredCentre, anchor: anchor!, camera }
  }).toPass({ timeout: 8_000 })
  return measure!
}

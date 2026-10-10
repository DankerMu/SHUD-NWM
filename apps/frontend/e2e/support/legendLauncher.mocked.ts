import type { Page } from '@playwright/test'

import { MOCK_LAYERS, MOCK_PRECIP_LEGEND } from './layerCatalog.mocked'

/**
 * 图例启动器 spec（openspec mobile-responsive-display task 3.2）的 mock 与共用量具。
 * 文件名带 `mocked` token：这里有覆盖全部 api v1 路径的宽路由。
 *
 * 图层目录 = 径流 + 降水两条（`layerCatalog.mocked` 的 `MOCK_LAYERS`）。降水条目只为让图例出现六级降水段（`precip` 的 URL 缺省即开启），
 * 不 mock 降水 index / PNG：栅格不画，页面可能因此挂一条降水提示，找地图空白点时须避开它。
 */
export { MOCK_PRECIP_LEGEND }

export interface LegendLauncherMockLog {
  /** 页面发出的 api v1 请求数（含瓦片）；零请求窗口的判据。 */
  apiRequests: number
}

export async function mockLegendLauncherApi(page: Page): Promise<LegendLauncherMockLog> {
  const log: LegendLauncherMockLog = { apiRequests: 0 }
  await page.route('**/api/v1/**', async (route) => {
    log.apiRequests += 1
    const url = new URL(route.request().url())
    const data = url.pathname === '/api/v1/layers' ? MOCK_LAYERS : []
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', data }) })
  })
  return log
}

/**
 * 等 api v1 请求计数连续 `quietMs` 不增长，返回此刻的计数（零请求窗口从这里开）。
 * `/` 上没有后台轮询，首屏的取数与瓦片请求落定后计数就不再动。
 */
export async function waitForApiQuiet(page: Page, log: LegendLauncherMockLog, quietMs = 1_000): Promise<number> {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const before = log.apiRequests
    await page.waitForTimeout(quietMs)
    if (log.apiRequests === before) return before
  }
  throw new Error(`api v1 request count never stayed flat for ${quietMs}ms (last count ${log.apiRequests}).`)
}

export type Box = { x: number; y: number; width: number; height: number }

export function intersects(a: Box, b: Box): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height
}

/** `inner` 整个落在 `outer` 内（容许亚像素取整误差）。 */
export function contains(outer: Box, inner: Box, tolerance = 0.5): boolean {
  return (
    inner.x >= outer.x - tolerance &&
    inner.y >= outer.y - tolerance &&
    inner.x + inner.width <= outer.x + outer.width + tolerance &&
    inner.y + inner.height <= outer.y + outer.height + tolerance
  )
}

/**
 * 在地图区里找一个“点下去落在地图画布上”的点：按实测包围盒铺候选网格，取首个
 * `elementFromPoint` 命中 MapLibre canvas 的点。不写死坐标——面板、提示、启动器、控制条、
 * 比例尺 / attribution、角色切换器谁盖在上面，都由浏览器的命中测试说了算。
 */
export async function findBlankMapPoint(page: Page): Promise<{ x: number; y: number }> {
  const point = await page.evaluate(() => {
    const region = document.querySelector('[data-testid="m11-fullscreen-map"]')
    if (!region) return null
    const rect = region.getBoundingClientRect()
    const step = 12
    for (let y = rect.top + step; y < rect.bottom - step; y += step) {
      for (let x = rect.left + step; x < rect.right - step; x += step) {
        const hit = document.elementFromPoint(x, y)
        if (hit instanceof HTMLCanvasElement && hit.classList.contains('maplibregl-canvas')) return { x, y }
      }
    }
    return null
  })
  if (!point) throw new Error('no point of the map region hits canvas.maplibregl-canvas: the map is fully covered or not mounted.')
  return point
}

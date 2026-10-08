import type { Page } from '@playwright/test'

import type { components } from '../../src/api/types'

/**
 * 图例启动器 spec（openspec mobile-responsive-display task 3.2）的 mock 与共用量具。
 * 文件名带 `mocked` token：这里有覆盖全部 api v1 路径的宽路由。
 *
 * 图层目录 = 径流 + 降水两条。降水条目只为让图例出现六级降水段（`precip` 的 URL 缺省即开启），
 * 不 mock 降水 index / PNG：栅格不画，页面可能因此挂一条降水提示，找地图空白点时须避开它。
 */
type Schemas = components['schemas']

/** 六级降水色阶（mm/24h），形状同 `/api/v1/layers` 里 `precip` 条目的 `metadata.legend`。 */
export const MOCK_PRECIP_LEGEND = [
  { min: 0.1, max: 10, color: '#A6F28F', label: '0.1-10' },
  { min: 10, max: 25, color: '#3DBA3D', label: '10-25' },
  { min: 25, max: 50, color: '#61B8FF', label: '25-50' },
  { min: 50, max: 100, color: '#0000FF', label: '50-100' },
  { min: 100, max: 250, color: '#FA00FA', label: '100-250' },
  { min: 250, max: null, color: '#800040', label: '≥250' },
] satisfies Schemas['PrecipLegendEntry'][]

const MOCK_LAYERS = [
  {
    layer_id: 'discharge',
    layer_name: 'Discharge',
    layer_type: 'hydrology',
    variables: ['q_down'],
    // 没有瓦片模板与有效时刻：径流叠加层不画，图例走前端的径流分级合同。
    metadata: { layer_id: 'discharge', tile_format: 'mvt', valid_times: [], fallback_available: false, release_blocking: false },
  },
  {
    layer_id: 'precip',
    layer_name: 'Past 24h precipitation',
    layer_type: 'meteorology',
    variables: ['prcp_rate_or_amount'],
    metadata: {
      layer_id: 'precip',
      tile_format: 'png',
      image_url_template: '/api/v1/precip/{source}/{cycle}/{valid_time}.png',
      index_url_template: '/api/v1/precip/{source}/{cycle}/index',
      bounds: [63, 8, 145, 64],
      legend: MOCK_PRECIP_LEGEND,
      window_hours: 24,
      unit: 'mm/24h',
      palette_version: 'v1',
      fallback_available: false,
      release_blocking: false,
    },
  },
] satisfies Schemas['Layer'][]

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

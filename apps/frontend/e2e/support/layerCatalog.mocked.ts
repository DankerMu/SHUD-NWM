import type { Page } from '@playwright/test'

import type { components } from '../../src/api/types'

/**
 * mocked 车道里图层目录（`/api/v1/layers`）条目的单一来源。
 * 文件名带 `mocked` token：这里有覆盖全部 api v1 路径的宽路由。
 *
 * `riverWindow.mocked.ts` 的径流条目（带瓦片模板与有效时刻）是另一种形状，不在这里。
 */
type Schemas = components['schemas']

/**
 * “只够让页面起来”的最小径流目录条目。故意不合 `Schemas['Layer']`（缺 `tile_format`、
 * `fallback_available`、`release_blocking` 等必填字段）：补字段会改变所有用它的 spec 收到的目录。
 */
export const MOCK_MINIMAL_DISCHARGE_LAYER = {
  layer_id: 'discharge',
  layer_name: 'Discharge',
  layer_type: 'hydrology',
  variables: ['q_down'],
  metadata: { layer_id: 'discharge', valid_times: [] },
}

/** 六级降水色阶（mm/24h），形状同 `/api/v1/layers` 里 `precip` 条目的 `metadata.legend`。 */
export const MOCK_PRECIP_LEGEND = [
  { min: 0.1, max: 10, color: '#A6F28F', label: '0.1-10' },
  { min: 10, max: 25, color: '#3DBA3D', label: '10-25' },
  { min: 25, max: 50, color: '#61B8FF', label: '25-50' },
  { min: 50, max: 100, color: '#0000FF', label: '50-100' },
  { min: 100, max: 250, color: '#FA00FA', label: '100-250' },
  { min: 250, max: null, color: '#800040', label: '≥250' },
] satisfies Schemas['PrecipLegendEntry'][]

/** 合 schema 的径流条目。没有瓦片模板与有效时刻：径流叠加层不画，图例走前端的径流分级合同。 */
export const MOCK_DISCHARGE_LAYER = {
  layer_id: 'discharge',
  layer_name: 'Discharge',
  layer_type: 'hydrology',
  variables: ['q_down'],
  metadata: { layer_id: 'discharge', tile_format: 'mvt', valid_times: [], fallback_available: false, release_blocking: false },
} satisfies Schemas['Layer']

/** 降水目录条目：只为让图例面板多出六级降水段（`precip` 的 URL 缺省即开启）；不 mock 降水 index / PNG。 */
export const MOCK_PRECIP_LAYER = {
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
} satisfies Schemas['Layer']

/** 图层目录 = 径流 + 降水两条。 */
export const MOCK_LAYERS = [MOCK_DISCHARGE_LAYER, MOCK_PRECIP_LAYER] satisfies Schemas['Layer'][]

/**
 * 宽路由：`/api/v1/layers` 回最小径流目录，其余 api v1 路径一律回空数组。
 * 只适用于不再注册别的路由的 spec——`page.route` 后注册者先匹配，叠加别的路由时顺序就有了意义。
 */
export async function mockMinimalDischargeCatalogApi(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    const data = url.pathname === '/api/v1/layers' ? [MOCK_MINIMAL_DISCHARGE_LAYER] : []
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', data }) })
  })
}

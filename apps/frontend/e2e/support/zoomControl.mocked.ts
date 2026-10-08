import { expect, type Page } from '@playwright/test'

/**
 * 缩放 / 指北控件两个 spec（task 3.1）共用的 mock 与就绪判据。
 * 文件名带 `mocked` token：这里有覆盖全部 api v1 路径的宽路由。
 */

export const ZOOM_CONTROL_SELECTORS = {
  zoomIn: '.maplibregl-ctrl-zoom-in',
  zoomOut: '.maplibregl-ctrl-zoom-out',
  compass: '.maplibregl-ctrl-compass',
} as const

export async function mockZoomControlApi(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    const data =
      url.pathname === '/api/v1/layers'
        ? [
            {
              layer_id: 'discharge',
              layer_name: 'Discharge',
              layer_type: 'hydrology',
              variables: ['q_down'],
              metadata: { layer_id: 'discharge', valid_times: [] },
            },
          ]
        : []
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', data }) })
  })
}

/**
 * 地图就绪 = 地图容器与 canvas 可见，且子控件已挂载。
 * `ScaleControl` 与 `NavigationControl` 是同一次提交挂载的兄弟控件：它在，才证明
 * “子控件已挂、缩放按钮确实没有”，而不是“地图还没挂上所以计数为 0”。
 * attribution 由 `Map` 构造时挂上、早于子控件，只证明地图实例存在。
 */
export async function expectMapControlsMounted(page: Page) {
  await expect(page.locator('[data-testid="m11-map-surface"]')).toBeVisible()
  await expect(page.locator('[data-testid="m11-map-surface"] canvas.maplibregl-canvas')).toBeVisible()
  await expect.poll(() => page.locator('.maplibregl-ctrl-scale').count()).toBeGreaterThanOrEqual(1)
  await expect.poll(() => page.locator('.maplibregl-ctrl-attrib').count()).toBeGreaterThanOrEqual(1)
}

export async function expectZoomControlAbsent(page: Page) {
  for (const selector of Object.values(ZOOM_CONTROL_SELECTORS)) {
    await expect(page.locator(selector), `${selector} 不应存在`).toHaveCount(0)
  }
}

export async function expectZoomControlPresent(page: Page) {
  for (const selector of Object.values(ZOOM_CONTROL_SELECTORS)) {
    const button = page.locator(selector)
    await expect(button, `${selector} 应恰一个`).toHaveCount(1)
    await expect(button).toBeVisible()
  }
}

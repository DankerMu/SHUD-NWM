import { expect, test } from '@playwright/test'

/**
 * 地图自带的版权归属（MapLibre attribution）是瓦片供应商的硬要求，必须可见。
 * 它固定在地图右下角，而径流量图例也浮在右下角——两者曾经互相压盖，图例把
 * attribution 遮成只露一角。这条守的是几何：两个矩形不许相交。
 *
 * 只能在真实浏览器里验：jsdom 不做布局，量不出任何一个矩形。
 */
test.describe('M11 单图浮层与地图自带控件不互相压盖', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url())
      if (url.pathname === '/api/v1/layers') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            status: 'ok',
            data: [
              {
                layer_id: 'discharge',
                layer_name: 'Discharge',
                layer_type: 'hydrology',
                variables: ['q_down'],
                metadata: { layer_id: 'discharge', valid_times: [] },
              },
            ],
          }),
        })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', data: [] }) })
    })
  })

  // 实测 1440/800/520px 下 attribution 都是同一条 145x24 的文字条（离底 10px），
  // 三个宽度跑一遍是为了确认避让距离不是只对某一个宽度成立——图例本身会随
  // 内容变宽变窄。
  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 800, height: 900 },
    { width: 520, height: 900 },
  ]) {
  test(`径流量图例不压盖 MapLibre 版权归属 @ ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport)
    await page.goto('/overview?source=gfs&layer=discharge&basemap=vector')

    const legend = page.locator('[data-testid="m11-floating-legend"]')
    const attribution = page.locator('.maplibregl-ctrl-attrib')
    await expect(legend).toBeVisible()
    await expect(attribution).toBeVisible()

    const [a, b] = [await legend.boundingBox(), await attribution.boundingBox()]
    expect(a, '图例应有可测量的布局矩形').not.toBeNull()
    expect(b, '版权归属应有可测量的布局矩形').not.toBeNull()

    const overlapX = Math.min(a!.x + a!.width, b!.x + b!.width) - Math.max(a!.x, b!.x)
    const overlapY = Math.min(a!.y + a!.height, b!.y + b!.height) - Math.max(a!.y, b!.y)
    const intersects = overlapX > 0 && overlapY > 0

    expect(
      intersects,
      `图例 ${JSON.stringify(a)} 与版权归属 ${JSON.stringify(b)} 相交 ${Math.round(overlapX)}x${Math.round(overlapY)}px`,
    ).toBe(false)
  })
  }
})

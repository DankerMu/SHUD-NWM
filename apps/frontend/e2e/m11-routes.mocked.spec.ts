import { expect, test } from '@playwright/test'

test.describe('M11 mocked discharge routes', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url())
      if (url.pathname === '/api/v1/basins') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ status: 'ok', data: [] }),
        })
      }
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

  test('normalizes overview route to the public discharge map shell', async ({ page }) => {
    await page.goto('/overview?source=gfs&layer=discharge&basemap=terrain')

    await expect(page).toHaveURL(/source=gfs/)
    await expect(page.locator('[data-testid="m11-fullscreen-map"]')).toBeVisible()
    await expect(page.locator('[data-testid="m11-floating-layer-switcher"]')).toBeVisible()
  })

  test('keeps station overlay as a separate query flag', async ({ page }) => {
    await page.goto('/overview?metStations=1')

    await expect(page).toHaveURL(/metStations=1/)
    await expect(page.locator('[data-testid="m11-map-surface"]')).toHaveAttribute('data-met-station-feature-count', '0')
  })
})

import { expect, test } from '@playwright/test'

test.describe('M15 visual smoke', () => {
  test('renders the discharge-first overview shell', async ({ page }) => {
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

    await page.goto('/overview')

    await expect(page.locator('[data-testid="m11-fullscreen-map"]')).toBeVisible()
    await expect(page.locator('[data-testid="m11-floating-legend"]')).toBeVisible()
  })
})

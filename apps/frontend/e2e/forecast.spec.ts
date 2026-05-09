import { expect, test, type Page, type Route } from '@playwright/test'

const forecastPayload = {
  segment_id: 'yangtze_rivnet_v01_riv_0007',
  issue_time: '2026-05-09T00:00:00Z',
  unit: 'm3/s',
  series: [
    {
      scenario_id: 'analysis_true_field',
      segment_role: 'analysis',
      points: [
        ['2026-05-08T22:00:00Z', 3180],
        ['2026-05-08T23:00:00Z', 3200],
        ['2026-05-09T00:00:00Z', 3225],
      ],
    },
    {
      scenario_id: 'forecast_gfs_deterministic',
      segment_role: 'forecast',
      points: [
        ['2026-05-09T01:00:00Z', 3300],
        ['2026-05-09T02:00:00Z', 3380],
        ['2026-05-09T03:00:00Z', 3460],
      ],
    },
  ],
  frequency_thresholds: [],
}

function success<T>(data: T) {
  return { status: 'success', data }
}

async function fulfill(route: Route, data: unknown) {
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(success(data)),
  })
}

async function mockForecastApi(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())

    if (url.pathname.endsWith('/forecast-series')) {
      return fulfill(route, forecastPayload)
    }

    throw new Error(`Unhandled mocked API route: ${url.pathname}`)
  })
}

async function clickRiverSegment(page: Page) {
  const canvas = page.locator('.maplibregl-canvas').first()
  await expect(canvas).toBeVisible()

  const box = await canvas.boundingBox()
  if (!box) throw new Error('Map canvas is not measurable')

  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2)
}

test.describe('forecast page', () => {
  test('renders the MapLibre map canvas', async ({ page }) => {
    await mockForecastApi(page)
    await page.goto('/')

    await expect(page.getByLabel('河网地图')).toBeVisible()
    await expect(page.locator('.maplibregl-canvas').first()).toBeVisible()
  })

  test('selects a segment and loads the forecast panel', async ({ page }) => {
    await mockForecastApi(page)
    await page.goto('/')

    await clickRiverSegment(page)

    await expect(page.getByRole('heading', { name: '预报工作台' })).toBeVisible()
    await expect(page.getByText('起报时间')).toBeVisible()
  })

  test('renders the forecast chart after a segment click', async ({ page }) => {
    await mockForecastApi(page)
    await page.goto('/')

    await clickRiverSegment(page)

    await expect(page.getByText('资料来源')).toBeVisible()
    await expect(page.locator('aside canvas').first()).toBeVisible()
  })
})

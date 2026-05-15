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

const riverSegments = {
  type: 'FeatureCollection',
  features: [
    {
      type: 'Feature',
      properties: {
        segment_id: 'backend-seg-7',
        river_segment_id: 'backend-seg-7',
        basin_version_id: 'backend-basin-v1',
        river_network_version_id: 'backend-rivnet-v1',
        name: 'Backend Segment 7',
        stream_order: 4,
      },
      geometry: {
        type: 'LineString',
        coordinates: [
          [108, 30.9],
          [110.8, 30.9],
        ],
      },
    },
  ],
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

    if (url.pathname === '/api/v1/models') {
      return fulfill(route, {
        items: [
          {
            model_id: 'model-1',
            basin_version_id: 'backend-basin-v1',
            river_network_version_id: 'backend-rivnet-v1',
            mesh_version_id: 'mesh-1',
            calibration_version_id: 'cal-1',
            shud_code_version: '2.0',
            active_flag: true,
            model_package_uri: 's3://models/model-1',
            resource_profile: {},
            created_at: '2026-05-09T00:00:00Z',
          },
        ],
        total: 1,
        limit: 1,
        offset: 0,
      })
    }

    if (url.pathname === '/api/v1/basin-versions/backend-basin-v1/river-segments') {
      expect(url.searchParams.get('river_network_version_id')).toBe('backend-rivnet-v1')
      return fulfill(route, riverSegments)
    }

    if (url.pathname.endsWith('/forecast-series')) {
      expect(url.pathname).toContain('/api/v1/basin-versions/backend-basin-v1/river-segments/backend-seg-7/')
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

  const x = box.x + box.width / 2
  const y = box.y + box.height / 2

  await page.mouse.move(x, y)
  await expect(page.getByText('backend-seg-7')).toBeVisible()

  const forecastLoaded = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return url.pathname.endsWith('/forecast-series') && response.status() === 200
  })
  await page.mouse.click(x, y)
  await forecastLoaded
}

async function gotoForecastPage(page: Page) {
  const riverSegmentsLoaded = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return url.pathname === '/api/v1/basin-versions/backend-basin-v1/river-segments' && response.status() === 200
  })

  await page.goto('/')
  await riverSegmentsLoaded
}

test.describe('forecast page', () => {
  test('renders the MapLibre map canvas', async ({ page }) => {
    await mockForecastApi(page)
    await gotoForecastPage(page)

    await expect(page.getByLabel('河网地图')).toBeVisible()
    await expect(page.locator('.maplibregl-canvas').first()).toBeVisible()
  })

  test('selects a segment and loads the forecast panel', async ({ page }) => {
    await mockForecastApi(page)
    await gotoForecastPage(page)

    await clickRiverSegment(page)

    await expect(page.getByRole('heading', { name: '预报工作台' })).toBeVisible()
    await expect(page.getByText('起报时间')).toBeVisible()
  })

  test('renders the forecast chart after a segment click', async ({ page }) => {
    await mockForecastApi(page)
    await gotoForecastPage(page)

    await clickRiverSegment(page)

    await expect(page.locator('aside').getByText('数据源')).toBeVisible()
    await expect(page.locator('aside').locator('div').filter({ hasText: /^数据源\s*GFS$/ })).toBeVisible()
    await expect(page.locator('aside canvas').first()).toBeVisible()
  })
})

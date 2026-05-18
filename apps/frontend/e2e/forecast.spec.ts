import { expect, test, type Page, type Route } from '@playwright/test'

const apiBase = 'https://api.example.test'

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
  total: 1,
  feature_total: 1,
  limit: 500,
  offset: 0,
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

function riverFeature(segmentId: string, coordinates: [number, number][], streamOrder = 2) {
  return {
    type: 'Feature',
    properties: {
      segment_id: segmentId,
      river_segment_id: segmentId,
      basin_version_id: 'backend-basin-v1',
      river_network_version_id: 'backend-rivnet-v1',
      name: `Backend Segment ${segmentId}`,
      stream_order: streamOrder,
    },
    geometry: {
      type: 'LineString',
      coordinates,
    },
  }
}

const firstRiverSegmentPage = {
  type: 'FeatureCollection',
  total: 501,
  feature_total: 501,
  limit: 500,
  offset: 0,
  features: Array.from({ length: 500 }, (_, index) =>
    riverFeature(`backend-seg-${index + 1}`, [
      [96 + index * 0.01, 33.5],
      [96.005 + index * 0.01, 33.5],
    ]),
  ),
}

const secondRiverSegmentPage = {
  type: 'FeatureCollection',
  total: 501,
  feature_total: 501,
  limit: 500,
  offset: 500,
  features: riverSegments.features,
}

const largeFirstRiverSegmentPage = {
  ...firstRiverSegmentPage,
  total: 2500,
  feature_total: 2500,
}

const largeSecondRiverSegmentPage = {
  ...secondRiverSegmentPage,
  total: 2500,
  feature_total: 2500,
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
            basin_id: 'backend-basin',
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
      expect(url.searchParams.get('limit')).toBe('500')
      expect(url.searchParams.get('offset')).toBe('0')
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

  await page.goto('/forecast', { waitUntil: 'domcontentloaded' })
  await riverSegmentsLoaded
}

test.describe('forecast page', () => {
  test('renders the MapLibre map canvas', async ({ page }) => {
    await mockForecastApi(page)
    await gotoForecastPage(page)

    await expect(page.getByLabel('河网地图')).toBeVisible()
    await expect(page.locator('.maplibregl-canvas').first()).toBeVisible()
  })

  test('uses the configured API base for model, river segment, and forecast series requests', async ({ page }) => {
    const origins: string[] = []
    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url())
      origins.push(url.origin)
      if (url.pathname === '/api/v1/models') {
        return fulfill(route, {
          items: [
            {
              model_id: 'model-1',
              basin_id: 'backend-basin',
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
        expect(url.searchParams.get('limit')).toBe('500')
        expect(url.searchParams.get('offset')).toBe('0')
        return fulfill(route, riverSegments)
      }
      if (url.pathname.endsWith('/forecast-series')) return fulfill(route, forecastPayload)
      throw new Error(`Unhandled mocked API route: ${url.pathname}`)
    })

    await gotoForecastPage(page)
    await clickRiverSegment(page)

    expect(new Set(origins)).toEqual(new Set([apiBase]))
  })

  test('selects a segment and loads the forecast panel', async ({ page }) => {
    await mockForecastApi(page)
    await gotoForecastPage(page)

    await clickRiverSegment(page)

    await expect(page.getByRole('heading', { name: '预报工作台' })).toBeVisible()
    await expect(page.getByText('起报时间')).toBeVisible()
  })

  test('offers basin drill-down handoff when active basin context is available', async ({ page }) => {
    await mockForecastApi(page)
    await gotoForecastPage(page)

    await expect(page.getByRole('link', { name: '进入流域分析' })).toHaveAttribute(
      'href',
      '/basins/backend-basin?basinVersionId=backend-basin-v1',
    )
  })

  test('renders the forecast chart after a segment click', async ({ page }) => {
    await mockForecastApi(page)
    await gotoForecastPage(page)

    await clickRiverSegment(page)

    await expect(page.locator('aside').getByText('数据源')).toBeVisible()
    await expect(page.locator('aside').locator('div').filter({ hasText: /^数据源\s*GFS$/ })).toBeVisible()
    await expect(page.locator('aside canvas').first()).toBeVisible()
  })

  test('paginates river segments before selecting a forecast segment', async ({ page }) => {
    const riverPageOffsets: string[] = []
    const forecastRequests: string[] = []

    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url())

      if (url.pathname === '/api/v1/models') {
        return fulfill(route, {
          items: [
            {
              model_id: 'model-1',
              basin_id: 'backend-basin',
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
        expect(url.searchParams.get('limit')).toBe('500')
        const offset = url.searchParams.get('offset') ?? ''
        riverPageOffsets.push(offset)
        return fulfill(route, offset === '0' ? firstRiverSegmentPage : secondRiverSegmentPage)
      }

      if (url.pathname.endsWith('/forecast-series')) {
        forecastRequests.push(url.pathname)
        return fulfill(route, forecastPayload)
      }

      throw new Error(`Unhandled mocked API route: ${url.pathname}`)
    })

    await gotoForecastPage(page)
    await expect.poll(() => [...new Set(riverPageOffsets)]).toEqual(['0', '500'])
    await clickRiverSegment(page)

    expect(forecastRequests).toEqual([
      '/api/v1/basin-versions/backend-basin-v1/river-segments/backend-seg-7/forecast-series',
    ])
  })

  test('bounds initial river pagination and shows a partial river preview notice', async ({ page }) => {
    const riverPageOffsets: string[] = []

    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url())

      if (url.pathname === '/api/v1/models') {
        return fulfill(route, {
          items: [
            {
              model_id: 'model-1',
              basin_id: 'backend-basin',
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
        expect(url.searchParams.get('limit')).toBe('500')
        const offset = url.searchParams.get('offset') ?? ''
        riverPageOffsets.push(offset)
        if (offset === '0') return fulfill(route, largeFirstRiverSegmentPage)
        if (offset === '500') return fulfill(route, largeSecondRiverSegmentPage)
        throw new Error(`Initial river loading must remain bounded; unexpected offset ${offset}`)
      }

      throw new Error(`Unhandled mocked API route: ${url.pathname}`)
    })

    await gotoForecastPage(page)

    await expect(page.getByRole('status').filter({ hasText: '河网预览' })).toContainText(
      '当前显示前 501 / 2,500 条河段',
    )
    expect(new Set(riverPageOffsets)).toEqual(new Set(['0', '500']))
    expect(riverPageOffsets.every((offset) => offset === '0' || offset === '500')).toBe(true)
  })
})

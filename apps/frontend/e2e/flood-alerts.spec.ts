import { expect, test, type Page, type Request, type Route } from '@playwright/test'

const run = {
  run_id: 'run-flood-1',
  run_type: 'forecast',
  scenario_id: 'forecast_gfs_deterministic',
  model_id: 'model-1',
  basin_version_id: 'basin-v1',
  source_id: 'gfs',
  cycle_time: '2026-05-12T00:00:00Z',
  status: 'frequency_done',
  start_time: '2026-05-12T00:00:00Z',
  end_time: '2026-05-12T03:00:00Z',
  created_at: '2026-05-12T00:00:00Z',
  updated_at: '2026-05-12T04:00:00Z',
}

const summary = {
  run_id: 'run-flood-1',
  total_segments: 4,
  usable_curves: 3,
  unavailable_count: 1,
  quality_note: null,
  levels: [{ level: 'warning', count: 2, color: '#f59e0b' }],
}

const ranking = {
  items: [
    {
      rank: 1,
      river_segment_id: 'seg-1',
      segment_id: 'seg-1',
      segment_name: 'Flood Segment 1',
      basin_version_id: 'basin-v1',
      q_value: 1234,
      q_unit: 'm3/s',
      return_period: 20,
      warning_level: 'warning',
      duration: '1h',
      valid_time: '2026-05-12T03:00:00Z',
    },
  ],
  total: 1,
  limit: 20,
  offset: 0,
}

const timeline = {
  run_id: 'run-flood-1',
  segment_id: 'seg-1',
  river_segment_id: 'seg-1',
  timesteps: [
    {
      valid_time: '2026-05-12T03:00:00Z',
      return_period: 20,
      warning_level: 'warning',
      q_value: 1234,
    },
  ],
  timeline: [],
  peak: {
    valid_time: '2026-05-12T03:00:00Z',
    return_period: 20,
    warning_level: 'warning',
    q_value: 1234,
  },
  frequency_thresholds: {
    Q2: 100,
    Q5: 200,
    Q10: 300,
    Q20: 400,
    Q50: 500,
    Q100: 600,
    sample_quality: { count: 30 },
  },
  quality_note: null,
}

const forecastPayload = {
  segment_id: 'seg-1',
  issue_time: '2026-05-12T00:00:00Z',
  unit: 'm3/s',
  series: [
    {
      scenario_id: 'forecast_gfs_deterministic',
      segment_role: 'forecast',
      points: [
        ['2026-05-12T01:00:00Z', 1100],
        ['2026-05-12T02:00:00Z', 1200],
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

async function mockFloodApi(page: Page, onRequest?: (request: Request) => void) {
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    onRequest?.(request)
    const url = new URL(request.url())

    if (url.pathname === '/api/v1/runs') {
      expect(url.searchParams.get('status')).toBe('frequency_done')
      return fulfill(route, { items: [run], total: 1, limit: 50, offset: 0 })
    }
    if (url.pathname === '/api/v1/flood-alerts/summary') {
      expect(url.searchParams.get('run_id')).toBe('run-flood-1')
      return fulfill(route, summary)
    }
    if (url.pathname === '/api/v1/flood-alerts/ranking') {
      expect(url.searchParams.get('run_id')).toBe('run-flood-1')
      expect(url.searchParams.get('limit')).toBe('20')
      return fulfill(route, ranking)
    }
    if (url.pathname === '/api/v1/tiles/flood-return-period') {
      expect(url.searchParams.get('run_id')).toBe('run-flood-1')
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ type: 'FeatureCollection', features: [] }),
      })
    }
    if (url.pathname === '/api/v1/flood-alerts/timeline') {
      expect(url.searchParams.get('run_id')).toBe('run-flood-1')
      expect(url.searchParams.get('segment_id')).toBe('seg-1')
      return fulfill(route, timeline)
    }
    if (url.pathname.endsWith('/forecast-series')) {
      expect(url.pathname).toContain('/api/v1/basin-versions/basin-v1/river-segments/seg-1/')
      return fulfill(route, forecastPayload)
    }

    throw new Error(`Unhandled mocked API route: ${request.method()} ${url.pathname}`)
  })
}

test.describe('flood alerts page', () => {
  test('loads latest run, summary, ranking, tile, and selected segment timeline through configured API base', async ({ page }) => {
    const calls: Array<{ origin: string; pathname: string }> = []
    await mockFloodApi(page, (request) => {
      const url = new URL(request.url())
      calls.push({ origin: url.origin, pathname: url.pathname })
    })

    await page.goto('/flood-alerts')

    await expect(page.getByRole('heading', { name: '洪水预警' })).toBeVisible()
    await expect(page.getByText('run-flood-1')).toBeVisible()
    await expect(page.getByText('2 条')).toBeVisible()
    const rankingRow = page.getByRole('row', { name: /Flood Segment 1/ })
    await expect(rankingRow).toBeVisible()

    await rankingRow.click()

    await expect(page.getByRole('heading', { name: 'Flood Segment 1' })).toBeVisible()
    await expect.poll(() => calls.map((call) => call.pathname)).toContain('/api/v1/flood-alerts/timeline')

    for (const path of [
      '/api/v1/runs',
      '/api/v1/flood-alerts/summary',
      '/api/v1/flood-alerts/ranking',
      '/api/v1/tiles/flood-return-period',
      '/api/v1/flood-alerts/timeline',
      '/api/v1/basin-versions/basin-v1/river-segments/seg-1/forecast-series',
    ]) {
      expect(calls.some((call) => call.origin === 'https://api.example.test' && call.pathname === path)).toBe(true)
    }
  })
})

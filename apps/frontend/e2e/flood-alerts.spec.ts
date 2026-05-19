import { expect, test, type Page, type Request, type Route } from '@playwright/test'

const apiBase = 'https://api.example.test'

const run = {
  run_id: 'run-flood-1',
  run_type: 'forecast',
  scenario_id: 'forecast_gfs_deterministic',
  model_id: 'model-1',
  basin_version_id: 'basin-v1',
  river_network_version_id: 'rivnet-v1',
  source_id: 'gfs',
  cycle_time: '2026-05-12T00:00:00Z',
  status: 'frequency_done',
  start_time: '2026-05-12T00:00:00Z',
  end_time: '2026-05-12T03:00:00Z',
  created_at: '2026-05-12T00:00:00Z',
  updated_at: '2026-05-12T04:00:00Z',
}

const ifsRun = {
  ...run,
  run_id: 'run-ifs-specific',
  scenario_id: 'forecast_ifs_deterministic',
  source_id: 'ifs',
  cycle_time: '2026-05-13T00:00:00Z',
  start_time: '2026-05-13T00:00:00Z',
  end_time: '2026-05-13T06:00:00Z',
  created_at: '2026-05-13T00:00:00Z',
  updated_at: '2026-05-13T07:00:00Z',
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
      river_network_version_id: 'rivnet-v1',
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
  river_network_version_id: 'rivnet-v1',
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
  river_network_version_id: 'rivnet-v1',
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

function readyRunsForStatus(status: string | null) {
  if (status === 'frequency_done') return [run]
  if (status === 'published') return [{ ...run, run_id: 'run-published-older', status: 'published', cycle_time: '2026-05-11T00:00:00Z' }]
  throw new Error(`Unexpected run status query: ${status}`)
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
      const items = readyRunsForStatus(url.searchParams.get('status'))
      return fulfill(route, { items, total: items.length, limit: 50, offset: 0 })
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
      expect(url.searchParams.get('river_network_version_id')).toBe('rivnet-v1')
      return fulfill(route, timeline)
    }
    if (url.pathname.endsWith('/forecast-series')) {
      expect(url.pathname).toContain('/api/v1/basin-versions/basin-v1/river-segments/seg-1/')
      expect(url.searchParams.get('river_network_version_id')).toBe('rivnet-v1')
      return fulfill(route, forecastPayload)
    }

    throw new Error(`Unhandled mocked API route: ${request.method()} ${url.pathname}`)
  })
}

test.describe('flood alerts page', () => {
  test('loads latest run, summary, ranking, tile, and selected segment timeline through configured API base', async ({ page }) => {
    const forecastSeriesPath = '/api/v1/basin-versions/basin-v1/river-segments/seg-1/forecast-series'
    const calls: Array<{ origin: string; pathname: string; searchParams: URLSearchParams }> = []
    await mockFloodApi(page, (request) => {
      const url = new URL(request.url())
      calls.push({ origin: url.origin, pathname: url.pathname, searchParams: url.searchParams })
    })

    await page.goto('/flood-alerts')

    await expect(page.getByRole('heading', { name: '洪水预警' })).toBeVisible()
    await expect(page.getByText('run-flood-1')).toBeVisible()
    await expect(page.getByText('2 条')).toBeVisible()
    const rankingRow = page.getByRole('row', { name: /Flood Segment 1/ })
    await expect(rankingRow).toBeVisible()

    const forecastSeriesResponse = page.waitForResponse((response) => {
      const url = new URL(response.url())
      return url.origin === apiBase && url.pathname === forecastSeriesPath && response.status() === 200
    })
    await rankingRow.click()
    await forecastSeriesResponse

    await expect(page.getByRole('heading', { name: 'Flood Segment 1' })).toBeVisible()
    await expect(page.getByRole('link', { name: '查看河段详情' })).toHaveAttribute(
      'href',
      /\/segments\/seg-1\?source=gfs&cycle=2026-05-12T00%3A00%3A00.000Z&validTime=2026-05-12T03%3A00%3A00.000Z&layer=flood-return-period&basinVersionId=basin-v1&riverNetworkVersionId=rivnet-v1&segmentId=seg-1/,
    )
    await expect.poll(() => calls.map((call) => call.pathname)).toContain('/api/v1/flood-alerts/timeline')
    const timelineCall = calls.find((call) => call.pathname === '/api/v1/flood-alerts/timeline')
    const forecastSeriesCall = calls.find((call) => call.pathname === forecastSeriesPath)
    expect(timelineCall?.searchParams.get('river_network_version_id')).toBe('rivnet-v1')
    expect(forecastSeriesCall?.searchParams.get('river_network_version_id')).toBe('rivnet-v1')

    for (const path of [
      '/api/v1/runs',
      '/api/v1/flood-alerts/summary',
      '/api/v1/flood-alerts/ranking',
      '/api/v1/tiles/flood-return-period',
      '/api/v1/flood-alerts/timeline',
      forecastSeriesPath,
    ]) {
      expect(calls.some((call) => call.origin === apiBase && call.pathname === path)).toBe(true)
    }
  })

  test('honors explicit IFS cycle and valid time handoff without latest GFS fallback', async ({ page }) => {
    const calls: string[] = []
    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url())
      calls.push(url.toString())

      if (url.pathname === '/api/v1/runs') {
        expect(url.searchParams.get('source')).toBe('IFS')
        expect(url.searchParams.get('cycle_time')).toBe('2026-05-13T00:00:00.000Z')
        const status = url.searchParams.get('status')
        expect(['frequency_done', 'published']).toContain(status)
        const items = status === 'frequency_done' ? [ifsRun] : []
        return fulfill(route, { items, total: items.length, limit: 50, offset: 0 })
      }
      if (url.pathname === '/api/v1/flood-alerts/summary') {
        expect(url.searchParams.get('run_id')).toBe('run-ifs-specific')
        expect(url.searchParams.get('valid_time')).toBe('2026-05-13T06:00:00.000Z')
        return fulfill(route, { ...summary, run_id: 'run-ifs-specific' })
      }
      if (url.pathname === '/api/v1/flood-alerts/ranking') {
        expect(url.searchParams.get('run_id')).toBe('run-ifs-specific')
        expect(url.searchParams.get('valid_time')).toBe('2026-05-13T06:00:00.000Z')
        return fulfill(route, {
          ...ranking,
          items: [
            {
              ...ranking.items[0],
              river_network_version_id: 'rivnet-v1',
              segment_name: 'IFS Specific Segment',
              valid_time: '2026-05-13T06:00:00Z',
            },
          ],
        })
      }
      if (url.pathname === '/api/v1/tiles/flood-return-period') {
        expect(url.searchParams.get('run_id')).toBe('run-ifs-specific')
        expect(url.searchParams.get('valid_time')).toBe('2026-05-13T06:00:00.000Z')
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ type: 'FeatureCollection', features: [] }),
        })
      }

      throw new Error(`Unhandled mocked API route: ${route.request().method()} ${url.pathname}`)
    })

    await page.goto('/flood-alerts?source=ifs&cycle=2026-05-13T00:00:00Z&validTime=2026-05-13T06:00:00Z')

    await expect(page.getByText('run-ifs-specific')).toBeVisible()
    await expect(page.getByRole('row', { name: /IFS Specific Segment/ })).toBeVisible()
    expect(calls.join('\n')).not.toContain('run-flood-1')
  })

  test('does not render latest GFS data when explicit IFS cycle is absent', async ({ page }) => {
    const calls: string[] = []
    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url())
      calls.push(url.toString())

      if (url.pathname === '/api/v1/runs') {
        expect(url.searchParams.get('source')).toBe('IFS')
        expect(url.searchParams.get('cycle_time')).toBe('2026-05-13T00:00:00.000Z')
        const status = url.searchParams.get('status')
        expect(['frequency_done', 'published']).toContain(status)
        const items = status === 'frequency_done' ? [run] : []
        return fulfill(route, { items, total: items.length, limit: 50, offset: 0 })
      }

      throw new Error(`Unexpected fallback request: ${url.pathname}`)
    })

    await page.goto('/flood-alerts?source=ifs&cycle=2026-05-13T00:00:00Z&validTime=2026-05-13T06:00:00Z')

    await expect(page.getByText('暂无洪水预警数据')).toBeVisible()
    await expect(page.getByText(/未找到 IFS 周期/)).toBeVisible()
    expect(calls.some((url) => url.includes('/api/v1/flood-alerts/summary'))).toBe(false)
    expect(calls.some((url) => url.includes('run-flood-1'))).toBe(false)
  })
})

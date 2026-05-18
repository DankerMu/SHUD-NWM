import { expect, test } from '@playwright/test'
import type { Page, Route } from '@playwright/test'

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

const overviewBasinVersion = {
  basin_version_id: 'bv-001',
  basin_id: 'basin-demo',
  version_label: 'v2026_01',
  geom: {
    type: 'MultiPolygon',
    coordinates: [[[[100, 30], [105, 30], [105, 35], [100, 35], [100, 30]]]],
  },
  active_flag: true,
  valid_from: '2026-01-01T00:00:00Z',
  valid_to: null,
  source_uri: null,
  checksum: null,
  created_at: '2026-05-01T00:00:00Z',
}

const basinRiverSegments = {
  type: 'FeatureCollection',
  total: 2,
  feature_total: 2,
  limit: 1000,
  offset: 0,
  features: [
    {
      type: 'Feature',
      properties: {
        segment_id: 'seg-001',
        river_segment_id: 'seg-001',
        basin_version_id: 'bv-001',
        river_network_version_id: 'rn-v1',
        name: 'North Branch 001',
        stream_order: 1,
        length_m: 800,
      },
      geometry: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
    },
    {
      type: 'Feature',
      properties: {
        segment_id: 'seg-009',
        river_segment_id: 'seg-009',
        basin_version_id: 'bv-001',
        river_network_version_id: 'rn-v1',
        name: 'Main Stem 009',
        stream_order: 3,
        length_m: 1200,
      },
      geometry: { type: 'LineString', coordinates: [[101, 31], [102, 32]] },
    },
  ],
}

async function mockOverviewApis(
  page: Page,
  options: { partialQueueFailure?: boolean; lowRequestPlan?: boolean; runSource?: 'gfs' | 'ifs' } = {},
) {
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())

    if (url.pathname === '/api/v1/basins') {
      return fulfill(route, [
        {
          basin_id: 'basin-demo',
          basin_name: 'Demo Basin',
          basin_group: 'major',
          description: null,
          created_at: '2026-05-01T00:00:00Z',
        },
      ])
    }
    if (url.pathname === '/api/v1/basins/basin-demo/versions') return fulfill(route, [overviewBasinVersion])
    if (url.pathname === '/api/v1/models') {
      return fulfill(route, {
        items: [
          {
            model_id: 'model-demo',
            model_name: 'Demo SHUD',
            basin_id: 'basin-demo',
            basin_name: 'Demo Basin',
            basin_version_id: 'bv-001',
            river_network_version_id: 'rn-v1',
            mesh_version_id: 'mesh-v1',
            calibration_version_id: 'cal-v1',
            segment_count: 2,
            active_flag: true,
            shud_code_version: 'v1',
            created_at: '2026-05-01T00:00:00Z',
          },
        ],
        total: 1,
        limit: 200,
        offset: 0,
      })
    }
    if (url.pathname === '/api/v1/runs') {
      return fulfill(route, {
        items: options.lowRequestPlan
          ? []
          : [
              {
                run_id: 'run-overview',
                run_type: 'forecast',
                scenario_id: options.runSource === 'ifs' ? 'forecast_ifs_deterministic' : 'forecast_gfs_deterministic',
                model_id: 'model-demo',
                basin_version_id: 'bv-001',
                source_id: options.runSource ?? 'gfs',
                cycle_time: '2026-05-18T00:00:00Z',
                status: 'frequency_done',
                start_time: '2026-05-18T00:00:00Z',
                end_time: '2026-05-18T03:00:00Z',
                created_at: '2026-05-18T00:00:00Z',
                updated_at: '2026-05-18T04:00:00Z',
              },
            ],
        total: options.lowRequestPlan ? 0 : 1,
        limit: 20,
        offset: 0,
      })
    }
    if (url.pathname === '/api/v1/layers') {
      return fulfill(route, [
        { layer_id: 'discharge', layer_name: 'River discharge', layer_type: 'hydrology', variables: ['q_down'] },
        { layer_id: 'flood-return-period', layer_name: 'Flood return period', layer_type: 'hydrology', variables: ['return_period'] },
      ])
    }
    if (url.pathname.startsWith('/api/v1/layers/') && url.pathname.endsWith('/valid-times')) {
      return fulfill(route, ['2026-05-18T00:00:00Z', '2026-05-18T06:00:00Z'])
    }
    if (url.pathname === '/api/v1/queue/depth') {
      if (options.partialQueueFailure) return route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
      return fulfill(route, { running: 1, pending: 0, idle: 3 })
    }
    if (url.pathname === '/api/v1/pipeline/status') {
      return fulfill(route, {
        source: 'GFS',
        cycle_time: '2026-05-18T00:00:00Z',
        current_state: 'running',
        started_at: '2026-05-18T00:00:00Z',
        updated_at: '2026-05-18T04:00:00Z',
        job_counts: { succeeded: 3, failed: 0, running: 1, pending: 0 },
      })
    }
    if (url.pathname === '/api/v1/flood-alerts/summary') {
      return fulfill(route, {
        run_id: 'run-overview',
        total_segments: 2,
        usable_curves: 2,
        unavailable_count: 0,
        quality_note: null,
        levels: [{ level: 'warning', count: 1, color: '#FFB74D' }],
      })
    }
    if (url.pathname === '/api/v1/flood-alerts/ranking') {
      return fulfill(route, {
        items: [
          {
            rank: 1,
            river_segment_id: 'seg-009',
            segment_id: 'seg-009',
            segment_name: 'Main Stem 009',
            basin_version_id: 'bv-001',
            q_value: 123,
            q_unit: 'm3/s',
            return_period: 20,
            warning_level: 'warning',
            duration: '1h',
            valid_time: '2026-05-18T06:00:00Z',
          },
        ],
        total: 1,
        limit: 200,
        offset: 0,
      })
    }
    if (url.pathname === '/api/v1/tiles/flood-return-period') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ type: 'FeatureCollection', features: [] }),
      })
    }
    if (url.pathname === '/api/v1/basin-versions/bv-001/river-segments') return fulfill(route, basinRiverSegments)
    if (url.pathname === '/api/v1/basin-versions/bv-001/river-segments/seg-009') {
      return fulfill(route, {
        river_segment_id: 'seg-009',
        river_network_version_id: 'rn-v1',
        segment_order: 3,
        downstream_segment_id: null,
        length_m: 1200,
        geom: { type: 'LineString', coordinates: [[101, 31], [102, 32]] },
        properties_json: {},
        created_at: '2026-05-01T00:00:00Z',
      })
    }
    if (url.pathname === '/api/v1/basin-versions/bv-001/river-segments/seg-009/forecast-series') {
      return fulfill(route, {
        river_segment_id: 'seg-009',
        issue_time: '2026-05-18T00:00:00Z',
        variable: 'q_down',
        unit: 'm3/s',
        frequency_thresholds: null,
        segments: [
          {
            scenario: 'forecast_ifs_deterministic',
            scenario_id: 'forecast_ifs_deterministic',
            source: 'IFS',
            segment_role: 'future_7_days',
            data: [{ valid_time: '2026-05-18T06:00:00Z', value: 456 }],
          },
        ],
      })
    }
    if (url.pathname === '/api/v1/flood-alerts/timeline') {
      return fulfill(route, {
        run_id: 'run-overview',
        segment_id: url.searchParams.get('segment_id') ?? 'seg-009',
        river_segment_id: url.searchParams.get('segment_id') ?? 'seg-009',
        timesteps: [],
        timeline: [],
        peak: { valid_time: '2026-05-18T06:00:00Z', return_period: 20, warning_level: 'warning', q_value: 456 },
        frequency_thresholds: null,
        quality_note: null,
      })
    }
    if (url.pathname === '/api/v1/lineage/river-point') {
      return fulfill(route, { target_type: 'river_point', target_id: url.searchParams.get('segment_id') ?? 'seg-009', nodes: [], edges: [] })
    }

    throw new Error(`Unhandled overview API request: ${url.pathname}`)
  })
}

async function mockFloodWorkflowApis(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())

    if (url.pathname === '/api/v1/runs') {
      return fulfill(route, {
        items: [
          {
            run_id: 'run-flood-route',
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
          },
        ],
        total: 1,
        limit: 50,
        offset: 0,
      })
    }
    if (url.pathname === '/api/v1/flood-alerts/summary') {
      return fulfill(route, {
        run_id: 'run-flood-route',
        total_segments: 4,
        usable_curves: 3,
        unavailable_count: 1,
        quality_note: null,
        levels: [{ level: 'warning', count: 2, color: '#f59e0b' }],
      })
    }
    if (url.pathname === '/api/v1/flood-alerts/ranking') {
      return fulfill(route, {
        items: [
          {
            rank: 1,
            river_segment_id: 'seg-route',
            segment_id: 'seg-route',
            segment_name: 'Flood Route Segment',
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
      })
    }
    if (url.pathname === '/api/v1/tiles/flood-return-period') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ type: 'FeatureCollection', features: [] }),
      })
    }

    throw new Error(`Unhandled flood route API request: ${url.pathname}`)
  })
}

async function mockMonitoringWorkflowApis(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())

    if (url.pathname === '/api/v1/pipeline/status') {
      return fulfill(route, {
        source: 'GFS',
        cycle_time: '2026-05-09T00:00:00Z',
        current_state: 'partially_failed',
        started_at: '2026-05-09T00:00:30Z',
        updated_at: '2026-05-09T00:08:00Z',
        job_counts: { succeeded: 3, failed: 1, running: 1, pending: 2 },
      })
    }
    if (url.pathname === '/api/v1/pipeline/stages') {
      return fulfill(route, [
        {
          stage: 'forcing',
          display_status: 'partially_failed',
          status: 'partially_failed',
          duration_seconds: 35,
          basin_progress: { completed: 3, total: 4, failed: 1 },
          basin_results: [],
        },
      ])
    }
    if (url.pathname === '/api/v1/queue/depth') return fulfill(route, { running: 2, pending: 4, idle: 6 })
    if (url.pathname === '/api/v1/metrics/stage-duration') return fulfill(route, [])
    if (url.pathname === '/api/v1/metrics/success-rate') return fulfill(route, [])
    if (url.pathname === '/api/v1/jobs') {
      return fulfill(route, {
        items: [
          {
            job_id: 'job-route',
            run_id: 'run-route',
            cycle_id: 'cycle-1',
            job_type: 'forecast',
            slurm_job_id: '1001',
            model_id: 'model-route',
            status: 'failed',
            stage: 'forecast',
            submitted_at: '2026-05-09T00:03:00Z',
            started_at: '2026-05-09T00:04:00Z',
            finished_at: '2026-05-09T00:06:00Z',
            exit_code: 1,
            retry_count: 0,
            error_code: 'E_MODEL',
            error_message: 'model failed',
            log_uri: null,
            duration_seconds: 120,
          },
        ],
        total: 1,
        limit: 12,
        offset: 0,
      })
    }

    throw new Error(`Unhandled monitoring route API request: ${url.pathname}`)
  })
}

async function selectOperatorRole(page: Page) {
  await expect(page.getByLabel('Role')).toBeVisible()
  await page.getByLabel('Role').click({ force: true })
  await page.getByRole('option', { name: 'Operator' }).click({ force: true })
}

test.describe('M11 navigation and route shells', () => {
  test('renders the national overview shell at / and /overview', async ({ page }) => {
    await mockOverviewApis(page)

    await page.goto('/')
    await expect(page.getByRole('heading', { name: '全国总览' })).toBeVisible()
    await expect(page.getByLabel('全国总览地图')).toBeVisible()
    await expect(page.getByRole('link', { name: /全国总览/ })).toBeVisible()
    await expect(page.getByLabel('全国流域树')).toContainText('Demo Basin')

    await page.goto('/overview?source=gfs&layer=flood-return-period&basemap=terrain')
    await expect(page.getByRole('heading', { name: '全国总览' })).toBeVisible()
    await expect(page.getByLabel('全国总览地图').getByText('flood-return-period')).toBeVisible()
    await expect(page.getByLabel('全国总览地图').getByText('terrain')).toBeVisible()
  })

  test('supports overview basin visibility, source/layer changes, popup drill-down, and summary links', async ({ page }) => {
    await mockOverviewApis(page, { lowRequestPlan: true })

    await page.goto('/overview?source=gfs')
    await expect(page.getByTestId('m11-map-surface')).toHaveAttribute('data-visible-basin-ids', 'basin-demo')

    await page.getByRole('checkbox', { name: 'Demo Basin 可见' }).click()
    await expect(page.getByTestId('m11-map-surface')).toHaveAttribute('data-visible-basin-ids', '')
    await expect(page.getByTestId('m11-basin-layer-unavailable')).toBeVisible()
    await page.getByRole('button', { name: '全选' }).click()
    await expect(page.getByTestId('m11-map-surface')).toHaveAttribute('data-visible-basin-ids', 'basin-demo')

    await page.getByRole('button', { name: /洪水重现期/ }).click()
    await expect(page).toHaveURL(/layer=flood-return-period/)
    await page.getByRole('button', { name: /^IFS/ }).click()
    await expect(page).toHaveURL(/source=ifs/)

    await expect(page.getByTestId('m11-basin-popup')).toHaveCount(0)
    await page.getByText('Demo Basin').click()
    await expect(page.getByTestId('m11-basin-popup')).toContainText('Demo Basin')
    await expect(page.getByRole('link', { name: /进入分析/ })).toHaveAttribute('href', /\/basins\/basin-demo.*basinVersionId=bv-001/)
    await page.getByRole('link', { name: /产品监控摘要/ }).click()
    await expect(page).toHaveURL(/\/monitoring/)
    await page.goBack()
    await expect(page.getByRole('heading', { name: '全国总览' })).toBeVisible()
    await page.getByRole('link', { name: /洪水预警摘要/ }).click()
    await expect(page).toHaveURL(/\/flood-alerts/)
  })

  test('resolves best summary links to concrete source and omits compare context', async ({ page }) => {
    await mockOverviewApis(page, { runSource: 'ifs' })

    await page.goto('/overview?source=best')

    await expect(page.getByRole('heading', { name: '全国总览' })).toBeVisible()
    await expect(page.getByRole('link', { name: /产品监控摘要/ })).toHaveAttribute(
      'href',
      '/monitoring?source=ifs&cycle=2026-05-18T00%3A00%3A00.000Z&validTime=2026-05-18T06%3A00%3A00.000Z',
    )
    await expect(page.getByRole('link', { name: /洪水预警摘要/ })).toHaveAttribute(
      'href',
      '/flood-alerts?source=ifs&cycle=2026-05-18T00%3A00%3A00.000Z&validTime=2026-05-18T06%3A00%3A00.000Z',
    )

    await page.goto('/overview?source=compare&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z')
    await expect(page.getByText('GFS+IFS 对比暂不支持跨页保真，已省略具体源上下文').first()).toBeVisible()
    await expect(page.getByRole('link', { name: /产品监控摘要/ })).toHaveAttribute('href', '/monitoring')
    await expect(page.getByRole('link', { name: /洪水预警摘要/ })).toHaveAttribute('href', '/flood-alerts')
  })

  test('keeps timeline visible and side panels collapsible at 1280', async ({ page }) => {
    await mockOverviewApis(page)
    await page.setViewportSize({ width: 1280, height: 900 })

    await page.goto('/overview')

    await expect(page.getByTestId('m11-timeline')).toBeInViewport()
    await expect(page.getByRole('button', { name: '折叠左侧面板' })).toBeVisible()
    await expect(page.getByRole('button', { name: '折叠右侧面板' })).toBeVisible()
    await page.getByRole('button', { name: '折叠左侧面板' }).click()
    await expect(page.getByTestId('m11-shell')).toHaveAttribute('data-left-panel', 'collapsed')
    await page.getByRole('button', { name: '折叠右侧面板' }).click()
    await expect(page.getByTestId('m11-shell')).toHaveAttribute('data-right-panel', 'collapsed')
    await expect(page.getByTestId('m11-timeline')).toBeInViewport()
  })

  test('renders successful overview sections when an optional summary request fails', async ({ page }) => {
    await mockOverviewApis(page, { partialQueueFailure: true })

    await page.goto('/overview?source=gfs')

    await expect(page.getByRole('heading', { name: '全国总览' })).toBeVisible()
    await expect(page.getByLabel('全国流域树')).toContainText('Demo Basin')
    await expect(page.getByText('queue: 暂不可用').first()).toBeVisible()
    await expect(page.getByText('径流量图例')).toBeVisible()
  })

  test('renders basin drill-down shell with restored query state and segment discovery', async ({ page }) => {
    await mockOverviewApis(page, { runSource: 'ifs' })

    await page.goto(
      '/basins/basin-demo?source=ifs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&layer=flood-return-period&basemap=satellite&warningLevel=orange&q=main&basinVersionId=bv-001&segmentId=seg-009',
    )

    await expect(page.getByRole('heading', { name: '流域分析' })).toBeVisible()
    await expect(page.getByLabel('流域钻取地图')).toBeVisible()
    await expect(page.getByLabel('河段发现')).toContainText('Demo Basin')
    await expect(page.getByLabel('河段发现')).toContainText('bv-001')
    await expect(page.getByPlaceholder('搜索河段名称或 ID')).toHaveValue('main')
    await expect(page.getByLabel('预警筛选')).toHaveValue('orange')
    await expect(page.getByRole('listitem').filter({ hasText: 'Main Stem 009' })).toHaveAttribute('aria-current', 'true')
    await expect(page.getByTestId('m11-map-surface')).toHaveAttribute('data-basemap', 'satellite')
    await expect(page.getByTestId('m11-map-surface')).toHaveAttribute('data-selected-segment-id', 'seg-009')
    await expect(page).toHaveURL(/cycle=2026-05-18T00%3A00%3A00.000Z/)

    await page.getByPlaceholder('搜索河段名称或 ID').fill('north')
    await expect(page).toHaveURL(/q=north/)
    await page.getByLabel('预警筛选').selectOption('')
    await expect(page).not.toHaveURL(/warningLevel=orange/)
  })

  test('keeps forecast workflow route reachable', async ({ page }) => {
    await page.route('**/api/v1/**', (route) => route.abort())

    await page.goto('/forecast')

    await expect(page.getByText('NHMS')).toBeVisible()
    await expect(page.getByRole('link', { name: /水文预报/ })).toBeVisible()
  })

  test('renders the flood alerts workflow route', async ({ page }) => {
    await mockFloodWorkflowApis(page)

    await page.goto('/flood-alerts?warningLevel=major')

    await expect(page.getByText('NHMS')).toBeVisible()
    await expect(page.getByRole('link', { name: /洪水预警/ })).toBeVisible()
    await expect(page.getByRole('heading', { name: '洪水预警' })).toBeVisible()
    await expect(page.getByRole('heading', { name: '预警统计' })).toBeVisible()
    await expect(page.getByLabel('洪水预警地图')).toBeVisible()
    await expect(page.getByRole('heading', { name: '预报时刻' })).toBeVisible()
    await expect(page.getByRole('heading', { name: '风险排名' })).toBeVisible()
    await expect(page.getByRole('row', { name: /Flood Route Segment/ })).toBeVisible()
  })

  test('renders the monitoring workflow route through allowed RBAC', async ({ page }) => {
    await mockMonitoringWorkflowApis(page)

    await page.goto('/monitoring')
    await expect(page.getByText('权限不足')).toBeVisible()
    await selectOperatorRole(page)

    await expect(page.getByText('NHMS')).toBeVisible()
    await expect(page.getByRole('link', { name: /产品监控/ })).toBeVisible()
    await expect(page.getByRole('heading', { name: '监控工作台' })).toBeVisible()
    await expect(page.getByRole('heading', { name: '当前周期' })).toBeVisible()
    await expect(page.getByRole('heading', { name: '七阶段流水线' })).toBeVisible()
    await expect(page.getByRole('heading', { name: '作业列表' })).toBeVisible()
    await expect(page.getByRole('heading', { name: '趋势' })).toBeVisible()
    await expect(page.getByRole('cell', { name: 'run-route' })).toBeVisible()
  })
})

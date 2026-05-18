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
    await page.route('**/api/v1/**', (route) => route.abort())

    await page.goto('/')
    await expect(page.getByRole('heading', { name: '全国总览' })).toBeVisible()
    await expect(page.getByLabel('全国总览地图')).toBeVisible()
    await expect(page.getByRole('link', { name: /全国总览/ })).toBeVisible()

    await page.goto('/overview?source=gfs&layer=flood-return-period&basemap=terrain')
    await expect(page.getByRole('heading', { name: '全国总览' })).toBeVisible()
    await expect(page.getByLabel('全国总览地图').getByText('flood-return-period')).toBeVisible()
    await expect(page.getByLabel('全国总览地图').getByText('terrain')).toBeVisible()
  })

  test('renders basin drill-down shell with restored query state', async ({ page }) => {
    await page.route('**/api/v1/**', (route) => route.abort())

    await page.goto(
      '/basins/basin-demo?basinVersionId=bv-001&segmentId=seg-009&source=best&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&warningLevel=orange&q=main',
    )

    await expect(page.getByRole('heading', { name: '流域分析' })).toBeVisible()
    await expect(page.getByLabel('流域钻取地图')).toBeVisible()
    await expect(page.getByText('basin-demo', { exact: true })).toBeVisible()
    await expect(page.getByText('seg-009').first()).toBeVisible()
    await expect(page.getByText('orange').first()).toBeVisible()
    await expect(page).toHaveURL(/cycle=2026-05-18T00%3A00%3A00.000Z/)
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

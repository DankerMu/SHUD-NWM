import { expect, test, type Page, type Route } from '@playwright/test'

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

async function mockMinimalApis(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())

    if (url.pathname === '/api/v1/runs') {
      return fulfill(route, { items: [], total: 0, limit: 50, offset: 0 })
    }
    if (url.pathname === '/api/v1/pipeline/status') {
      return route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({
          status: 'error',
          error: { code: 'PIPELINE_CYCLE_NOT_FOUND', message: 'No cycle' },
        }),
      })
    }
    if (url.pathname === '/api/v1/pipeline/stages') {
      return route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({
          status: 'error',
          error: { code: 'PIPELINE_CYCLE_NOT_FOUND', message: 'No cycle' },
        }),
      })
    }
    if (url.pathname === '/api/v1/queue/depth') {
      return fulfill(route, { running: 0, pending: 0, idle: 0 })
    }
    if (url.pathname === '/api/v1/jobs') {
      return fulfill(route, { items: [], total: 0, limit: 12, offset: 0 })
    }
    if (url.pathname === '/api/v1/metrics/stage-duration') return fulfill(route, [])
    if (url.pathname === '/api/v1/metrics/success-rate') return fulfill(route, [])

    throw new Error(`Unhandled preview API route: ${url.pathname}`)
  })
}

async function mockMonitoringApis(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())

    if (url.pathname === '/api/v1/pipeline/status') {
      return fulfill(route, {
        source: 'GFS',
        cycle_time: '2026-05-09T00:00:00Z',
        current_state: 'forecast_running',
        started_at: '2026-05-09T00:00:30Z',
        updated_at: '2026-05-09T00:08:00Z',
        job_counts: { succeeded: 0, failed: 1, running: 1, pending: 0 },
      })
    }
    if (url.pathname === '/api/v1/pipeline/stages') return fulfill(route, [])
    if (url.pathname === '/api/v1/queue/depth') return fulfill(route, { running: 1, pending: 0, idle: 0 })
    if (url.pathname === '/api/v1/jobs') {
      return fulfill(route, {
        items: [
          {
            job_id: 'job-failed',
            run_id: 'run-failed',
            cycle_id: 'cycle-1',
            job_type: 'forecast',
            slurm_job_id: '1001',
            model_id: 'model-b',
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
          {
            job_id: 'job-running',
            run_id: 'run-running',
            cycle_id: 'cycle-1',
            job_type: 'forecast',
            slurm_job_id: '1002',
            model_id: 'model-c',
            status: 'running',
            stage: 'forecast',
            submitted_at: '2026-05-09T00:07:00Z',
            started_at: '2026-05-09T00:08:00Z',
            finished_at: null,
            exit_code: null,
            retry_count: 0,
            error_code: null,
            error_message: null,
            log_uri: null,
            duration_seconds: null,
          },
        ],
        total: 2,
        limit: 12,
        offset: 0,
      })
    }
    if (url.pathname === '/api/v1/metrics/stage-duration') return fulfill(route, [])
    if (url.pathname === '/api/v1/metrics/success-rate') return fulfill(route, [])

    throw new Error(`Unhandled preview monitoring API route: ${url.pathname}`)
  })
}

test.describe('production preview deep links', () => {
  test('loads /monitoring without local role selector', async ({ page }) => {
    await mockMinimalApis(page)
    await page.goto('/monitoring')

    await expect(page.getByText('NHMS')).toBeVisible()
    await expect(page.getByRole('heading', { name: '监控工作台' })).toBeVisible()
    await expect(page.getByLabel('Role')).toHaveCount(0)
  })

  test('does not expose retry or cancel actions for production configured operator role', async ({ page }) => {
    await mockMonitoringApis(page)
    await page.goto('/monitoring')

    await expect(page.getByRole('heading', { name: '监控工作台' })).toBeVisible()
    await expect(page.getByRole('cell', { name: 'run-failed' })).toBeVisible()
    await expect(page.getByRole('cell', { name: 'run-running' })).toBeVisible()
    await expect(page.getByRole('button', { name: /重试/ })).toHaveCount(0)
    await expect(page.getByRole('button', { name: /取消/ })).toHaveCount(0)
  })

  test('loads /flood-alerts without local role selector', async ({ page }) => {
    await mockMinimalApis(page)
    await page.goto('/flood-alerts')

    await expect(page.getByText('NHMS')).toBeVisible()
    await expect(page.getByRole('heading', { name: '暂无洪水预警数据' })).toBeVisible()
    await expect(page.getByLabel('Role')).toHaveCount(0)
  })
})

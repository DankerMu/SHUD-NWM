import { expect, test, type Page, type Route } from '@playwright/test'

const cycleTime = '2026-05-09T00:00:00Z'

const cycle = {
  source: 'GFS',
  cycle_time: cycleTime,
  current_state: 'partially_failed',
  started_at: '2026-05-09T00:00:30Z',
  updated_at: '2026-05-09T00:08:00Z',
  job_counts: { succeeded: 3, failed: 1, running: 1, pending: 2 },
}

const stages = [
  {
    stage: 'download',
    display_status: 'succeeded',
    status: 'succeeded',
    duration_seconds: 12,
    basin_progress: { completed: 4, total: 4, failed: 0 },
    basin_results: [],
  },
  {
    stage: 'forcing',
    display_status: 'partially_failed',
    status: 'partially_failed',
    duration_seconds: 35,
    basin_progress: { completed: 3, total: 4, failed: 1 },
    basin_results: [
      {
        model_id: 'model-b',
        basin_id: 'basin-2',
        status: 'failed',
        error_code: 'FORCING_MISSING',
        error_message: 'forcing input missing',
      },
    ],
  },
  {
    stage: 'forecast',
    display_status: 'running',
    status: 'running',
    duration_seconds: 88,
    basin_progress: { completed: 2, total: 4, failed: 0 },
    basin_results: [],
  },
]

const jobs = [
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
    log_uri: 's3://logs/job-failed.log',
    duration_seconds: 120,
  },
  {
    job_id: 'job-success',
    run_id: 'run-success',
    cycle_id: 'cycle-1',
    job_type: 'forecast',
    slurm_job_id: '1002',
    model_id: 'model-a',
    status: 'succeeded',
    stage: 'download',
    submitted_at: '2026-05-09T00:01:00Z',
    started_at: '2026-05-09T00:01:30Z',
    finished_at: '2026-05-09T00:02:00Z',
    exit_code: 0,
    retry_count: 0,
    error_code: null,
    error_message: null,
    log_uri: 's3://logs/job-success.log',
    duration_seconds: 30,
  },
]

const stageDurationMetrics = [
  { date: '2026-05-03', stage: 'download', average_duration_seconds: 11, job_count: 8 },
  { date: '2026-05-04', stage: 'download', average_duration_seconds: 14, job_count: 8 },
  { date: '2026-05-03', stage: 'forecast', average_duration_seconds: 80, job_count: 8 },
  { date: '2026-05-04', stage: 'forecast', average_duration_seconds: 86, job_count: 8 },
]

const successRateMetrics = [
  { date: '2026-05-03', success_rate: 0.9, succeeded_cycles: 9, total_cycles: 10 },
  { date: '2026-05-04', success_rate: 0.8, succeeded_cycles: 8, total_cycles: 10 },
]

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

async function mockMonitoringApi(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())

    if (url.pathname === '/api/v1/pipeline/status') return fulfill(route, cycle)
    if (url.pathname === '/api/v1/pipeline/stages') return fulfill(route, stages)
    if (url.pathname === '/api/v1/queue/depth') return fulfill(route, { running: 2, pending: 4, idle: 6 })
    if (url.pathname === '/api/v1/metrics/stage-duration') return fulfill(route, stageDurationMetrics)
    if (url.pathname === '/api/v1/metrics/success-rate') return fulfill(route, successRateMetrics)
    if (url.pathname === '/api/v1/jobs/job-failed/logs') {
      return fulfill(route, {
        job_id: 'job-failed',
        log_uri: 's3://logs/job-failed.log',
        content: 'forecast stderr: model failed',
      })
    }
    if (url.pathname === '/api/v1/runs/run-failed/retry' && request.method() === 'POST') {
      return fulfill(route, { job_id: 'job-failed-retry', run_id: 'run-failed', retry_count: 1, status: 'pending' })
    }
    if (url.pathname === '/api/v1/jobs') {
      const status = url.searchParams.get('status')
      const filteredJobs = status ? jobs.filter((job) => job.status === status) : jobs
      return fulfill(route, {
        items: filteredJobs,
        total: filteredJobs.length,
        limit: Number(url.searchParams.get('limit') ?? 12),
        offset: Number(url.searchParams.get('offset') ?? 0),
      })
    }

    throw new Error(`Unhandled mocked API route: ${request.method()} ${url.pathname}`)
  })
}

async function selectRole(page: Page, roleName: 'Viewer' | 'Operator' | 'Model Admin' | 'Sys Admin') {
  await page.getByLabel('Role').click()
  await page.getByRole('option', { name: roleName }).click()
}

async function openMonitoringAsOperator(page: Page) {
  await mockMonitoringApi(page)
  await page.goto('/monitoring')
  await selectRole(page, 'Operator')
  await expect(page.getByRole('heading', { name: '监控工作台' })).toBeVisible()
}

test.describe('monitoring page', () => {
  test('loads summary bar, stage cards, jobs table, and trend charts', async ({ page }) => {
    await openMonitoringAsOperator(page)

    await expect(page.getByRole('heading', { name: '当前周期' })).toBeVisible()
    await expect(page.getByRole('heading', { name: '七阶段流水线' })).toBeVisible()
    await expect(page.getByRole('heading', { name: '作业列表' })).toBeVisible()
    await expect(page.getByRole('heading', { name: '趋势' })).toBeVisible()
    await expect(page.getByRole('cell', { name: 'run-failed' })).toBeVisible()
    await expect(page.getByRole('cell', { name: 'run-success' })).toBeVisible()
  })

  test('expands a failed stage to show basin failures', async ({ page }) => {
    await openMonitoringAsOperator(page)

    await page.getByRole('button', { name: /强迫场.*partially_failed/ }).click()

    await expect(page.getByText('model-b')).toBeVisible()
    await expect(page.getByText('FORCING_MISSING')).toBeVisible()
    await expect(page.getByText('forcing input missing')).toBeVisible()
  })

  test('updates the jobs table when filters change', async ({ page }) => {
    await openMonitoringAsOperator(page)

    await page.getByLabel('Status filter').click()
    await page.getByRole('option', { name: 'succeeded' }).click()

    await expect(page.getByRole('cell', { name: 'run-success' })).toBeVisible()
    await expect(page.getByRole('cell', { name: 'run-failed' })).toHaveCount(0)
  })

  test('opens the job log modal and shows log content', async ({ page }) => {
    await openMonitoringAsOperator(page)

    const failedRow = page.getByRole('row', { name: /run-failed/ })
    await failedRow.getByRole('button', { name: /查看日志/ }).click()

    await expect(page.getByRole('dialog')).toContainText('作业日志 job-failed')
    await expect(page.getByRole('dialog')).toContainText('forecast stderr: model failed')
  })

  test('shows retry for operator and hides it when role becomes viewer', async ({ page }) => {
    await openMonitoringAsOperator(page)

    await expect(page.getByRole('row', { name: /run-failed/ }).getByRole('button', { name: /重试/ })).toBeVisible()

    await selectRole(page, 'Viewer')

    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.getByRole('button', { name: /重试/ })).toHaveCount(0)
  })

  test('denies monitoring access to viewer role', async ({ page }) => {
    await mockMonitoringApi(page)
    await page.goto('/monitoring')

    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.getByRole('heading', { name: '监控工作台' })).toHaveCount(0)
  })
})

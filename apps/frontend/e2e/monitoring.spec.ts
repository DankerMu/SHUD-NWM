import { expect, test, type Page, type Request, type Route } from '@playwright/test'

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
  {
    job_id: 'job-running',
    run_id: 'run-running',
    cycle_id: 'cycle-1',
    job_type: 'forecast',
    slurm_job_id: '1003',
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
    log_uri: 's3://logs/job-running.log',
    duration_seconds: null,
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

const controlledCycleTime = '2026-05-21T00:00:00.000Z'
const controlledCycleId = 'gfs_2026052100'
const controlledRunId = 'qhh_gfs_2026052100_controlled_failure'
const controlledFailedJobId = 'qhh_controlled_forecast_failed'
const controlledRetryJobId = `${controlledRunId}_retry_active`

interface MonitoringApiMockOptions {
  onRetryRequest?: (request: Request) => void
  onCancelRequest?: (request: Request) => void
  onApiRequest?: (request: Request) => void
}

function success<T>(data: T) {
  return { status: 'success', data }
}

function expectedFormattedDate(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

async function fulfill(route: Route, data: unknown) {
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(success(data)),
  })
}

async function mockMonitoringApi(page: Page, options: MonitoringApiMockOptions = {}) {
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    options.onApiRequest?.(request)
    const url = new URL(request.url())

    if (url.pathname === '/api/v1/pipeline/status') return fulfill(route, cycle)
    if (url.pathname === '/api/v1/pipeline/stages') return fulfill(route, stages)
    if (url.pathname === '/api/v1/queue/depth') return fulfill(route, { running: 2, pending: 4, idle: 6 })
    if (url.pathname === '/api/v1/metrics/stage-duration') {
      expect(url.searchParams.get('source')).toBe('GFS')
      return fulfill(route, stageDurationMetrics)
    }
    if (url.pathname === '/api/v1/metrics/success-rate') {
      expect(url.searchParams.get('source')).toBe('GFS')
      return fulfill(route, successRateMetrics)
    }
    if (url.pathname === '/api/v1/jobs/job-failed/logs') {
      return fulfill(route, {
        job_id: 'job-failed',
        log_uri: 's3://logs/job-failed.log',
        content: 'forecast stderr: model failed',
      })
    }
    if (url.pathname === '/api/v1/runs/run-failed/retry' && request.method() === 'POST') {
      options.onRetryRequest?.(request)
      return fulfill(route, { job_id: 'job-failed-retry', run_id: 'run-failed', retry_count: 1, status: 'pending' })
    }
    if (url.pathname === '/api/v1/runs/run-running/cancel' && request.method() === 'POST') {
      options.onCancelRequest?.(request)
      return fulfill(route, {
        run_id: 'run-running',
        cancelled_jobs: [{ ...jobs[2], status: 'cancelled' }],
        cancelled: [{ ...jobs[2], status: 'cancelled' }],
        failed_jobs: [],
        slurm_failures: [],
        partial_failure: false,
        idempotent_jobs: [],
        hydro_run: null,
        forecast_cycle: null,
      })
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
  await expect(page.getByLabel('Role')).toBeVisible()
  await page.getByLabel('Role').click({ force: true })
  await page.getByRole('option', { name: roleName }).click({ force: true })
}

async function openMonitoringAsOperator(page: Page, mockOptions?: MonitoringApiMockOptions) {
  await mockMonitoringApi(page, mockOptions)
  await page.goto('/monitoring')
  await expect(page.getByText('权限不足')).toBeVisible()
  await selectRole(page, 'Operator')
  await expect(page.getByRole('heading', { name: '监控工作台' })).toBeVisible()
}

function controlledStatus(retrySubmitted: boolean) {
  return {
    cycle_id: controlledCycleId,
    source: 'GFS',
    cycle_time: controlledCycleTime,
    current_state: 'failed_run',
    started_at: '2026-05-21T00:00:00Z',
    updated_at: '2026-05-21T00:36:00Z',
    job_counts: retrySubmitted
      ? { succeeded: 4, failed: 1, running: 0, pending: 0 }
      : { succeeded: 3, failed: 1, running: 0, pending: 0 },
  }
}

function controlledStages(retrySubmitted: boolean) {
  const successStages = [
    { stage: 'download', display_status: 'succeeded', status: 'succeeded', duration_seconds: 240 },
    { stage: 'convert', display_status: 'succeeded', status: 'succeeded', duration_seconds: 300 },
    { stage: 'forcing', display_status: 'succeeded', status: 'succeeded', duration_seconds: 360 },
  ].map((stage) => ({
    ...stage,
    basin_progress: { completed: 1, total: 1, failed: 0 },
    basin_results_limit: 50,
    basin_results_total: 1,
    basin_results_returned: 1,
    basin_results_truncated: false,
    basin_results: [],
  }))

  const forecastJob = retrySubmitted ? controlledRetryJob() : controlledFailedJob()
  return [
    ...successStages,
    {
      stage: 'forecast',
      display_status: retrySubmitted ? 'succeeded' : 'failed',
      status: retrySubmitted ? 'succeeded' : 'failed',
      duration_seconds: retrySubmitted ? 480 : 300,
      basin_progress: { completed: retrySubmitted ? 1 : 0, total: 1, failed: retrySubmitted ? 0 : 1 },
      basin_results_limit: 50,
      basin_results_total: 1,
      basin_results_returned: 1,
      basin_results_truncated: false,
      basin_results: [forecastJob],
    },
  ]
}

function controlledFailedJob() {
  return {
    job_id: controlledFailedJobId,
    run_id: controlledRunId,
    cycle_id: controlledCycleId,
    run_type: 'forecast',
    scenario: 'forecast_gfs_deterministic',
    job_type: 'forecast_qhh_stage',
    slurm_job_id: 'slurm_qhh_forecast_failed_2100',
    model_id: 'basins_qhh_shud',
    status: 'failed',
    stage: 'forecast',
    submitted_at: '2026-05-21T00:30:00Z',
    started_at: '2026-05-21T00:31:00Z',
    finished_at: '2026-05-21T00:36:00Z',
    exit_code: 1,
    retry_count: 1,
    error_code: 'NODE_FAILURE',
    error_message: 'Controlled QHH forecast failure for retry evidence.',
    log_uri: 'qhh/controlled/forecast_failed.log',
    duration_seconds: 300,
  }
}

function controlledRetryJob() {
  return {
    job_id: controlledRetryJobId,
    run_id: controlledRunId,
    cycle_id: controlledCycleId,
    run_type: 'forecast',
    scenario: 'forecast_gfs_deterministic',
    job_type: 'forecast_qhh_stage',
    slurm_job_id: 'slurm_retry',
    model_id: 'basins_qhh_shud',
    status: 'succeeded',
    stage: 'forecast',
    submitted_at: '2026-05-21T00:42:00Z',
    started_at: '2026-05-21T00:42:30Z',
    finished_at: '2026-05-21T00:50:00Z',
    exit_code: 0,
    retry_count: 2,
    error_code: null,
    error_message: null,
    log_uri: 'qhh/controlled/retry_succeeded.log',
    duration_seconds: 450,
  }
}

async function mockControlledOpsApi(page: Page, options: MonitoringApiMockOptions = {}) {
  let retrySubmitted = false
  const observedJobsQueries: Array<{ source: string | null; cycle: string | null }> = []

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    options.onApiRequest?.(request)
    const url = new URL(request.url())

    if (url.pathname === '/api/v1/pipeline/status') {
      expect(url.searchParams.get('source')).toBe('GFS')
      expect(url.searchParams.get('cycle_time')).toBe(controlledCycleTime)
      return fulfill(route, controlledStatus(retrySubmitted))
    }
    if (url.pathname === '/api/v1/pipeline/stages') {
      expect(url.searchParams.get('source')).toBe('GFS')
      expect(url.searchParams.get('cycle_time')).toBe(controlledCycleTime)
      return fulfill(route, controlledStages(retrySubmitted))
    }
    if (url.pathname === '/api/v1/queue/depth') return fulfill(route, { running: 0, pending: 0, idle: 2 })
    if (url.pathname === '/api/v1/metrics/stage-duration') return fulfill(route, stageDurationMetrics)
    if (url.pathname === '/api/v1/metrics/success-rate') return fulfill(route, successRateMetrics)
    if (url.pathname === `/api/v1/jobs/${controlledFailedJobId}/logs`) {
      return fulfill(route, {
        job_id: controlledFailedJobId,
        log_uri: 'qhh/controlled/forecast_failed.log',
        content: `controlled qhh forecast failure\nrun_id=${controlledRunId}\nstage=forecast\nreason=NODE_FAILURE`,
      })
    }
    if (url.pathname === `/api/v1/runs/${controlledRunId}/retry` && request.method() === 'POST') {
      options.onRetryRequest?.(request)
      retrySubmitted = true
      return fulfill(route, {
        job_id: controlledRetryJobId,
        pipeline_job_id: controlledRetryJobId,
        run_id: controlledRunId,
        retry_count: 2,
        status: 'submitted',
        execution_status: 'submitted',
        slurm_job_id: 'slurm_retry',
      })
    }
    if (url.pathname === '/api/v1/jobs') {
      observedJobsQueries.push({
        source: url.searchParams.get('source'),
        cycle: url.searchParams.get('cycle_time'),
      })
      const items = retrySubmitted
        ? [controlledRetryJob(), controlledFailedJob()]
        : [controlledFailedJob()]
      return fulfill(route, {
        items,
        total: items.length,
        limit: Number(url.searchParams.get('limit') ?? 12),
        offset: Number(url.searchParams.get('offset') ?? 0),
      })
    }

    throw new Error(`Unhandled controlled ops API route: ${request.method()} ${url.pathname}`)
  })

  return {
    observedJobsQueries,
    retrySubmitted: () => retrySubmitted,
  }
}

test.describe('monitoring page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/monitoring')
    await expect(
      page.getByLabel('Role'),
      'monitoring E2E requires the explicit dev/test role override; use the Playwright webServer or run the target server with VITE_ENABLE_ROLE_OVERRIDE=true',
    ).toBeVisible()
  })

  test('keeps viewer as the default role before local dev override is used', async ({ page }) => {
    await mockMonitoringApi(page)
    await page.goto('/monitoring')

    await expect(page.getByLabel('Role')).toHaveText('Viewer')
    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.getByRole('heading', { name: '监控工作台' })).toHaveCount(0)
  })

  test('loads summary bar, stage cards, jobs table, and trend charts', async ({ page }) => {
    await openMonitoringAsOperator(page)

    await expect(page.getByRole('heading', { name: '当前周期' })).toBeVisible()
    await expect(page.getByRole('heading', { name: '七阶段流水线' })).toBeVisible()
    await expect(page.getByRole('heading', { name: '作业列表' })).toBeVisible()
    await expect(page.getByRole('heading', { name: '趋势' })).toBeVisible()

    const summarySection = page.locator('section').filter({
      has: page.getByRole('heading', { name: '当前周期' }),
    }).first()
    await expect(summarySection).toContainText(cycle.source)
    await expect(summarySection).toContainText(expectedFormattedDate(cycleTime))
    await expect(summarySection).toContainText(/成功\s*3/)
    await expect(summarySection).toContainText(/失败\s*1/)
    await expect(summarySection).toContainText(/运行中\s*1/)
    await expect(summarySection).toContainText(/等待\s*2/)

    await expect(page.getByRole('button', { name: /下载.*succeeded/ })).toBeVisible()
    await expect(page.getByRole('cell', { name: 'run-failed' })).toBeVisible()
    await expect(page.getByRole('cell', { name: 'run-success' })).toBeVisible()
    await expect(page.getByRole('row', { name: /run-failed/ })).toContainText('model-b')
    await expect(page.getByRole('row', { name: /run-failed/ })).toContainText('failed')
  })

  test('expands a failed stage to show basin failures', async ({ page }) => {
    await openMonitoringAsOperator(page)

    const stageSection = page.locator('section, div').filter({
      has: page.getByRole('heading', { name: '七阶段流水线' }),
    }).first()

    await stageSection.getByRole('button', { name: /强迫场.*partially_failed/ }).click()

    const basinFailures = stageSection.locator('div').filter({
      hasText: 'FORCING_MISSING',
    }).last()
    await expect(basinFailures).toContainText('model-b')
    await expect(basinFailures).toContainText('FORCING_MISSING')
    await expect(basinFailures).toContainText('forcing input missing')
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

  test('shows retry for dev override operator and sends the role header', async ({ page }) => {
    const retryRequests: Array<{ method: string; pathname: string; role: string | null }> = []
    await openMonitoringAsOperator(page, {
      onRetryRequest: (request) => {
        retryRequests.push({
          method: request.method(),
          pathname: new URL(request.url()).pathname,
          role: request.headers()['x-user-role'] ?? null,
        })
      },
    })

    const retryButton = page.getByRole('row', { name: /run-failed/ }).getByRole('button', { name: /重试/ })
    await expect(retryButton).toBeVisible()
    await retryButton.click()

    await expect.poll(() => retryRequests).toEqual([
      { method: 'POST', pathname: '/api/v1/runs/run-failed/retry', role: 'operator' },
    ])
    await expect(page.getByRole('listitem').filter({ hasText: '重试已提交' })).toBeVisible()

    await selectRole(page, 'Viewer')

    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.getByRole('button', { name: /重试/ })).toHaveCount(0)
  })

  test('proves the /ops controlled failure log and retry lifecycle', async ({ page }) => {
    const retryRequests: Array<{ method: string; pathname: string; role: string | null }> = []
    const opsApi = await mockControlledOpsApi(page, {
      onRetryRequest: (request) => {
        retryRequests.push({
          method: request.method(),
          pathname: new URL(request.url()).pathname,
          role: request.headers()['x-user-role'] ?? null,
        })
      },
    })

    await page.goto(`/ops?source=gfs&cycle=${encodeURIComponent(controlledCycleTime)}`)
    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.getByRole('button', { name: /重试/ })).toHaveCount(0)

    await selectRole(page, 'Operator')
    await expect(page.getByRole('heading', { name: '运维工作台' })).toBeVisible()
    const currentStateField = page.getByText('Current State').locator('..')
    await expect(currentStateField).toContainText('failed_run')
    await expect(page.getByRole('cell', { name: controlledRunId })).toBeVisible()
    const failedRow = page.getByRole('row', { name: new RegExp(controlledFailedJobId) })
    await expect(failedRow).toContainText('forecast')
    await expect(failedRow).toContainText('failed')
    await expect(failedRow).toContainText('slurm_qhh_forecast_failed_2100')

    await failedRow.getByRole('button', { name: /查看日志/ }).click()
    await expect(page.getByRole('dialog')).toContainText(`作业日志 ${controlledFailedJobId}`)
    await expect(page.getByRole('dialog')).toContainText('controlled qhh forecast failure')
    await expect(page.getByRole('dialog')).toContainText(controlledRunId)
    await page.keyboard.press('Escape')
    await expect(page.getByRole('dialog')).toHaveCount(0)

    await failedRow.getByRole('button', { name: /重试/ }).click()
    await expect.poll(() => retryRequests).toEqual([
      { method: 'POST', pathname: `/api/v1/runs/${controlledRunId}/retry`, role: 'operator' },
    ])
    await expect.poll(() => opsApi.retrySubmitted()).toBe(true)
    await expect(page.getByRole('listitem').filter({ hasText: '重试已提交' })).toBeVisible()

    const retryRow = page.getByRole('row', { name: new RegExp(controlledRetryJobId) })
    await expect(retryRow).toBeVisible()
    await expect(retryRow).toContainText('succeeded')
    await expect(retryRow).toContainText('slurm_retry')
    await expect(retryRow).toContainText('2')
    await expect(currentStateField).toContainText('failed_run')
    await expect(page.getByRole('button', { name: /预报.*succeeded/ })).toBeVisible()
    await expect(page.getByText('qhh_sibling_cycle_forecast_failed')).toHaveCount(0)
    expect(opsApi.observedJobsQueries.length).toBeGreaterThan(0)
    for (const query of opsApi.observedJobsQueries) {
      expect(query).toEqual({ source: 'GFS', cycle: controlledCycleTime })
    }

    await selectRole(page, 'Viewer')
    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.getByRole('button', { name: /重试/ })).toHaveCount(0)
  })

  test('shows cancel for dev override operator and hides it when role becomes viewer', async ({ page }) => {
    const cancelRequests: Array<{ method: string; pathname: string; role: string | null }> = []
    await openMonitoringAsOperator(page, {
      onCancelRequest: (request) => {
        cancelRequests.push({
          method: request.method(),
          pathname: new URL(request.url()).pathname,
          role: request.headers()['x-user-role'] ?? null,
        })
      },
    })

    const cancelButton = page.getByRole('row', { name: /run-running/ }).getByRole('button', { name: /取消/ })
    await expect(cancelButton).toBeVisible()
    await cancelButton.click()

    await expect.poll(() => cancelRequests).toEqual([
      { method: 'POST', pathname: '/api/v1/runs/run-running/cancel', role: 'operator' },
    ])
    await expect(page.getByRole('listitem').filter({ hasText: '取消请求已提交' })).toBeVisible()

    await selectRole(page, 'Viewer')

    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.getByRole('button', { name: /取消/ })).toHaveCount(0)
  })

  test('uses the configured API base for monitoring reads and operator actions', async ({ page }) => {
    const origins: Array<{ origin: string; pathname: string; method: string }> = []
    await openMonitoringAsOperator(page, {
      onApiRequest: (request) => {
        const url = new URL(request.url())
        origins.push({ origin: url.origin, pathname: url.pathname, method: request.method() })
      },
    })

    await expect.poll(() => origins.map((call) => call.pathname)).toContain('/api/v1/metrics/stage-duration')
    await expect.poll(() => origins.map((call) => call.pathname)).toContain('/api/v1/metrics/success-rate')
    await page.getByRole('row', { name: /run-failed/ }).getByRole('button', { name: /重试/ }).click()
    await page.getByRole('row', { name: /run-running/ }).getByRole('button', { name: /取消/ }).click()

    const expectedPaths = new Set([
      '/api/v1/pipeline/status',
      '/api/v1/pipeline/stages',
      '/api/v1/queue/depth',
      '/api/v1/metrics/stage-duration',
      '/api/v1/metrics/success-rate',
      '/api/v1/jobs',
      '/api/v1/runs/run-failed/retry',
      '/api/v1/runs/run-running/cancel',
    ])
    for (const path of expectedPaths) {
      expect(origins.some((call) => call.origin === 'https://api.example.test' && call.pathname === path)).toBe(true)
    }
  })

  test('denies monitoring access to viewer role', async ({ page }) => {
    await mockMonitoringApi(page)
    await page.goto('/monitoring')

    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.getByRole('heading', { name: '监控工作台' })).toHaveCount(0)
  })

  test('serves monitoring deep link through the SPA fallback', async ({ page }) => {
    await mockMonitoringApi(page)
    await page.goto('/monitoring')

    await expect(page.getByText('NHMS')).toBeVisible()
    await expect(page.getByText('权限不足')).toBeVisible()
  })
})

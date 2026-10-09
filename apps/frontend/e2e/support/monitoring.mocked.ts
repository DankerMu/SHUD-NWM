import { expect, type Page, type Request, type Route } from '@playwright/test'

/**
 * 运维页（`/monitoring`、`/ops`）与模型资产页的 API mock（openspec mobile-responsive-display task 6.1 口径 (8)）。
 * 原样搬自 `e2e/monitoring.mocked.spec.ts`，供该 spec 与运维页兜底的移动 / 桌面 spec 共用；
 * 6.2 的模型资产移动 spec 在 `mockModelAssetsApi` 上扩展。
 * 文件名带 `mocked` token：这里有覆盖全部 api v1 路径的宽路由。未列出的路径直接抛错，不兜底。
 */

export const cycleTime = '2026-05-09T00:00:00Z'

export const cycle = {
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

export const jobs = [
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

export const controlledCycleTime = '2026-05-21T00:00:00.000Z'
export const controlledCycleId = 'gfs_2026052100'
export const controlledRunId = 'qhh_gfs_2026052100_controlled_failure'
export const controlledFailedJobId = 'qhh_controlled_forecast_failed'
export const controlledRetryJobId = `${controlledRunId}_retry_active`

export interface MonitoringApiMockOptions {
  /** 日志接口返回的正文；缺省为各 mock 原有的短日志（既有断言依赖缺省值）。 */
  logContent?: string
  onRetryRequest?: (request: Request) => void
  onCancelRequest?: (request: Request) => void
  onApiRequest?: (request: Request) => void
}

export function success<T>(data: T) {
  return { status: 'success', data }
}

export function expectedFormattedDate(value: string) {
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

export async function fulfill(route: Route, data: unknown) {
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(success(data)),
  })
}

// AppShell（去 NavBar 后）在每个路由挂载时全局加载一次 runtime config。
// compute_control 角色：控制变更/Slurm 路由可用，queue depth 正常（非 display_readonly）。
export const runtimeConfig = {
  service_role: 'compute_control' as const,
  control_mutations_enabled: true,
  slurm_routes_enabled: true,
  queue_depth_mode: 'slurm_gateway' as const,
  display_readonly: false,
}

export async function mockMonitoringApi(page: Page, options: MonitoringApiMockOptions = {}) {
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    options.onApiRequest?.(request)
    const url = new URL(request.url())

    if (url.pathname === '/api/v1/runtime/config') return fulfill(route, runtimeConfig)
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
        content: options.logContent ?? 'forecast stderr: model failed',
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

export async function mockControlledOpsApi(page: Page, options: MonitoringApiMockOptions = {}) {
  let retrySubmitted = false
  const observedJobsQueries: Array<{ source: string | null; cycle: string | null }> = []

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    options.onApiRequest?.(request)
    const url = new URL(request.url())

    if (url.pathname === '/api/v1/runtime/config') return fulfill(route, runtimeConfig)
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
        content: options.logContent ?? `controlled qhh forecast failure\nrun_id=${controlledRunId}\nstage=forecast\nreason=NODE_FAILURE`,
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

export function modelAssetDetailPayload() {
  return {
    model_id: 'basins_basin_a_shud',
    model_name: 'alias-a',
    basin_id: 'basins_basin_a',
    basin_name: 'Basin A',
    basin_version_id: 'basins_basin_a_vbasins',
    river_network_version_id: 'basins_basin_a_rivnet_vbasins',
    mesh_version_id: 'basins_basin_a_mesh_vbasins',
    calibration_version_id: 'basins_basin_a_shud_calib_vbasins',
    segment_count: 42,
    mesh_uri: 's3://nhms/models/basins_basin_a_shud/vbasins/package/alias-a.sp.mesh',
    mesh_checksum: 'mesh-sha-1',
    shud_code_version: 'basins-shud',
    active_flag: false,
    lifecycle_state: 'inactive',
    model_package_uri: 's3://nhms/models/basins_basin_a_shud/vbasins/package/',
    package_checksum: 'package-sha-1',
    manifest_uri: 's3://nhms/models/basins_basin_a_shud/vbasins/manifest.json',
    source_inventory_checksum: 'inventory-sha-1',
    basin_slug: 'basin-a',
    shud_input_name: 'alias-a',
    source_path: 's3://nhms/sources/basin-a',
    resolved_source_path: 'https://assets.example.test/basin-a',
    source_uri: 's3://nhms/sources/basin-a',
    source_is_symlink: false,
    resource_profile: {
      basin_slug: 'basin-a',
      shud_input_name: 'alias-a',
      manifest_uri: 's3://nhms/models/basins_basin_a_shud/vbasins/manifest.json',
      package_checksum: 'package-sha-1',
      source_inventory_checksum: 'inventory-sha-1',
      segment_count: 42,
      mesh: {
        uri: 's3://nhms/models/basins_basin_a_shud/vbasins/package/alias-a.sp.mesh',
        checksum: 'mesh-sha-1',
      },
      source_lineage: {
        source_path: 's3://nhms/sources/basin-a',
        source_uri: 's3://nhms/sources/basin-a',
      },
    },
    created_at: '2026-05-14T00:00:00Z',
  }
}

export async function mockModelAssetsApi(page: Page) {
  const model = modelAssetDetailPayload()
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/v1/runtime/config') return fulfill(route, runtimeConfig)
    if (url.pathname === '/api/v1/models' && route.request().method() === 'GET') {
      return fulfill(route, { items: [model], total: 1, limit: 50, offset: 0 })
    }
    if (url.pathname === `/api/v1/models/${model.model_id}` && route.request().method() === 'GET') {
      return fulfill(route, model)
    }
    throw new Error(`Unhandled mocked API route: ${route.request().method()} ${url.pathname}`)
  })
}

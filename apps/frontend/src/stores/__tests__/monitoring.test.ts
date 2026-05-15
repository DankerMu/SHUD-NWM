import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import type { PipelineCycle, PipelineJob, PipelineJobPage, PipelineStage, QueueState } from '@/stores/monitoring'
import { useMonitoringStore } from '@/stores/monitoring'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
}))

const TEST_CYCLE_TIME = '2026-05-09T00:00:00Z'

const cycle: PipelineCycle = {
  cycle_id: 'cycle-1',
  source: 'GFS',
  cycle_time: TEST_CYCLE_TIME,
  current_state: 'running',
  started_at: '2026-05-09T00:01:00Z',
  updated_at: '2026-05-09T00:10:00Z',
  job_counts: { succeeded: 2, failed: 1, running: 1, pending: 0 },
}

const stages: PipelineStage[] = [
  makeStage('download', 'succeeded', 10, 4, 4),
  makeStage('forcing', 'partially_failed', 25, 4, 3, 1),
  makeStage('forecast', 'running', 42, 4, 2),
]

const queue: QueueState = { running: 2, pending: 3, idle: 5 }

function makeStage(
  stage: string,
  status: PipelineStage['display_status'],
  durationSeconds: number,
  total: number,
  completed: number,
  failed = 0,
): PipelineStage {
  return {
    stage,
    display_status: status,
    status,
    duration_seconds: durationSeconds,
    basin_progress: { completed, total, failed },
    basin_results: [],
  }
}

function makeJob(overrides: Partial<PipelineJob> = {}): PipelineJob {
  return {
    job_id: 'job-1',
    run_id: 'forecast-gfs-run-1',
    cycle_id: 'cycle-1',
    job_type: 'forecast',
    slurm_job_id: '123',
    model_id: 'model-a',
    status: 'failed',
    stage: 'forecast',
    submitted_at: '2026-05-09T00:05:00Z',
    started_at: '2026-05-09T00:06:00Z',
    finished_at: '2026-05-09T00:12:00Z',
    exit_code: 1,
    retry_count: 0,
    error_code: 'E_MODEL',
    error_message: 'Model failed',
    log_uri: 's3://logs/job-1.log',
    duration_seconds: 360,
    ...overrides,
  }
}

function success<T>(data: T) {
  return { data: { status: 'success', data }, error: undefined }
}

function failure(message: string) {
  return { data: undefined, error: { error: { message } } }
}

function resetStore() {
  useMonitoringStore.setState(
    {
      ...useMonitoringStore.getInitialState(),
      cycleTime: TEST_CYCLE_TIME,
    },
    true,
  )
}

describe('useMonitoringStore', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    resetStore()
  })

  it('fetchAll updates cycle, stages, and queue state', async () => {
    let statusQuery: Record<string, unknown> | undefined
    let stagesQuery: Record<string, unknown> | undefined

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } } | undefined
      if (path === '/api/v1/pipeline/status') {
        statusQuery = options?.params?.query
        return success(cycle) as never
      }
      if (path === '/api/v1/pipeline/stages') {
        stagesQuery = options?.params?.query
        return success(stages) as never
      }
      if (path === '/api/v1/queue/depth') return success(queue) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    await useMonitoringStore.getState().fetchAll()

    const state = useMonitoringStore.getState()
    expect(state.cycle).toEqual(cycle)
    expect(state.stages).toHaveLength(3)
    expect(state.queue).toEqual(queue)
    expect(state.isPolling).toBe(false)
    expect(state.error).toBeNull()
    expect(statusQuery).toMatchObject({ source: 'GFS', cycle_time: TEST_CYCLE_TIME })
    expect(stagesQuery).toMatchObject({ source: 'GFS', cycle_time: TEST_CYCLE_TIME })
  })

  it('routes monitoring status, stages, jobs, and trends through the OpenAPI client', async () => {
    const calls: Array<{ path: string; query?: Record<string, unknown> }> = []

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } } | undefined
      calls.push({ path, query: options?.params?.query })
      if (path === '/api/v1/pipeline/status') return success(cycle) as never
      if (path === '/api/v1/pipeline/stages') return success(stages) as never
      if (path === '/api/v1/queue/depth') return success(queue) as never
      if (path === '/api/v1/jobs') {
        return success({ items: [makeJob()], total: 1, limit: 12, offset: 0 }) as never
      }
      if (path === '/api/v1/metrics/stage-duration') return success([]) as never
      if (path === '/api/v1/metrics/success-rate') return success([]) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    await useMonitoringStore.getState().fetchAll()
    await useMonitoringStore.getState().fetchJobs()
    await client.GET('/api/v1/metrics/stage-duration', { params: { query: { source: 'GFS', days: 30 } } })
    await client.GET('/api/v1/metrics/success-rate', { params: { query: { source: 'GFS', days: 30 } } })

    expect(calls.map((call) => call.path)).toEqual([
      '/api/v1/pipeline/status',
      '/api/v1/pipeline/stages',
      '/api/v1/queue/depth',
      '/api/v1/jobs',
      '/api/v1/metrics/stage-duration',
      '/api/v1/metrics/success-rate',
    ])
    expect(calls[3].query).toMatchObject({ source: 'GFS', cycle_time: TEST_CYCLE_TIME })
  })

  it('fetchJobs sends filters and pagination and stores normalized jobs', async () => {
    const jobPage: PipelineJobPage = {
      items: [makeJob({ run_id: 'analysis-run-1', job_type: 'analysis' })],
      total: 24,
      limit: 5,
      offset: 5,
    }
    let query: Record<string, unknown> | undefined

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } }
      if (path !== '/api/v1/jobs') throw new Error(`Unexpected GET ${path}`)
      query = options.params?.query
      return success(jobPage) as never
    })

    await useMonitoringStore.getState().fetchJobs({
      status: 'failed',
      runType: 'analysis',
      scenario: 'analysis_true_field',
      sortBy: 'duration_seconds',
      sortOrder: 'asc',
      page: 2,
      pageSize: 5,
    })

    expect(query).toMatchObject({
      source: 'GFS',
      cycle_time: TEST_CYCLE_TIME,
      status: 'failed',
      run_type: 'analysis',
      scenario: 'analysis_true_field',
      sort_by: 'duration_seconds',
      sort_order: 'asc',
      limit: 5,
      offset: 5,
    })
    expect(useMonitoringStore.getState().jobs).toMatchObject([
      { run_type: 'analysis', scenario: 'analysis_true_field' },
    ])
    expect(useMonitoringStore.getState().jobTotal).toBe(24)
  })

  it('sets error state when an API call fails', async () => {
    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      if (path === '/api/v1/pipeline/status') return failure('backend unavailable') as never
      if (path === '/api/v1/pipeline/stages') return success(stages) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    await expect(useMonitoringStore.getState().fetchAll()).rejects.toThrow('backend unavailable')

    const state = useMonitoringStore.getState()
    expect(state.error).toBe('backend unavailable')
    expect(state.isPolling).toBe(false)
  })
})

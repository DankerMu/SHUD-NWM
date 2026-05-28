import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import type { PipelineCycle, PipelineJob, PipelineJobPage, PipelineStage, QueueState, RuntimeConfig } from '@/stores/monitoring'
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

const displayRuntimeConfig: RuntimeConfig = {
  service_role: 'display_readonly',
  control_mutations_enabled: false,
  slurm_routes_enabled: false,
  queue_depth_mode: 'display_readonly_unavailable',
  display_readonly: true,
}

const driftedDisplayRuntimeConfig: RuntimeConfig = {
  service_role: 'display_readonly',
  control_mutations_enabled: true,
  slurm_routes_enabled: true,
  queue_depth_mode: 'slurm_gateway',
  display_readonly: false,
}

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
    basin_results_limit: 50,
    basin_results_total: 0,
    basin_results_returned: 0,
    basin_results_truncated: false,
    basin_results: [],
  }
}

function makeBasinResult(
  overrides: Partial<PipelineStage['basin_results'][number]> = {},
): PipelineStage['basin_results'][number] {
  return {
    job_id: 'basin-job-1',
    run_id: 'run-selected',
    cycle_id: 'cycle-1',
    job_type: 'forecast',
    slurm_job_id: '123',
    model_id: 'model-selected',
    basin_id: 'qhh-001',
    status: 'failed',
    stage: 'forecast',
    submitted_at: '2026-05-09T00:05:00Z',
    started_at: '2026-05-09T00:06:00Z',
    finished_at: '2026-05-09T00:12:00Z',
    duration_seconds: 360,
    retry_count: 0,
    error_code: 'E_MODEL',
    error_message: 'Model failed',
    log_uri: 's3://logs/basin-job-1.log',
    ...overrides,
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

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve
    reject = promiseReject
  })
  return { promise, resolve, reject }
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
    expect(state.cycleContext).toEqual({ source: 'GFS', cycleTime: TEST_CYCLE_TIME })
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
    expect(useMonitoringStore.getState().jobsContext).toEqual({ source: 'GFS', cycleTime: TEST_CYCLE_TIME })
    expect(useMonitoringStore.getState().jobTotal).toBe(24)
  })

  it('fetches status, stages, and jobs for the selected source/cycle context', async () => {
    const calls: Array<Record<string, unknown> | undefined> = []
    useMonitoringStore.setState({
      source: 'IFS',
      cycleTime: '2026-05-18T00:00:00.000Z',
    })

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } } | undefined
      calls.push(options?.params?.query)
      if (path === '/api/v1/pipeline/status') return success({ ...cycle, source: 'IFS', cycle_time: '2026-05-18T00:00:00.000Z' }) as never
      if (path === '/api/v1/pipeline/stages') return success(stages) as never
      if (path === '/api/v1/queue/depth') return success(queue) as never
      if (path === '/api/v1/jobs') return success({ items: [makeJob({ run_id: 'ifs-run-1' })], total: 1, limit: 12, offset: 0 }) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    await useMonitoringStore.getState().fetchAll()
    await useMonitoringStore.getState().fetchJobs()

    expect(calls).toEqual([
      { source: 'IFS', cycle_time: '2026-05-18T00:00:00.000Z' },
      { source: 'IFS', cycle_time: '2026-05-18T00:00:00.000Z' },
      undefined,
      expect.objectContaining({ source: 'IFS', cycle_time: '2026-05-18T00:00:00.000Z' }),
    ])
    expect(useMonitoringStore.getState().jobs).toMatchObject([{ run_id: 'ifs-run-1' }])
    expect(useMonitoringStore.getState().cycleContext).toEqual({
      source: 'IFS',
      cycleTime: '2026-05-18T00:00:00.000Z',
    })
    expect(useMonitoringStore.getState().jobsContext).toEqual({
      source: 'IFS',
      cycleTime: '2026-05-18T00:00:00.000Z',
    })
  })

  it('clears selected payload context with selected rows', () => {
    useMonitoringStore.setState({
      cycle,
      cycleContext: { source: 'GFS', cycleTime: TEST_CYCLE_TIME },
      stages,
      jobs: [makeJob({ run_id: 'old-cycle-run' })],
      jobsContext: { source: 'GFS', cycleTime: TEST_CYCLE_TIME },
      jobTotal: 1,
    })

    useMonitoringStore.getState().clearSelectedContext()

    const state = useMonitoringStore.getState()
    expect(state.cycle).toBeNull()
    expect(state.cycleContext).toBeNull()
    expect(state.stages).toEqual([])
    expect(state.jobs).toEqual([])
    expect(state.jobsContext).toBeNull()
    expect(state.jobTotal).toBe(0)
  })

  it('ignores stale pipeline success and failure after source/cycle changes', async () => {
    const oldStatus = deferred<unknown>()
    const oldStages = deferred<unknown>()
    const oldQueue = deferred<unknown>()
    const newStatus = deferred<unknown>()
    const newStages = deferred<unknown>()
    const newQueue = deferred<unknown>()
    const calls: Array<Record<string, unknown> | undefined> = []

    vi.mocked(client.GET).mockImplementation((...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } } | undefined
      calls.push(options?.params?.query)
      const source = options?.params?.query?.source
      if (path === '/api/v1/pipeline/status') {
        return (source === 'GFS' ? oldStatus.promise : newStatus.promise) as never
      }
      if (path === '/api/v1/pipeline/stages') {
        return (source === 'GFS' ? oldStages.promise : newStages.promise) as never
      }
      if (path === '/api/v1/queue/depth') {
        return (source === 'GFS' ? oldQueue.promise : newQueue.promise) as never
      }
      throw new Error(`Unexpected GET ${path}`)
    })

    const oldFetch = useMonitoringStore.getState().fetchAll({ clearOnFailure: true })
    useMonitoringStore.getState().setSource('IFS')
    useMonitoringStore.getState().setCycleTime('2026-05-18T00:00:00Z')
    const newFetch = useMonitoringStore.getState().fetchAll({ clearOnFailure: true })

    newStatus.resolve(success({ ...cycle, source: 'IFS', cycle_time: '2026-05-18T00:00:00.000Z' }))
    newStages.resolve(success([makeStage('forecast', 'succeeded', 20, 1, 1)]))
    newQueue.resolve(success(queue))
    await newFetch

    oldStatus.resolve(success({ ...cycle, cycle_id: 'old-gfs-cycle' }))
    oldStages.resolve(success([makeStage('download', 'failed', 99, 1, 0, 1)]))
    oldQueue.resolve(success(queue))
    await oldFetch

    const state = useMonitoringStore.getState()
    expect(state.source).toBe('IFS')
    expect(state.cycle).toMatchObject({ source: 'IFS', cycle_time: '2026-05-18T00:00:00.000Z' })
    expect(state.stages).toMatchObject([{ stage: 'forecast', display_status: 'succeeded' }])
    expect(state.stages).not.toMatchObject([{ stage: 'download', display_status: 'failed' }])

    const staleStatusFailure = deferred<unknown>()
    const staleStagesFailure = deferred<unknown>()
    const staleQueueFailure = deferred<unknown>()
    vi.mocked(client.GET).mockImplementation((...args: unknown[]) => {
      const path = String(args[0])
      if (path === '/api/v1/pipeline/status') return staleStatusFailure.promise as never
      if (path === '/api/v1/pipeline/stages') return staleStagesFailure.promise as never
      if (path === '/api/v1/queue/depth') return staleQueueFailure.promise as never
      throw new Error(`Unexpected GET ${path}`)
    })

    useMonitoringStore.getState().setSource('IFS')
    const staleFailure = useMonitoringStore.getState().fetchAll({ clearOnFailure: true })
    useMonitoringStore.getState().setSource('GFS')
    staleStatusFailure.reject(new Error('stale pipeline failed'))
    staleStagesFailure.resolve(success(stages))
    staleQueueFailure.resolve(success(queue))
    await staleFailure

    expect(useMonitoringStore.getState().cycle).toMatchObject({ source: 'IFS' })
    expect(useMonitoringStore.getState().source).toBe('GFS')
    expect(calls).toEqual(expect.arrayContaining([
      expect.objectContaining({ source: 'GFS', cycle_time: TEST_CYCLE_TIME }),
      expect.objectContaining({ source: 'IFS', cycle_time: '2026-05-18T00:00:00Z' }),
    ]))
  })

  it('ignores stale jobs success and failure after source/cycle changes', async () => {
    const oldJobs = deferred<unknown>()
    const newJobs = deferred<unknown>()

    vi.mocked(client.GET).mockImplementation((...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } } | undefined
      if (path !== '/api/v1/jobs') throw new Error(`Unexpected GET ${path}`)
      return (options?.params?.query?.source === 'GFS' ? oldJobs.promise : newJobs.promise) as never
    })

    const oldFetch = useMonitoringStore.getState().fetchJobs(undefined, { clearOnFailure: true })
    useMonitoringStore.getState().setSource('IFS')
    useMonitoringStore.getState().setCycleTime('2026-05-18T00:00:00Z')
    const newFetch = useMonitoringStore.getState().fetchJobs(undefined, { clearOnFailure: true })

    newJobs.resolve(success({ items: [makeJob({ run_id: 'ifs-run-1' })], total: 1, limit: 12, offset: 0 }))
    await newFetch

    oldJobs.resolve(success({ items: [makeJob({ run_id: 'old-gfs-run' })], total: 1, limit: 12, offset: 0 }))
    await oldFetch

    expect(useMonitoringStore.getState().jobs).toMatchObject([{ run_id: 'ifs-run-1' }])

    const staleJobsFailure = deferred<unknown>()
    vi.mocked(client.GET).mockImplementation((...args: unknown[]) => {
      const path = String(args[0])
      if (path !== '/api/v1/jobs') throw new Error(`Unexpected GET ${path}`)
      return staleJobsFailure.promise as never
    })

    useMonitoringStore.getState().setSource('IFS')
    const staleFailure = useMonitoringStore.getState().fetchJobs(undefined, { clearOnFailure: true })
    useMonitoringStore.getState().setSource('GFS')
    staleJobsFailure.reject(new Error('stale jobs failed'))
    await staleFailure

    expect(useMonitoringStore.getState().jobs).toMatchObject([{ run_id: 'ifs-run-1' }])
    expect(useMonitoringStore.getState().jobsError).toBeNull()
  })

  it('preserves last-known pipeline rows on legacy monitoring refresh failure', async () => {
    useMonitoringStore.setState({
      cycle,
      cycleContext: { source: 'GFS', cycleTime: TEST_CYCLE_TIME },
      stages,
      jobs: [makeJob({ run_id: 'old-cycle-run' })],
      jobsContext: { source: 'GFS', cycleTime: TEST_CYCLE_TIME },
      jobTotal: 1,
    })

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      if (path === '/api/v1/pipeline/status') return failure('backend unavailable') as never
      if (path === '/api/v1/pipeline/stages') return success(stages) as never
      if (path === '/api/v1/queue/depth') return success(queue) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    await expect(useMonitoringStore.getState().fetchAll()).rejects.toThrow('backend unavailable')

    const state = useMonitoringStore.getState()
    expect(state.error).toBe('backend unavailable')
    expect(state.operationalError).toBe('backend unavailable')
    expect(state.cycle).toEqual(cycle)
    expect(state.cycleContext).toEqual({ source: 'GFS', cycleTime: TEST_CYCLE_TIME })
    expect(state.stages).toEqual(stages)
    expect(state.jobs).toMatchObject([{ run_id: 'old-cycle-run' }])
    expect(state.jobsContext).toEqual({ source: 'GFS', cycleTime: TEST_CYCLE_TIME })
    expect(state.jobTotal).toBe(1)
    expect(state.isPolling).toBe(false)
  })

  it('clears selected context on ops pipeline refresh failure', async () => {
    useMonitoringStore.setState({
      cycle,
      cycleContext: { source: 'GFS', cycleTime: TEST_CYCLE_TIME },
      stages,
      jobs: [makeJob({ run_id: 'old-cycle-run' })],
      jobsContext: { source: 'GFS', cycleTime: TEST_CYCLE_TIME },
      jobTotal: 1,
    })

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      if (path === '/api/v1/pipeline/status') return failure('backend unavailable') as never
      if (path === '/api/v1/pipeline/stages') return success(stages) as never
      if (path === '/api/v1/queue/depth') return success(queue) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    await expect(useMonitoringStore.getState().fetchAll({ clearOnFailure: true })).rejects.toThrow('backend unavailable')

    const state = useMonitoringStore.getState()
    expect(state.error).toBe('backend unavailable')
    expect(state.cycle).toBeNull()
    expect(state.cycleContext).toBeNull()
    expect(state.stages).toEqual([])
    expect(state.jobs).toEqual([])
    expect(state.jobsContext).toBeNull()
    expect(state.jobTotal).toBe(0)
    expect(state.isPolling).toBe(false)
  })

  it('preserves pipeline refresh errors when jobs refresh succeeds', async () => {
    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      if (path === '/api/v1/pipeline/status') return failure('pipeline unavailable') as never
      if (path === '/api/v1/pipeline/stages') return success(stages) as never
      if (path === '/api/v1/jobs') return success({ items: [], total: 0, limit: 12, offset: 0 }) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    await expect(useMonitoringStore.getState().fetchAll()).rejects.toThrow('pipeline unavailable')
    await useMonitoringStore.getState().fetchJobs()

    const state = useMonitoringStore.getState()
    expect(state.operationalError).toBe('pipeline unavailable')
    expect(state.jobsError).toBeNull()
    expect(state.error).toBe('pipeline unavailable')
    expect(state.jobs).toEqual([])
  })

  it('preserves last-known jobs on legacy monitoring jobs fetch failure', async () => {
    useMonitoringStore.setState({
      jobs: [makeJob({ run_id: 'old-cycle-run' })],
      jobsContext: { source: 'GFS', cycleTime: TEST_CYCLE_TIME },
      jobTotal: 1,
    })
    vi.mocked(client.GET).mockResolvedValue(failure('jobs unsupported for selected context') as never)

    await expect(useMonitoringStore.getState().fetchJobs()).rejects.toThrow('jobs unsupported for selected context')

    const state = useMonitoringStore.getState()
    expect(state.jobsError).toBe('jobs unsupported for selected context')
    expect(state.jobs).toMatchObject([{ run_id: 'old-cycle-run' }])
    expect(state.jobsContext).toEqual({ source: 'GFS', cycleTime: TEST_CYCLE_TIME })
    expect(state.jobTotal).toBe(1)
  })

  it('clears stale jobs on ops jobs fetch failure', async () => {
    useMonitoringStore.setState({
      jobs: [makeJob({ run_id: 'old-cycle-run' })],
      jobsContext: { source: 'GFS', cycleTime: TEST_CYCLE_TIME },
      jobTotal: 1,
    })
    vi.mocked(client.GET).mockResolvedValue(failure('jobs unsupported for selected context') as never)

    await expect(useMonitoringStore.getState().fetchJobs(undefined, { clearOnFailure: true })).rejects.toThrow('jobs unsupported for selected context')

    const state = useMonitoringStore.getState()
    expect(state.jobsError).toBe('jobs unsupported for selected context')
    expect(state.jobs).toEqual([])
    expect(state.jobsContext).toBeNull()
    expect(state.jobTotal).toBe(0)
  })

  it('sends complete strict identity for status, stages, and jobs requests', async () => {
    const calls: Array<{ path: string; query?: Record<string, unknown> }> = []
    useMonitoringStore.setState({
      source: 'GFS',
      cycleTime: TEST_CYCLE_TIME,
      strictIdentity: {
        source: 'GFS',
        cycleTime: TEST_CYCLE_TIME,
        runId: 'run-strict',
        modelId: 'model-strict',
      },
    })

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const options = args[1] as { params?: { query?: Record<string, unknown> } } | undefined
      calls.push({ path, query: options?.params?.query })
      if (path === '/api/v1/pipeline/status') return success({ ...cycle, source: 'GFS', cycle_time: TEST_CYCLE_TIME }) as never
      if (path === '/api/v1/pipeline/stages') return success(stages) as never
      if (path === '/api/v1/queue/depth') return success(queue) as never
      if (path === '/api/v1/jobs') {
        return success({
          items: [makeJob({ run_id: 'run-strict', model_id: 'model-strict' })],
          total: 1,
          limit: 12,
          offset: 0,
        }) as never
      }
      throw new Error(`Unexpected GET ${path}`)
    })

    await useMonitoringStore.getState().fetchAll()
    await useMonitoringStore.getState().fetchJobs()

    expect(calls).toEqual(expect.arrayContaining([
      { path: '/api/v1/pipeline/status', query: expect.objectContaining({ source: 'GFS', cycle_time: TEST_CYCLE_TIME, run_id: 'run-strict', model_id: 'model-strict' }) },
      { path: '/api/v1/pipeline/stages', query: expect.objectContaining({ source: 'GFS', cycle_time: TEST_CYCLE_TIME, run_id: 'run-strict', model_id: 'model-strict' }) },
      { path: '/api/v1/jobs', query: expect.objectContaining({ source: 'GFS', cycle_time: TEST_CYCLE_TIME, run_id: 'run-strict', model_id: 'model-strict' }) },
    ]))
    expect(useMonitoringStore.getState().jobsContext).toEqual({
      source: 'GFS',
      cycleTime: TEST_CYCLE_TIME,
      runId: 'run-strict',
      modelId: 'model-strict',
    })
  })

  it('marks display queue depth unavailable without calling the Slurm queue endpoint', async () => {
    const paths: string[] = []
    useMonitoringStore.setState({ runtimeConfig: displayRuntimeConfig })

    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      paths.push(path)
      if (path === '/api/v1/pipeline/status') return success(cycle) as never
      if (path === '/api/v1/pipeline/stages') return success(stages) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    await useMonitoringStore.getState().fetchAll()

    expect(paths).toEqual(['/api/v1/pipeline/status', '/api/v1/pipeline/stages'])
    expect(useMonitoringStore.getState().queue).toBeNull()
    expect(useMonitoringStore.getState().queueError).toContain('display_readonly')
    expect(useMonitoringStore.getState().stages).toHaveLength(3)
  })

  it('normalizes drifted display runtime config and skips queue depth fail-closed', async () => {
    const paths: string[] = []
    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      paths.push(path)
      if (path === '/api/v1/runtime/config') return success(driftedDisplayRuntimeConfig) as never
      if (path === '/api/v1/pipeline/status') return success(cycle) as never
      if (path === '/api/v1/pipeline/stages') return success(stages) as never
      if (path === '/api/v1/queue/depth') throw new Error('queue depth must not be called for display_readonly')
      throw new Error(`Unexpected GET ${path}`)
    })

    await useMonitoringStore.getState().fetchRuntimeConfig()
    await useMonitoringStore.getState().fetchAll()

    expect(useMonitoringStore.getState().runtimeConfig).toMatchObject({
      service_role: 'display_readonly',
      control_mutations_enabled: false,
      slurm_routes_enabled: false,
      queue_depth_mode: 'display_readonly_unavailable',
      display_readonly: true,
    })
    expect(paths).toEqual(['/api/v1/runtime/config', '/api/v1/pipeline/status', '/api/v1/pipeline/stages'])
    expect(useMonitoringStore.getState().queueError).toContain('display_readonly')
  })

  it('rejects wrong-run strict jobs instead of storing mismatched PASS evidence', async () => {
    useMonitoringStore.setState({
      strictIdentity: {
        source: 'GFS',
        cycleTime: TEST_CYCLE_TIME,
        runId: 'run-selected',
        modelId: 'model-selected',
      },
    })
    vi.mocked(client.GET).mockResolvedValue(success({
      items: [makeJob({ run_id: 'run-other', model_id: 'model-other', status: 'succeeded' })],
      total: 1,
      limit: 12,
      offset: 0,
    }) as never)

    await expect(useMonitoringStore.getState().fetchJobs(undefined, { clearOnFailure: true })).rejects.toThrow('strict identity mismatch')

    expect(useMonitoringStore.getState().jobs).toEqual([])
    expect(useMonitoringStore.getState().jobsError).toContain('strict identity mismatch')
    expect(useMonitoringStore.getState().jobsContext).toBeNull()
  })

  it('rejects strict status source/cycle mismatches before storing ops context', async () => {
    useMonitoringStore.setState({
      runtimeConfig: displayRuntimeConfig,
      cycle: { ...cycle, current_state: 'old-selected' },
      cycleContext: { source: 'GFS', cycleTime: TEST_CYCLE_TIME, runId: 'run-selected', modelId: 'model-selected' },
      stages,
      strictIdentity: {
        source: 'GFS',
        cycleTime: TEST_CYCLE_TIME,
        runId: 'run-selected',
        modelId: 'model-selected',
      },
    })
    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      if (path === '/api/v1/pipeline/status') {
        return success({ ...cycle, source: 'IFS', cycle_time: '2026-05-10T00:00:00Z' }) as never
      }
      if (path === '/api/v1/pipeline/stages') return success(stages) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    await expect(useMonitoringStore.getState().fetchAll({ clearOnFailure: true })).rejects.toThrow('strict identity mismatch')

    const state = useMonitoringStore.getState()
    expect(state.cycle).toBeNull()
    expect(state.cycleContext).toBeNull()
    expect(state.stages).toEqual([])
    expect(state.operationalError).toContain('strict identity mismatch')
  })

  it.each([
    ['missing run/model identity', makeBasinResult({ run_id: null, model_id: null })],
    ['mismatched run/model identity', makeBasinResult({ run_id: 'run-other', model_id: 'model-other' })],
  ])('rejects strict stage basin results with %s', async (_caseName, basinResult) => {
    const failedStage = makeStage('forecast', 'failed', 120, 1, 0, 1)
    failedStage.basin_results = [basinResult]
    const strictStages = [failedStage]
    useMonitoringStore.setState({
      runtimeConfig: displayRuntimeConfig,
      cycle,
      cycleContext: { source: 'GFS', cycleTime: TEST_CYCLE_TIME, runId: 'run-selected', modelId: 'model-selected' },
      stages,
      strictIdentity: {
        source: 'GFS',
        cycleTime: TEST_CYCLE_TIME,
        runId: 'run-selected',
        modelId: 'model-selected',
      },
    })
    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      if (path === '/api/v1/pipeline/status') return success(cycle) as never
      if (path === '/api/v1/pipeline/stages') return success(strictStages) as never
      throw new Error(`Unexpected GET ${path}`)
    })

    await expect(useMonitoringStore.getState().fetchAll({ clearOnFailure: true })).rejects.toThrow('strict identity mismatch')

    const state = useMonitoringStore.getState()
    expect(state.cycle).toBeNull()
    expect(state.cycleContext).toBeNull()
    expect(state.stages).toEqual([])
    expect(state.operationalError).toContain('strict identity mismatch')
  })
})

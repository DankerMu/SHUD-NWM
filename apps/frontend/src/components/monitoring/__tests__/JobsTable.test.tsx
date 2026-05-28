import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { JobsTable, buildDiagnosticPayload } from '@/components/monitoring/JobsTable'
import { useToast } from '@/hooks/useToast'
import type { AuthRole } from '@/stores/auth'
import { type PipelineJob, useMonitoringStore } from '@/stores/monitoring'

const mocks = vi.hoisted(() => ({
  authState: {
    role: 'viewer' as AuthRole,
    canUseActions: false,
  },
  getMock: vi.fn(),
  postMock: vi.fn(),
}))

vi.mock('@/stores/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/stores/auth')>()
  return {
    ...actual,
    useAuthStore: (selector: (state: { role: AuthRole; setRole: (role: AuthRole) => void }) => unknown) =>
      selector({ role: mocks.authState.role, setRole: vi.fn() }),
    canUseDevRoleActions: (role: AuthRole) =>
      mocks.authState.canUseActions && ['operator', 'model_admin', 'sys_admin'].includes(role),
  }
})

vi.mock('@/api/client', () => ({
  client: {
    GET: mocks.getMock,
    POST: mocks.postMock,
  },
}))

const failedJob = {
  job_id: 'job-failed',
  run_id: 'run-failed',
  cycle_id: 'cycle-1',
  run_type: 'forecast',
  scenario: 'forecast_gfs_deterministic',
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
}

const runningJob = {
  job_id: 'job-running',
  run_id: 'run-running',
  cycle_id: 'cycle-1',
  run_type: 'forecast',
  scenario: 'forecast_gfs_deterministic',
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
  log_uri: 's3://logs/job-running.log',
  duration_seconds: null,
}

const queuedJob = {
  job_id: 'job-queued',
  run_id: 'run-queued',
  cycle_id: 'cycle-1',
  run_type: 'forecast',
  scenario: 'forecast_gfs_deterministic',
  job_type: 'forecast',
  slurm_job_id: '1003',
  model_id: 'model-q',
  status: 'queued',
  stage: 'forecast',
  submitted_at: '2026-05-09T00:09:00Z',
  started_at: null,
  finished_at: null,
  exit_code: null,
  retry_count: 0,
  error_code: null,
  error_message: null,
  log_uri: 's3://logs/job-queued.log',
  duration_seconds: null,
}

function makeJob(overrides: Partial<PipelineJob> = {}): PipelineJob {
  return {
    ...failedJob,
    ...overrides,
  }
}

function success<T>(data: T) {
  return { data: { status: 'success', data }, error: undefined }
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

describe('JobsTable RBAC action boundary', () => {
  beforeEach(() => {
    mocks.authState.role = 'viewer'
    mocks.authState.canUseActions = false
    mocks.getMock.mockReset()
    mocks.getMock.mockResolvedValue(success({ job_id: 'job-failed', log_uri: 's3://logs/job-failed.log', content: 'current log' }))
    mocks.postMock.mockReset()
    mocks.postMock.mockResolvedValue({ data: { status: 'ok' }, error: undefined })
    useMonitoringStore.setState({
      ...useMonitoringStore.getInitialState(),
      jobs: [failedJob, runningJob],
      jobTotal: 2,
      fetchJobs: vi.fn().mockResolvedValue(undefined),
      fetchAll: vi.fn().mockResolvedValue(undefined),
    })
    useToast.setState({ toasts: [] })
  })

  it('does not show retry or cancel actions for a configured production operator without dev override', () => {
    mocks.authState.role = 'operator'
    mocks.authState.canUseActions = false

    render(<JobsTable />)

    expect(screen.getByRole('row', { name: /run-failed/ })).toBeVisible()
    expect(screen.queryByRole('button', { name: /重试/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /取消/ })).not.toBeInTheDocument()
  })

  it('does not show retry or cancel actions for analyst even when dev override is enabled', () => {
    mocks.authState.role = 'analyst'
    mocks.authState.canUseActions = true

    render(<JobsTable />)

    expect(screen.queryByRole('button', { name: /重试/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /取消/ })).not.toBeInTheDocument()
  })

  it.each(['operator', 'model_admin', 'sys_admin'] as const)(
    'shows dev override %s retry/log actions and sends the compatible role header',
    async (role) => {
      mocks.authState.role = role
      mocks.authState.canUseActions = true

      render(<JobsTable cancelControlsEnabled={false} />)
      await waitFor(() => expect(useMonitoringStore.getState().fetchJobs).toHaveBeenCalledTimes(1))

      const row = screen.getByRole('row', { name: /run-failed/ })
      await userEvent.click(within(row).getByRole('button', { name: /重试/ }))
      await waitFor(() => expect(mocks.postMock).toHaveBeenCalledTimes(1))
      await userEvent.click(within(screen.getByRole('row', { name: /run-failed/ })).getByRole('button', { name: /查看日志/ }))

      expect(await screen.findByRole('dialog')).toHaveTextContent('作业日志 job-failed')
      expect(mocks.getMock).toHaveBeenCalledWith('/api/v1/jobs/{job_id}/logs', {
        params: { path: { job_id: 'job-failed' } },
      })
      expect(mocks.postMock).toHaveBeenCalledWith(
        '/api/v1/runs/{run_id}/retry',
        expect.objectContaining({
          params: {
            path: { run_id: 'run-failed' },
            header: { 'X-User-Role': role },
          },
        }),
      )
    },
  )

  it('shows dev override operator retry and cancel actions with separate compatible role headers', async () => {
    mocks.authState.role = 'operator'
    mocks.authState.canUseActions = true

    render(<JobsTable />)
    await waitFor(() => expect(useMonitoringStore.getState().fetchJobs).toHaveBeenCalledTimes(1))

    await userEvent.click(within(screen.getByRole('row', { name: /run-failed/ })).getByRole('button', { name: /重试/ }))
    await userEvent.click(within(screen.getByRole('row', { name: /run-running/ })).getByRole('button', { name: /取消/ }))

    await waitFor(() => expect(mocks.postMock).toHaveBeenCalledTimes(2))
    expect(mocks.postMock).toHaveBeenNthCalledWith(
      1,
      '/api/v1/runs/{run_id}/retry',
      expect.objectContaining({
        params: {
          path: { run_id: 'run-failed' },
          header: { 'X-User-Role': 'operator' },
        },
      }),
    )
    expect(mocks.postMock).toHaveBeenNthCalledWith(
      2,
      '/api/v1/runs/{run_id}/cancel',
      expect.objectContaining({
        params: {
          path: { run_id: 'run-running' },
          header: { 'X-User-Role': 'operator' },
        },
      }),
    )
  })

  it.each(['failed', 'submission_failed', 'partially_failed', 'permanently_failed'] as const)(
    'shows retry for authorized retryable %s jobs with a run id',
    (status) => {
      mocks.authState.role = 'operator'
      mocks.authState.canUseActions = true
      useMonitoringStore.setState({
        jobs: [makeJob({ job_id: `job-${status}`, run_id: `run-${status}`, status })],
        jobTotal: 1,
      })

      render(<JobsTable />)

      const row = screen.getByRole('row', { name: new RegExp(`run-${status}`) })
      expect(within(row).getByRole('button', { name: /重试/ })).toBeVisible()
    },
  )

  it('does not show retry for retryable jobs without a run id', () => {
    mocks.authState.role = 'operator'
    mocks.authState.canUseActions = true
    useMonitoringStore.setState({
      jobs: [makeJob({ job_id: 'job-missing-run', run_id: null, status: 'failed' })],
      jobTotal: 1,
    })

    render(<JobsTable />)

    const row = screen.getByRole('row', { name: /job-missing-run/ })
    expect(within(row).queryByRole('button', { name: /重试/ })).not.toBeInTheDocument()
  })

  it('keeps retry available while cancel controls are explicitly disabled', () => {
    mocks.authState.role = 'operator'
    mocks.authState.canUseActions = true

    render(<JobsTable cancelControlsEnabled={false} />)

    expect(within(screen.getByRole('row', { name: /run-failed/ })).getByRole('button', { name: /重试/ })).toBeVisible()
    expect(within(screen.getByRole('row', { name: /run-running/ })).queryByRole('button', { name: /取消/ })).not.toBeInTheDocument()
  })

  it('posts one retry while pending and suppresses duplicate clicks until refresh completes', async () => {
    mocks.authState.role = 'operator'
    mocks.authState.canUseActions = true
    const retryRequest = deferred<unknown>()
    const fetchAll = vi.fn().mockResolvedValue(undefined)
    const fetchJobs = vi.fn().mockResolvedValue(undefined)
    mocks.postMock.mockReturnValueOnce(retryRequest.promise)
    useMonitoringStore.setState({
      jobs: [failedJob],
      jobTotal: 1,
      fetchAll,
      fetchJobs,
    })

    render(<JobsTable />)
    await waitFor(() => expect(fetchJobs).toHaveBeenCalledTimes(1))
    fetchJobs.mockClear()

    const retryButton = within(screen.getByRole('row', { name: /run-failed/ })).getByRole('button', { name: /重试/ })
    await userEvent.click(retryButton)
    await waitFor(() => expect(mocks.postMock).toHaveBeenCalledTimes(1))
    expect(retryButton).toBeDisabled()

    await userEvent.click(retryButton)
    expect(mocks.postMock).toHaveBeenCalledTimes(1)
    expect(fetchAll).not.toHaveBeenCalled()
    expect(fetchJobs).not.toHaveBeenCalled()

    retryRequest.resolve(success({ status: 'submitted' }))
    await waitFor(() => expect(fetchAll).toHaveBeenCalledTimes(1))
    expect(fetchJobs).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(retryButton).not.toBeDisabled())
  })

  it('refreshes status, stages, and jobs for the selected source/cycle after retry success', async () => {
    mocks.authState.role = 'operator'
    mocks.authState.canUseActions = true
    const events: string[] = []
    const fetchAll = vi.fn().mockImplementation(async () => {
      const { source, cycleTime } = useMonitoringStore.getState()
      events.push(`all:${source}:${cycleTime}`)
    })
    const fetchJobs = vi.fn().mockImplementation(async () => {
      const { source, cycleTime } = useMonitoringStore.getState()
      events.push(`jobs:${source}:${cycleTime}`)
    })
    mocks.postMock.mockImplementationOnce(async () => {
      events.push('post:run-failed')
      return success({ status: 'submitted' })
    })
    useMonitoringStore.setState({
      source: 'IFS',
      cycleTime: '2026-05-18T00:00:00.000Z',
      jobs: [failedJob],
      jobTotal: 1,
      fetchAll,
      fetchJobs,
    })

    render(<JobsTable autoFetch={false} />)

    await userEvent.click(within(screen.getByRole('row', { name: /run-failed/ })).getByRole('button', { name: /重试/ }))

    await waitFor(() =>
      expect(events).toEqual([
        'post:run-failed',
        'all:IFS:2026-05-18T00:00:00.000Z',
        'jobs:IFS:2026-05-18T00:00:00.000Z',
      ]),
    )
  })

  it('refreshes the current selected source/cycle if selection changes before retry settles', async () => {
    mocks.authState.role = 'operator'
    mocks.authState.canUseActions = true
    const retryRequest = deferred<unknown>()
    const refreshContexts: string[] = []
    const fetchAll = vi.fn().mockImplementation(async () => {
      const { source, cycleTime } = useMonitoringStore.getState()
      refreshContexts.push(`all:${source}:${cycleTime}`)
    })
    const fetchJobs = vi.fn().mockImplementation(async () => {
      const { source, cycleTime } = useMonitoringStore.getState()
      refreshContexts.push(`jobs:${source}:${cycleTime}`)
    })
    mocks.postMock.mockReturnValueOnce(retryRequest.promise)
    useMonitoringStore.setState({
      source: 'GFS',
      cycleTime: '2026-05-18T00:00:00.000Z',
      jobs: [failedJob],
      jobTotal: 1,
      fetchAll,
      fetchJobs,
    })

    render(<JobsTable autoFetch={false} />)

    await userEvent.click(within(screen.getByRole('row', { name: /run-failed/ })).getByRole('button', { name: /重试/ }))
    await waitFor(() => expect(mocks.postMock).toHaveBeenCalledTimes(1))
    useMonitoringStore.getState().setSource('IFS')
    useMonitoringStore.getState().setCycleTime('2026-05-19T06:00:00.000Z')

    retryRequest.resolve(success({ status: 'submitted' }))

    await waitFor(() =>
      expect(refreshContexts).toEqual([
        'all:IFS:2026-05-19T06:00:00.000Z',
        'jobs:IFS:2026-05-19T06:00:00.000Z',
      ]),
    )
  })

  it('renders formal pipeline job fields from the backend contract', () => {
    render(<JobsTable actionsEnabled={false} />)

    expect(screen.getByRole('columnheader', { name: 'job_id' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'run_id' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'stage' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'status' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'slurm_job_id' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'submitted_at' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'started_at' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'finished_at' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'duration' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'retry_count' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'log' })).toBeInTheDocument()

    const row = screen.getByRole('row', { name: /job-failed.*run-failed.*forecast.*failed.*1001.*2m.*available/ })
    expect(within(row).getByText('job-failed')).toBeInTheDocument()
    expect(within(row).getByText('run-failed')).toBeInTheDocument()
    expect(within(row).getByText('forecast')).toBeInTheDocument()
    expect(within(row).getByText('1001')).toBeInTheDocument()
    expect(within(row).getByText('0')).toBeInTheDocument()
    expect(within(row).getByText('available')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /重试/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /取消/ })).not.toBeInTheDocument()
  })

  it('can render log availability without opening the #212 log modal surface', () => {
    render(<JobsTable actionsEnabled={false} logControlsEnabled={false} />)

    expect(screen.getAllByText('available')).toHaveLength(2)
    expect(screen.queryByRole('button', { name: /查看日志/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('copies only safe diagnostic fields and keeps notified state local', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    useMonitoringStore.setState({
      source: 'GFS',
      cycleTime: '2026-05-09T00:00:00Z',
      jobs: [failedJob],
      jobTotal: 1,
    })

    render(
      <JobsTable
        autoFetch={false}
        diagnosticsEnabled
        retryControlsEnabled={false}
        cancelControlsEnabled={false}
        strictIdentity={{
          source: 'GFS',
          cycleTime: '2026-05-09T00:00:00.000Z',
          runId: 'run-failed',
          modelId: 'model-b',
        }}
      />,
    )

    expect(screen.getByTestId('ops-manual-recovery-guidance')).toHaveTextContent('22 compute-control')
    await userEvent.click(screen.getByRole('button', { name: /复制诊断/ }))

    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
    const payload = JSON.parse(writeText.mock.calls[0][0])
    expect(payload).toEqual({
      source_id: 'GFS',
      cycle_time: '2026-05-09T00:00:00.000Z',
      run_id: 'run-failed',
      model_id: 'model-b',
      stage: 'forecast',
      job_id: 'job-failed',
      slurm_job_id: '1001',
      status: 'failed',
      error_code: 'E_MODEL',
      error_message: 'model failed',
      log_uri: 's3://logs/job-failed.log',
    })
    expect(Object.keys(payload)).toEqual([
      'source_id',
      'cycle_time',
      'run_id',
      'model_id',
      'stage',
      'job_id',
      'slurm_job_id',
      'status',
      'error_code',
      'error_message',
      'log_uri',
    ])

    await userEvent.click(screen.getByRole('button', { name: /标记已通知/ }))
    expect(screen.getByRole('button', { name: /已通知/ })).toBeDisabled()
    expect(mocks.postMock).not.toHaveBeenCalled()
    expect(mocks.getMock).not.toHaveBeenCalled()
  })

  it('omits absent diagnostic fields instead of fabricating unavailable values', () => {
    expect(buildDiagnosticPayload(makeJob({
      run_id: null,
      model_id: null,
      slurm_job_id: null,
      error_code: null,
      error_message: null,
      log_uri: null,
    }), {
      sourceId: 'GFS',
      cycleTime: '2026-05-09T00:00:00Z',
      runId: null,
      modelId: null,
    })).toEqual({
      source_id: 'GFS',
      cycle_time: '2026-05-09T00:00:00Z',
      stage: 'forecast',
      job_id: 'job-failed',
      status: 'failed',
    })
  })

  it('shows explicit unavailable state instead of stale rows when jobs fail to load', () => {
    useMonitoringStore.setState({
      jobs: [],
      jobTotal: 0,
      jobsError: 'jobs unsupported for selected context',
    })

    render(<JobsTable actionsEnabled={false} />)

    expect(screen.queryByRole('row', { name: /run-failed/ })).not.toBeInTheDocument()
    expect(screen.getByText(/当前 source\/cycle 的作业不可用：jobs unsupported for selected context/)).toBeInTheDocument()
  })

  it('allows an operator with dev actions to cancel queued jobs but does not show retry', async () => {
    mocks.authState.role = 'operator'
    mocks.authState.canUseActions = true
    useMonitoringStore.setState({
      jobs: [queuedJob],
      jobTotal: 1,
    })

    render(<JobsTable />)
    await waitFor(() => expect(useMonitoringStore.getState().fetchJobs).toHaveBeenCalledTimes(1))

    const queuedRow = screen.getByRole('row', { name: /run-queued/ })
    expect(within(queuedRow).getByRole('button', { name: /取消/ })).toBeVisible()
    expect(within(queuedRow).queryByRole('button', { name: /重试/ })).not.toBeInTheDocument()

    await userEvent.click(within(queuedRow).getByRole('button', { name: /取消/ }))

    await waitFor(() => expect(mocks.postMock).toHaveBeenCalledTimes(1))
    expect(mocks.postMock).toHaveBeenCalledWith(
      '/api/v1/runs/{run_id}/cancel',
      expect.objectContaining({
        params: {
          path: { run_id: 'run-queued' },
          header: { 'X-User-Role': 'operator' },
        },
      }),
    )
  })

  it('renders backend forbidden action failures and refreshes monitoring state without success toast', async () => {
    mocks.authState.role = 'operator'
    mocks.authState.canUseActions = true
    mocks.postMock.mockResolvedValueOnce({
      data: undefined,
      error: { error: { code: 'RBAC_FORBIDDEN', message: 'Actor roles are not authorized.' } },
    })

    render(<JobsTable />)

    await userEvent.click(within(screen.getByRole('row', { name: /run-failed/ })).getByRole('button', { name: /重试/ }))

    await waitFor(() =>
      expect(useToast.getState().toasts.at(-1)).toMatchObject({
        title: '重试失败',
        description: 'Actor roles are not authorized.',
        variant: 'destructive',
      }),
    )
    expect(useToast.getState().toasts).not.toContainEqual(expect.objectContaining({ title: '重试已提交' }))
    expect(useMonitoringStore.getState().fetchAll).toHaveBeenCalledTimes(1)
    expect(useMonitoringStore.getState().fetchJobs).toHaveBeenCalledTimes(2)
  })

  it('shows retry success toast and clears pending state after scoped refresh', async () => {
    mocks.authState.role = 'operator'
    mocks.authState.canUseActions = true
    const retryRequest = deferred<unknown>()
    const fetchAll = vi.fn().mockResolvedValue(undefined)
    const fetchJobs = vi.fn().mockResolvedValue(undefined)
    mocks.postMock.mockReturnValueOnce(retryRequest.promise)
    useMonitoringStore.setState({
      jobs: [failedJob],
      jobTotal: 1,
      fetchAll,
      fetchJobs,
    })

    render(<JobsTable autoFetch={false} />)

    const retryButton = within(screen.getByRole('row', { name: /run-failed/ })).getByRole('button', { name: /重试/ })
    await userEvent.click(retryButton)
    await waitFor(() => expect(retryButton).toBeDisabled())

    retryRequest.resolve(success({ status: 'submitted' }))

    await waitFor(() => expect(useToast.getState().toasts.at(-1)).toMatchObject({ title: '重试已提交' }))
    expect(fetchAll).toHaveBeenCalledTimes(1)
    expect(fetchJobs).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(retryButton).not.toBeDisabled())
  })

  it('shows retry network failures, refreshes, and clears pending state', async () => {
    mocks.authState.role = 'operator'
    mocks.authState.canUseActions = true
    const fetchAll = vi.fn().mockResolvedValue(undefined)
    const fetchJobs = vi.fn().mockResolvedValue(undefined)
    mocks.postMock.mockRejectedValueOnce(new Error('network down'))
    useMonitoringStore.setState({
      jobs: [failedJob],
      jobTotal: 1,
      fetchAll,
      fetchJobs,
    })

    render(<JobsTable autoFetch={false} />)

    const retryButton = within(screen.getByRole('row', { name: /run-failed/ })).getByRole('button', { name: /重试/ })
    await userEvent.click(retryButton)

    await waitFor(() =>
      expect(useToast.getState().toasts.at(-1)).toMatchObject({
        title: '重试失败',
        description: 'network down',
        variant: 'destructive',
      }),
    )
    expect(useToast.getState().toasts).not.toContainEqual(expect.objectContaining({ title: '重试已提交' }))
    expect(fetchAll).toHaveBeenCalledTimes(1)
    expect(fetchJobs).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(retryButton).not.toBeDisabled())
  })

  it('refreshes monitoring state after terminal retry submission failures', async () => {
    mocks.authState.role = 'operator'
    mocks.authState.canUseActions = true
    const fetchAll = vi.fn().mockResolvedValue(undefined)
    const fetchJobs = vi.fn().mockResolvedValue(undefined)
    mocks.postMock.mockResolvedValueOnce({
      data: undefined,
      error: { error: { code: 'RETRY_SUBMISSION_FAILED', message: 'Retry submission failed.' } },
    })
    useMonitoringStore.setState({
      jobs: [failedJob],
      jobTotal: 1,
      fetchAll,
      fetchJobs,
    })

    render(<JobsTable autoFetch={false} />)

    await userEvent.click(within(screen.getByRole('row', { name: /run-failed/ })).getByRole('button', { name: /重试/ }))

    await waitFor(() =>
      expect(useToast.getState().toasts.at(-1)).toMatchObject({
        title: '重试失败',
        description: 'Retry submission failed.',
        variant: 'destructive',
      }),
    )
    expect(fetchAll).toHaveBeenCalledTimes(1)
    expect(fetchJobs).toHaveBeenCalledTimes(1)
  })

  it('closes an open log modal when the visible selected-context jobs no longer contain that job', async () => {
    render(<JobsTable autoFetch={false} />)

    await userEvent.click(within(screen.getByRole('row', { name: /run-failed/ })).getByRole('button', { name: /查看日志/ }))

    expect(await screen.findByRole('dialog')).toBeInTheDocument()

    useMonitoringStore.setState({
      jobs: [makeJob({ job_id: 'job-new-cycle', run_id: 'run-new-cycle', status: 'succeeded' })],
      jobTotal: 1,
    })

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })
})

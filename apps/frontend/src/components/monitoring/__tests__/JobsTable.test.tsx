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
  }
})

vi.mock('@/api/client', () => ({
  client: {
    GET: mocks.getMock,
    POST: mocks.postMock,
  },
}))

const failedJob: PipelineJob = {
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

const runningJob: PipelineJob = {
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

const queuedJob: PipelineJob = {
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

describe('JobsTable RBAC action boundary', () => {
  beforeEach(() => {
    mocks.authState.role = 'viewer'
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

  it.each(['viewer', 'analyst', 'operator', 'model_admin', 'sys_admin'] as const)(
    'renders no run-action control and sends no run request for %s (#3179, #3394)',
    async (role) => {
      mocks.authState.role = role
      useMonitoringStore.setState({ jobs: [failedJob, runningJob, queuedJob], jobTotal: 3 })

      render(<JobsTable />)
      await waitFor(() => expect(useMonitoringStore.getState().fetchJobs).toHaveBeenCalledTimes(1))

      for (const runId of ['run-failed', 'run-running', 'run-queued']) {
        const row = screen.getByRole('row', { name: new RegExp(runId) })
        expect(within(row).getByRole('button', { name: /查看日志/ })).toBeVisible()
        expect(within(row).queryByRole('button', { name: /重试/ })).not.toBeInTheDocument()
        expect(within(row).queryByRole('button', { name: /取消/ })).not.toBeInTheDocument()
        for (const button of within(row).getAllByRole('button')) {
          await userEvent.click(button)
          await userEvent.keyboard('{Escape}')
        }
      }
      expect(mocks.getMock).toHaveBeenCalledWith('/api/v1/jobs/{job_id}/logs', {
        params: { path: { job_id: 'job-failed' } },
      })
      expect(mocks.postMock).not.toHaveBeenCalled()
    },
  )

  it.each(['operator', 'model_admin', 'sys_admin'] as const)(
    'shows no retry button for any job status to dev override %s (#3179)',
    (role) => {
      mocks.authState.role = role
      const statuses = ['failed', 'submission_failed', 'partially_failed', 'permanently_failed', 'cancelled', 'running'] as const
      useMonitoringStore.setState({
        jobs: [
          ...statuses.map((status) => makeJob({ job_id: `job-${status}`, run_id: `run-${status}`, status })),
          makeJob({ job_id: 'job-missing-run', run_id: null, status: 'failed' }),
        ],
        jobTotal: statuses.length + 1,
      })

      render(<JobsTable />)

      for (const status of statuses) {
        expect(screen.getByRole('row', { name: new RegExp(`run-${status}`) })).toBeVisible()
      }
      expect(screen.getByRole('row', { name: /job-missing-run/ })).toBeVisible()
      expect(screen.queryByRole('button', { name: /重试/ })).not.toBeInTheDocument()
    },
  )

  it('gives an authorized operator no way to post a retry for a failed run (#3179)', async () => {
    mocks.authState.role = 'operator'
    useMonitoringStore.setState({
      jobs: [failedJob],
      jobTotal: 1,
    })

    render(<JobsTable />)
    await waitFor(() => expect(useMonitoringStore.getState().fetchJobs).toHaveBeenCalledTimes(1))

    const failedRow = screen.getByRole('row', { name: /run-failed/ })
    expect(within(failedRow).getByRole('button', { name: /查看日志/ })).toBeVisible()
    expect(within(failedRow).queryByRole('button', { name: /重试/ })).not.toBeInTheDocument()
    for (const button of within(failedRow).getAllByRole('button')) {
      await userEvent.click(button)
      await userEvent.keyboard('{Escape}')
    }
    expect(mocks.postMock).not.toHaveBeenCalled()
  })

  it('renders formal pipeline job fields from the backend contract', () => {
    render(<JobsTable />)

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
    render(<JobsTable logControlsEnabled={false} />)

    expect(screen.getAllByText('available')).toHaveLength(2)
    expect(screen.queryByRole('button', { name: /查看日志/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('keeps diagnostics guidance role-neutral unless display_readonly is explicit', () => {
    const { rerender } = render(
      <JobsTable
        autoFetch={false}
        diagnosticsEnabled
      />,
    )

    expect(screen.getByTestId('ops-manual-recovery-guidance')).toHaveTextContent('22 compute-control')
    expect(screen.getByTestId('ops-manual-recovery-guidance')).not.toHaveTextContent(/display_readonly|27/)

    rerender(
      <JobsTable
        autoFetch={false}
        diagnosticsDisplayReadonly
        diagnosticsEnabled
      />,
    )

    expect(screen.getByTestId('ops-manual-recovery-guidance')).toHaveTextContent('27 display_readonly')
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
        strictIdentity={{
          source: 'GFS',
          cycleTime: '2026-05-09T00:00:00.000Z',
          runId: 'run-failed',
          modelId: 'model-b',
        }}
      />,
    )

    expect(screen.getByTestId('ops-manual-recovery-guidance')).toHaveTextContent('22 compute-control')
    expect(screen.getByTestId('ops-manual-recovery-guidance')).not.toHaveTextContent(/display_readonly|27/)
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

    render(<JobsTable />)

    expect(screen.queryByRole('row', { name: /run-failed/ })).not.toBeInTheDocument()
    expect(screen.getByText(/当前 source\/cycle 的作业不可用：jobs unsupported for selected context/)).toBeInTheDocument()
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

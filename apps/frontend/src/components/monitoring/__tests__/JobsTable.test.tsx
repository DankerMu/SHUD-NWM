import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { JobsTable } from '@/components/monitoring/JobsTable'
import { useToast } from '@/hooks/useToast'
import type { AuthRole } from '@/stores/auth'
import { useMonitoringStore } from '@/stores/monitoring'

const mocks = vi.hoisted(() => ({
  authState: {
    role: 'viewer' as AuthRole,
    canUseActions: false,
  },
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
    POST: mocks.postMock,
  },
}))

const failedJob = {
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
}

const runningJob = {
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
  log_uri: 's3://logs/job-running.log',
  duration_seconds: null,
}

const queuedJob = {
  job_id: 'job-queued',
  run_id: 'run-queued',
  cycle_id: 'cycle-1',
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

describe('JobsTable RBAC action boundary', () => {
  beforeEach(() => {
    mocks.authState.role = 'viewer'
    mocks.authState.canUseActions = false
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

  it('shows dev override operator actions and sends the compatible role header', async () => {
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
})

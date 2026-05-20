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

import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { LogModal } from '@/components/monitoring/LogModal'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn(),
  },
}))

function success(content: string, jobId = 'job-1') {
  return {
    data: {
      status: 'success',
      data: {
        job_id: jobId,
        log_uri: `s3://logs/${jobId}.log`,
        content,
      },
    },
    error: undefined,
  }
}

function failure(message: string) {
  return {
    data: undefined,
    error: { error: { code: 'JOB_LOG_NOT_FOUND', message } },
  }
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

describe('LogModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('loads log content through the backend job logs endpoint', async () => {
    vi.mocked(client.GET).mockResolvedValue(success('bounded backend log content') as never)

    render(<LogModal jobId="job-1" open onOpenChange={vi.fn()} />)

    expect(screen.getByText('加载中...')).toBeInTheDocument()
    expect(await screen.findByText('bounded backend log content')).toBeInTheDocument()
    expect(client.GET).toHaveBeenCalledWith('/api/v1/jobs/{job_id}/logs', {
      params: { path: { job_id: 'job-1' } },
    })
    expect(screen.queryByText('s3://logs/job-1.log')).not.toBeInTheDocument()
  })

  it('renders empty logs explicitly', async () => {
    vi.mocked(client.GET).mockResolvedValue(success('', 'job-empty') as never)

    render(<LogModal jobId="job-empty" open onOpenChange={vi.fn()} />)

    expect(await screen.findByText('(空日志)')).toBeInTheDocument()
  })

  it('renders backend errors as unavailable log content', async () => {
    vi.mocked(client.GET).mockResolvedValue(failure('Job log file was not found.') as never)

    render(<LogModal jobId="job-missing" open onOpenChange={vi.fn()} />)

    expect(await screen.findByText('加载失败: Job log file was not found.')).toBeInTheDocument()
  })

  it('passes strict identity to the backend log request', async () => {
    vi.mocked(client.GET).mockResolvedValue(success('strict log', 'job-strict') as never)

    render(
      <LogModal
        jobId="job-strict"
        open
        onOpenChange={vi.fn()}
        strictIdentity={{
          source: 'GFS',
          cycleTime: '2026-05-09T00:00:00.000Z',
          runId: 'run-strict',
          modelId: 'model-strict',
        }}
      />,
    )

    expect(await screen.findByText('strict log')).toBeInTheDocument()
    expect(client.GET).toHaveBeenCalledWith('/api/v1/jobs/{job_id}/logs', {
      params: {
        path: { job_id: 'job-strict' },
        query: {
          source: 'GFS',
          cycle_time: '2026-05-09T00:00:00.000Z',
          run_id: 'run-strict',
          model_id: 'model-strict',
        },
      },
    })
  })

  it('sanitizes backend log errors before rendering them to browser users', async () => {
    vi.mocked(client.GET).mockResolvedValue(failure('Open file:///scratch/node22/.nhms-runs/run/job.log or /scratch/node22/private/job.log') as never)

    render(<LogModal jobId="job-private-path" open onOpenChange={vi.fn()} />)

    const error = await screen.findByText(/加载失败:/)
    expect(error).toHaveTextContent('受限文件 URI')
    expect(error).toHaveTextContent('本地路径已隐藏')
    expect(error).not.toHaveTextContent('file:///scratch')
    expect(error).not.toHaveTextContent('/scratch/node22')
  })

  it('renders thrown backend failures as unavailable log content', async () => {
    vi.mocked(client.GET).mockRejectedValue(new Error('logs service unavailable'))

    render(<LogModal jobId="job-error" open onOpenChange={vi.fn()} />)

    expect(await screen.findByText('加载失败: logs service unavailable')).toBeInTheDocument()
  })

  it('clears stale content when switching jobs before the previous log request resolves', async () => {
    const oldLog = deferred<unknown>()
    const newLog = deferred<unknown>()
    vi.mocked(client.GET)
      .mockReturnValueOnce(oldLog.promise as never)
      .mockReturnValueOnce(newLog.promise as never)

    const { rerender } = render(<LogModal jobId="job-old" open onOpenChange={vi.fn()} />)

    await waitFor(() => expect(client.GET).toHaveBeenCalledTimes(1))
    rerender(<LogModal jobId="job-new" open onOpenChange={vi.fn()} />)

    expect(screen.getByText('加载中...')).toBeInTheDocument()
    expect(screen.queryByText('old log content')).not.toBeInTheDocument()

    oldLog.resolve(success('old log content', 'job-old'))
    newLog.resolve(success('new log content', 'job-new'))

    expect(await screen.findByText('new log content')).toBeInTheDocument()
    expect(screen.queryByText('old log content')).not.toBeInTheDocument()
    expect(client.GET).toHaveBeenNthCalledWith(2, '/api/v1/jobs/{job_id}/logs', {
      params: { path: { job_id: 'job-new' } },
    })
  })
})

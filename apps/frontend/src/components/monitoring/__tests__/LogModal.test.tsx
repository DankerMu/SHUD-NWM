import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { LogModal } from '@/components/monitoring/LogModal'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn(),
  },
}))

function success(content: string) {
  return {
    data: {
      status: 'success',
      data: {
        job_id: 'job-1',
        log_uri: 's3://logs/job-1.log',
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
    vi.mocked(client.GET).mockResolvedValue(success('') as never)

    render(<LogModal jobId="job-empty" open onOpenChange={vi.fn()} />)

    expect(await screen.findByText('(空日志)')).toBeInTheDocument()
  })

  it('renders backend errors as unavailable log content', async () => {
    vi.mocked(client.GET).mockResolvedValue(failure('Job log file was not found.') as never)

    render(<LogModal jobId="job-missing" open onOpenChange={vi.fn()} />)

    expect(await screen.findByText('加载失败: Job log file was not found.')).toBeInTheDocument()
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

    oldLog.resolve(success('old log content'))
    newLog.resolve(success('new log content'))

    expect(await screen.findByText('new log content')).toBeInTheDocument()
    expect(screen.queryByText('old log content')).not.toBeInTheDocument()
    expect(client.GET).toHaveBeenNthCalledWith(2, '/api/v1/jobs/{job_id}/logs', {
      params: { path: { job_id: 'job-new' } },
    })
  })
})

import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'

import { client } from '@/api/client'
import { MonitoringPage } from '@/pages/MonitoringPage'
import { useMonitoringStore, type RuntimeConfig } from '@/stores/monitoring'

vi.mock('@/api/client', () => ({
  client: { GET: vi.fn(), POST: vi.fn() },
}))

vi.mock('@/components/charts/TrendLine', () => ({
  TrendLine: ({ title }: { title: string }) => <div>{title}</div>,
}))

// fixture 钉死的接缝（Seams under test）：把一个面板模块桩成渲染期抛错；开关关着时透传真组件，
// 同一份文件也承担 happy-path 的零 fallback 断言。
const jobsTableControl = vi.hoisted(() => ({ throws: false }))

vi.mock('@/components/monitoring/JobsTable', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/components/monitoring/JobsTable')>()
  return {
    ...actual,
    JobsTable: (props: Parameters<typeof actual.JobsTable>[0]) => {
      if (jobsTableControl.throws) throw new Error('jobs payload shape drift')
      return <actual.JobsTable {...props} />
    },
  }
})

const runtimeConfig: RuntimeConfig = {
  service_role: 'display_readonly',
  control_mutations_enabled: false,
  slurm_routes_enabled: false,
  queue_depth_mode: 'display_readonly_unavailable',
  display_readonly: true,
}

function success<T>(data: T) {
  return { data: { status: 'success', data }, error: undefined }
}

function renderMonitoring() {
  render(
    <MemoryRouter initialEntries={['/monitoring']}>
      <MonitoringPage mode="monitoring" />
    </MemoryRouter>,
  )
}

let consoleError: MockInstance<typeof console.error>

beforeEach(() => {
  vi.clearAllMocks()
  jobsTableControl.throws = false
  consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
  vi.mocked(client.GET).mockResolvedValue(success([]) as never)
  // 取数全部置空：本文件只看渲染期的面板隔离，不看轮询与接口。
  useMonitoringStore.setState({
    runtimeConfig,
    runtimeConfigError: null,
    fetchRuntimeConfig: async () => undefined,
    fetchAll: async () => undefined,
    fetchJobs: async () => undefined,
  })
})

afterEach(() => {
  consoleError.mockRestore()
})

describe('MonitoringPage region error boundaries', () => {
  it('renders every panel with zero fallbacks on the happy path', async () => {
    renderMonitoring()

    expect(screen.getByText('当前周期')).toBeInTheDocument()
    expect(screen.getAllByText('七阶段流水线').length).toBeGreaterThan(0)
    expect(screen.getByText('作业列表')).toBeInTheDocument()
    expect(screen.getByText('趋势')).toBeInTheDocument()
    await waitFor(() => expect(client.GET).toHaveBeenCalled())
    expect(screen.queryAllByTestId(/^region-error-|^route-error-fallback$/)).toHaveLength(0)
  })

  it('contains a jobs table render throw to its own panel', async () => {
    jobsTableControl.throws = true
    renderMonitoring()

    const fallback = screen.getByTestId('region-error-jobs')
    expect(fallback).toHaveAttribute('role', 'alert')
    expect(fallback).toHaveTextContent('此区域加载失败')
    expect(screen.queryByText('作业列表')).toBeNull()
    expect(screen.getByText('当前周期')).toBeInTheDocument()
    expect(screen.getAllByText('七阶段流水线').length).toBeGreaterThan(0)
    expect(screen.getByText('趋势')).toBeInTheDocument()
    expect(screen.queryAllByTestId(/^region-error-/).map((node) => node.getAttribute('data-testid'))).toEqual([
      'region-error-jobs',
    ])
    expect(consoleError.mock.calls.some((call) => call[0] === '[RegionErrorBoundary]' && call[1] === '作业')).toBe(true)
    await waitFor(() => expect(client.GET).toHaveBeenCalled())
  })
})

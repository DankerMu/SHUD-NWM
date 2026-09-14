import { act, render, screen } from '@testing-library/react'
import { MemoryRouter, useNavigate, type NavigateFunction } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'

import { AppFrame } from '@/App'
import { useAuthStore } from '@/stores/auth'
import { useMonitoringStore, type RuntimeConfig } from '@/stores/monitoring'

// 三个页面模块各担一个角色（`App` 按具名导出取页面，桩须同名）：
// - OverviewPage：渲染期抛错（页面组件自身崩溃）；
// - ModelAssetsPage：模块导入被拒（chunk 加载失败，React.lazy 把拒绝抛给最近的边界）；
// - MonitoringPage：正常渲染的目标路由，用来证明导航后页面区复位。
vi.mock('@/pages/OverviewPage', () => ({
  OverviewPage: function OverviewPageRenderThrow() {
    throw new Error('overview render exploded')
  },
}))

vi.mock('@/pages/ModelAssetsPage', () => {
  throw new Error('chunk load failed')
})

vi.mock('@/pages/MonitoringPage', () => ({
  MonitoringPage: function MonitoringPageStub({ mode }: { mode?: string }) {
    return <div data-testid="monitoring-page-stub" data-mode={mode} />
  },
}))

const displayRuntimeConfig: RuntimeConfig = {
  service_role: 'display_readonly',
  control_mutations_enabled: false,
  slurm_routes_enabled: false,
  queue_depth_mode: 'display_readonly_unavailable',
  display_readonly: true,
}

let navigateRef: NavigateFunction | null = null

function NavigateProbe() {
  navigateRef = useNavigate()
  return null
}

function renderFrameAt(entry: string) {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <NavigateProbe />
      <AppFrame />
    </MemoryRouter>,
  )
}

let consoleError: MockInstance<typeof console.error>

beforeEach(() => {
  navigateRef = null
  consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
  // runtime config 已在手：AppShell 不发 fetch，RBACGate 不进等待态。
  useMonitoringStore.setState({ runtimeConfig: displayRuntimeConfig, runtimeConfigError: null })
})

afterEach(() => {
  consoleError.mockRestore()
})

describe('AppFrame route error boundary', () => {
  it('keeps the header when a routed page throws and recovers on navigation to another route', async () => {
    useAuthStore.setState({ role: 'operator' })
    renderFrameAt('/')

    const fallback = await screen.findByTestId('route-error-fallback')
    expect(fallback).toHaveAttribute('role', 'alert')
    expect(fallback).toHaveTextContent('页面加载失败')
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '刷新页面' })).toBeInTheDocument()
    expect(screen.getByRole('banner')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('overview render exploded')

    await act(async () => {
      navigateRef?.('/monitoring')
    })

    const page = await screen.findByTestId('monitoring-page-stub')
    expect(page).toHaveAttribute('data-mode', 'monitoring')
    expect(screen.queryByTestId('route-error-fallback')).toBeNull()
    expect(screen.getByRole('banner')).toBeInTheDocument()
  })

  it('catches a rejected lazy page import in the route fallback', async () => {
    useAuthStore.setState({ role: 'sys_admin' })
    renderFrameAt('/system/model-assets')

    // 只断言被接住；「重试」不承诺能恢复 chunk 失败（React.lazy 缓存了拒绝，见 design D2）。
    const fallback = await screen.findByTestId('route-error-fallback')
    expect(fallback).toHaveTextContent('页面加载失败')
    expect(screen.getByRole('button', { name: '刷新页面' })).toBeInTheDocument()
    expect(screen.getByRole('banner')).toBeInTheDocument()
  })
})

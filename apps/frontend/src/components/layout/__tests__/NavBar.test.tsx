import { render, screen, waitFor } from '@testing-library/react'
import { BrowserRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { NavBar } from '@/components/layout/NavBar'
import { client } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useMonitoringStore } from '@/stores/monitoring'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
}))

let meteorologyContractsReady = true
vi.mock('@/lib/meteorology/contracts', () => ({
  hasMinimumMeteorologyContracts: () => meteorologyContractsReady,
}))

const computeRuntimeConfig = {
  service_role: 'compute_control',
  control_mutations_enabled: true,
  slurm_routes_enabled: true,
  queue_depth_mode: 'slurm_gateway',
  display_readonly: false,
} as const

const devRuntimeConfig = {
  service_role: 'dev_monolith',
  control_mutations_enabled: true,
  slurm_routes_enabled: true,
  queue_depth_mode: 'slurm_gateway',
  display_readonly: false,
} as const

const displayRuntimeConfig = {
  service_role: 'display_readonly',
  control_mutations_enabled: false,
  slurm_routes_enabled: false,
  queue_depth_mode: 'display_readonly_unavailable',
  display_readonly: true,
} as const

type RuntimeConfig = typeof computeRuntimeConfig | typeof displayRuntimeConfig | typeof devRuntimeConfig

function setRuntimeConfig(runtimeConfig: RuntimeConfig | null, error: string | null = null) {
  useMonitoringStore.setState({ runtimeConfig, runtimeConfigError: error })
}

function renderNavBar() {
  render(
    <BrowserRouter>
      <NavBar />
    </BrowserRouter>,
  )
}

describe('NavBar display_readonly downgrade', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    meteorologyContractsReady = true
    useAuthStore.setState({ role: 'sys_admin' })
    setRuntimeConfig(computeRuntimeConfig)
    vi.mocked(client.GET).mockResolvedValue({
      data: { status: 'success', data: computeRuntimeConfig },
      error: undefined,
    } as never)
  })

  afterEach(() => {
    setRuntimeConfig(null)
  })

  it('hides /ops and /monitoring entries under display_readonly runtime config', () => {
    setRuntimeConfig(displayRuntimeConfig)
    renderNavBar()

    expect(screen.queryByRole('link', { name: /内部诊断/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /产品监控/ })).not.toBeInTheDocument()
    // 非降级入口保持
    expect(screen.getByRole('link', { name: /全国总览/ })).toHaveAttribute('href', '/overview')
  })

  it('keeps /ops and /monitoring entries under compute_control', () => {
    setRuntimeConfig(computeRuntimeConfig)
    renderNavBar()

    expect(screen.getByRole('link', { name: /内部诊断/ })).toHaveAttribute('href', '/ops')
    expect(screen.getByRole('link', { name: /产品监控/ })).toHaveAttribute('href', '/monitoring')
  })

  it('keeps /ops and /monitoring entries under dev_monolith', () => {
    setRuntimeConfig(devRuntimeConfig)
    renderNavBar()

    expect(screen.getByRole('link', { name: /内部诊断/ })).toHaveAttribute('href', '/ops')
    expect(screen.getByRole('link', { name: /产品监控/ })).toHaveAttribute('href', '/monitoring')
  })

  it('fail-safe: shows entries before runtime config resolves, then collapses once display_readonly confirmed', async () => {
    setRuntimeConfig(null)
    vi.mocked(client.GET).mockResolvedValue({
      data: { status: 'success', data: displayRuntimeConfig },
      error: undefined,
    } as never)

    renderNavBar()

    // 未就绪默认显示（非 display_readonly 处理）
    expect(screen.getByRole('link', { name: /内部诊断/ })).toHaveAttribute('href', '/ops')
    expect(screen.getByRole('link', { name: /产品监控/ })).toHaveAttribute('href', '/monitoring')

    // 由 NavBar 触发的 runtime config 加载完成、确认 display_readonly 后收起
    await waitFor(() =>
      expect(screen.queryByRole('link', { name: /内部诊断/ })).not.toBeInTheDocument(),
    )
    expect(screen.queryByRole('link', { name: /产品监控/ })).not.toBeInTheDocument()
    expect(vi.mocked(client.GET).mock.calls.some(([path]) => path === '/api/v1/runtime/config')).toBe(true)
  })

  it('downgrade decision uses runtime config, not a build-time role constant', () => {
    // 角色具备运维权限，但 display_readonly 仍隐藏运维入口（来源是 runtime config）
    useAuthStore.setState({ role: 'sys_admin' })
    setRuntimeConfig(displayRuntimeConfig)
    renderNavBar()

    expect(screen.queryByRole('link', { name: /内部诊断/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /产品监控/ })).not.toBeInTheDocument()
  })

  it('keeps /meteorology contract gating: hidden when contracts are not ready', () => {
    meteorologyContractsReady = false
    setRuntimeConfig(computeRuntimeConfig)
    renderNavBar()

    expect(screen.queryByRole('link', { name: /气象数据/ })).not.toBeInTheDocument()
  })

  it('keeps /meteorology contract gating: shown when contracts are ready', () => {
    meteorologyContractsReady = true
    setRuntimeConfig(computeRuntimeConfig)
    renderNavBar()

    expect(screen.getByRole('link', { name: /气象数据/ })).toHaveAttribute('href', '/meteorology')
  })

  it('keeps role-based filtering for /ops under compute mode', () => {
    useAuthStore.setState({ role: 'viewer' })
    setRuntimeConfig(computeRuntimeConfig)
    renderNavBar()

    // viewer 角色不在 /ops roles 内，原有 role 过滤保持
    expect(screen.queryByRole('link', { name: /内部诊断/ })).not.toBeInTheDocument()
    // 但无 role 限制的 /monitoring 仍显示
    expect(screen.getByRole('link', { name: /产品监控/ })).toHaveAttribute('href', '/monitoring')
  })
})

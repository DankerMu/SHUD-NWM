import { act, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DISPLAY_READONLY_RUNTIME_CONFIG_DEADLINE_MS, RBACGate } from '@/components/layout/RBACGate'
import { type AuthRole, useAuthStore } from '@/stores/auth'
import { useMonitoringStore } from '@/stores/monitoring'

const allowedRoles: AuthRole[] = ['operator', 'model_admin', 'sys_admin']

function renderGate(allowDisplayReadonly = false) {
  render(
    <RBACGate roles={allowedRoles} allowDisplayReadonly={allowDisplayReadonly}>
      <div>allowed content</div>
    </RBACGate>,
  )
}

describe('RBACGate', () => {
  beforeEach(() => {
    vi.useRealTimers()
    useAuthStore.setState({ role: 'viewer' })
    useMonitoringStore.setState({ runtimeConfig: null, runtimeConfigError: null, fetchRuntimeConfig: async () => undefined })
  })

  it.each(['viewer', 'analyst'] satisfies AuthRole[])('blocks %s role', (role) => {
    useAuthStore.setState({ role })
    useMonitoringStore.setState({ runtimeConfigError: 'runtime unavailable' })
    renderGate()

    expect(screen.getByText('权限不足')).toBeInTheDocument()
    expect(screen.queryByText('allowed content')).not.toBeInTheDocument()
  })

  it('waits briefly for an explicit runtime mode without flashing permission denied', () => {
    vi.useFakeTimers()
    const fetchRuntimeConfig = vi.fn().mockResolvedValue(undefined)
    useMonitoringStore.setState({ fetchRuntimeConfig })
    renderGate(true)

    expect(fetchRuntimeConfig).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('status')).toHaveTextContent('正在确认只读诊断访问策略')
    act(() => vi.advanceTimersByTime(DISPLAY_READONLY_RUNTIME_CONFIG_DEADLINE_MS - 1))
    expect(screen.queryByText('权限不足')).not.toBeInTheDocument()
    expect(screen.queryByText('allowed content')).not.toBeInTheDocument()
  })

  it('fails closed after the readonly runtime-config deadline when the request never resolves', () => {
    vi.useFakeTimers()
    const fetchRuntimeConfig = vi.fn(() => new Promise<void>(() => undefined))
    useMonitoringStore.setState({ fetchRuntimeConfig })
    renderGate(true)

    act(() => vi.advanceTimersByTime(DISPLAY_READONLY_RUNTIME_CONFIG_DEADLINE_MS))

    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.getByText('权限不足')).toBeInTheDocument()
    expect(screen.queryByText('allowed content')).not.toBeInTheDocument()
  })

  it('cancels the readonly runtime-config deadline on unmount', () => {
    vi.useFakeTimers()
    const fetchRuntimeConfig = vi.fn(() => new Promise<void>(() => undefined))
    useMonitoringStore.setState({ fetchRuntimeConfig })
    const { unmount } = render(
      <RBACGate roles={allowedRoles} allowDisplayReadonly>
        <div>allowed content</div>
      </RBACGate>,
    )

    unmount()
    act(() => vi.advanceTimersByTime(DISPLAY_READONLY_RUNTIME_CONFIG_DEADLINE_MS))

    expect(vi.getTimerCount()).toBe(0)
  })

  it('releases a viewer when exact display_readonly config arrives after the deadline', () => {
    vi.useFakeTimers()
    const fetchRuntimeConfig = vi.fn(() => new Promise<void>(() => undefined))
    useMonitoringStore.setState({ fetchRuntimeConfig })
    renderGate(true)

    act(() => vi.advanceTimersByTime(DISPLAY_READONLY_RUNTIME_CONFIG_DEADLINE_MS))
    expect(screen.getByText('权限不足')).toBeInTheDocument()

    act(() => {
      useMonitoringStore.setState({
        runtimeConfig: {
          service_role: 'display_readonly',
          display_readonly: true,
          control_mutations_enabled: false,
          slurm_routes_enabled: false,
          queue_depth_mode: 'display_readonly_unavailable',
        },
      })
    })

    expect(screen.getByText('allowed content')).toBeInTheDocument()
    expect(screen.queryByText('权限不足')).not.toBeInTheDocument()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('allows a viewer only when the caller explicitly permits an exact display_readonly runtime role', () => {
    useMonitoringStore.setState({
      runtimeConfig: {
        service_role: 'display_readonly',
        display_readonly: true,
        control_mutations_enabled: false,
        slurm_routes_enabled: false,
        queue_depth_mode: 'display_readonly_unavailable',
      },
    })
    renderGate(true)

    expect(screen.getByText('allowed content')).toBeInTheDocument()
    expect(screen.queryByText('权限不足')).not.toBeInTheDocument()
  })

  it.each(['compute_control', 'dev_monolith', 'slurm_gateway'] as const)(
    'blocks a viewer when %s falsely claims display_readonly',
    (serviceRole) => {
      useMonitoringStore.setState({
        runtimeConfig: {
          service_role: serviceRole,
          display_readonly: true,
          control_mutations_enabled: false,
          slurm_routes_enabled: false,
          queue_depth_mode: 'display_readonly_unavailable',
        },
      })
      renderGate(true)

      expect(screen.getByText('权限不足')).toBeInTheDocument()
      expect(screen.queryByText('allowed content')).not.toBeInTheDocument()
    },
  )

  it('blocks a viewer when display_readonly role contradicts its matching flag', () => {
    useMonitoringStore.setState({
      runtimeConfig: {
        service_role: 'display_readonly',
        display_readonly: false,
        control_mutations_enabled: false,
        slurm_routes_enabled: false,
        queue_depth_mode: 'display_readonly_unavailable',
      },
    })
    renderGate(true)

    expect(screen.getByText('权限不足')).toBeInTheDocument()
    expect(screen.queryByText('allowed content')).not.toBeInTheDocument()
  })


  it('blocks a viewer after a runtime-config error', () => {
    useMonitoringStore.setState({ runtimeConfigError: 'runtime unavailable' })
    renderGate(true)

    expect(screen.getByText('权限不足')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it.each(['operator', 'model_admin', 'sys_admin'] satisfies AuthRole[])('allows %s role', (role) => {
    useAuthStore.setState({ role })
    renderGate()

    expect(screen.getByText('allowed content')).toBeInTheDocument()
    expect(screen.queryByText('权限不足')).not.toBeInTheDocument()
  })
})

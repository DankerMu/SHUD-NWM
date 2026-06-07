import { render, screen, waitFor } from '@testing-library/react'
import { BrowserRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AppShell } from '@/components/layout/AppShell'
import { useMonitoringStore } from '@/stores/monitoring'

const setRole = vi.fn()

vi.mock('@/stores/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/stores/auth')>()
  return {
    ...actual,
    isRoleOverrideEnabled: false,
    useAuthStore: (selector: (state: { role: 'operator'; setRole: typeof setRole }) => unknown) =>
      selector({ role: 'operator', setRole }),
  }
})

function renderAppShell() {
  render(
    <BrowserRouter>
      <AppShell>
        <div>content</div>
      </AppShell>
    </BrowserRouter>,
  )
}

describe('AppShell production RBAC boundary', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useMonitoringStore.setState({ runtimeConfig: null, runtimeConfigError: null })
  })

  afterEach(() => {
    useMonitoringStore.setState({ runtimeConfig: null, runtimeConfigError: null })
  })

  it('does not expose a local role selector when role override is disabled', () => {
    renderAppShell()

    expect(screen.queryByLabelText('Role')).not.toBeInTheDocument()
    expect(screen.getByText('content')).toBeInTheDocument()
    expect(setRole).not.toHaveBeenCalled()
  })

  it('renders no top navigation links (single-map shell)', () => {
    renderAppShell()

    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /全国总览/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /气象数据/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /内部诊断/ })).not.toBeInTheDocument()
  })

  it('loads runtime config on mount (migrated from NavBar) and skips when already resolved', async () => {
    const fetchRuntimeConfig = vi.fn().mockResolvedValue(undefined)
    useMonitoringStore.setState({ runtimeConfig: null, runtimeConfigError: null, fetchRuntimeConfig })

    renderAppShell()

    await waitFor(() => expect(fetchRuntimeConfig).toHaveBeenCalledTimes(1))
  })

  it('does not refetch runtime config when it is already present', () => {
    const fetchRuntimeConfig = vi.fn().mockResolvedValue(undefined)
    useMonitoringStore.setState({
      runtimeConfig: {
        service_role: 'compute_control',
        control_mutations_enabled: true,
        slurm_routes_enabled: true,
        queue_depth_mode: 'slurm_gateway',
        display_readonly: false,
      },
      runtimeConfigError: null,
      fetchRuntimeConfig,
    })

    renderAppShell()

    expect(fetchRuntimeConfig).not.toHaveBeenCalled()
  })
})

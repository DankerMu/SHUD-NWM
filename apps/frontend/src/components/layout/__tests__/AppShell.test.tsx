import { render, screen } from '@testing-library/react'
import { BrowserRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { AppShell } from '@/components/layout/AppShell'

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

describe('AppShell production RBAC boundary', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('does not expose a local role selector when role override is disabled', () => {
    render(
      <BrowserRouter>
        <AppShell>
          <div>content</div>
        </AppShell>
      </BrowserRouter>,
    )

    expect(screen.queryByLabelText('Role')).not.toBeInTheDocument()
    expect(screen.getByText('content')).toBeInTheDocument()
    expect(setRole).not.toHaveBeenCalled()
  })

  it('shows only implemented workflow navigation entries', () => {
    render(
      <BrowserRouter>
        <AppShell>
          <div>content</div>
        </AppShell>
      </BrowserRouter>,
    )

    expect(screen.getByRole('link', { name: /全国总览/ })).toHaveAttribute('href', '/overview')
    expect(screen.getByRole('link', { name: /水文预报/ })).toHaveAttribute('href', '/forecast')
    expect(screen.getByRole('link', { name: /洪水预警/ })).toHaveAttribute('href', '/flood-alerts')
    expect(screen.getByRole('link', { name: /产品监控/ })).toHaveAttribute('href', '/monitoring')
    expect(screen.queryByText('气象数据')).not.toBeInTheDocument()
    expect(screen.queryByText('系统管理')).not.toBeInTheDocument()
  })
})

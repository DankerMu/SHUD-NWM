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
})

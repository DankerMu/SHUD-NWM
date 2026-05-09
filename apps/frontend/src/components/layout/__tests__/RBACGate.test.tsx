import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'

import { RBACGate } from '@/components/layout/RBACGate'
import { type AuthRole, useAuthStore } from '@/stores/auth'

const allowedRoles: AuthRole[] = ['operator', 'model_admin', 'sys_admin']

function renderGate() {
  render(
    <RBACGate roles={allowedRoles}>
      <div>allowed content</div>
    </RBACGate>,
  )
}

describe('RBACGate', () => {
  beforeEach(() => {
    useAuthStore.setState({ role: 'viewer' })
  })

  it('blocks viewer role', () => {
    renderGate()

    expect(screen.getByText('权限不足')).toBeInTheDocument()
    expect(screen.queryByText('allowed content')).not.toBeInTheDocument()
  })

  it.each(['operator', 'model_admin', 'sys_admin'] satisfies AuthRole[])('allows %s role', (role) => {
    useAuthStore.setState({ role })
    renderGate()

    expect(screen.getByText('allowed content')).toBeInTheDocument()
    expect(screen.queryByText('权限不足')).not.toBeInTheDocument()
  })
})

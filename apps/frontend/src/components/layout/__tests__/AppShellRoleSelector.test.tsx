import { cleanup, render, screen } from '@testing-library/react'
import { BrowserRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { MOBILE_FORM_QUERY, MOBILE_LANDSCAPE_QUERY } from '@/hooks/useMobileForm'
import { useMonitoringStore } from '@/stores/monitoring'

/**
 * 开发用角色切换器在两种形态下的渲染（openspec mobile-responsive-display task 2.5）。
 * `isRoleOverrideEnabled` 是模块级常量，所以每个用例重置模块后按需 mock 再动态导入 AppShell。
 */

const setRole = vi.fn()

async function renderAppShell(roleOverrideEnabled: boolean) {
  vi.resetModules()
  vi.doMock('@/stores/auth', async (importOriginal) => {
    const actual = await importOriginal<typeof import('@/stores/auth')>()
    return {
      ...actual,
      isRoleOverrideEnabled: roleOverrideEnabled,
      useAuthStore: (selector: (state: { role: 'operator'; setRole: typeof setRole }) => unknown) =>
        selector({ role: 'operator', setRole }),
    }
  })
  // resetModules 之后 store 也是新实例：在同一模块图里给它装上不发请求的 fetchRuntimeConfig。
  const { useMonitoringStore: freshStore } = await import('@/stores/monitoring')
  freshStore.setState({ runtimeConfig: null, runtimeConfigError: null, fetchRuntimeConfig: vi.fn().mockResolvedValue(undefined) })
  const { AppShell } = await import('@/components/layout/AppShell')
  return render(
    <BrowserRouter>
      <AppShell>
        <div>content</div>
      </AppShell>
    </BrowserRouter>,
  )
}

function stubMatchMedia(matching: ReadonlySet<string>) {
  window.matchMedia = ((query: string) => ({
    matches: matching.has(query),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => true,
  })) as unknown as typeof window.matchMedia
}

const forms = [
  { name: 'desktop form', matching: new Set<string>(), expected: 'desktop' },
  { name: 'mobile form (portrait)', matching: new Set([MOBILE_FORM_QUERY]), expected: 'mobile' },
  { name: 'mobile form (short landscape)', matching: new Set([MOBILE_FORM_QUERY, MOBILE_LANDSCAPE_QUERY]), expected: 'mobile' },
] as const

describe('AppShell role selector across viewport forms', () => {
  let originalMatchMedia: typeof window.matchMedia

  beforeEach(() => {
    originalMatchMedia = window.matchMedia
    vi.clearAllMocks()
  })

  afterEach(() => {
    cleanup()
    window.matchMedia = originalMatchMedia
    vi.doUnmock('@/stores/auth')
    vi.resetModules()
    useMonitoringStore.setState({ runtimeConfig: null, runtimeConfigError: null })
  })

  for (const form of forms) {
    it(`does not render the selector when role override is disabled — ${form.name}`, async () => {
      stubMatchMedia(form.matching)
      const { container } = await renderAppShell(false)

      // 前提：桩确实把外壳带进了要测的形态。
      expect(container.querySelector('[data-viewport-form]')).toHaveAttribute('data-viewport-form', form.expected)
      expect(screen.queryByLabelText('Role')).not.toBeInTheDocument()
      expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
      expect(screen.getByText('content')).toBeInTheDocument()
      expect(setRole).not.toHaveBeenCalled()
    })
  }

  it('keeps the desktop placement and width when role override is enabled in desktop form', async () => {
    stubMatchMedia(new Set())
    await renderAppShell(true)

    const trigger = screen.getByLabelText('Role')
    expect(trigger).toHaveTextContent('Operator')
    expect(trigger).toHaveClass('w-36')
    expect(trigger).not.toHaveClass('w-14')
    // 定位容器的类名逐字等于改动前的桌面类名。
    expect(trigger.parentElement?.className).toBe('absolute right-4 top-4 z-30')
  })

  it('renders a compact left-edge trigger that keeps the role name in the DOM in mobile form', async () => {
    stubMatchMedia(new Set([MOBILE_FORM_QUERY]))
    await renderAppShell(true)

    const trigger = screen.getByLabelText('Role')
    expect(trigger).toHaveTextContent('Operator')
    expect(trigger).toHaveClass('w-14')
    expect(trigger).not.toHaveClass('w-36')
    const wrapper = trigger.parentElement
    expect(wrapper).toHaveClass('absolute', 'left-0', 'top-1/2', '-translate-y-1/2', 'z-[200]')
    expect(wrapper).not.toHaveClass('right-4')
    expect(wrapper).not.toHaveClass('top-4')
    expect(wrapper).not.toHaveClass('z-30')
  })
})

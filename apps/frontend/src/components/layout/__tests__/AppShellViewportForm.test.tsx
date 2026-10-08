import { render } from '@testing-library/react'
import { BrowserRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AppShell } from '@/components/layout/AppShell'
import { MOBILE_FORM_QUERY, MOBILE_LANDSCAPE_QUERY } from '@/hooks/useMobileForm'
import { useMonitoringStore } from '@/stores/monitoring'

vi.mock('@/stores/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/stores/auth')>()
  return { ...actual, isRoleOverrideEnabled: false }
})

type ChangeListener = (event: MediaQueryListEvent) => void

function renderAppShell() {
  return render(
    <BrowserRouter>
      <AppShell>
        <div>content</div>
      </AppShell>
    </BrowserRouter>,
  )
}

function shellRoot(container: HTMLElement) {
  const roots = container.querySelectorAll('[data-viewport-form]')
  expect(roots).toHaveLength(1)
  return roots[0] as HTMLElement
}

describe('AppShell viewport-form markers', () => {
  let originalMatchMedia: typeof window.matchMedia

  beforeEach(() => {
    originalMatchMedia = window.matchMedia
    useMonitoringStore.setState({
      runtimeConfig: null,
      runtimeConfigError: null,
      fetchRuntimeConfig: vi.fn().mockResolvedValue(undefined),
    })
  })

  afterEach(() => {
    window.matchMedia = originalMatchMedia
    useMonitoringStore.setState({ runtimeConfig: null, runtimeConfigError: null })
  })

  it('renders desktop / false under the global always-false matchMedia stub', () => {
    const { container } = renderAppShell()
    const root = shellRoot(container)

    expect(root).toHaveAttribute('data-viewport-form', 'desktop')
    expect(root).toHaveAttribute('data-viewport-short-landscape', 'false')
    // 根节点就是外壳最外层容器（既有布局类逐字保留，只追加）。
    expect(root).toBe(container.firstElementChild)
    expect(root.className.startsWith('relative flex h-dvh w-full flex-col overflow-hidden bg-background text-foreground')).toBe(true)
  })

  it('reflects the hook result on the root when the queries match', () => {
    const matching = new Set<string>([MOBILE_FORM_QUERY])
    window.matchMedia = ((query: string) => ({
      matches: matching.has(query),
      media: query,
      onchange: null,
      addEventListener: (_type: string, _listener: ChangeListener) => {},
      removeEventListener: (_type: string, _listener: ChangeListener) => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => true,
    })) as unknown as typeof window.matchMedia

    const portrait = renderAppShell()
    expect(shellRoot(portrait.container)).toHaveAttribute('data-viewport-form', 'mobile')
    expect(shellRoot(portrait.container)).toHaveAttribute('data-viewport-short-landscape', 'false')
    portrait.unmount()

    matching.add(MOBILE_LANDSCAPE_QUERY)
    const landscape = renderAppShell()
    expect(shellRoot(landscape.container)).toHaveAttribute('data-viewport-form', 'mobile')
    expect(shellRoot(landscape.container)).toHaveAttribute('data-viewport-short-landscape', 'true')
  })
})

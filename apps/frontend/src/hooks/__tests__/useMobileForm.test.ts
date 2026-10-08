import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { MOBILE_FORM_QUERY, MOBILE_LANDSCAPE_QUERY, useMobileForm } from '@/hooks/useMobileForm'

type ChangeListener = (event: MediaQueryListEvent) => void

/** 可控 matchMedia 桩：按查询字符串记录取值与监听，用例可单独对某一条查询派发 change。 */
function installControllableMatchMedia(initial: Record<string, boolean>) {
  const matches = new Map<string, boolean>(Object.entries(initial))
  const listeners = new Map<string, Set<ChangeListener>>()
  const requested: string[] = []

  window.matchMedia = ((query: string) => {
    requested.push(query)
    if (!listeners.has(query)) listeners.set(query, new Set())
    const own = listeners.get(query) as Set<ChangeListener>
    return {
      get matches() {
        return matches.get(query) ?? false
      },
      media: query,
      onchange: null,
      addEventListener: (type: string, listener: ChangeListener) => {
        if (type === 'change') own.add(listener)
      },
      removeEventListener: (type: string, listener: ChangeListener) => {
        if (type === 'change') own.delete(listener)
      },
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => true,
    } as unknown as MediaQueryList
  }) as typeof window.matchMedia

  return {
    requested,
    listenerCount: (query: string) => listeners.get(query)?.size ?? 0,
    set(query: string, value: boolean) {
      matches.set(query, value)
      for (const listener of [...(listeners.get(query) ?? [])]) {
        listener({ matches: value, media: query } as MediaQueryListEvent)
      }
    },
  }
}

describe('useMobileForm', () => {
  let originalMatchMedia: typeof window.matchMedia

  beforeEach(() => {
    originalMatchMedia = window.matchMedia
  })

  afterEach(() => {
    window.matchMedia = originalMatchMedia
  })

  it('exports the D1 media-query strings', () => {
    expect(MOBILE_FORM_QUERY).toBe('(max-width: 767.98px), (max-height: 499.98px)')
    expect(MOBILE_LANDSCAPE_QUERY).toBe('(max-height: 499.98px) and (orientation: landscape)')
  })

  it.each([
    { mobile: false, landscape: false },
    { mobile: true, landscape: false },
    { mobile: true, landscape: true },
  ])('reads the initial value of both queries on first render: %o', (expected) => {
    const media = installControllableMatchMedia({
      [MOBILE_FORM_QUERY]: expected.mobile,
      [MOBILE_LANDSCAPE_QUERY]: expected.landscape,
    })

    const { result } = renderHook(() => useMobileForm())

    expect(result.current).toEqual(expected)
    expect(media.requested).toContain(MOBILE_FORM_QUERY)
    expect(media.requested).toContain(MOBILE_LANDSCAPE_QUERY)
  })

  it('updates `mobile` when only the mobile query changes', () => {
    const media = installControllableMatchMedia({})
    const { result } = renderHook(() => useMobileForm())
    expect(result.current).toEqual({ mobile: false, landscape: false })

    act(() => media.set(MOBILE_FORM_QUERY, true))
    expect(result.current).toEqual({ mobile: true, landscape: false })

    act(() => media.set(MOBILE_FORM_QUERY, false))
    expect(result.current).toEqual({ mobile: false, landscape: false })
  })

  it('updates `landscape` when only the landscape query changes', () => {
    const media = installControllableMatchMedia({ [MOBILE_FORM_QUERY]: true })
    const { result } = renderHook(() => useMobileForm())
    expect(result.current).toEqual({ mobile: true, landscape: false })

    act(() => media.set(MOBILE_LANDSCAPE_QUERY, true))
    expect(result.current).toEqual({ mobile: true, landscape: true })

    act(() => media.set(MOBILE_LANDSCAPE_QUERY, false))
    expect(result.current).toEqual({ mobile: true, landscape: false })
  })

  it('subscribes to both queries and removes both listeners on unmount', () => {
    const media = installControllableMatchMedia({})
    const { result, unmount } = renderHook(() => useMobileForm())

    expect(media.listenerCount(MOBILE_FORM_QUERY)).toBe(1)
    expect(media.listenerCount(MOBILE_LANDSCAPE_QUERY)).toBe(1)

    unmount()

    expect(media.listenerCount(MOBILE_FORM_QUERY)).toBe(0)
    expect(media.listenerCount(MOBILE_LANDSCAPE_QUERY)).toBe(0)
    // 卸载后再派发 change 不得再驱动状态。
    media.set(MOBILE_FORM_QUERY, true)
    media.set(MOBILE_LANDSCAPE_QUERY, true)
    expect(result.current).toEqual({ mobile: false, landscape: false })
  })

  it('falls back to desktop without throwing when matchMedia is missing', () => {
    ;(window as { matchMedia?: unknown }).matchMedia = undefined

    const { result, unmount } = renderHook(() => useMobileForm())

    expect(result.current).toEqual({ mobile: false, landscape: false })
    expect(() => unmount()).not.toThrow()
  })

  it('falls back to desktop without throwing when matchMedia throws', () => {
    window.matchMedia = (() => {
      throw new Error('matchMedia unsupported')
    }) as typeof window.matchMedia

    const { result, unmount } = renderHook(() => useMobileForm())

    expect(result.current).toEqual({ mobile: false, landscape: false })
    expect(() => unmount()).not.toThrow()
  })
})

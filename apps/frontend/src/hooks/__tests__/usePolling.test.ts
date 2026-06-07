import { renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { usePolling } from '@/hooks/usePolling'

describe('usePolling', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('invokes the callback once on mount when enabled', async () => {
    const callback = vi.fn().mockResolvedValue(undefined)
    renderHook(() => usePolling(callback, 10_000, true))
    await vi.waitFor(() => expect(callback).toHaveBeenCalledTimes(1))
  })

  it('does not invoke the callback when disabled', () => {
    const callback = vi.fn()
    renderHook(() => usePolling(callback, 10_000, false))
    expect(callback).not.toHaveBeenCalled()
  })

  it('clears the armed timer on unmount and stops polling', async () => {
    const callback = vi.fn().mockResolvedValue(undefined)
    const clearTimeoutSpy = vi.spyOn(window, 'clearTimeout')
    const { unmount } = renderHook(() => usePolling(callback, 10_000, true))
    // 首拍成功后会 re-arm 一个 10s 定时器（正常行为）。
    await vi.waitFor(() => expect(callback).toHaveBeenCalledTimes(1))
    await Promise.resolve()
    unmount()
    // 卸载清掉已排的定时器，之后不再触发回调。
    expect(clearTimeoutSpy).toHaveBeenCalled()
    const callsAfterUnmount = callback.mock.calls.length
    await Promise.resolve()
    expect(callback.mock.calls.length).toBe(callsAfterUnmount)
  })

  it('does not throw "document is not defined" nor re-arm when the document global is gone as an in-flight callback resolves (jsdom teardown race)', async () => {
    let resolveInFlight: () => void = () => {}
    const callback = vi.fn().mockImplementation(
      () => new Promise<void>((resolve) => {
        resolveInFlight = () => resolve()
      }),
    )
    const setTimeoutSpy = vi.spyOn(window, 'setTimeout')
    renderHook(() => usePolling(callback, 10_000, true))
    await vi.waitFor(() => expect(callback).toHaveBeenCalledTimes(1))

    // 模拟测试文件 jsdom teardown：in-flight 回调 resolve 时全局 document 已被删除。
    const realDocument = globalThis.document
    const unhandled: unknown[] = []
    const onUnhandled = (event: PromiseRejectionEvent) => {
      unhandled.push(event.reason)
      event.preventDefault()
    }
    window.addEventListener('unhandledrejection', onUnhandled)
    try {
      // @ts-expect-error 故意制造 document 缺失以触发竞态。
      delete globalThis.document
      resolveInFlight()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    } finally {
      globalThis.document = realDocument
      window.removeEventListener('unhandledrejection', onUnhandled)
    }

    // 修复前：finally 裸读 document.hidden → ReferenceError: document is not defined（unhandled）。
    expect(unhandled.some((reason) => String(reason).includes('document is not defined'))).toBe(false)
    // teardown 后不得再排下一拍（无存活 document 不重排）。
    expect(setTimeoutSpy.mock.calls.some(([, delay]) => delay === 10_000)).toBe(false)
  })
})

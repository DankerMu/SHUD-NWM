import { useEffect, useRef } from 'react'

/**
 * 异步续延（await 之后的 finally / 排队的 tick）可能在测试 jsdom 卸载后、或 SSR 下运行，
 * 此时全局 `document` 不存在。裸访问 `document.hidden` 会抛 `ReferenceError: document is not defined`，
 * 把全部通过的测试判成失败（CI flaky）。无 document 时视为"可见"，不阻断也不崩。
 */
function documentHidden() {
  return typeof document !== 'undefined' && document.hidden
}

export function usePolling(
  callback: () => Promise<void> | void,
  intervalMs = 10_000,
  enabled = true,
) {
  const callbackRef = useRef(callback)
  const inFlightRef = useRef(false)
  callbackRef.current = callback

  useEffect(() => {
    if (!enabled) return

    let timerId: number | undefined
    let stopped = false

    const clearTimer = () => {
      if (timerId !== undefined) {
        window.clearTimeout(timerId)
        timerId = undefined
      }
    }

    const tick = async () => {
      clearTimer()
      if (stopped || documentHidden() || inFlightRef.current) return

      inFlightRef.current = true
      try {
        await callbackRef.current()
      } catch {
        console.error('Polling callback failed')
      } finally {
        inFlightRef.current = false
        // 仅在环境仍存活（document 在）且可见时重排下一拍——卸载/teardown 后不再排程。
        if (!stopped && typeof document !== 'undefined' && !document.hidden) {
          timerId = window.setTimeout(tick, intervalMs)
        }
      }
    }

    const handleVisibilityChange = () => {
      if (documentHidden()) {
        clearTimer()
        return
      }

      if (!inFlightRef.current) void tick()
    }

    document.addEventListener('visibilitychange', handleVisibilityChange)
    void tick()

    return () => {
      stopped = true
      inFlightRef.current = false
      clearTimer()
      document.removeEventListener('visibilitychange', handleVisibilityChange)
    }
  }, [enabled, intervalMs])
}

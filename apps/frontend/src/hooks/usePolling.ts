import { useEffect, useRef } from 'react'

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
      if (stopped || document.hidden || inFlightRef.current) return

      inFlightRef.current = true
      try {
        await callbackRef.current()
      } catch {
        console.error('Polling callback failed')
      } finally {
        inFlightRef.current = false
        if (!stopped && !document.hidden) {
          timerId = window.setTimeout(tick, intervalMs)
        }
      }
    }

    const handleVisibilityChange = () => {
      if (document.hidden) {
        clearTimer()
        return
      }

      if (!inFlightRef.current) void tick()
    }

    document.addEventListener('visibilitychange', handleVisibilityChange)
    void tick()

    return () => {
      stopped = true
      clearTimer()
      document.removeEventListener('visibilitychange', handleVisibilityChange)
    }
  }, [enabled, intervalMs])
}

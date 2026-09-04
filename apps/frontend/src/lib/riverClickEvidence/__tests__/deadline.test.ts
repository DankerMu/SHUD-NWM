import { describe, expect, it, vi } from 'vitest'

import { createRiverClickDeadline, withRiverClickDeadline } from '../deadline'

const HARD_TIMEOUT = 2500

describe('river-click bounded deadline helper', () => {
  it('resolves the wrapped value when the promise settles before the deadline', async () => {
    const deadline = createRiverClickDeadline(1000, () => 10)
    const result = await withRiverClickDeadline(Promise.resolve('ok'), deadline, () => 'expired')
    expect(result).toBe('ok')
  })

  it('rejects with the original error when the promise rejects before the deadline', async () => {
    const deadline = createRiverClickDeadline(1000, () => 10)
    await expect(
      withRiverClickDeadline(Promise.reject(new Error('boom')), deadline, () => 'expired'),
    ).rejects.toThrow('boom')
  })

  it('resolves the onExpired value when the deadline expires first', async () => {
    let now = 0
    const deadline = createRiverClickDeadline(50, () => now)
    const promise = withRiverClickDeadline(new Promise<never>(() => undefined), deadline, () => 'expired-value')
    now = 60
    await expect(promise).resolves.toBe('expired-value')
  })

  it('rejects (does not hang or escape) when onExpired throws inside the timer callback', async () => {
    let now = 0
    const deadline = createRiverClickDeadline(50, () => now)
    const promise = withRiverClickDeadline<never>(
      new Promise<never>(() => undefined),
      deadline,
      () => {
        throw new Error('expiry-throw')
      },
    )
    now = 60
    await expect(promise).rejects.toThrow('expiry-throw')
  }, HARD_TIMEOUT)

  it('clears the timer exactly once and does not double-settle when promise and deadline race', async () => {
    vi.useFakeTimers()
    try {
      let now = 0
      const deadline = createRiverClickDeadline(100, () => now)
      let resolvePromise: ((value: string) => void) | null = null
      const promise = withRiverClickDeadline(
        new Promise<string>((resolve) => {
          resolvePromise = resolve
        }),
        deadline,
        () => 'expired',
      )
      // Deadline expires first: the timer callback settles with 'expired'.
      now = 200
      await vi.advanceTimersByTimeAsync(100)
      await expect(promise).resolves.toBe('expired')
      // A late promise resolution must NOT double-settle or throw.
      resolvePromise!('late-value')
      await Promise.resolve()
      await expect(promise).resolves.toBe('expired')
    } finally {
      vi.useRealTimers()
    }
  }, HARD_TIMEOUT)
})

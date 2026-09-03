/**
 * Monotonic bounded-deadline utility for the river-click live lane (#1970).
 * Pure module importable from Node (Playwright) without the `@/` alias.
 */

export interface RiverClickDeadline {
  /** Monotonic now() in ms as of the deadline creation. */
  readonly createdMs: number
  /** Absolute deadline timestamp on the same monotonic clock. */
  readonly absoluteMs: number
  /** Remaining whole milliseconds from now (>= 0). */
  remaining(): number
  /** True when now() >= absoluteMs. */
  expired(): boolean
  /** Now() value at the current moment. */
  now(): number
}

/**
 * One monotonic absolute deadline shared across preflight/page/sample work.
 * `now` defaults to performance.now() (monotonic, fractional); tests inject
 * their own. A deadline is created BEFORE the first preflight fetch and passed
 * as a value through the whole lane; no independent Date.now()+N resets.
 */
export function createRiverClickDeadline(
  budgetMs: number,
  now: () => number = () => performance.now(),
  startedAt: number | null = null,
): RiverClickDeadline {
  const created = startedAt ?? now()
  const absolute = created + Math.max(0, budgetMs)
  return {
    createdMs: created,
    absoluteMs: absolute,
    remaining: () => Math.max(0, absolute - now()),
    expired: () => now() >= absolute,
    now: () => now(),
  }
}

/** Instantiate a new deadline with a stored clock (identical clock semantics). */
export function derivedRiverClickDeadline(parent: RiverClickDeadline, budgetMs: number): RiverClickDeadline {
  return createRiverClickDeadline(budgetMs, () => parent.now(), parent.now())
}

/**
 * Race `promise` against the deadline; resolves the promise's value or calls
 * `onExpired()` when the deadline expires first. If `onExpired` throws inside
 * the timer callback the returned promise rejects with that error (never a
 * pending promise or an uncaught exception). The timer is cleared exactly once
 * on every settle path.
 */
export function withRiverClickDeadline<T>(
  promise: Promise<T>,
  deadline: RiverClickDeadline,
  onExpired: () => T,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    let settled = false
    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      try {
        resolve(onExpired())
      } catch (error) {
        reject(error)
      }
    }, Math.max(0, deadline.remaining()))
    promise.then(
      (value) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        resolve(value)
      },
      (error) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        reject(error)
      },
    )
  })
}

/**
 * Shared faithful fake Playwright page for the river-click lane unit tests.
 *
 * JSHandle seam: each evaluateHandle creates ONE handle wrapping ONLY the
 * object its expression resolved to (the hook for the window.__nhmsRiverClickEvidence
 * expression, the map node for the m11-map-surface query) — never a synthetic
 * {hook,map}. Handle arguments passed to evaluate are resolved recursively to
 * the underlying objects (real Playwright injects JSHandles as actual page-side
 * objects). Dispose calls are counted on the state so tests can prove exactly
 * two disposals per attempt.
 */

import { vi } from 'vitest'

import type { RiverClickJsHandle, RiverClickLanePageSurface } from '../../playwright.river-click-lane-preflight'

export interface RiverClickFakePageState {
  listeners: Record<string, Array<(arg: unknown) => void>>
  evaluateNames: string[]
  evaluateImpl: (text: string) => unknown
  closeImpl?: (captured: unknown) => unknown
  sleepMs?: number
  /** Invoked when the quiet wait begins (long waitForTimeout call). */
  onQuiet?: () => void
  /** Count of JSHandle.dispose() calls (both handles per attempt). Optional
   *  with a zero default so hand-written literals stay valid; the factory
   *  always sets it. */
  handleDisposals?: number
  /** Number of evaluateHandle calls (one per captured object). Optional with a
   *  zero default, same rationale. */
  handleCaptures?: number
  /** When set, each dispose awaits this many ms before counting: proves the
   *  attempt AWAITS disposal completion before settling. */
  deferredDisposeMs?: number
}

/** Create a fresh fake state with the required defaults. */
export function makeFakePageState(): RiverClickFakePageState {
  return {
    listeners: { request: [], response: [], requestfailed: [] },
    evaluateNames: [],
    evaluateImpl: () => undefined,
    sleepMs: 1,
    handleDisposals: 0,
    handleCaptures: 0,
  }
}

export function makeFakePage(state: RiverClickFakePageState): RiverClickLanePageSurface {
  const handleRegistry = new Map<object, { value: unknown; disposed: boolean }>()
  const makeHandle = (value: unknown): RiverClickJsHandle => {
    const record = { value, disposed: false }
    const handle = {
      dispose: async () => {
        if (state.deferredDisposeMs !== undefined) {
          await new Promise((resolve) => setTimeout(resolve, state.deferredDisposeMs))
        }
        record.disposed = true
        state.handleDisposals = (state.handleDisposals ?? 0) + 1
      },
    } as unknown as object
    handleRegistry.set(handle, record)
    return handle as unknown as RiverClickJsHandle
  }
  const unwrap = (value: unknown): unknown => {
    if (typeof value !== 'object' || value === null) return value
    const record = handleRegistry.get(value as object)
    if (record !== undefined) return record.value
    if (Array.isArray(value)) return value.map(unwrap)
    const out: Record<string, unknown> = {}
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      out[key] = unwrap(item)
    }
    return out
  }
  return {
    goto: vi.fn(async () => undefined),
    addInitScript: vi.fn(async () => undefined),
    waitForTimeout: vi.fn(async (ms: number) => {
      if (state.onQuiet && ms >= 100) state.onQuiet()
      if (state.sleepMs) await new Promise((resolve) => setTimeout(resolve, state.sleepMs))
    }),
    evaluate: vi.fn(async (expr: unknown, ...args: unknown[]) => {
      // Faithful Playwright semantics (frame.evaluate -> isFunction):
      // - function expression: CALLED in the page with the single argument and
      //   its RETURN VALUE is returned (a function that merely returns a script
      //   string resolves to that string, never to the script's result);
      // - string expression: the script itself (return value = the script's
      //   completion value), routed through evaluateImpl as the browser stand-in.
      if (typeof expr === 'function') {
        const fn = expr as (arg: unknown) => unknown
        const source = String(fn)
        if (source.includes('m11-river-forecast-panel')) {
          state.evaluateNames.push('timeoutMs m11-map-surface closeFn')
          if (state.closeImpl) return state.closeImpl(unwrap(args[0]))
          return state.evaluateImpl('timeoutMs m11-map-surface closeFn')
        }
        const value = args.length > 0 ? fn(unwrap(args[0])) : fn(undefined)
        state.evaluateNames.push(String(value))
        return value
      }
      state.evaluateNames.push(String(expr))
      return state.evaluateImpl(String(expr))
    }) as never,
    evaluateHandle: vi.fn(async (expr: unknown, ...args: unknown[]) => {
      state.handleCaptures = (state.handleCaptures ?? 0) + 1
      state.evaluateNames.push('handle-capture')
      if (typeof expr === 'function') {
        const fn = expr as (arg: unknown) => unknown
        return makeHandle(fn(args.length > 0 ? args[0] : undefined))
      }
      const script = String(expr)
      if (script.includes('window.__nhmsRiverClickEvidence')) {
        return makeHandle((window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence)
      }
      return makeHandle(document.querySelector('[data-testid="m11-map-surface"]'))
    }) as never,
    on: vi.fn((event: string, listener: (arg: unknown) => void) => {
      state.listeners[event].push(listener)
    }) as never,
    off: vi.fn((event: string, listener: (arg: unknown) => void) => {
      const index = state.listeners[event].indexOf(listener)
      if (index >= 0) state.listeners[event].splice(index, 1)
    }) as never,
    requests: vi.fn(() => []) as never,
  }
}

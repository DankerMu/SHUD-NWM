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
 *
 * Close routing is identity-based: only the imported production helper
 * `closeRiverClickPanelInPage` may be diverted to `closeImpl`. Source text and
 * function names are never consulted.
 *
 * Real-click seam: `page.mouse.click(x, y)` records the click and runs
 * `onMouseClick` (the test's stand-in for the product reacting to the click,
 * e.g. issuing the series requests). While the page-side capture is armed the
 * click also records one TRUSTED pointer-down at (x, y) stamped
 * `pointerTimeStamp` — exactly what the browser delivers for real input. The
 * exact arm/take capture scripts fall back to that page-side stand-in when
 * `evaluateImpl` returns undefined for them (the hook's classification:
 * unarmed/none -> HOOK_POINTER_MISSING, more than one -> HOOK_POINTER_INVALID);
 * a test overrides either by returning a value (or throwing) from evaluateImpl.
 */

import { vi } from 'vitest'

import {
  closeRiverClickPanelInPage,
  RIVER_CLICK_ARM_CAPTURE_SCRIPT,
  RIVER_CLICK_TAKE_CAPTURE_SCRIPT,
} from '../../playwright.river-click-lane-attempt'
import type { RiverClickJsHandle, RiverClickLanePageSurface } from '../../playwright.river-click-lane-preflight'

export interface RiverClickFakePageState {
  listeners: Record<string, Array<(arg: unknown) => void>>
  evaluateNames: string[]
  evaluateImpl: (text: string) => unknown
  /** Explicit state-machine close outcome. Routed only when evaluate is the
   *  production `closeRiverClickPanelInPage` function identity. */
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
  /** Every page.mouse.click(x, y) in call order. */
  mouseClicks?: Array<{ x: number; y: number }>
  /** Runs after each recorded mouse click (the product's reaction). */
  onMouseClick?: (x: number, y: number) => void
  /** timeStamp of the trusted pointer-down a click produces (default 1000). */
  pointerTimeStamp?: number
  /** Page-side capture stand-in state (null = not armed). */
  pointerCapture?: { events: Array<{ timeStamp: number; clientX: number; clientY: number }> } | null
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
    mouseClicks: [],
    pointerCapture: null,
  }
}

/** The hook's take classification over the fake page's recorded pointer-downs. */
function takeFakePointerCapture(state: RiverClickFakePageState): unknown {
  const armed = state.pointerCapture ?? null
  state.pointerCapture = null
  if (armed === null || armed.events.length === 0) return { error: 'HOOK_POINTER_MISSING' }
  if (armed.events.length > 1) return { error: 'HOOK_POINTER_INVALID' }
  return { ...armed.events[0], isTrusted: true }
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
        const captured = unwrap(args[0])
        if (fn === closeRiverClickPanelInPage) {
          state.evaluateNames.push('timeoutMs m11-map-surface closeFn')
          if (state.closeImpl) return state.closeImpl(captured)
          return fn(captured)
        }
        const value = args.length > 0 ? fn(captured) : fn(undefined)
        state.evaluateNames.push(String(value))
        return value
      }
      const script = String(expr)
      state.evaluateNames.push(script)
      const value = state.evaluateImpl(script)
      if (value === undefined && script === RIVER_CLICK_ARM_CAPTURE_SCRIPT) {
        state.pointerCapture = { events: [] }
        return undefined
      }
      if (value === undefined && script === RIVER_CLICK_TAKE_CAPTURE_SCRIPT) return takeFakePointerCapture(state)
      return value
    }) as never,
    mouse: {
      click: vi.fn(async (x: number, y: number) => {
        state.evaluateNames.push('mouse-click')
        if (!state.mouseClicks) state.mouseClicks = []
        state.mouseClicks.push({ x, y })
        state.pointerCapture?.events.push({ timeStamp: state.pointerTimeStamp ?? 1000, clientX: x, clientY: y })
        state.onMouseClick?.(x, y)
      }),
    },
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

/** The located identity + viewport point the fake page-side hook resolves by default. */
export const FAKE_LOCATED = {
  basinId: 'basins_qhh',
  riverSegmentId: 'seg-001',
  basinVersionId: 'bv-001',
  riverNetworkVersionId: 'rn-001',
  clientX: 412.5,
  clientY: 318,
}

/**
 * Page-side stand-in for one successful locate (the in-page wrapper outcome
 * `{ok:true, value}`). The following REAL click (fake page.mouse.click)
 * records a trusted pointer-down stamped `t0` and runs `onClick` — the product
 * reacting to the click (e.g. the two series requests).
 */
export function fakeLocateThenClick(
  state: RiverClickFakePageState,
  options: { t0?: number; onClick?: () => void; identity?: Partial<typeof FAKE_LOCATED> } = {},
) {
  state.pointerTimeStamp = options.t0 ?? 1000
  state.onMouseClick = options.onClick
  return { ok: true, value: { ...FAKE_LOCATED, ...options.identity } }
}

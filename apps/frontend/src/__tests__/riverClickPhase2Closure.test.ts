import { describe, expect, it, vi } from 'vitest'
import { mkdtempSync, rmSync, readFileSync, chmodSync, realpathSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

import {
  readResponseBounded,
  type RiverClickLaneBrowserRequest,
  type RiverClickLaneBrowserResponse,
} from '../../playwright.river-click-lane-preflight'
import { runRiverClickAttempt, riverClickChartStateScript, type RiverClickAttemptOptions } from '../../playwright.river-click-lane-attempt'
import {
  runRiverClickLane,
  type RiverClickLaneEnv,
  type RiverClickLaneIdentity,
  type RiverClickLanePageSurface,
} from '../../playwright.river-click-lane'
import { createRiverClickEvidenceHook, createRiverClickHookController, selectRenderedRiverFeature, type RiverClickHookMap } from '../lib/riverClickEvidence/hook'
import { validateRiverClickEvidenceDocument } from '../lib/riverClickEvidence/receipt'
import { parseRiverClickConfig } from '../lib/riverClickEvidence/config'
import { runRiverClickLiveEvidenceOwner } from '../../playwright.river-click-evidence-owner'
import { publishRiverClickEvidence } from '../../playwright.river-click-evidence'
import { publishRiverClickTerminal } from '../../playwright.river-click-terminal'
import { createRiverClickDeadline } from '../lib/riverClickEvidence/deadline'
import { makeFakePage as pageOf, makeFakePageState as makeState, type RiverClickFakePageState as FakeState } from '../test/riverClickFakePage'

const repoRoot = path.resolve(__dirname, '../../../../')

/**
 * Phase-2 closure discriminants. Each test here is a RED test against the
 * pre-fix source: it asserts the exact load-bearing behavior the fixture
 * requires, so a fix that merely renames a constant fails it.
 */

function validConfigEnv() {
  return {
    PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test',
    PLAYWRIGHT_LIVE_API_BASE_URL: 'https://api.example.test',
    PLAYWRIGHT_LIVE_RIVER_BASIN_ID: 'basins_qhh',
    PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID: 'seg-001',
    PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: '/private/ev/nhms-frontend-river-click-live-evidence-a.json',
  } as Record<string, string>
}

describe('phase2 closure: actual entrypoint owns the pre-browser decision', () => {
  it('the live profile wires a REAL globalSetup that invokes the pre-browser owner before ANY browser fixture', () => {
    const config = readFileSync(path.join(repoRoot, 'apps/frontend/playwright.live-display.config.ts'), 'utf8')
    expect(config).toMatch(/globalSetup:\s*'\.\/playwright\.live-display\.global-setup\.ts'/)
    const setup = readFileSync(path.join(repoRoot, 'apps/frontend/playwright.live-display.global-setup.ts'), 'utf8')
    expect(setup).toMatch(/runRiverClickLiveEvidenceOwner/)
    expect(setup).toMatch(/publishRiverClickEvidence/)
    // The page-fixture test must NOT own the pre-browser decision anymore: the
    // spec consumes already-vetted config (parse only), no owner call.
    const spec = readFileSync(path.join(repoRoot, 'apps/frontend/e2e/live-display.spec.ts'), 'utf8')
    expect(spec).not.toMatch(/runRiverClickLiveEvidenceOwner/)
    expect(spec).toMatch(/parseRiverClickConfig\(process\.env\)/)
  })

  it('owner publishes one BLOCKED receipt via the REAL publisher into a mode-0700 parent when URLs are missing', async () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-river-p2-')))
    chmodSync(parent, 0o700)
    try {
      const receiptPath = path.join(parent, 'nhms-frontend-river-click-live-evidence-phase2.json')
      const env = validConfigEnv()
      delete env.PLAYWRIGHT_LIVE_BASE_URL
      env.PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH = receiptPath
      const result = await runRiverClickLiveEvidenceOwner(env, {
        publish: (p, receipt) => publishRiverClickEvidence(p, receipt),
      })
      expect(result.ok).toBe(false)
      if (!result.ok) expect(result.receiptWritten).toBe(true)
      const document = JSON.parse(readFileSync(receiptPath, 'utf8'))
      expect(document.status).toBe('BLOCKED')
      expect(document.failure.code).toBe('REQUIRED_ENV_MISSING')
      expect(validateRiverClickEvidenceDocument(document).ok).toBe(true)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })
})

describe('phase2 closure: one absolute whole-run deadline', () => {
  it('lane accepts an externally created absolute deadline and enforces it through the terminal', async () => {
    // A lane that would otherwise complete (each attempt records t0=1000,
    // t1=1500 -> duration 500 < 2000) MUST fail with WHOLE_RUN_TIMEOUT when the
    // caller supplies an absolute 100ms deadline on a clock that advances 60ms
    // per dispatch: the injected deadline is the one object enforced from
    // preflight through receipt construction. If the lane ignored the injected
    // deadline and used its own long budget, this run would reach PASS.
    const state = makeState()
    let elapsed = 0
    let attemptCount = 0
    state.evaluateImpl = (text: string) => {
      if (text.includes('typeof window.__nhmsRiverClickEvidence.selectRenderedRiver')) return true
      if (text.includes('selectRenderedRiver')) {
        attemptCount += 1
        elapsed += 60
        emitSeriesPair(state, () => Promise.resolve(null))
        return { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', dispatchNowMs: 1000 + attemptCount * 0.5 }
      }
      if (text.includes('m11-river-panel-chart')) {
        if (text.includes('chartVisible')) return { chart: true, chartVisible: true, partial: false, empty: false }
        return { chart: true, partial: false, empty: false }
      }
      if (text.includes('timeoutMs') && text.includes('m11-map-surface')) {
        return { closed: true, mapPresent: true, mapSame: true, hookSame: true }
      }
      if (text.includes('performance.now()')) return 1500
      return undefined
    }
    const page = pageOf(state)
    const result = await runRiverClickLane(
      { config: config(), page } as RiverClickLaneEnv,
      defaultFetch() as never,
      { deadline: createRiverClickDeadline(100, () => elapsed), attemptDeadlineMs: 2_000, pollMs: 2, quietMs: 30 },
    )
    if (result.ok) throw new Error('must fail but got ok with p95=' + String(result.terminal.p95Ms))
    if (result.terminal.failure === null) throw new Error('failure terminal must carry a classification')
    expect(result.terminal.failure.code).toBe('WHOLE_RUN_TIMEOUT')
    // The injected deadline was enforced mid-run: the warmup completed and is
    // retained on the terminal, and the fake clock was never allowed to run
    // anywhere near the lane's own 360s budget.
    expect(result.terminal.warmup).not.toBeNull()
    expect(elapsed).toBeLessThan(360_000)
  })

  it('a shortened quiet interval is refused instead of being accepted on expiry', async () => {
    // A quietMs request larger than the sample budget must fail closed with a
    // timeout, never silently succeed with a clipped quiet.
    const state = makeState()
    let hookCalls = 0
    state.evaluateImpl = (text: string) => {
      if (text.includes('selectRenderedRiver')) {
        hookCalls += 1
        emitSeriesPair(state, () => Promise.resolve(null))
        return { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', dispatchNowMs: 1000 }
      }
      if (text.includes('m11-river-panel-chart')) return { chart: true, chartVisible: true, partial: false, empty: false }
      if (text.includes('timeoutMs') && text.includes('m11-map-surface')) return { closed: true, mapPresent: true, mapSame: true, hookSame: true }
      if (text.includes('performance.now()')) return 1500
      return undefined
    }
    const attempt = await runRiverClickAttempt(
      { config: config(), page: pageOf(state) },
      identity(),
      identity().requestedFeature,
      { attemptDeadlineMs: 100, pollMs: 2, quietMs: 2500, wholeDeadline: createRiverClickDeadline(5_000, () => 0) },
    )
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) expect(['SAMPLE_TIMEOUT', 'WHOLE_RUN_TIMEOUT']).toContain(attempt.failure.code)
  })

  it('lane INTERNAL_ERROR carries a fixed message, never the raw error text', async () => {
    const page = makePage(() => { throw new Error('raw secret boom with tokens') })
    const result = await runRiverClickLane(
      { config: config(), page } as never,
      () => Promise.reject(new Error('raw secret boom with tokens')) as never,
      { deadlineMs: 2_000, now: () => 0 },
    )
    expect(result.ok).toBe(false)
    const message = result.ok ? '' : result.terminal.failure?.message ?? ''
    expect(message).not.toMatch(/raw secret boom/)
    expect(message).not.toMatch(/tokens/)
  })

  it('a goto raced by whole-deadline expiry is WHOLE_RUN_TIMEOUT, never HOOK_SELECTION_FAILED', async () => {
    const state = makeState()
    // goto never resolves; the injected 40ms absolute deadline expires first.
    state.evaluateImpl = (text: string) => {
      if (text.includes('typeof window.__nhmsRiverClickEvidence.selectRenderedRiver')) return false
      return undefined
    }
    const page = pageOf(state)
    page.goto = vi.fn(async () => new Promise(() => undefined)) as never
    // Clock advances 50ms per call: goto is raced by a 90ms absolute deadline.
    let elapsed = 0
    const clock = () => {
      elapsed += 50
      return elapsed
    }
    const result = await runRiverClickLane(
      { config: config(), page } as RiverClickLaneEnv,
      defaultFetch() as never,
      { deadline: createRiverClickDeadline(90, clock), now: clock, attemptDeadlineMs: 2_000, pollMs: 2, quietMs: 30 },
    )
    expect(result.ok).toBe(false)
    if (result.terminal.failure === null) throw new Error('failure terminal must carry a classification')
    expect(result.terminal.failure.code).toBe('WHOLE_RUN_TIMEOUT')
  })

  it('a throwing waitForTimeout during the quiet interval fails closed as a bounded timeout, never INTERNAL_ERROR', async () => {
    const state = makeState()
    let hookCalls = 0
    state.evaluateImpl = (text: string) => {
      if (text.includes('selectRenderedRiver')) {
        hookCalls += 1
        emitSeriesPair(state, () => Promise.resolve(null))
        return { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', dispatchNowMs: 1000 }
      }
      if (text.includes('m11-river-panel-chart')) return { chart: true, chartVisible: true, partial: false, empty: false }
      if (text.includes('performance.now()')) return 1500
      return undefined
    }
    const page = pageOf(state)
    state.closeImpl = () => ({ closed: true, mapPresent: true, mapSame: true, hookSame: true })
    let elapsed = 0
    const clock = () => elapsed
    // The LONG quiet wait THROWS (simulates a hung/failed waitForTimeout): the
    // quiet must fail closed as a bounded timeout, never leak INTERNAL_ERROR.
    page.waitForTimeout = vi.fn(async (ms: number) => {
      if (ms >= 100) throw new Error('waitForTimeout failed mid-quiet')
      elapsed += ms
    }) as never
    const attempt = await runRiverClickAttempt(
      { config: config(), page },
      identity(),
      identity().requestedFeature,
      { attemptDeadlineMs: 500, pollMs: 2, quietMs: 250, wholeDeadline: createRiverClickDeadline(5_000, clock), now: clock },
    )
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) expect(['SAMPLE_TIMEOUT', 'WHOLE_RUN_TIMEOUT']).toContain(attempt.failure.code)
  })

  it('a quiet wait whose waitForTimeout overshoots the effective deadline is a timeout, never accepted', async () => {
    const state = makeState()
    let hookCalls = 0
    let elapsed = 0
    const clock = () => elapsed
    state.evaluateImpl = (text: string) => {
      if (text.includes('selectRenderedRiver')) {
        hookCalls += 1
        emitSeriesPair(state, () => Promise.resolve(null))
        return { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', dispatchNowMs: 1000 }
      }
      if (text.includes('m11-river-panel-chart')) return { chart: true, chartVisible: true, partial: false, empty: false }
      if (text.includes('performance.now()')) return 1500
      return undefined
    }
    const page = pageOf(state)
    state.closeImpl = () => ({ closed: true, mapPresent: true, mapSame: true, hookSame: true })
    // Only the LONG quiet wait (250ms) overshoots by advancing the clock beyond
    // the sample budget; the 2ms polls stay negligible so the earlier phases
    // complete within budget and the quiet wait is what triggers the timeout.
    page.waitForTimeout = vi.fn(async (ms: number) => {
      if (ms >= 100) elapsed += ms * 4
      else elapsed += ms
    }) as never
    const attempt = await runRiverClickAttempt(
      { config: config(), page },
      identity(),
      identity().requestedFeature,
      { attemptDeadlineMs: 500, pollMs: 2, quietMs: 250, wholeDeadline: createRiverClickDeadline(5_000, clock), now: clock },
    )
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) expect(attempt.failure.code).toBe('SAMPLE_TIMEOUT')
  })
})

describe('phase2 closure: hook waits inside ONE budget and keeps one stable object', () => {
  function collectableMap(): RiverClickHookMap {
    return {
      loaded: () => true,
      isStyleLoaded: () => true,
      fitBounds: vi.fn(),
      project: vi.fn(() => ({ x: 100, y: 100 })),
      queryRenderedFeatures: vi.fn(() => [feature()]),
      getCanvas: () => ({ style: { cursor: '' } }),
      once: vi.fn((_event: string, callback: () => void) => {
        queueMicrotask(callback)
      }),
      off: vi.fn(),
    }
  }
  function feature() {
    return {
      layer: { id: 'm11-discharge-line-hit' },
      geometry: { type: 'LineString', coordinates: [[100, 30], [101, 31]] },
      properties: {
        basin_id: 'basins_qhh',
        river_segment_id: 'seg-001',
        segment_id: 'seg-001',
        basin_version_id: 'bv-001',
        river_network_version_id: 'rn-001',
      },
    }
  }
  const input = { bbox: [[100, 30], [102, 32]] as [[number, number], [number, number]], anchor: [100.5, 30.5] as [number, number], basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' }

  it('selection waits for a delayed map/layer inside one total budget instead of failing immediately', async () => {
    let ready = false
    const map = collectableMap()
    map.loaded = () => ready
    map.isStyleLoaded = () => ready
    const controller = createRiverClickHookController({
      getMap: () => map,
      getOverlayHitLayerId: () => (ready ? 'm11-discharge-line-hit' : null),
      now: () => 0,
      select: async ({ map: m, getOverlayHitLayerId, deadlineMs }) => {
        // emulate the hook waiting for readiness across the budget calls
        ready = true
        expect(deadlineMs).toBeGreaterThan(0)
        return selectRenderedRiverFeature({
          input,
          map: m,
          getOverlayHitLayerId,
          now: () => 0,
          deadlineMs,
        })
      },
    })
    const hook = createRiverClickEvidenceHook({ onOverlayClick: vi.fn(), controller })
    const result = await hook.selectRenderedRiver(input)
    expect(result).toMatchObject({ basinId: 'basins_qhh' })
  })

  it('keeps the exact hook object stable across callback identity changes (no replacement on rerender)', () => {
    const surface = readFileSync(path.join(repoRoot, 'apps/frontend/src/components/map/M11MapLibreSurface.tsx'), 'utf8')
    // The effect must not depend on onOverlayClick: a ref-based stable hook.
    const hookEffect = surface.slice(surface.indexOf('useEffect(() => {\n    if ((window as'))
    expect(hookEffect).not.toMatch(/}, \[onOverlayClick\]\)/)
    expect(hookEffect).toMatch(/useRef|useCallback/)
  })
})

describe('phase2 closure: attempt observation is request-identity strict', () => {
  it('correlates ONLY by response.request() === armed request; a URL-equal foreign request is not accepted', async () => {
    const state = makeState()
    let hookCalls = 0
    const url = seriesUrl()
    state.evaluateImpl = (text: string) => {
      if (text.includes('selectRenderedRiver')) {
        hookCalls += 1
        const armed = { method: () => 'GET', url: () => url }
        emit(state, 'request', armed)
        // Both sources: GFS is armed but its response carries a DIFFERENT
        // request object with the same URL; IFS is fully matched so the only
        // possible outcome is the un-correlated GFS response.
        const foreign = { method: () => 'GET', url: () => url }
        emit(state, 'response', { url: () => url, status: () => 200, finished: () => Promise.resolve(null), request: () => foreign })
        emitSeriesPair(state, () => Promise.resolve(null), 'IFS')
        return { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', dispatchNowMs: 1000 }
      }
      if (text.includes('m11-river-panel-chart')) return { chart: false, partial: false, empty: false }
      return undefined
    }
    const attempt = await runRiverClickAttempt(
      { config: config(), page: pageOf(state) },
      identity(),
      identity().requestedFeature,
      { attemptDeadlineMs: 400, pollMs: 2, quietMs: 10 },
    )
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) expect(attempt.failure.code).toBe('SERIES_REQUEST_INVALID')
  })

  it('requires the chart to be actually visible; a DOM-only presence is insufficient', async () => {
    const state = makeState()
    let hookCalls = 0
    state.evaluateImpl = (text: string) => {
      if (text.includes('selectRenderedRiver')) {
        hookCalls += 1
        emitSeriesPair(state, () => Promise.resolve(null))
        return { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', dispatchNowMs: 1000 }
      }
      if (text.includes('m11-river-panel-chart')) {
        // DOM present but NOT visible (rect zero): must not count as complete.
        return { chart: true, chartVisible: false, partial: false, empty: false }
      }
      if (text.includes('timeoutMs') && text.includes('m11-map-surface')) return { closed: false, mapPresent: false, mapSame: false, hookSame: false }
      if (text.includes('performance.now()')) return 1500
      return undefined
    }
    const attempt = await runRiverClickAttempt(
      { config: config(), page: pageOf(state) },
      identity(),
      identity().requestedFeature,
      { attemptDeadlineMs: 500, pollMs: 2, quietMs: 10 },
    )
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) expect(attempt.failure.code).toBe('CHART_INCOMPLETE')
  })

  it('rejects a chart hidden by display:none / visibility:hidden even when the rect is nonzero', async () => {
    const state = makeState()
    let hookCalls = 0
    state.evaluateImpl = (text: string) => {
      if (text.includes('selectRenderedRiver')) {
        hookCalls += 1
        emitSeriesPair(state, () => Promise.resolve(null))
        return { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', dispatchNowMs: 1000 }
      }
      if (text.includes('m11-river-panel-chart')) {
        // Visible rect but display:none: real visibility, not only positive rect.
        return { chart: true, chartVisible: false, partial: false, empty: false }
      }
      if (text.includes('timeoutMs') && text.includes('m11-map-surface')) return { closed: false, mapPresent: false, mapSame: false, hookSame: false }
      if (text.includes('performance.now()')) return 1500
      return undefined
    }
    const attempt = await runRiverClickAttempt(
      { config: config(), page: pageOf(state) },
      identity(),
      identity().requestedFeature,
      { attemptDeadlineMs: 500, pollMs: 2, quietMs: 10 },
    )
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) expect(attempt.failure.code).toBe('CHART_INCOMPLETE')
  })

  it('production chart script rejects display:none, visibility:hidden, and a hidden ancestor while accepting a visible chart (jsdom)', () => {
    const script = riverClickChartStateScript()
    // jsdom has no layout engine: getBoundingClientRect returns 0s and
    // getComputedStyle does not resolve display:none from inline style on an
    // element with zero size. Stub both to emulate a real browser: the rect is
    // nonzero unless the element (or an ancestor) is display:none; the
    // computed style reflects inline display/visibility.
    const originalRect = HTMLElement.prototype.getBoundingClientRect
    const originalGetComputedStyle = globalThis.getComputedStyle
    HTMLElement.prototype.getBoundingClientRect = function getBoundingClientRect() {
      // Display:none (self or an ancestor) -> zero rect like a browser.
      let node: HTMLElement | null = this
      while (node) {
        const style = node.style
        if (style.display === 'none') return { width: 0, height: 0, x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0 } as DOMRect
        node = node.parentElement
      }
      return { width: 100, height: 100, x: 0, y: 0, top: 0, left: 0, right: 100, bottom: 100 } as DOMRect
    }
    globalThis.getComputedStyle = ((el: Element) => {
      const node = el as HTMLElement
      const display = node.style.display || ''
      const visibility = node.style.visibility || 'visible'
      return { display, visibility } as CSSStyleDeclaration
    }) as typeof getComputedStyle

    const run = () => (0, eval)(script) as { chart: boolean; chartVisible: boolean; partial: boolean; empty: boolean }

    try {
      // Visible chart: nonzero rect + no hidden style.
      document.body.innerHTML = '<div data-testid="m11-river-panel-chart" style="width:100px;height:100px">chart</div>'
      let state = run()
      expect(state.chart).toBe(true)
      expect(state.chartVisible).toBe(true)

      // display:none on the chart itself (zero rect from the stub -> not visible).
      document.body.innerHTML = '<div data-testid="m11-river-panel-chart" style="display:none;width:100px;height:100px">chart</div>'
      state = run()
      expect(state.chart).toBe(true)
      expect(state.chartVisible).toBe(false)

      // visibility:hidden (rect nonzero but style hidden).
      document.body.innerHTML = '<div data-testid="m11-river-panel-chart" style="visibility:hidden;width:100px;height:100px">chart</div>'
      state = run()
      expect(state.chart).toBe(true)
      expect(state.chartVisible).toBe(false)

      // hidden ANCESTOR: the chart itself has no hidden style but an ancestor is
      // display:none (stub rect zero) -> not visible.
      document.body.innerHTML = '<div style="display:none"><div data-testid="m11-river-panel-chart" style="width:100px;height:100px">chart</div></div>'
      state = run()
      expect(state.chart).toBe(true)
      expect(state.chartVisible).toBe(false)
    } finally {
      HTMLElement.prototype.getBoundingClientRect = originalRect
      globalThis.getComputedStyle = originalGetComputedStyle
      document.body.innerHTML = ''
    }
  })

  it('close requires the exact pre-dispatch hook object + map node; no extra global is written', async () => {
    const state = makeState()
    let hookCalls = 0
    state.evaluateImpl = (text: string) => {
      if (text.includes('selectRenderedRiver')) {
        hookCalls += 1
        emitSeriesPair(state, () => Promise.resolve(null))
        return { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', dispatchNowMs: 1000 }
      }
      if (text.includes('m11-river-panel-chart')) {
        if (text.includes('chartVisible')) return { chart: true, chartVisible: true, partial: false, empty: false }
        return { chart: true, chartVisible: false, partial: false, empty: false }
      }
      if (text.includes('timeoutMs') && text.includes('m11-map-surface')) return { closed: true, mapPresent: true, mapSame: true, hookSame: true }
      if (text.includes('performance.now()')) return 1500
      return undefined
    }
    const attempt = await runRiverClickAttempt(
      { config: config(), page: pageOf(state) },
      identity(),
      identity().requestedFeature,
      { attemptDeadlineMs: 500, pollMs: 2, quietMs: 10 },
    )
    expect(attempt.ok).toBe(true)
    const names = state.evaluateNames.join('\n')
    expect(names).not.toMatch(/__nhmsRiverClickEvidenceRef/)
    expect(names).toMatch(/m11-map-surface/)
  })

  it('fails when the hook object is REPLACED between dispatch and close (capture is pre-dispatch)', async () => {
    const state = makeState()
    let hookCalls = 0
    const preDispatchHook = { marker: 'original-hook' }
    state.evaluateImpl = (text: string) => {
      if (text.includes('selectRenderedRiver')) {
        hookCalls += 1
        emitSeriesPair(state, () => Promise.resolve(null))
        return { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', dispatchNowMs: 1000 }
      }
      if (text.includes('m11-river-panel-chart')) return { chart: true, chartVisible: true, partial: false, empty: false }
      if (text.includes('performance.now()')) {
        // Replacement happens AFTER dispatch (capture already taken) and BEFORE
        // the scoped close: the hook object is no longer the captured one.
        ;(window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence = { marker: 'replaced-hook' }
        return 1500
      }
      return undefined
    }
    state.closeImpl = (captured) => {
      const c = captured as { hook: { marker: string } }
      const current = (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence
      // hookSame = current window hook is STILL the captured pre-dispatch object.
      return { closed: true, mapPresent: true, mapSame: true, hookSame: c.hook === current }
    }
    // Stub the hook the capture must see BEFORE dispatch.
    ;(window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence = preDispatchHook
    const attempt = await runRiverClickAttempt(
      { config: config(), page: pageOf(state) },
      identity(),
      identity().requestedFeature,
      { attemptDeadlineMs: 500, pollMs: 2, quietMs: 10 },
    )
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) expect(attempt.failure.code).toBe('CHART_INCOMPLETE')
    // The capture MUST happen before the dispatch: the handle-capture marker
    // precedes the selectRenderedRiver dispatch evaluate.
    const names = state.evaluateNames
    const captureIdx = names.indexOf('handle-capture')
    const dispatchIdx = names.findIndex((n) => n.includes('selectRenderedRiver'))
    expect(captureIdx).toBeGreaterThanOrEqual(0)
    expect(dispatchIdx).toBeGreaterThan(captureIdx)
    delete (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence
  })

  it('fails when the m11-map-surface node is REPLACED between dispatch and close', async () => {
    const state = makeState()
    let hookCalls = 0
    state.closeImpl = (captured) => {
      const c = captured as { map: unknown }
      const current = document.querySelector('[data-testid="m11-map-surface"]')
      // mapSame = current map node is STILL the captured pre-dispatch node.
      return { closed: true, mapPresent: true, mapSame: c.map === current, hookSame: true }
    }
    // A real map node for the capture to hold BEFORE dispatch.
    document.body.innerHTML = '<div data-testid="m11-map-surface" id="map-original">map</div>'
    ;(window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence = { marker: 'hook' }
    state.evaluateImpl = (text: string) => {
      if (text.includes('selectRenderedRiver')) {
        hookCalls += 1
        // Replace the map node DURING dispatch (before close): a new node with
        // the same testid mounts, so the pre-dispatch node is no longer current.
        document.body.innerHTML = '<div data-testid="m11-map-surface" id="map-replaced">map2</div>'
        emitSeriesPair(state, () => Promise.resolve(null))
        return { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', dispatchNowMs: 1000 }
      }
      if (text.includes('m11-river-panel-chart')) return { chart: true, chartVisible: true, partial: false, empty: false }
      if (text.includes('performance.now()')) return 1500
      return undefined
    }
    const attempt = await runRiverClickAttempt(
      { config: config(), page: pageOf(state) },
      identity(),
      identity().requestedFeature,
      { attemptDeadlineMs: 500, pollMs: 2, quietMs: 10 },
    )
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) expect(attempt.failure.code).toBe('CHART_INCOMPLETE')
    document.body.innerHTML = ''
    delete (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence
  })

  it('fails when the hook/map identities are REPLACED during the QUIET interval (post-quiet probe), with both handles disposed', async () => {
    const state = makeState()
    let quietReplaced = false
    state.closeImpl = () => ({ closed: true, mapPresent: true, mapSame: true, hookSame: true })
    state.evaluateImpl = (text: string) => {
      if (text.includes('selectRenderedRiver')) {
        document.body.innerHTML = '<div data-testid="m11-map-surface" id="map-original">map</div>'
        ;(window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence = { marker: 'hook-original' }
        emitSeriesPair(state, () => Promise.resolve(null))
        return { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', dispatchNowMs: 1000 }
      }
      if (text.includes('m11-river-panel-chart')) return { chart: true, chartVisible: true, partial: false, empty: false }
      if (text.includes('performance.now()')) return 1500
      return undefined
    }
    // Replacement happens DURING the quiet interval, AFTER the scoped close
    // passed (the post-quiet identity probe must catch it).
    state.onQuiet = () => {
      quietReplaced = true
      document.body.innerHTML = '<div data-testid="m11-map-surface" id="map-replaced">map2</div>'
      ;(window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence = { marker: 'hook-replaced' }
    }
    const attempt = await runRiverClickAttempt(
      { config: config(), page: pageOf(state) },
      identity(),
      identity().requestedFeature,
      { attemptDeadlineMs: 500, pollMs: 2, quietMs: 250 },
    )
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) expect(attempt.failure.code).toBe('CHART_INCOMPLETE')
    expect(quietReplaced).toBe(true)
    // Both handles disposed even on the quiet-replacement FAIL.
    expect(state.handleDisposals).toBe(2)
    document.body.innerHTML = ''
    delete (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence
  })

  it('disposes BOTH pre-dispatch handles on an early hook-rejection/drift failure', async () => {
    const state = makeState()
    state.evaluateImpl = (text: string) => {
      // Hook rejection: dispatch never returns; capture already happened.
      if (text.includes('selectRenderedRiver')) {
        throw new Error('hook rejected before dispatch')
      }
      return undefined
    }
    const attempt = await runRiverClickAttempt(
      { config: config(), page: pageOf(state) },
      identity(),
      identity().requestedFeature,
      { attemptDeadlineMs: 500, pollMs: 2, quietMs: 10 },
    )
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) expect(attempt.failure.code).toBe('HOOK_SELECTION_FAILED')
    expect(state.handleDisposals).toBe(2)
  })

  it('AWAITS disposal completion before settling: a deferred dispose resolves before runRiverClickAttempt returns', async () => {
    const state = makeState()
    // Each dispose takes 30ms real time; if the attempt voided the dispose
    // promises and returned early, handleDisposals would still be 0 right
    // after settle. Awaiting proves the attempt does not leak handles.
    state.deferredDisposeMs = 30
    state.evaluateImpl = (text: string) => {
      if (text.includes('selectRenderedRiver')) {
        emitSeriesPair(state, () => Promise.resolve(null))
        return { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', dispatchNowMs: 1000 }
      }
      if (text.includes('m11-river-panel-chart')) return { chart: true, chartVisible: true, partial: false, empty: false }
      if (text.includes('timeoutMs') && text.includes('m11-map-surface')) return { closed: true, mapPresent: true, mapSame: true, hookSame: true }
      if (text.includes('performance.now()')) return 1500
      return undefined
    }
    const attempt = await runRiverClickAttempt(
      { config: config(), page: pageOf(state) },
      identity(),
      identity().requestedFeature,
      { attemptDeadlineMs: 500, pollMs: 2, quietMs: 10 },
    )
    expect(attempt.ok).toBe(true)
    // Both deferred disposals completed BEFORE the attempt promise settled.
    expect(state.handleDisposals).toBe(2)
  })

  it('panel absence BEFORE the scoped close is a FAIL, not success', async () => {
    const state = makeState()
    let hookCalls = 0
    state.evaluateImpl = (text: string) => {
      if (text.includes('selectRenderedRiver')) {
        hookCalls += 1
        emitSeriesPair(state, () => Promise.resolve(null))
        return { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', dispatchNowMs: 1000 }
      }
      if (text.includes('m11-river-panel-chart')) return { chart: true, chartVisible: true, partial: false, empty: false }
      if (text.includes('timeoutMs') && text.includes('m11-map-surface')) return { closed: false, mapPresent: false, mapSame: false, hookSame: false }
      if (text.includes('performance.now()')) return 1500
      return undefined
    }
    const attempt = await runRiverClickAttempt(
      { config: config(), page: pageOf(state) },
      identity(),
      identity().requestedFeature,
      { attemptDeadlineMs: 500, pollMs: 2, quietMs: 10 },
    )
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) expect(attempt.failure.code).toBe('CHART_INCOMPLETE')
  })
})

describe('phase2 closure: preflight stream and geometry parity', () => {
  it('a stream error after a complete-looking JSON chunk fails preflight, never accepted', async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('{"status":"ok","data":{"complete":true}}'))
        controller.error(new Error('stream exploded mid-body'))
      },
    })
    const response = new Response(stream, { status: 200 })
    await expect(readResponseBounded(response, 262144)).rejects.toThrow()
  })

  it('geometry sanitizer preserves finite Z and skips sub-2-point parts exactly like the app owner', async () => {
    const { parseRiverClickPreflightSegment } = await import('../lib/riverClickEvidence/preflight')
    const { getM11SelectedSegmentGeometryBudgetStatus } = await import('../lib/m11/overviewDataContracts')
    const geom3d = { type: 'MultiLineString', coordinates: [[[100, 30, 500], [101, 31, 501]]] }
    const lane = parseRiverClickPreflightSegment({ river_segment_id: 'seg-001', river_network_version_id: 'rn-001', geom: geom3d }, 'seg-001', 'rn-001', 'bv-001')
    const app = getM11SelectedSegmentGeometryBudgetStatus(geom3d as never)
    expect(lane.ok).toBe(true)
    expect(app.ok).toBe(true)
    expect(app.sanitizedGeometry).toEqual({ type: 'MultiLineString', coordinates: [[[100, 30, 500], [101, 31, 501]]] })
    // A part with a single point is skipped by the app as long as one valid part remains.
    const mixed = { type: 'MultiLineString', coordinates: [[[100, 30]], [[102, 32], [103, 33]]] }
    const laneMixed = parseRiverClickPreflightSegment({ river_segment_id: 'seg-001', river_network_version_id: 'rn-001', geom: mixed }, 'seg-001', 'rn-001', 'bv-001')
    const appMixed = getM11SelectedSegmentGeometryBudgetStatus(mixed as never)
    expect(laneMixed.ok).toBe(true)
    expect(appMixed.ok).toBe(true)
    expect(appMixed.sanitizedGeometry?.coordinates).toEqual([[[102, 32], [103, 33]]])
  })
})

describe('phase2 closure: identity-drift FAIL retains the mismatching rendered identity', () => {
  it('executes the real lane with an induced drift, then publishes the terminal through the real publisher', async () => {
    // The hook returns seg-NEW while the preflight requested seg-001: the lane
    // must FAIL with IDENTITY_DRIFT and carry the ACTUAL mismatching rendered
    // identity; publishRiverClickTerminal then builds + publishes the receipt
    // via the real mode-0700 publisher without equalizing it.
    const state = makeState()
    let hookCalls = 0
    state.evaluateImpl = (text: string) => {
      if (text.includes('typeof window.__nhmsRiverClickEvidence.selectRenderedRiver')) return true
      if (text.includes('selectRenderedRiver')) {
        hookCalls += 1
        emitSeriesPair(state, () => Promise.resolve(null))
        return { basinId: 'basins_qhh', riverSegmentId: 'seg-NEW', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', dispatchNowMs: 1000 }
      }
      if (text.includes('m11-river-panel-chart')) return { chart: true, chartVisible: true, partial: false, empty: false }
      if (text.includes('timeoutMs') && text.includes('m11-map-surface')) return { closed: true, mapPresent: true, mapSame: true, hookSame: true }
      if (text.includes('performance.now()')) return 1500
      return undefined
    }
    const lane = await runRiverClickLane(
      { config: config(), page: pageOf(state) } as RiverClickLaneEnv,
      defaultFetch() as never,
      { deadlineMs: 10_000, attemptDeadlineMs: 500, mapDeadlineMs: 500, pollMs: 2, quietMs: 10 },
    )
    expect(lane.ok).toBe(false)
    if (lane.ok) throw new Error('lane must fail on identity drift')
    expect(hookCalls).toBe(1)
    expect(lane.terminal.failure?.code).toBe('IDENTITY_DRIFT')
    expect(lane.terminal.requestedFeature).toEqual({ basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' })
    expect(lane.terminal.renderedFeature?.riverSegmentId).toBe('seg-NEW')

    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-river-p2-drift-')))
    chmodSync(parent, 0o700)
    try {
      const receiptPath = path.join(parent, 'nhms-frontend-river-click-live-evidence-drift.json')
      const published = publishRiverClickTerminal(
        lane.terminal,
        { startedAt: '2026-09-02T00:59:50Z', endedAt: '2026-09-02T00:59:51Z', frontendOrigin: 'https://display.example.test', apiOrigin: 'https://api.example.test', receiptPath },
        { publish: (p, receipt) => publishRiverClickEvidence(p, receipt) },
      )
      expect(published.ok).toBe(true)
      const written = JSON.parse(readFileSync(receiptPath, 'utf8'))
      expect(written.status).toBe('FAIL')
      expect(written.failure.code).toBe('IDENTITY_DRIFT')
      expect(written.requested_feature.river_segment_id).toBe('seg-001')
      // The ACTUAL mismatching rendered identity is retained, never equalized.
      expect(written.rendered_feature.river_segment_id).toBe('seg-NEW')
      expect(validateRiverClickEvidenceDocument(written).ok).toBe(true)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })
})

function seriesUrl(source: 'GFS' | 'IFS' = 'GFS') {
  const product = source === 'GFS'
    ? { run_id: 'run-gfs', model_id: 'model-gfs', issue_time: '2026-09-02T00:00:00.000Z', scenarios: 'forecast_gfs_deterministic' }
    : { run_id: 'run-ifs', model_id: 'model-ifs', issue_time: '2026-09-02T06:00:00.000Z', scenarios: 'forecast_ifs_deterministic' }
  return 'https://api.example.test/api/v1/basin-versions/bv-001/river-segments/seg-001/forecast-series?' + new URLSearchParams({
    river_network_version_id: 'rn-001', variables: 'q_down', include_analysis: 'false',
    ...product,
  }).toString()
}

function config() {
  const parsed = parseRiverClickConfig(validConfigEnv())
  if (!parsed.ok) throw new Error('bad config')
  return parsed.config
}

function identity(): RiverClickLaneIdentity {
  return {
    requestedFeature: { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' },
    gfs: {
      sourceId: 'GFS', basinId: 'basins_qhh', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001',
      runId: 'run-gfs', modelId: 'model-gfs', cycleTime: '2026-09-02T00:00:00Z', scenario: 'forecast_gfs_deterministic',
    },
    ifs: {
      sourceId: 'IFS', basinId: 'basins_qhh', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001',
      runId: 'run-ifs', modelId: 'model-ifs', cycleTime: '2026-09-02T06:00:00Z', scenario: 'forecast_ifs_deterministic',
    },
    preflightGfs: {
      source: 'GFS', scenario: 'forecast_gfs_deterministic', runId: 'run-gfs', modelId: 'model-gfs',
      issueTime: '2026-09-02T00:00:00Z', riverNetworkVersionId: 'rn-001',
    },
    preflightIfs: {
      source: 'IFS', scenario: 'forecast_ifs_deterministic', runId: 'run-ifs', modelId: 'model-ifs',
      issueTime: '2026-09-02T06:00:00Z', riverNetworkVersionId: 'rn-001',
    },
    bbox: [[100, 30], [102, 32]] as [[number, number], [number, number]],
    anchor: [100.5, 30.5] as [number, number],
  }
}

function makePage(evalImpl: (text: string) => unknown): RiverClickLanePageSurface {
  const state = makeState()
  state.evaluateImpl = evalImpl
  return pageOf(state)
}

function emit(state: FakeState, event: string, arg: unknown) {
  for (const listener of [...state.listeners[event]]) {
    ;(listener as (value: unknown) => void)(arg)
  }
}

function emitSeriesPair(state: FakeState, finished: () => Promise<unknown>, only: 'GFS' | 'IFS' | null = null) {
  for (const source of ['GFS', 'IFS'] as const) {
    if (only !== null && source !== only) continue
    const url = seriesUrl(source)
    const req: RiverClickLaneBrowserRequest = { method: () => 'GET', url: () => url }
    emit(state, 'request', req)
    const response: RiverClickLaneBrowserResponse = { url: () => url, status: () => 200, finished, request: () => req }
    emit(state, 'response', response)
  }
}

function responseOf(status: number, body: string): Response {
  return new Response(body, { status })
}


function defaultFetch() {
  const productPayload = (source: string) => ({
    status: 'ready',
    source_id: source,
    basin_id: 'basins_qhh',
    basin_version_id: 'bv-001',
    river_network_version_id: 'rn-001',
    run_id: source === 'GFS' ? 'run-gfs' : 'run-ifs',
    model_id: source === 'GFS' ? 'model-gfs' : 'model-ifs',
    cycle_time: source === 'GFS' ? '2026-09-02T00:00:00Z' : '2026-09-02T06:00:00Z',
  })
  const segmentPayload = {
    river_segment_id: 'seg-001',
    river_network_version_id: 'rn-001',
    geom: { type: 'LineString', coordinates: [[100, 30], [101, 31], [102, 32]] },
  }
  const envelope = (data: unknown) => JSON.stringify({ status: 'ok', data })
  return (_url: string, _init: RequestInit): Promise<Response> => {
    if (_url.includes('latest-product')) {
      const source = new URL(_url).searchParams.get('source')
      return Promise.resolve(responseOf(200, envelope(productPayload(source === 'GFS' ? 'GFS' : 'IFS'))))
    }
    if (_url.includes('/river-segments/')) return Promise.resolve(responseOf(200, envelope(segmentPayload)))
    return Promise.resolve(responseOf(404, '{}'))
  }
}

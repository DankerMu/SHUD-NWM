/**
 * One-attempt observer half of the browser-side no-mock river-click P95 lane
 * (#1970): arm/classify/disarm the exact Playwright request event listeners,
 * bound one shared per-sample deadline, and drive dispatch -> series -> chart
 * -> close -> quiet. Pure logic with an injected minimal Playwright surface so
 * it is unit-testable in vitest without a browser.
 */

import {
  RIVER_CLICK_PER_SAMPLE_DEADLINE_MS,
  RIVER_CLICK_WHOLE_RUN_DEADLINE_MS,
} from './src/lib/riverClickEvidence/constants'
import { createRiverClickDeadline, withRiverClickDeadline, type RiverClickDeadline } from './src/lib/riverClickEvidence/deadline'
import { matchRiverClickSeriesRequest } from './src/lib/riverClickEvidence/requestMatching'
import type { RiverClickFeatureIdentity } from './src/lib/riverClickEvidence/receipt'
import {
  failureOf,
  simpleFailure,
  timeoutValue,
  TIMEOUT_SENTINEL,
  type RiverClickFailureShape,
  type RiverClickJsHandle,
  type RiverClickLaneBrowserRequest,
  type RiverClickLaneBrowserResponse,
  type RiverClickLaneEnv,
  type RiverClickLaneIdentity,
} from './playwright.river-click-lane-preflight'

/**
 * Chart-visibility script: the chart node must be ACTUALLY visible — positive
 * rect AND not display:none / visibility:hidden|collapse. Exported so tests can
 * execute the exact production script in jsdom.
 */
export function riverClickChartStateScript(): string {
  return `(() => {
    const node = document.querySelector('[data-testid="m11-river-panel-chart"]')
    const vis = node instanceof HTMLElement
      ? getComputedStyle(node)
      : null
    const hiddenByStyle = vis !== null && (vis.display === 'none' || vis.visibility === 'hidden' || vis.visibility === 'collapse')
    const rect = node instanceof HTMLElement ? node.getBoundingClientRect() : null
    return {
      chart: Boolean(node),
      chartVisible: Boolean(
        node instanceof HTMLElement &&
        rect !== null &&
        rect.width > 0 &&
        rect.height > 0 &&
        !hiddenByStyle,
      ),
      partial: Boolean(document.querySelector('[data-testid="m11-river-panel-partial"]')),
      empty: Boolean(document.querySelector('[data-testid="m11-river-panel-empty"]')),
    }
  })()`
}

export interface RiverClickPanelCloseCaptured {
  hook: unknown
  map: unknown
  budgetMs: number
  pollMs: number
}

export interface RiverClickPanelCloseOutcome {
  closed: boolean
  mapPresent: boolean
  mapSame: boolean
  hookSame: boolean
}

/**
 * Production-owned panel close used by Playwright `page.evaluate`. jsdom tests
 * execute this exact function; the fake page must not synthesize a close result
 * from source-string matching.
 */
export function closeRiverClickPanelInPage(
  captured: RiverClickPanelCloseCaptured,
): Promise<RiverClickPanelCloseOutcome> | RiverClickPanelCloseOutcome {
  const panel = document.querySelector('[data-testid="m11-river-forecast-panel"]')
  if (!panel) return { closed: false, mapPresent: false, mapSame: false, hookSame: false }
  const close = panel.querySelector('[aria-label="关闭面板"]')
  if (!(close instanceof HTMLElement)) return { closed: false, mapPresent: false, mapSame: false, hookSame: false }
  const capturedHook = captured.hook
  const capturedMap = captured.map
  const budgetMs = captured.budgetMs
  const poll = captured.pollMs
  close.click()
  return new Promise((resolve) => {
    const startedAt = Date.now()
    const probe = () => {
      const panelNow = document.querySelector('[data-testid="m11-river-forecast-panel"]')
      const mapNow = document.querySelector('[data-testid="m11-map-surface"]')
      const hookNow = (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence
      if (!panelNow) {
        resolve({
          closed: true,
          mapPresent: Boolean(mapNow),
          mapSame: mapNow === capturedMap,
          hookSame: hookNow === capturedHook,
        })
        return
      }
      if (mapNow !== capturedMap || hookNow !== capturedHook) {
        resolve({ closed: false, mapPresent: Boolean(mapNow), mapSame: mapNow === capturedMap, hookSame: hookNow === capturedHook })
        return
      }
      if (Date.now() - startedAt >= budgetMs) {
        resolve({ closed: false, mapPresent: Boolean(mapNow), mapSame: mapNow === capturedMap, hookSame: hookNow === capturedHook })
        return
      }
      setTimeout(probe, poll)
    }
    probe()
  })
}

export interface RiverClickAttemptOptions {
  attemptDeadlineMs?: number
  pollMs?: number
  quietMs?: number
  wholeDeadline?: RiverClickDeadline
  now?: () => number
}

export type RiverClickAttemptResult =
  | {
      ok: true
      rendered: RiverClickFeatureIdentity
      t0Ms: number
      t1Ms: number
      gfsStatus: number
      ifsStatus: number
    }
  | {
      ok: false
      failure: RiverClickFailureShape
      /** The actual hook-returned identity when selection succeeded before the
       *  failure; null when the hook never resolved. */
      rendered: RiverClickFeatureIdentity | null
    }

interface RequestRecord {
  source: 'GFS' | 'IFS'
  request: RiverClickLaneBrowserRequest
  response: RiverClickLaneBrowserResponse | null
  requestFailed: boolean
  finishedError: boolean
  completed: boolean
  status: number | null
}

function requestIdentityForCorrelation(request: RiverClickLaneBrowserRequest, response: RiverClickLaneBrowserResponse): boolean {
  // Playwright ALWAYS supplies Response.request() identity. Correlation is ONLY
  // by exact request-object equality; a response without a request() accessor
  // (or one whose accessor throws) fails closed rather than falling back to
  // method+URL equality, which would let an unrelated same-URL response count.
  const origin = response.request?.()
  return origin === request
}

function classifyRequest(
  request: RiverClickLaneBrowserRequest,
  env: RiverClickLaneEnv,
  expectedRendered: RiverClickFeatureIdentity,
  identity: RiverClickLaneIdentity,
  records: RequestRecord[],
): 'matched-gfs' | 'matched-ifs' | 'invalid' | 'other' {
  let method: string
  let url: string
  try {
    method = request.method()
    url = request.url()
  } catch {
    return 'other'
  }
  const gfsMatch = matchRiverClickSeriesRequest(method, url, {
    apiOrigin: env.config.apiOrigin,
    basinVersionId: expectedRendered.basinVersionId,
    segmentId: expectedRendered.riverSegmentId,
    product: identity.preflightGfs,
  })
  const ifsMatch = gfsMatch.matched
    ? { matched: false as const }
    : matchRiverClickSeriesRequest(method, url, {
        apiOrigin: env.config.apiOrigin,
        basinVersionId: expectedRendered.basinVersionId,
        segmentId: expectedRendered.riverSegmentId,
        product: identity.preflightIfs,
      })
  const matched = gfsMatch.matched ? gfsMatch : ifsMatch
  if (!matched.matched) {
    // A forecast-series-shaped request that did not match (wrong identity/
    // wrong method/malformed) is an unexpected series request.
    try {
      if (new URL(url).pathname.includes('/forecast-series')) return 'invalid'
    } catch {
      // unparseable URL is not a series request; classified by the matcher
    }
    return 'other'
  }
  if (records.some((record) => record.source === matched.source)) {
    return 'invalid'
  }
  return matched.source === 'GFS' ? 'matched-gfs' : 'matched-ifs'
}

/**
 * One complete attempt. Observation is armed BEFORE dispatch and stays armed
 * through the scoped close/unmount + quiet interval; it is disarmed only after
 * every terminal path. One shared per-sample deadline starts before the hook
 * evaluate and covers hook, both finished responses, chart, close/unmount, and
 * quiet. Request classification happens at REQUEST time using the real method
 * and URL; every forecast-series request must be exactly one GFS + one IFS
 * (wrong-method/wrong-identity/duplicate/extra fail SERIES_REQUEST_INVALID).
 */
export async function runRiverClickAttempt(
  env: RiverClickLaneEnv,
  identity: RiverClickLaneIdentity,
  expectedRendered: RiverClickFeatureIdentity,
  options: RiverClickAttemptOptions = {},
): Promise<RiverClickAttemptResult> {
  const { page, config } = env
  const clock = options.now ?? (() => performance.now())
  const attemptDeadlineMs = options.attemptDeadlineMs ?? RIVER_CLICK_PER_SAMPLE_DEADLINE_MS
  const pollMs = options.pollMs ?? 100
  const quietMs = options.quietMs ?? 250
  const wholeDeadline = options.wholeDeadline ?? createRiverClickDeadline(RIVER_CLICK_WHOLE_RUN_DEADLINE_MS, clock)
  const startedAt = clock()
  const sampleDeadline = createRiverClickDeadline(attemptDeadlineMs, clock, startedAt)
  // The effective bound is the earlier of the sample and whole-run deadlines.
  const effectiveDeadline: RiverClickDeadline = sampleDeadline.absoluteMs <= wholeDeadline.absoluteMs ? sampleDeadline : wholeDeadline

  let records: RequestRecord[] = []
  let unexpectedSeriesCount = 0
  let renderedSeen: RiverClickFeatureIdentity | null = null
  // The pre-dispatch handle pair owned by THIS attempt; both are disposed on
  // every terminal (success/hook-rejection/drift/response/chart/close/quiet/
  // timeout) and disposal failure NEVER masks the attempt terminal.
  let ownedHandles: RiverClickJsHandle[] = []
  let responseListener: ((response: RiverClickLaneBrowserResponse) => void) | null = null
  let requestListener: ((request: RiverClickLaneBrowserRequest) => void) | null = null
  let requestFailedListener: ((request: RiverClickLaneBrowserRequest) => void) | null = null

  const arm = () => {
    records = []
    unexpectedSeriesCount = 0
    requestListener = (request: RiverClickLaneBrowserRequest) => {
      const classification = classifyRequest(request, env, expectedRendered, identity, records)
      if (classification === 'matched-gfs') {
        records.push({ source: 'GFS', request, response: null, requestFailed: false, finishedError: false, completed: false, status: null })
        return
      }
      if (classification === 'matched-ifs') {
        records.push({ source: 'IFS', request, response: null, requestFailed: false, finishedError: false, completed: false, status: null })
        return
      }
      if (classification === 'invalid') {
        unexpectedSeriesCount += 1
      }
    }
    responseListener = (response: RiverClickLaneBrowserResponse) => {
      let record: RequestRecord | undefined
      try {
        record = records.find((candidate) => candidate.response === null && requestIdentityForCorrelation(candidate.request, response))
      } catch {
        record = undefined
      }
      if (!record) {
        // A response with no matching armed request (synthetic seam or extra).
        try {
          if (new URL(response.url()).pathname.includes('/forecast-series')) {
            unexpectedSeriesCount += 1
          }
        } catch {
          // ignore unparseable URLs
        }
        return
      }
      record.response = response
      try {
        record.status = response.status()
      } catch {
        unexpectedSeriesCount += 1
        return
      }
      let finished: Promise<unknown>
      try {
        finished = response.finished()
      } catch {
        record.finishedError = true
        return
      }
      Promise.resolve(finished)
        .then((result: unknown) => {
          if (result instanceof Error) {
            // finished() resolves null | Error; an Error result is failure.
            record.finishedError = true
            return
          }
          record.completed = true
        })
        .catch(() => {
          record.finishedError = true
        })
    }
    requestFailedListener = (request: RiverClickLaneBrowserRequest) => {
      const record = records.find((candidate) => candidate.request === request)
      if (record) {
        record.requestFailed = true
        return
      }
      try {
        if (new URL(request.url()).pathname.includes('/forecast-series') && !records.some((candidate) => candidate.request === request)) {
          unexpectedSeriesCount += 1
        }
      } catch {
        // ignore unparseable URLs
      }
    }
    page.on('request', requestListener as never)
    page.on('response', responseListener as never)
    page.on('requestfailed', requestFailedListener as never)
  }

  const disarm = () => {
    if (responseListener) {
      page.off('response', responseListener as never)
      responseListener = null
    }
    if (requestListener) {
      page.off('request', requestListener as never)
      requestListener = null
    }
    if (requestFailedListener) {
      page.off('requestfailed', requestFailedListener as never)
      requestFailedListener = null
    }
  }

  const insufficientRawTimeout = (): RiverClickFailureShape => {
    if (wholeDeadline.expired()) {
      return failureOf('WHOLE_RUN_TIMEOUT', 'sample', 'whole-run deadline exceeded during a sample', null)
    }
    return failureOf('SAMPLE_TIMEOUT', 'sample', 'per-sample deadline exceeded')
  }

  /** Bounded sleep that never overshoots the shared deadline. */
  const boundedSleep = async (ms: number): Promise<void> => {
    const remaining = effectiveDeadline.remaining()
    if (remaining <= 0) return
    await page.waitForTimeout(Math.min(ms, remaining))
  }

  /** Full-quiet sleep: requires the WHOLE requested quiet to fit and elapse,
   *  never hanging or overshooting. The wait is raced against the effective
   *  (earlier of sample/whole-run) deadline; a waitForTimeout that fails, hangs
   *  or overshoots past the bound is a timeout, never an accepted attempt. */
  const fullQuietSleep = async (ms: number): Promise<RiverClickFailureShape | null> => {
    if (effectiveDeadline.remaining() < ms) {
      return insufficientRawTimeout()
    }
    let waited = false
    try {
      await withRiverClickDeadline(
        page.waitForTimeout(ms),
        effectiveDeadline,
        () => timeoutValue(),
      )
      waited = true
    } catch {
      waited = false
    }
    if (!waited || effectiveDeadline.expired()) {
      if (wholeDeadline.expired()) {
        return failureOf('WHOLE_RUN_TIMEOUT', 'sample', 'whole-run deadline exceeded during quiet interval', null)
      }
      return failureOf('SAMPLE_TIMEOUT', 'sample', 'per-sample deadline exceeded during quiet interval')
    }
    return null
  }

  const result = await (async (): Promise<RiverClickAttemptResult> => {
  try {
    arm()
    // Capture the EXACT hook object and the EXACT m11-map-surface DOM node
    // immediately BEFORE dispatch, retained as page-side handles (JSHandle in
    // real Playwright; each evaluateHandle represents ONLY that actual object).
    // The scoped close and the post-quiet identity probe compare the SAME
    // objects, and BOTH handles are disposed (awaited) after the terminal is
    // decided on every path (success/hook-rejection/drift/response/chart/
    // close/quiet/timeout).
    let hookHandle: RiverClickJsHandle | null = null
    let mapHandle: RiverClickJsHandle | null = null
    try {
      hookHandle = await page.evaluateHandle<unknown>(`window.__nhmsRiverClickEvidence`)
      ownedHandles.push(hookHandle)
      mapHandle = await page.evaluateHandle<unknown>(`document.querySelector('[data-testid="m11-map-surface"]')`)
      ownedHandles.push(mapHandle)
    } catch {
      return { ok: false, failure: failureOf('HOOK_SELECTION_FAILED', 'map', 'pre-dispatch hook/map capture failed'), rendered: null }
    }
    let dispatch: { basinId: string; riverSegmentId: string; basinVersionId: string; riverNetworkVersionId: string; dispatchNowMs: number }
    try {
      const evaluated = await withRiverClickDeadline(
        page.evaluate<{
          basinId: string
          riverSegmentId: string
          basinVersionId: string
          riverNetworkVersionId: string
          dispatchNowMs: number
        }>(
          `window.__nhmsRiverClickEvidence.selectRenderedRiver(${JSON.stringify({
            bbox: identity.bbox,
            anchor: identity.anchor,
            basinId: identity.requestedFeature.basinId,
            riverSegmentId: identity.requestedFeature.riverSegmentId,
            basinVersionId: identity.requestedFeature.basinVersionId,
            riverNetworkVersionId: identity.requestedFeature.riverNetworkVersionId,
          })})`,
        ),
        effectiveDeadline,
        () => timeoutValue(),
      )
      if ((evaluated as unknown) === TIMEOUT_SENTINEL) {
        return { ok: false, failure: insufficientRawTimeout(), rendered: null }
      }
      dispatch = evaluated as NonNullable<typeof evaluated>
    } catch {
      if (wholeDeadline.expired()) {
        return { ok: false, failure: failureOf('WHOLE_RUN_TIMEOUT', 'sample', 'whole-run deadline exceeded before dispatch', null), rendered: null }
      }
      return { ok: false, failure: failureOf('HOOK_SELECTION_FAILED', 'map', 'hook invocation failed'), rendered: null }
    }
    if (!dispatch || typeof dispatch.dispatchNowMs !== 'number' || !Number.isFinite(dispatch.dispatchNowMs)) {
      return { ok: false, failure: failureOf('HOOK_SELECTION_FAILED', 'map', 'selectRenderedRiver resolved without dispatchNowMs'), rendered: null }
    }
    const t0 = dispatch.dispatchNowMs
    // Compare ALL FOUR returned feature identities (not a preset equality).
    const rendered: RiverClickFeatureIdentity = {
      basinId: dispatch.basinId,
      riverSegmentId: dispatch.riverSegmentId,
      basinVersionId: dispatch.basinVersionId,
      riverNetworkVersionId: dispatch.riverNetworkVersionId,
    }
    renderedSeen = rendered
    if (
      rendered.basinId !== expectedRendered.basinId ||
      rendered.riverSegmentId !== expectedRendered.riverSegmentId ||
      rendered.basinVersionId !== expectedRendered.basinVersionId ||
      rendered.riverNetworkVersionId !== expectedRendered.riverNetworkVersionId
    ) {
      return { ok: false, failure: failureOf('IDENTITY_DRIFT', 'map', 'rendered feature identity drifted from preflight'), rendered }
    }

    // ONE shared deadline: t0 -> both finished 2xx responses -> chart -> close
    // -> quiet. No second independent deadline.
    let gfsStatus = 0
    let ifsStatus = 0
    for (;;) {
      if (unexpectedSeriesCount > 0) {
        return { ok: false, failure: failureOf('SERIES_REQUEST_INVALID', 'sample', 'unexpected or duplicate forecast-series request observed'), rendered }
      }
      const gfs = records.find((record) => record.source === 'GFS')
      const ifs = records.find((record) => record.source === 'IFS')
      if (gfs?.requestFailed || ifs?.requestFailed) {
        return { ok: false, failure: failureOf('SERIES_RESPONSE_ERROR', 'sample', 'a forecast-series request failed on the network'), rendered }
      }
      if (gfs?.response && (gfs.status === null || gfs.status < 200 || gfs.status > 299)) {
        return { ok: false, failure: failureOf('SERIES_RESPONSE_ERROR', 'sample', 'GFS forecast-series status not 2xx', null, gfs.status, null), rendered }
      }
      if (ifs?.response && (ifs.status === null || ifs.status < 200 || ifs.status > 299)) {
        return { ok: false, failure: failureOf('SERIES_RESPONSE_ERROR', 'sample', 'IFS forecast-series status not 2xx', null, null, ifs.status), rendered }
      }
      if (gfs?.finishedError || ifs?.finishedError) {
        return { ok: false, failure: failureOf('SERIES_RESPONSE_ERROR', 'sample', 'a forecast-series response failed to finish'), rendered }
      }
      if (gfs?.completed && ifs?.completed) {
        gfsStatus = gfs.status as number
        ifsStatus = ifs.status as number
        break
      }
      if (effectiveDeadline.expired()) {
        // A matched request without a completed response at the deadline is the
        // per-sample/whole-run expiry, not a hard response error.
        return { ok: false, failure: insufficientRawTimeout(), rendered }
      }
      await boundedSleep(pollMs)
    }

    // Chart becomes complete within the SAME deadline (no second reset). The
    // chart element must be ACTUALLY VISIBLE: a DOM node that exists but is
    // hidden/zero-sized is not complete evidence.
    let chartReady = false
    let partial = false
    for (;;) {
      let state: { chart: boolean; chartVisible: boolean; partial: boolean; empty: boolean } | undefined
      try {
        state = await withRiverClickDeadline(
          page.evaluate<{ chart: boolean; chartVisible: boolean; partial: boolean; empty: boolean }>(
            riverClickChartStateScript(),
          ),
          effectiveDeadline,
          () => timeoutValue(),
        )
        if ((state as unknown) === TIMEOUT_SENTINEL) {
          return { ok: false, failure: insufficientRawTimeout(), rendered }
        }
      } catch {
        if (wholeDeadline.expired()) {
          return { ok: false, failure: failureOf('WHOLE_RUN_TIMEOUT', 'sample', 'whole-run deadline exceeded during chart wait', null), rendered }
        }
        return { ok: false, failure: failureOf('CHART_INCOMPLETE', 'sample', 'chart state evaluation failed'), rendered }
      }
      // An undefined chart state (page not responding with the shape) is not
      // chart-ready; keep polling under the shared deadline. A chart DOM node
      // that exists but is NOT visible is incomplete evidence: it can never
      // become visible without a new render, so it fails immediately rather
      // than burning the sample budget.
      if (state?.chart && state.chartVisible && !state.partial && !state.empty) {
        chartReady = true
        break
      }
      if (state?.chart && !state.chartVisible) {
        partial = true
        break
      }
      if (state?.partial || state?.empty) {
        partial = true
        break
      }
      if (effectiveDeadline.expired()) {
        return { ok: false, failure: insufficientRawTimeout(), rendered }
      }
      await boundedSleep(pollMs)
    }
    if (partial) {
      return { ok: false, failure: failureOf('CHART_INCOMPLETE', 'sample', 'river panel chart is partial or empty'), rendered }
    }
    if (!chartReady) {
      return { ok: false, failure: insufficientRawTimeout(), rendered }
    }

    let t1: number
    try {
      const evaluated = await withRiverClickDeadline(page.evaluate<number>('performance.now()'), effectiveDeadline, () => timeoutValue())
      if ((evaluated as unknown) === TIMEOUT_SENTINEL) {
        return { ok: false, failure: insufficientRawTimeout(), rendered }
      }
      t1 = evaluated as number
    } catch {
      if (wholeDeadline.expired()) {
        return { ok: false, failure: failureOf('WHOLE_RUN_TIMEOUT', 'sample', 'whole-run deadline exceeded reading t1', null), rendered }
      }
      return { ok: false, failure: failureOf('TIMING_INVALID', 'sample', 'browser t1 read failed'), rendered }
    }
    if (!Number.isFinite(t1)) {
      return { ok: false, failure: failureOf('TIMING_INVALID', 'sample', 'browser t1 is not finite'), rendered }
    }

    // Scoped close/unmount INSIDE the attempt lifetime: observation stays armed
    // through the close and the quiet interval. The exact hook object and the
    // exact `m11-map-surface` DOM node captured immediately before the required
    // close must still be the SAME objects after unmount; the panel ABSENT
    // before the scoped close is a FAIL (never a success). All identity proof
    // happens inside ONE page evaluate so no window global is ever added.
    let closed = false
    {
      const closeBudgetMs = effectiveDeadline.remaining()
      let outcome: { closed: boolean; mapPresent: boolean; mapSame: boolean; hookSame: boolean } | undefined
      try {
        // ONE function evaluate (real Playwright CALLS the function with the
        // captured JSHandles injected as the actual pre-dispatch objects). The
        // hook + map compared here are the SAME objects captured BEFORE
        // dispatch, so a replacement between dispatch and close is a FAIL. No
        // window global is added: the handles are function arguments.
        outcome = await withRiverClickDeadline(
          page.evaluate<RiverClickPanelCloseOutcome | Promise<RiverClickPanelCloseOutcome>>(
            closeRiverClickPanelInPage as (arg: unknown) => RiverClickPanelCloseOutcome | Promise<RiverClickPanelCloseOutcome>,
            { hook: hookHandle, map: mapHandle, budgetMs: Math.max(0, closeBudgetMs), pollMs },
          ),
          effectiveDeadline,
          () => timeoutValue(),
        )
        if ((outcome as unknown) === TIMEOUT_SENTINEL) {
          return { ok: false, failure: insufficientRawTimeout(), rendered }
        }
      } catch {
        if (wholeDeadline.expired()) {
          return { ok: false, failure: failureOf('WHOLE_RUN_TIMEOUT', 'sample', 'whole-run deadline exceeded during panel close', null), rendered }
        }
        return { ok: false, failure: failureOf('CHART_INCOMPLETE', 'sample', 'panel close evaluation failed'), rendered }
      }
      if (outcome === undefined || (outcome as unknown) === TIMEOUT_SENTINEL || !outcome.closed || !outcome.mapPresent || !outcome.mapSame || !outcome.hookSame) {
        return {
          ok: false,
          failure: failureOf(
            'CHART_INCOMPLETE',
            'sample',
            'panel did not close/unmount with the exact pre-dispatch hook object and map node preserved',
          ),
          rendered,
        }
      }
      closed = true
    }
    if (!closed) {
      return { ok: false, failure: failureOf('CHART_INCOMPLETE', 'sample', 'panel did not close/unmount after the attempt'), rendered }
    }

    // Quiet interval: observation stays armed. A matching/unexpected series
    // request arriving during quiet invalidates the attempt. The FULL quiet
    // must elapse: a shortened quiet (because the sample budget expired) is a
    // timeout, never an accepted attempt.
    if (unexpectedSeriesCount > 0) {
      return { ok: false, failure: failureOf('SERIES_REQUEST_INVALID', 'sample', 'unexpected forecast-series request during quiet interval'), rendered }
    }
    const quietFailure = await fullQuietSleep(quietMs)
    if (quietFailure !== null) {
      return { ok: false, failure: quietFailure, rendered }
    }
    if (unexpectedSeriesCount > 0) {
      return { ok: false, failure: failureOf('SERIES_REQUEST_INVALID', 'sample', 'unexpected forecast-series request during quiet interval'), rendered }
    }

    // After the FULL quiet interval and while the observer is STILL armed, the
    // exact pre-dispatch hook object and m11-map-surface node must STILL be the
    // SAME objects: a replacement during quiet would pass the earlier
    // close-time check but silently invalidate every future observation.
    let quietIdentity: { hookSame: boolean; mapSame: boolean } | null = null
    try {
      const outcome = await withRiverClickDeadline(
        page.evaluate<{ hookSame: boolean; mapSame: boolean }>(
          (captured: unknown) => {
            const c = captured as { hook: unknown; map: unknown }
            const currentHook = (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence
            return {
              hookSame: currentHook === c.hook,
              mapSame: document.querySelector('[data-testid="m11-map-surface"]') === c.map,
            }
          },
          { hook: hookHandle, map: mapHandle },
        ),
        effectiveDeadline,
        () => timeoutValue(),
      )
      if ((outcome as unknown) === TIMEOUT_SENTINEL) {
        return { ok: false, failure: insufficientRawTimeout(), rendered }
      }
      if (outcome !== undefined) quietIdentity = outcome
    } catch {
      // Probe evaluation failed: fail closed under the same rule as the chart
      // state read (whole-run expiry wins, otherwise closed incomplete).
      if (wholeDeadline.expired()) {
        return { ok: false, failure: failureOf('WHOLE_RUN_TIMEOUT', 'sample', 'whole-run deadline exceeded during quiet identity probe'), rendered }
      }
      return { ok: false, failure: failureOf('CHART_INCOMPLETE', 'sample', 'quiet identity probe evaluation failed'), rendered }
    }
    if (quietIdentity === null || !quietIdentity.hookSame || !quietIdentity.mapSame) {
      return {
        ok: false,
        failure: failureOf('CHART_INCOMPLETE', 'sample', 'hook/map identity changed during the quiet interval'),
        rendered,
      }
    }

    return { ok: true, rendered, t0Ms: t0, t1Ms: t1, gfsStatus, ifsStatus }
  } catch (error) {
    // Any unexpected lane exception becomes a bounded INTERNAL_ERROR (never raw
    // exception text) after the observation is fully disarmed.
    return { ok: false, failure: failureOf('INTERNAL_ERROR', 'sample', 'river-click attempt failed internally'), rendered: renderedSeen }
  } finally {
    disarm()
  }
  })()
  // Dispose BOTH pre-dispatch handles after the terminal is decided, AWAITING
  // every disposal promise (real Playwright disposal is async; voiding it would
  // leak the JSHandles past the attempt settle). Promise.allSettled swallows
  // rejections so a disposal failure can NEVER mask the attempt terminal.
  if (ownedHandles.length > 0) {
    await Promise.allSettled(ownedHandles.map((handle) => {
      try {
        return handle.dispose()
      } catch {
        return Promise.resolve()
      }
    }))
  }
  return result
}

/** Re-export the attempt failure filter used by orchestrator/tests. */
export function isAttemptFailure(result: RiverClickAttemptResult): result is { ok: false; failure: RiverClickFailureShape; rendered: RiverClickFeatureIdentity | null } {
  return !result.ok
}

/** Keep the per-sample deadline constant importable for the orchestrator. */
export { RIVER_CLICK_PER_SAMPLE_DEADLINE_MS }

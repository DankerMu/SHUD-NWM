import { describe, expect, it, vi } from 'vitest'

import { RIVER_CLICK_WHOLE_RUN_DEADLINE_MS } from '../lib/riverClickEvidence/constants'
import { parseRiverClickConfig } from '../lib/riverClickEvidence/config'
import { createRiverClickDeadline } from '../lib/riverClickEvidence/deadline'
import {
  boundedMessage,
  readResponseBounded,
  resolveRiverClickIdentity,
  runRiverClickAttempt,
  runRiverClickLane,
  laneFailure,
  type RiverClickLaneBrowserRequest,
  type RiverClickLaneBrowserResponse,
  type RiverClickLanePageSurface,
} from '../../playwright.river-click-lane'
import type { RiverClickLaneIdentity } from '../../playwright.river-click-lane'
import { makeFakePage, makeFakePageState, type RiverClickFakePageState } from '../test/riverClickFakePage'
import { RIVER_CLICK_EXACT_THRESHOLD_DURATIONS } from '../test/riverClickThresholdFixture'

type FakePageState = RiverClickFakePageState

const CONFIG = {
  PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test',
  PLAYWRIGHT_LIVE_API_BASE_URL: 'https://api.example.test',
  PLAYWRIGHT_LIVE_RIVER_BASIN_ID: 'basins_qhh',
  PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID: 'seg-001',
  PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: '/private/evidence/nhms-frontend-river-click-live-evidence-1.json',
}

function config() {
  const parsed = parseRiverClickConfig(CONFIG)
  if (!parsed.ok) throw new Error('fixture config must parse')
  return parsed.config
}

function productPayload(source: 'GFS' | 'IFS') {
  return {
    basin_id: 'basins_qhh',
    model_id: source === 'GFS' ? 'model-gfs' : 'model-ifs',
    basin_version_id: 'bv-001',
    river_network_version_id: 'rn-001',
    source_id: source,
    // IFS cycle is a separate 06:00Z cycle, matching the series fixture below.
    cycle_time: source === 'GFS' ? '2026-09-02T00:00:00Z' : '2026-09-02T06:00:00Z',
    run_id: `run-${source.toLowerCase()}`,
    forcing_version_id: 'f-001',
    station_count: 0,
    expected_station_count: null,
    segment_count: 0,
    expected_segment_count: null,
    status: 'ready',
    run_status: 'finished',
    valid_time_start: null,
    valid_time_end: null,
    river_valid_time_start: null,
    river_valid_time_end: null,
    forcing_valid_time_start: null,
    forcing_valid_time_end: null,
    available_horizon_hours: 240,
    expected_horizon_hours: 240,
    shorter_horizon: false,
    availability: { ready: true, unavailable_reasons: [], quality_flags: [], quality_notes: [] },
    quality: { status: 'ok' },
  }
}

const SEGMENT_PAYLOAD = {
  river_segment_id: 'seg-001',
  river_network_version_id: 'rn-001',
  segment_order: 1,
  downstream_segment_id: null,
  length_m: 1000,
  geom: { type: 'LineString', coordinates: [[100, 30], [101, 31], [102, 32]] },
  properties_json: {},
  created_at: '2026-01-01T00:00:00Z',
}

function envelope(data: unknown) {
  return { status: 'ok', data }
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'content-type': 'application/json', 'content-encoding': 'identity' },
  })
}

function fakeFetch(routes: Record<string, (url: string) => Response>) {
  return vi.fn(async (url: string, init: RequestInit) => {
    const pathname = new URL(url).pathname
    const route = routes[pathname]
    if (!route) throw new Error(`no fake route for ${pathname}`)
    return route(url)
  })
}

function defaultFetch() {
  return fakeFetch({
    '/api/v1/mvp/qhh/latest-product': (url) =>
      jsonResponse(envelope(new URL(url).searchParams.get('source') === 'GFS' ? productPayload('GFS') : productPayload('IFS'))),
    '/api/v1/basin-versions/bv-001/river-segments/seg-001': () => jsonResponse(envelope(SEGMENT_PAYLOAD)),
  })
}

describe('river-click lane preflight identity resolution', () => {
  it('resolves current GFS+IFS identity and segment framing from current APIs', async () => {
    const fetchImpl = fakeFetch({
      '/api/v1/mvp/qhh/latest-product': (url) =>
        jsonResponse(envelope(new URL(url).searchParams.get('source') === 'GFS' ? productPayload('GFS') : productPayload('IFS'))),
      '/api/v1/basin-versions/bv-001/river-segments/seg-001': () => jsonResponse(envelope(SEGMENT_PAYLOAD)),
    })
    const result = await resolveRiverClickIdentity(config(), fetchImpl as never)
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('preflight must resolve')
    expect(result.identity.requestedFeature).toEqual({
      basinId: 'basins_qhh',
      riverSegmentId: 'seg-001',
      basinVersionId: 'bv-001',
      riverNetworkVersionId: 'rn-001',
    })
    expect(result.identity.bbox).toEqual([[100, 30], [102, 32]])
    expect(result.identity.anchor).toEqual([101, 31])
  })

  it('preflight has NO rendered feature: rendered identity is absent until the hook returns it', async () => {
    const fetchImpl = defaultFetch()
    const result = await resolveRiverClickIdentity(config(), fetchImpl as never)
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('preflight must resolve')
    expect('renderedFeature' in result.identity).toBe(false)
  })

  it('issues exactly three preflight requests with credentials omit and exact query sets', async () => {
    const seen: Array<{ url: string; init: RequestInit }> = []
    const fetchImpl = vi.fn(async (url: string, init: RequestInit) => {
      seen.push({ url, init })
      const parsed = new URL(url)
      if (parsed.pathname === '/api/v1/mvp/qhh/latest-product') {
        return jsonResponse(envelope(new URL(url).searchParams.get('source') === 'GFS' ? productPayload('GFS') : productPayload('IFS')))
      }
      return jsonResponse(envelope(SEGMENT_PAYLOAD))
    })
    const result = await resolveRiverClickIdentity(config(), fetchImpl as never)
    expect(result.ok).toBe(true)
    expect(seen).toHaveLength(3)
    expect(seen.map((entry) => new URL(entry.url).pathname).sort()).toEqual([
      '/api/v1/basin-versions/bv-001/river-segments/seg-001',
      '/api/v1/mvp/qhh/latest-product',
      '/api/v1/mvp/qhh/latest-product',
    ])
    for (const entry of seen) {
      expect(entry.init.credentials).toBe('omit')
    }
    const latest = seen.filter((entry) => entry.url.includes('latest-product')).map((entry) => new URL(entry.url).search)
    expect(latest).toEqual([
      '?source=GFS&identity_only=true&basin_id=basins_qhh',
      '?source=IFS&identity_only=true&basin_id=basins_qhh',
    ])
    const detail = seen.find((entry) => entry.url.includes('river-segments'))!
    expect(detail.url).toBe(
      'https://api.example.test/api/v1/basin-versions/bv-001/river-segments/seg-001?river_network_version_id=rn-001',
    )
  })

  it('aborts a hanging preflight fetch with WHOLE_RUN_TIMEOUT under a tiny deadline (non-hanging)', async () => {
    // Holder defeats closure control-flow narrowing: the mock assigns inside a
    // callback, so a plain `let signal: AbortSignal | null` would be narrowed
    // to `null` (never) at the assertion site.
    const signalHolder: { value: AbortSignal | null } = { value: null }
    const fetchImpl = vi.fn((_url: string, init: RequestInit): Promise<Response> => {
      signalHolder.value = init.signal ?? null
      return new Promise<Response>(() => undefined)
    })
    const started = Date.now()
    const clock = () => Date.now()
    const result = await resolveRiverClickIdentity(config(), fetchImpl, createRiverClickDeadline(30, clock))
    const elapsed = Date.now() - started
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.failure.code).toBe('WHOLE_RUN_TIMEOUT')
      expect(result.failure.stage).toBe('preflight')
    }
    expect(elapsed).toBeLessThan(5000)
    expect(signalHolder.value?.aborted).toBe(true)
  })

  it('fails closed when GFS and IFS disagree on current version identity', async () => {
    const ifsDifferent = productPayload('IFS')
    ifsDifferent.river_network_version_id = 'rn-other'
    const fetchImpl2 = fakeFetch({
      '/api/v1/mvp/qhh/latest-product': (url) =>
        jsonResponse(envelope(new URL(url).searchParams.get('source') === 'GFS' ? productPayload('GFS') : ifsDifferent)),
      '/api/v1/basin-versions/bv-001/river-segments/seg-001': () => jsonResponse(envelope(SEGMENT_PAYLOAD)),
    })
    const result = await resolveRiverClickIdentity(config(), fetchImpl2 as never)
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.failure.code).toBe('IDENTITY_MISMATCH')
  })

  it('flags non-2xx preflight responses as PREFLIGHT_HTTP_ERROR', async () => {
    const fetchImpl = fakeFetch({
      '/api/v1/mvp/qhh/latest-product': () => jsonResponse({ status: 'error' }, 503),
    })
    const result = await resolveRiverClickIdentity(config(), fetchImpl as never)
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.failure.code).toBe('PREFLIGHT_HTTP_ERROR')
  })
})

function makeIdentity(): RiverClickLaneIdentity {
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
    bbox: [[100, 30], [102, 32]],
    anchor: [100.5, 30.5],
  }
}

function seriesQuery(source: 'GFS' | 'IFS') {
  const product = source === 'GFS'
    ? { run_id: 'run-gfs', model_id: 'model-gfs', issue_time: '2026-09-02T00:00:00Z', scenarios: 'forecast_gfs_deterministic' }
    : { run_id: 'run-ifs', model_id: 'model-ifs', issue_time: '2026-09-02T06:00:00Z', scenarios: 'forecast_ifs_deterministic' }
  const params = new URLSearchParams({
    river_network_version_id: 'rn-001',
    variables: 'q_down',
    include_analysis: 'false',
    ...product,
  })
  return `https://api.example.test/api/v1/basin-versions/bv-001/river-segments/seg-001/forecast-series?${params.toString()}`
}

function emit(state: FakePageState, event: string, arg: unknown) {
  for (const listener of [...state.listeners[event]]) {
    ;(listener as (value: unknown) => void)(arg)
  }
}

function requestOf(method: string, url: string): RiverClickLaneBrowserRequest {
  return { method: () => method, url: () => url }
}

function responseOf(request: RiverClickLaneBrowserRequest, finished: () => Promise<unknown> = () => Promise.resolve(null)): RiverClickLaneBrowserResponse {
  const url = request.url()
  return { url: () => url, status: () => 200, finished, request: () => request, method: () => request.method() }
}

/** Emit one waiting pair: request event then response event sharing the Request object. */
function emitSeriesPair(state: FakePageState, source: 'GFS' | 'IFS', finished: () => Promise<unknown> = () => Promise.resolve(null)) {
  const url = seriesQuery(source)
  const req = requestOf('GET', url)
  emit(state, 'request', req)
  emit(state, 'response', responseOf(req, finished))
}

const HOOK_OK = {
  basinId: 'basins_qhh',
  riverSegmentId: 'seg-001',
  basinVersionId: 'bv-001',
  riverNetworkVersionId: 'rn-001',
  dispatchNowMs: 1000,
}

describe('river-click lane attempt observation', () => {
  it('flags only a partial GFS response with a request-without-response as SERIES_RESPONSE_ERROR', async () => {
    const state: FakePageState = {
      listeners: { request: [], response: [], requestfailed: [] },
      handleDisposals: 0,
      evaluateNames: [],
      sleepMs: 1,
      evaluateImpl: (text) => {
        if (text.includes('selectRenderedRiver')) {
          emitSeriesPair(state, 'GFS')
          return HOOK_OK
        }
        if (text.includes('m11-river-panel-chart')) return { chart: false, partial: false, empty: false }
        return undefined
      },
    }
    const attempt = await runRiverClickAttempt(
      { config: config(), page: makeFakePage(state) },
      makeIdentity(),
      makeIdentity().requestedFeature,
      { attemptDeadlineMs: 150, pollMs: 5, quietMs: 10 },
    )
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) expect(attempt.failure.code).toBe('SAMPLE_TIMEOUT')
  })

  it('classifies a malformed percent-encoding series URL as SERIES_REQUEST_INVALID at observer boundary', async () => {
    const url = 'https://api.example.test/api/v1/basin-versions/bv-001/river-segments/seg-001/forecast-series?issue_time=%E0%A4%A'
    const state: FakePageState = {
      listeners: { request: [], response: [], requestfailed: [] },
      handleDisposals: 0,
      evaluateNames: [],
      sleepMs: 1,
      evaluateImpl: (text) => {
        if (text.includes('selectRenderedRiver')) {
          emit(state, 'request', requestOf('GET', url))
          return HOOK_OK
        }
        return undefined
      },
    }
    const attempt = await runRiverClickAttempt(
      { config: config(), page: makeFakePage(state) },
      makeIdentity(),
      makeIdentity().requestedFeature,
      { attemptDeadlineMs: 150, pollMs: 5, quietMs: 10 },
    )
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) expect(attempt.failure.code).toBe('SERIES_REQUEST_INVALID')
  })

  it('refuses a double-encoded issue_time at the observer boundary (no second decode)', async () => {
    const url = `https://api.example.test/api/v1/basin-versions/bv-001/river-segments/seg-001/forecast-series?${new URLSearchParams({
      river_network_version_id: 'rn-001',
      variables: 'q_down',
      include_analysis: 'false',
      run_id: 'run-gfs',
      model_id: 'model-gfs',
      scenarios: 'forecast_gfs_deterministic',
      issue_time: '2026-09-02T00%3A00%3A00.000Z',
    }).toString()}`
    const state: FakePageState = {
      listeners: { request: [], response: [], requestfailed: [] },
      handleDisposals: 0,
      evaluateNames: [],
      sleepMs: 1,
      evaluateImpl: (text) => {
        if (text.includes('selectRenderedRiver')) {
          emit(state, 'request', requestOf('GET', url))
          return HOOK_OK
        }
        return undefined
      },
    }
    const attempt = await runRiverClickAttempt(
      { config: config(), page: makeFakePage(state) },
      makeIdentity(),
      makeIdentity().requestedFeature,
      { attemptDeadlineMs: 150, pollMs: 5, quietMs: 10 },
    )
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) expect(attempt.failure.code).toBe('SERIES_REQUEST_INVALID')
  })

  it('classifies a wrong-method forecast-series request as SERIES_REQUEST_INVALID at request time', async () => {
    const state: FakePageState = {
      listeners: { request: [], response: [], requestfailed: [] },
      handleDisposals: 0,
      evaluateNames: [],
      sleepMs: 1,
      evaluateImpl: (text) => {
        if (text.includes('selectRenderedRiver')) {
          emit(state, 'request', requestOf('POST', seriesQuery('GFS')))
          return HOOK_OK
        }
        return undefined
      },
    }
    const attempt = await runRiverClickAttempt(
      { config: config(), page: makeFakePage(state) },
      makeIdentity(),
      makeIdentity().requestedFeature,
      { attemptDeadlineMs: 150, pollMs: 5, quietMs: 10 },
    )
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) {
      expect(attempt.failure.code).toBe('SERIES_REQUEST_INVALID')
      // The message must not carry the raw method/URL/query material.
      expect(attempt.failure.message).not.toMatch(/POST|run_id|issue_time|variables/)
    }
  })

  it('classifies an over-identity series request (wrong run_id) as SERIES_REQUEST_INVALID', async () => {
    const url = seriesQuery('GFS').replace('run_id=run-gfs', 'run_id=run-other')
    const state: FakePageState = {
      listeners: { request: [], response: [], requestfailed: [] },
      handleDisposals: 0,
      evaluateNames: [],
      sleepMs: 1,
      evaluateImpl: (text) => {
        if (text.includes('selectRenderedRiver')) {
          emit(state, 'request', requestOf('GET', url))
          return HOOK_OK
        }
        return undefined
      },
    }
    const attempt = await runRiverClickAttempt(
      { config: config(), page: makeFakePage(state) },
      makeIdentity(),
      makeIdentity().requestedFeature,
      { attemptDeadlineMs: 150, pollMs: 5, quietMs: 10 },
    )
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) expect(attempt.failure.code).toBe('SERIES_REQUEST_INVALID')
  })

  it('fails a duplicate GFS request during the quiet interval and does not count the attempt', async () => {
    const state: FakePageState = {
      listeners: { request: [], response: [], requestfailed: [] },
      handleDisposals: 0,
      evaluateNames: [],
      sleepMs: 1,
      evaluateImpl: () => undefined,
    }
    state.evaluateImpl = (text) => {
      if (text.includes('selectRenderedRiver')) {
        emitSeriesPair(state, 'GFS')
        emitSeriesPair(state, 'IFS')
        return HOOK_OK
      }
      if (text.includes('m11-river-panel-chart')) return { chart: true, chartVisible: true, partial: false, empty: false }
      if (text.includes('performance.now()')) return 1500
      return undefined
    }
    state.closeImpl = () => {
      if (!state.evaluateNames.some((name) => name.includes('quiet-dup-sent'))) {
        state.evaluateNames.push('quiet-dup-sent')
        emit(state, 'request', requestOf('GET', seriesQuery('GFS')))
      }
      return { closed: true, mapPresent: true, mapSame: true, hookSame: true }
    }
    const attempt = await runRiverClickAttempt(
      { config: config(), page: makeFakePage(state) },
      makeIdentity(),
      makeIdentity().requestedFeature,
      { attemptDeadlineMs: 500, pollMs: 5, quietMs: 30 },
    )
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) expect(attempt.failure.code).toBe('SERIES_REQUEST_INVALID')
    expect(state.listeners.response).toHaveLength(0)
    expect(state.listeners.request).toHaveLength(0)
  })

  it('records a synchronous throw from response.url/status/request/finished as fixed codes, never escaping the callback', async () => {
    const state: FakePageState = {
      listeners: { request: [], response: [], requestfailed: [] },
      handleDisposals: 0,
      evaluateNames: [],
      sleepMs: 1,
      evaluateImpl: () => undefined,
    }
    state.evaluateImpl = (text) => {
      if (text.includes('selectRenderedRiver')) {
        emit(state, 'request', requestOf('GET', seriesQuery('GFS')))
        emit(state, 'response', {
          url: () => { throw new Error('url boom') },
          status: () => { throw new Error('status boom') },
          finished: () => { throw new Error('finished boom') },
          request: () => { throw new Error('request boom') },
        } as never)
        emitSeriesPair(state, 'IFS')
        return HOOK_OK
      }
      return undefined
    }
    const attempt = await runRiverClickAttempt(
      { config: config(), page: makeFakePage(state) },
      makeIdentity(),
      makeIdentity().requestedFeature,
      { attemptDeadlineMs: 150, pollMs: 5, quietMs: 10 },
    )
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) {
      expect(['SERIES_RESPONSE_ERROR', 'SERIES_REQUEST_INVALID', 'CHART_INCOMPLETE', 'SAMPLE_TIMEOUT']).toContain(attempt.failure.code)
      expect(attempt.failure.message).not.toMatch(/boom|url|status|finished/)
    }
  })

  it('treats response.finished() resolving an Error as a failure, not success', async () => {
    const state: FakePageState = {
      listeners: { request: [], response: [], requestfailed: [] },
      handleDisposals: 0,
      evaluateNames: [],
      sleepMs: 1,
      evaluateImpl: (text) => {
        if (text.includes('selectRenderedRiver')) {
          emitSeriesPair(state, 'GFS', () => Promise.resolve(new Error('stream reset')))
          emitSeriesPair(state, 'IFS')
          return HOOK_OK
        }
        return undefined
      },
    }
    const attempt = await runRiverClickAttempt(
      { config: config(), page: makeFakePage(state) },
      makeIdentity(),
      makeIdentity().requestedFeature,
      { attemptDeadlineMs: 150, pollMs: 5, quietMs: 10 },
    )
    expect(attempt.ok).toBe(false)
    if (!attempt.ok) expect(attempt.failure.code).toBe('SERIES_RESPONSE_ERROR')
  })

  it('cleans up all listeners after a failed attempt (no leak, armed through close/quiet)', async () => {
    const state: FakePageState = {
      listeners: { request: [], response: [], requestfailed: [] },
      handleDisposals: 0,
      evaluateNames: [],
      sleepMs: 1,
      evaluateImpl: (text) => {
        if (text.includes('selectRenderedRiver')) return HOOK_OK
        return undefined
      },
    }
    const attempt = await runRiverClickAttempt(
      { config: config(), page: makeFakePage(state) },
      makeIdentity(),
      makeIdentity().requestedFeature,
      { attemptDeadlineMs: 100, pollMs: 5, quietMs: 10 },
    )
    expect(attempt.ok).toBe(false)
    expect(state.listeners.request).toHaveLength(0)
    expect(state.listeners.response).toHaveLength(0)
    expect(state.listeners.requestfailed).toHaveLength(0)
  })
})

describe('river-click lane closed failure helper', () => {
  it('builds a bounded terminal without any sample claim', () => {
    const terminal = laneFailure('REQUIRED_ENV_MISSING', 'runtime', 'missing env')
    if (terminal.failure === null) throw new Error('lane failure must carry a classification')
    expect(terminal.failure.code).toBe('REQUIRED_ENV_MISSING')
    expect(terminal.samples).toEqual([])
    expect(terminal.warmup).toBeNull()
    expect(terminal.p95Ms).toBeNull()
  })
})

describe('river-click lane orchestrator', () => {
  it('fails closed when the pre-start hook flag cannot be installed', async () => {
    const page = makeFakePage({ ...makeFakePageState(), evaluateImpl: () => undefined })
    ;(page.addInitScript as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('no init'))
    const result = await runRiverClickLane({ config: config(), page }, defaultFetch() as never)
    expect(result.ok).toBe(false)
    if (!result.ok) {
      if (result.terminal.failure === null) throw new Error('lane terminal must carry a classification')
      expect(result.terminal.failure.code).toBe('HOOK_PREREQUISITE_MISSING')
    }
  })

  it('runs one complete warmup plus 20 serial fresh-source attempts: 42 requests/42 correlated responses/21 hook calls/21 closes, listeners armed through every close+quiet and back to zero', async () => {
    const state: FakePageState = {
      listeners: { request: [], response: [], requestfailed: [] },
      handleDisposals: 0,
      evaluateNames: [],
      evaluateImpl: () => undefined,
    }
    let attemptCount = 0
    let closeCount = 0
    let requestEventCount = 0
    let correlatedResponseCount = 0
    // Wrap emit so every request/response event is counted as it fires.
    const emitCounted = (event: string, arg: unknown) => {
      if (event === 'request') requestEventCount += 1
      if (event === 'response') correlatedResponseCount += 1
      emit(state, event, arg)
    }
    // Route emitSeriesPair through the counted emitter.
    const countedSeries = (source: 'GFS' | 'IFS') => {
      const url = seriesQuery(source)
      const req = requestOf('GET', url)
      emitCounted('request', req)
      emitCounted('response', responseOf(req, () => Promise.resolve(null)))
    }
    const activeDuringClose: Array<{ request: number; response: number; requestfailed: number }> = []
    state.evaluateImpl = (text) => {
      if (text.includes('typeof window.__nhmsRiverClickEvidence.selectRenderedRiver')) return true
      if (text.includes('selectRenderedRiver')) {
        attemptCount += 1
        const attempt = attemptCount
        countedSeries('GFS')
        countedSeries('IFS')
        return { ...HOOK_OK, dispatchNowMs: 1000 + attempt * 0.5 }
      }
      if (text.includes('m11-river-panel-chart')) return { chart: true, chartVisible: true, partial: false, empty: false }
      if (text.includes('performance.now()')) return 1050 + attemptCount * 0.5
      return undefined
    }
    state.closeImpl = () => {
      closeCount += 1
      activeDuringClose.push({
        request: state.listeners.request.length,
        response: state.listeners.response.length,
        requestfailed: state.listeners.requestfailed.length,
      })
      return { closed: true, mapPresent: true, mapSame: true, hookSame: true }
    }
    const result = await runRiverClickLane(
      { config: config(), page: makeFakePage(state) },
      defaultFetch() as never,
      { deadlineMs: RIVER_CLICK_WHOLE_RUN_DEADLINE_MS, attemptDeadlineMs: 500, mapDeadlineMs: 500, pollMs: 2, quietMs: 10 },
    )
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error(`lane must succeed: ${result.terminal.failure?.code}`)
    const terminal = result.terminal
    expect(terminal.samples).toHaveLength(20)
    expect(terminal.warmup).not.toBeNull()
    expect(terminal.p95Ms).not.toBeNull()
    // exact counts: 21 hook dispatches (warmup + 20), 42 series requests,
    // 42 exact-request-correlated completed responses, 21 scoped closes.
    expect(attemptCount).toBe(21)
    expect(requestEventCount).toBe(42)
    expect(correlatedResponseCount).toBe(42)
    expect(closeCount).toBe(21)
    // warmup index 0, samples exactly 1..20.
    expect(terminal.warmup?.index).toBe(0)
    for (let i = 0; i < terminal.samples.length; i += 1) {
      expect(terminal.samples[i].index).toBe(i + 1)
    }
    // listeners were active during every close+quiet...
    expect(activeDuringClose).toHaveLength(21)
    for (const active of activeDuringClose) {
      expect(active.request).toBeGreaterThan(0)
      expect(active.response).toBeGreaterThan(0)
      expect(active.requestfailed).toBeGreaterThan(0)
    }
    // ...and are fully removed afterwards.
    expect(state.listeners.request).toHaveLength(0)
    expect(state.listeners.response).toHaveLength(0)
    expect(state.listeners.requestfailed).toHaveLength(0)
    // Every created JSHandle (2 per attempt x 21 attempts) is disposed.
    expect(state.handleDisposals).toBe(42)
  })

  it('classifies a 20-duration fixture whose sorted index 18 is exactly 2000 as THRESHOLD_EXCEEDED with p95_ms=2000', async () => {
    const state: FakePageState = {
      listeners: { request: [], response: [], requestfailed: [] },
      handleDisposals: 0,
      evaluateNames: [],
      evaluateImpl: () => undefined,
    }
    let attemptCount = 0
    state.evaluateImpl = (text) => {
      if (text.includes('typeof window.__nhmsRiverClickEvidence.selectRenderedRiver')) return true
      if (text.includes('selectRenderedRiver')) {
        attemptCount += 1
        emitSeriesPair(state, 'GFS')
        emitSeriesPair(state, 'IFS')
        return { ...HOOK_OK, dispatchNowMs: 1000 }
      }
      if (text.includes('m11-river-panel-chart')) return { chart: true, chartVisible: true, partial: false, empty: false }
      if (text.includes('performance.now()')) {
        const sampleIndex = Math.max(0, attemptCount - 2)
        const duration = attemptCount === 1 ? 100 : RIVER_CLICK_EXACT_THRESHOLD_DURATIONS[sampleIndex]
        return 1000 + duration
      }
      return undefined
    }
    state.closeImpl = () => ({ closed: true, mapPresent: true, mapSame: true, hookSame: true })
    const result = await runRiverClickLane(
      { config: config(), page: makeFakePage(state) },
      defaultFetch() as never,
      { deadlineMs: RIVER_CLICK_WHOLE_RUN_DEADLINE_MS, attemptDeadlineMs: 500, mapDeadlineMs: 500, pollMs: 2, quietMs: 10 },
    )
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('exact-threshold lane must FAIL')
    expect(result.terminal.failure?.code).toBe('THRESHOLD_EXCEEDED')
    expect(result.terminal.p95Ms).toBe(2000)
    expect(result.terminal.samples).toHaveLength(20)
    expect(result.terminal.warmup).not.toBeNull()
  })

  it('fails at sample 7 while preserving warmup + samples 1..6 and rendered identity from the first success', async () => {
    const state: FakePageState = {
      listeners: { request: [], response: [], requestfailed: [] },
      handleDisposals: 0,
      evaluateNames: [],
      sleepMs: 1,
      evaluateImpl: () => undefined,
    }
    let attemptCount = 0
    state.evaluateImpl = (text) => {
      if (text.includes('typeof window.__nhmsRiverClickEvidence.selectRenderedRiver')) return true
      if (text.includes('selectRenderedRiver')) {
        attemptCount += 1
        if (attemptCount === 8) {
          // sample 7 fails: the hook resolves but no series request is emitted.
          return { ...HOOK_OK, dispatchNowMs: 1000 }
        }
        emitSeriesPair(state, 'GFS')
        emitSeriesPair(state, 'IFS')
        return { ...HOOK_OK, dispatchNowMs: 1000 + attemptCount * 0.5 }
      }
      if (text.includes('m11-river-panel-chart')) return { chart: true, chartVisible: true, partial: false, empty: false }
      if (text.includes('performance.now()')) return 1050 + attemptCount * 0.5
      return undefined
    }
    state.closeImpl = () => ({ closed: true, mapPresent: true, mapSame: true, hookSame: true })
    const result = await runRiverClickLane(
      { config: config(), page: makeFakePage(state) },
      defaultFetch() as never,
      { deadlineMs: 10_000, attemptDeadlineMs: 120, mapDeadlineMs: 500, pollMs: 2, quietMs: 10 },
    )
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('sample 7 must fail')
    expect(result.terminal.warmup).not.toBeNull()
    expect(result.terminal.samples).toHaveLength(6)
    expect(result.terminal.samples.map((sample) => sample.index)).toEqual([1, 2, 3, 4, 5, 6])
    expect(result.terminal.failure?.sampleIndex).toBe(7)
    expect(result.terminal.failure?.stage).toBe('sample')
    const expectedRendered = { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' }
    expect(result.terminal.renderedFeature).toEqual(expectedRendered)
    expect(result.terminal.requestedFeature).toEqual(expectedRendered)
    expect(result.terminal.gfs).not.toBeNull()
    expect(result.terminal.ifs).not.toBeNull()
  })

  it('preserves the exact hook-returned rendered identity when the warmup hook succeeds but one source times out', async () => {
    const state: FakePageState = {
      listeners: { request: [], response: [], requestfailed: [] },
      handleDisposals: 0,
      evaluateNames: [],
      sleepMs: 1,
      evaluateImpl: () => undefined,
    }
    let attemptCount = 0
    const expectedRendered = { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' }
    state.evaluateImpl = (text) => {
      if (text.includes('typeof window.__nhmsRiverClickEvidence.selectRenderedRiver')) return true
      if (text.includes('selectRenderedRiver')) {
        attemptCount += 1
        // Warmup: hook SUCCEEDS, only GFS emits; IFS never arrives -> timeout.
        emitSeriesPair(state, 'GFS')
        return { ...expectedRendered, dispatchNowMs: 1000 }
      }
      return undefined
    }
    const result = await runRiverClickLane(
      { config: config(), page: makeFakePage(state) },
      defaultFetch() as never,
      { deadlineMs: 10_000, attemptDeadlineMs: 100, mapDeadlineMs: 500, pollMs: 2, quietMs: 10 },
    )
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('warmup must fail on missing IFS')
    // The rendered identity is the ACTUAL hook return, even though the attempt
    // failed during the series wait; no sample/warmup is counted.
    expect(result.terminal.failure?.stage).toBe('warmup')
    expect(result.terminal.failure?.sampleIndex).toBe(0)
    expect(result.terminal.renderedFeature).toEqual(expectedRendered)
    expect(result.terminal.samples).toEqual([])
    expect(result.terminal.warmup).toBeNull()
  })

  it('caps a hook-rejection warmup failure at stage warmup/sample_index 0 with rendered_feature still null', async () => {
    const state: FakePageState = {
      listeners: { request: [], response: [], requestfailed: [] },
      handleDisposals: 0,
      evaluateNames: [],
      sleepMs: 1,
      evaluateImpl: () => undefined,
    }
    let attemptCount = 0
    state.evaluateImpl = (text) => {
      if (text.includes('typeof window.__nhmsRiverClickEvidence.selectRenderedRiver')) return true
      if (text.includes('selectRenderedRiver')) {
        attemptCount += 1
        // Hook REJECTS: no rendered identity is ever produced.
        throw new Error('hook selection rejected')
      }
      return undefined
    }
    const result = await runRiverClickLane(
      { config: config(), page: makeFakePage(state) },
      defaultFetch() as never,
      { deadlineMs: 10_000, attemptDeadlineMs: 100, mapDeadlineMs: 500, pollMs: 2, quietMs: 10 },
    )
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('warmup must fail')
    expect(result.terminal.failure?.stage).toBe('warmup')
    expect(result.terminal.failure?.sampleIndex).toBe(0)
    // The hook never succeeded, so rendered_feature stays null.
    expect(result.terminal.renderedFeature).toBeNull()
    expect(result.terminal.samples).toEqual([])
    expect(result.terminal.warmup).toBeNull()
  })

  it('returns WHOLE_RUN_TIMEOUT, not SAMPLE_TIMEOUT, when the global deadline expires first', async () => {
    const state: FakePageState = {
      listeners: { request: [], response: [], requestfailed: [] },
      handleDisposals: 0,
      evaluateNames: [],
      sleepMs: 1,
      evaluateImpl: () => undefined,
    }
    let attemptCount = 0
    state.evaluateImpl = (text) => {
      if (text.includes('typeof window.__nhmsRiverClickEvidence.selectRenderedRiver')) return true
      if (text.includes('selectRenderedRiver')) {
        attemptCount += 1
        emitSeriesPair(state, 'GFS')
        emitSeriesPair(state, 'IFS')
        return { ...HOOK_OK, dispatchNowMs: 1000 }
      }
      return undefined
    }
    // whole-run 60ms expires during the first attempt's response wait (attempt 500ms).
    const result = await runRiverClickLane(
      { config: config(), page: makeFakePage(state) },
      defaultFetch() as never,
      { deadlineMs: 60, attemptDeadlineMs: 500, mapDeadlineMs: 500, pollMs: 5, quietMs: 10 },
    )
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('must time out')
    expect(result.terminal.failure?.code).toBe('WHOLE_RUN_TIMEOUT')
  })

  it('returns SAMPLE_TIMEOUT when the per-sample deadline expires while the whole-run deadline is far away', async () => {
    const state: FakePageState = {
      listeners: { request: [], response: [], requestfailed: [] },
      handleDisposals: 0,
      evaluateNames: [],
      sleepMs: 1,
      evaluateImpl: () => undefined,
    }
    let attemptCount = 0
    state.evaluateImpl = (text) => {
      if (text.includes('typeof window.__nhmsRiverClickEvidence.selectRenderedRiver')) return true
      if (text.includes('selectRenderedRiver')) {
        attemptCount += 1
        emitSeriesPair(state, 'GFS')
        return { ...HOOK_OK, dispatchNowMs: 1000 }
      }
      return undefined
    }
    const result = await runRiverClickLane(
      { config: config(), page: makeFakePage(state) },
      defaultFetch() as never,
      { deadlineMs: 10_000, attemptDeadlineMs: 60, mapDeadlineMs: 500, pollMs: 5, quietMs: 10 },
    )
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('must time out')
    expect(result.terminal.failure?.code).toBe('SAMPLE_TIMEOUT')
    expect(result.terminal.failure?.sampleIndex).toBe(0)
    expect(result.terminal.failure?.stage).toBe('warmup')
  })

  it('waits for the post-goto hook up to the bounded map deadline and FAILs when absent', async () => {
    const state: FakePageState = {
      listeners: { request: [], response: [], requestfailed: [] },
      handleDisposals: 0,
      evaluateNames: [],
      sleepMs: 1,
      evaluateImpl: () => undefined,
    }
    let hookChecks = 0
    state.evaluateImpl = (text) => {
      if (text.includes('typeof window.__nhmsRiverClickEvidence.selectRenderedRiver')) {
        hookChecks += 1
        return false
      }
      return undefined
    }
    const result = await runRiverClickLane(
      { config: config(), page: makeFakePage(state) },
      defaultFetch() as never,
      { deadlineMs: 10_000, attemptDeadlineMs: 300, mapDeadlineMs: 100, pollMs: 5, quietMs: 10 },
    )
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.terminal.failure?.code).toBe('HOOK_SELECTION_FAILED')
    }
    expect(hookChecks).toBeGreaterThan(0)
  })
})

describe('river-click bounded response streaming reader', () => {
  it('retains at most max+1 bytes from a real multi-chunk over-limit Response body', async () => {
    const max = 262_144
    const chunk = new Uint8Array(65_536).fill(0x78)
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (let i = 0; i < 8; i += 1) {
          controller.enqueue(chunk)
        }
        controller.close()
      },
    })
    const response = new Response(stream, { status: 200, headers: { 'content-encoding': 'identity' } })
    const { bytes, overflow } = await readResponseBounded(response, max)
    expect(overflow).toBe(true)
    expect(bytes.byteLength).toBeLessThanOrEqual(max + 1)
    expect(bytes.byteLength).toBe(max + 1)
  })

  it('sets overflow=true for a body of exactly max+1 bytes', async () => {
    const max = 10
    const body = new Uint8Array(max + 1).fill(0x78)
    const response = new Response(body, { status: 200 })
    const { bytes, overflow } = await readResponseBounded(response, max)
    expect(bytes.byteLength).toBe(max + 1)
    expect(overflow).toBe(true)
  })

  it('retains the full body when it is under the ceiling', async () => {
    const payload = JSON.stringify({ status: 'ok', data: { a: 1 } })
    const response = new Response(payload, { status: 200, headers: { 'content-type': 'application/json', 'content-encoding': 'identity' } })
    const { bytes, overflow } = await readResponseBounded(response, 262_144)
    expect(overflow).toBe(false)
    expect(new TextDecoder().decode(bytes)).toBe(payload)
  })

  it('treats a body-less Response as zero bytes without calling arrayBuffer', async () => {
    const response = new Response(null, { status: 204 })
    const spy = vi.spyOn(response, 'arrayBuffer')
    const { bytes, overflow } = await readResponseBounded(response, 262_144)
    expect(overflow).toBe(false)
    expect(bytes.byteLength).toBe(0)
    expect(spy).not.toHaveBeenCalled()
    spy.mockRestore()
  })

  it('cancels the reader when the abort signal fires and does not hang', async () => {
    const max = 100
    const controller = new AbortController()
    const stream = new ReadableStream<Uint8Array>({
      start(streamController) {
        controller.signal.addEventListener('abort', () => {
          try {
            streamController.error(new DOMException('aborted', 'AbortError'))
          } catch {
            // already errored
          }
        })
      },
    })
    const response = new Response(stream, { status: 200 })
    const result = readResponseBounded(response, max, { signal: controller.signal })
    setTimeout(() => controller.abort(), 5)
    await expect(result).resolves.toMatchObject({ overflow: false })
  })
})

describe('river-click bounded message truncation', () => {
  it('truncates multibyte input at the UTF-8 boundary, never exceeding 512 bytes', () => {
    const message = '😀'.repeat(480) // 4 bytes per code point => 1920 bytes
    const capped = boundedMessage(message)
    expect(new TextEncoder().encode(capped).byteLength).toBeLessThanOrEqual(512)
    expect(capped.endsWith('... [truncated]')).toBe(true)
  })

  it('keeps short ASCII messages verbatim', () => {
    expect(boundedMessage('river-click lane failure')).toBe('river-click lane failure')
  })
})

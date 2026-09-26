/**
 * Shared river-click lane unit-test fixtures (#1970): the parsed five-key
 * config, the preflight identity, and request/response emitters that drive the
 * fake Playwright page's listeners. Test-only; never imported by production.
 */

import { parseRiverClickConfig } from '../lib/riverClickEvidence/config'
import type {
  RiverClickLaneBrowserRequest,
  RiverClickLaneBrowserResponse,
  RiverClickLaneIdentity,
} from '../../playwright.river-click-lane'
import { makeFakePageState, type RiverClickFakePageState } from './riverClickFakePage'

type FakePageState = RiverClickFakePageState

export const CONFIG = {
  PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test',
  PLAYWRIGHT_LIVE_API_BASE_URL: 'https://api.example.test',
  PLAYWRIGHT_LIVE_RIVER_BASIN_ID: 'basins_qhh',
  PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID: 'seg-001',
  PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH: '/private/evidence/nhms-frontend-river-click-live-evidence-1.json',
}

export function config() {
  const parsed = parseRiverClickConfig(CONFIG)
  if (!parsed.ok) throw new Error('fixture config must parse')
  return parsed.config
}

export function makeIdentity(): RiverClickLaneIdentity {
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

export function seriesQuery(source: 'GFS' | 'IFS') {
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

export function emit(state: FakePageState, event: string, arg: unknown) {
  for (const listener of [...state.listeners[event]]) {
    ;(listener as (value: unknown) => void)(arg)
  }
}

export function requestOf(method: string, url: string): RiverClickLaneBrowserRequest {
  return { method: () => method, url: () => url }
}

export function responseOf(request: RiverClickLaneBrowserRequest, finished: () => Promise<unknown> = () => Promise.resolve(null)): RiverClickLaneBrowserResponse {
  const url = request.url()
  return { url: () => url, status: () => 200, finished, request: () => request, method: () => request.method() }
}

/** Emit one waiting pair: request event then response event sharing the Request object. */
export function emitSeriesPair(state: FakePageState, source: 'GFS' | 'IFS', finished: () => Promise<unknown> = () => Promise.resolve(null)) {
  const url = seriesQuery(source)
  const req = requestOf('GET', url)
  emit(state, 'request', req)
  emit(state, 'response', responseOf(req, finished))
}

/** True for the page script of one locate (not the readiness probe, which names the method in quotes). */
export function isLocate(text: string) {
  return text.includes('.locateRenderedRiver(')
}

export function freshState(overrides: Partial<FakePageState> = {}): FakePageState {
  return { ...makeFakePageState(), ...overrides }
}

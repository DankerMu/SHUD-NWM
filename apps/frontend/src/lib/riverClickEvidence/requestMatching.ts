import { RIVER_CLICK_VARIABLE } from './constants'
import { normalizeRiverClickCycleTime } from './preflight'

export interface RiverClickProductRequestIdentity {
  source: 'GFS' | 'IFS'
  scenario: string
  runId: string
  modelId: string
  issueTime: string
  riverNetworkVersionId: string
}

export interface RiverClickSeriesMatchInput {
  apiOrigin: string
  basinVersionId: string
  segmentId: string
  product: RiverClickProductRequestIdentity
}

export type RiverClickSeriesMatch =
  | { matched: true; source: 'GFS' | 'IFS' }
  | { matched: false }

const REQUIRED_QUERY_KEYS = [
  'river_network_version_id',
  'run_id',
  'model_id',
  'issue_time',
  'variables',
  'scenarios',
  'include_analysis',
] as const

/**
 * Safely decode a percent-encoded component. A malformed encoding (e.g.
 * '%E0%A4%A') yields null rather than throwing; double-encoded values decode
 * only once, so they never equal a single-encoded canonical value.
 */
function safeDecodeComputed(component: string): string | null {
  try {
    return decodeURIComponent(component)
  } catch {
    return null
  }
}

/**
 * Exact forecast-series request classifier. Get on the exact configured API
 * origin and decoded path; exactly one value per required query key, values
 * equal to the preflight product identity and fixed values. `run_types`,
 * unknown and duplicate query keys are rejected. issue_time is compared as a
 * canonical RFC3339 instant (the product code sends normalizeHydroMetCycle ->
 * toISOString, i.e. '.000Z'). Latest-product, segment detail, tiles, runtime
 * config and unrelated reads never match. Never throws on malformed input.
 */
export function matchRiverClickSeriesRequest(
  method: string,
  url: string,
  input: RiverClickSeriesMatchInput,
): RiverClickSeriesMatch {
  if (method.toUpperCase() !== 'GET') return { matched: false }
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return { matched: false }
  }
  if (parsed.origin !== input.apiOrigin) return { matched: false }

  const expectedPath = `/api/v1/basin-versions/${input.basinVersionId}/river-segments/${input.segmentId}/forecast-series`
  // URL.pathname keeps its encoded form; decode exactly once (never double) and
  // compare the raw path (no encodeURIComponent round-trip surprises).
  if (safeDecodeComputed(parsed.pathname) !== expectedPath) {
    return { matched: false }
  }

  const seen = new Map<string, string[]>()
  for (const [key, value] of new URLSearchParams(parsed.search)) {
    const existing = seen.get(key) ?? []
    existing.push(value)
    seen.set(key, existing)
  }
  for (const key of seen.keys()) {
    if (!REQUIRED_QUERY_KEYS.includes(key as (typeof REQUIRED_QUERY_KEYS)[number])) return { matched: false }
    if (seen.get(key)!.length !== 1) return { matched: false }
  }
  for (const key of REQUIRED_QUERY_KEYS) {
    if (!seen.has(key)) return { matched: false }
  }

  const value = (key: string) => seen.get(key)![0]
  if (value('river_network_version_id') !== input.product.riverNetworkVersionId) return { matched: false }
  if (value('run_id') !== input.product.runId) return { matched: false }
  if (value('model_id') !== input.product.modelId) return { matched: false }
  // URLSearchParams ALREADY decodes percent-encoding once; the matcher must
  // NOT decode the query value again (a second decode would accept
  // double-encoded input and it can also throw on malformed input). If the
  // decoded value is still percent-encoded it is not an RFC3339 instant.
  if (safeDecodeComputed(value('issue_time')) === null) return { matched: false }
  const urlInstant = normalizeRiverClickCycleTime(value('issue_time'))
  const productInstant = normalizeRiverClickCycleTime(input.product.issueTime)
  // Both sides must parse as strict RFC3339 instants and denote the same UTC
  // instant (canonical toISOString equality).
  if (urlInstant === null || productInstant === null || urlInstant !== productInstant) return { matched: false }
  if (value('variables') !== RIVER_CLICK_VARIABLE) return { matched: false }
  if (value('scenarios') !== input.product.scenario) return { matched: false }
  if (value('include_analysis') !== 'false') return { matched: false }

  return { matched: true, source: input.product.source }
}

import { describe, expect, it } from 'vitest'

import { matchRiverClickSeriesRequest, type RiverClickSeriesMatchInput } from '../requestMatching'

const base: RiverClickSeriesMatchInput = {
  apiOrigin: 'https://api.example.test',
  basinVersionId: 'bv-001',
  segmentId: 'seg-001',
  product: {
    source: 'GFS',
    scenario: 'forecast_gfs_deterministic',
    runId: 'run-001',
    modelId: 'model-gfs',
    issueTime: '2026-09-02T00:00:00Z',
    riverNetworkVersionId: 'rn-001',
  },
}

function urlWith(query: Record<string, string>) {
  const params = new URLSearchParams(query)
  return `https://api.example.test/api/v1/basin-versions/bv-001/river-segments/seg-001/forecast-series?${params.toString()}`
}

const EXPECTED_QUERY: Record<string, string> = {
  river_network_version_id: 'rn-001',
  run_id: 'run-001',
  model_id: 'model-gfs',
  issue_time: '2026-09-02T00:00:00Z',
  variables: 'q_down',
  scenarios: 'forecast_gfs_deterministic',
  include_analysis: 'false',
}

function requestUrl(overrides: Record<string, string | string[]> = {}) {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries({ ...EXPECTED_QUERY, ...overrides })) {
    if (Array.isArray(value)) {
      for (const entry of value) params.append(key, entry)
    } else {
      params.set(key, value)
    }
  }
  return `https://api.example.test/api/v1/basin-versions/bv-001/river-segments/seg-001/forecast-series?${params.toString()}`
}

describe('river-click forecast-series request matching', () => {
  it('matches exactly one GFS forecast-series GET with exact URL/query identity', () => {
    const result = matchRiverClickSeriesRequest('GET', requestUrl(), base)
    expect(result).toMatchObject({ matched: true, source: 'GFS' })
  })

  it('matches the IFS source with its own scenario', () => {
    const result = matchRiverClickSeriesRequest(
      'GET',
      requestUrl({ scenarios: 'forecast_ifs_deterministic', run_id: 'run-002', model_id: 'model-ifs', issue_time: '2026-09-02T06:00:00Z' }),
      { ...base, product: { ...base.product, source: 'IFS', scenario: 'forecast_ifs_deterministic', runId: 'run-002', modelId: 'model-ifs', issueTime: '2026-09-02T06:00:00Z' } },
    )
    expect(result).toMatchObject({ matched: true, source: 'IFS' })
  })

  it('rejects non-GET methods', () => {
    expect(matchRiverClickSeriesRequest('POST', requestUrl(), base).matched).toBe(false)
  })

  it('rejects URLs outside the configured origin or exact path', () => {
    expect(matchRiverClickSeriesRequest('GET', 'https://other.example.test/api/v1/basin-versions/bv-001/river-segments/seg-001/forecast-series?x=1', base).matched).toBe(false)
    expect(matchRiverClickSeriesRequest('GET', 'https://api.example.test/api/v1/basin-versions/bv-001/river-segments/other/forecast-series?x=1', base).matched).toBe(false)
  })

  it('rejects missing, extra, duplicate, or wrong-valued required query keys', () => {
    expect(matchRiverClickSeriesRequest('GET', requestUrl({ river_network_version_id: 'other' }), base).matched).toBe(false)
    expect(matchRiverClickSeriesRequest('GET', urlWith({ ...EXPECTED_QUERY, variables: 'q_up' }), base).matched).toBe(false)
    expect(matchRiverClickSeriesRequest('GET', urlWith({ ...EXPECTED_QUERY, include_analysis: 'true' }), base).matched).toBe(false)
    expect(matchRiverClickSeriesRequest('GET', urlWith({ ...EXPECTED_QUERY, run_types: 'hindcast' }), base).matched).toBe(false)
    expect(matchRiverClickSeriesRequest('GET', urlWith({ ...EXPECTED_QUERY, unknown_key: '1' }), base).matched).toBe(false)

    const dup = `https://api.example.test/api/v1/basin-versions/bv-001/river-segments/seg-001/forecast-series?${new URLSearchParams({
      ...EXPECTED_QUERY,
    }).toString()}&run_id=run-001`
    expect(matchRiverClickSeriesRequest('GET', dup, base).matched).toBe(false)

    const missing = urlWith({ ...EXPECTED_QUERY })
    const withoutScenario = new URLSearchParams(missing)
    withoutScenario.delete('scenarios')
    expect(matchRiverClickSeriesRequest('GET', `https://api.example.test/api/v1/basin-versions/bv-001/river-segments/seg-001/forecast-series?${withoutScenario.toString()}`, base).matched).toBe(false)
  })

  it('requires exactly one value per required key', () => {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(EXPECTED_QUERY)) {
      params.set(key, value)
      if (key === 'run_id') params.append(key, 'run-002')
    }
    const url = `https://api.example.test/api/v1/basin-versions/bv-001/river-segments/seg-001/forecast-series?${params.toString()}`
    expect(matchRiverClickSeriesRequest('GET', url, base).matched).toBe(false)
  })

  it('does not classify latest-product, segment detail, tiles, or unrelated reads as series requests', () => {
    expect(matchRiverClickSeriesRequest('GET', 'https://api.example.test/api/v1/mvp/qhh/latest-product?source=GFS', base).matched).toBe(false)
    expect(matchRiverClickSeriesRequest('GET', 'https://api.example.test/api/v1/basin-versions/bv-001/river-segments/seg-001?x=1', base).matched).toBe(false)
    expect(matchRiverClickSeriesRequest('GET', 'https://api.example.test/api/v1/tiles/hydro/2026-09-02T00:00:00Z/q_down/3/1/1.pbf', base).matched).toBe(false)
    expect(matchRiverClickSeriesRequest('GET', 'https://api.example.test/api/v1/runtime/config', base).matched).toBe(false)
    expect(matchRiverClickSeriesRequest('GET', 'https://api.example.test/api/v1/pipeline/status', base).matched).toBe(false)
  })

  it('accepts encoded and normalized issue_time values that decode to the product value', () => {
    const encoded = `https://api.example.test/api/v1/basin-versions/bv-001/river-segments/seg-001/forecast-series?${new URLSearchParams(EXPECTED_QUERY).toString()}`
    expect(matchRiverClickSeriesRequest('GET', encoded, base).matched).toBe(true)
  })

  it('never throws on a malformed percent-encoding in the path or query', () => {
    const malformedPath = 'https://api.example.test/api/v1/basin-versions/%E0%A4%A/river-segments/seg-001/forecast-series?x=1'
    expect(() => matchRiverClickSeriesRequest('GET', malformedPath, base)).not.toThrow()
    expect(matchRiverClickSeriesRequest('GET', malformedPath, base).matched).toBe(false)

    const malformedQuery = `https://api.example.test/api/v1/basin-versions/bv-001/river-segments/seg-001/forecast-series?issue_time=%E0%A4%A`
    expect(() => matchRiverClickSeriesRequest('GET', malformedQuery, base)).not.toThrow()
    expect(matchRiverClickSeriesRequest('GET', malformedQuery, base).matched).toBe(false)
  })

  it('rejects a wrong method even when the URL and query are otherwise exact', () => {
    expect(matchRiverClickSeriesRequest('POST', requestUrl(), base).matched).toBe(false)
    expect(matchRiverClickSeriesRequest('DELETE', requestUrl(), base).matched).toBe(false)
    expect(matchRiverClickSeriesRequest('get', requestUrl(), base).matched).toBe(true)
  })

  it('never throws on a percent-encoded issue_time and refuses double-encoded input', () => {
    // URLSearchParams encodes '%' as '%25', so the raw URL carries %253A;
    // URLSearchParams decodes one level to '...T00%3A00%3A00.000Z'. That value
    // is STILL percent-encoded, so it is NOT a valid RFC3339 instant; the
    // matcher must refuse it (never decode the query value a second time).
    const doubleEncoded = `https://api.example.test/api/v1/basin-versions/bv-001/river-segments/seg-001/forecast-series?${new URLSearchParams({
      ...EXPECTED_QUERY,
      issue_time: '2026-09-02T00%3A00%3A00.000Z',
    }).toString()}`
    expect(() => matchRiverClickSeriesRequest('GET', doubleEncoded, base)).not.toThrow()
    expect(matchRiverClickSeriesRequest('GET', doubleEncoded, base).matched).toBe(false)
  })

  it('compares canonical toISOString instants on both URL and product sides', () => {
    // loadHydroMetRiverForecast sends normalizeHydroMetCycle(...) -> toISOString
    // ('.000Z'); the matcher must compare canonical instants, so the raw 'Z' URL
    // form (as the backend may echo) matches a canonical '.000Z' product.
    const urlRawZ = requestUrl({ issue_time: '2026-09-02T00:00:00Z' })
    expect(
      matchRiverClickSeriesRequest('GET', urlRawZ, { ...base, product: { ...base.product, issueTime: '2026-09-02T00:00:00.000Z' } }).matched,
    ).toBe(true)

    const urlMillis = requestUrl({ issue_time: '2026-09-02T00:00:00.000Z' })
    expect(
      matchRiverClickSeriesRequest('GET', urlMillis, { ...base, product: { ...base.product, issueTime: '2026-09-02T00:00:00.000Z' } }).matched,
    ).toBe(true)

    // A +08:00 offset on the URL denotes the same instant and must also match.
    const urlOffset = requestUrl({ issue_time: '2026-09-02T08:00:00+08:00' })
    expect(
      matchRiverClickSeriesRequest('GET', urlOffset, { ...base, product: { ...base.product, issueTime: '2026-09-02T00:00:00.000Z' } }).matched,
    ).toBe(true)

    // A genuinely different instant must not match.
    const urlLater = requestUrl({ issue_time: '2026-09-02T06:00:00Z' })
    expect(
      matchRiverClickSeriesRequest('GET', urlLater, { ...base, product: { ...base.product, issueTime: '2026-09-02T00:00:00.000Z' } }).matched,
    ).toBe(false)
  })
})

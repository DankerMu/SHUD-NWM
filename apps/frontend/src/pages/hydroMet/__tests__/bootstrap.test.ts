import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import type { components } from '@/api/types'
import {
  HYDRO_MET_STATION_SERIES_LIMIT,
  HYDRO_MET_STATION_VARIABLES,
  boundedHydroMetStationSeriesLimit,
  loadHydroMetStationSeries,
  validateHydroMetStationSeriesIdentity,
} from '@/lib/hydroMet/stationSeries'
import {
  HYDRO_MET_RIVER_SEGMENT_LIMIT,
  HYDRO_MET_STATION_LIMIT,
  loadHydroMetBootstrap,
  type QhhLatestProduct,
} from '@/pages/hydroMet/bootstrap'
import {
  mergeHydroMetQueryState,
  needsHydroMetQueryReplacement,
  parseHydroMetQueryState,
  serializeHydroMetQueryState,
} from '@/lib/hydroMet/queryState'
import { sanitizeHydroMetMessage } from '@/lib/hydroMet/runtime'
import {
  HYDRO_MET_RIVER_FORECAST_VARIABLE,
  hydroMetRiverScenarioForSource,
  loadHydroMetRiverForecast,
  riverForecastRequestKey,
  validateHydroMetRiverForecastForChart,
} from '@/lib/hydroMet/riverForecast'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn(),
  },
}))

function success<T>(data: T) {
  return { status: 'success', data }
}

function latestProduct(overrides: Partial<QhhLatestProduct> = {}): QhhLatestProduct {
  return {
    basin_id: 'basins_qhh',
    model_id: 'basins_qhh_shud',
    basin_version_id: 'basins_qhh_vbasins',
    river_network_version_id: 'basins_qhh_rivnet_vbasins',
    source_id: 'GFS',
    cycle_time: '2026-05-21T00:00:00Z',
    run_id: 'qhh_gfs_2026052100_smoke',
    forcing_version_id: 'forc_gfs_2026052100_basins_qhh_shud',
    station_count: 386,
    expected_station_count: 386,
    segment_count: 1633,
    expected_segment_count: 1633,
    status: 'ready',
    run_status: 'frequency_done',
    valid_time_start: '2026-05-21T00:00:00Z',
    valid_time_end: '2026-05-28T00:00:00Z',
    river_valid_time_start: '2026-05-21T00:00:00Z',
    river_valid_time_end: '2026-05-28T00:00:00Z',
    forcing_valid_time_start: '2026-05-21T00:00:00Z',
    forcing_valid_time_end: '2026-05-28T00:00:00Z',
    available_horizon_hours: 168,
    expected_horizon_hours: 168,
    shorter_horizon: false,
    availability: {
      ready: true,
      unavailable_reasons: [],
      quality_flags: [],
      quality_notes: [],
    },
    quality: {
      station_sample_count: 10,
      river_sample_count: 10,
      required_station_variables: ['PRCP', 'TEMP', 'RH', 'wind', 'Rn', 'Press'],
      station_variable_coverage: [],
      candidate_limit: 20,
      search_limit: 20,
      context_limit: 20,
      query_indexes: [],
    },
    ...overrides,
  }
}

const stationPage = {
  items: [
    {
      station_id: 'qhh_forc_001',
      basin_version_id: 'basins_qhh_vbasins',
      station_name: 'QHH forcing 001',
      geom: { type: 'Point', coordinates: [104, 31] },
      elevation_m: 320,
      station_role: 'forcing',
      active_flag: true,
      properties_json: null,
      created_at: '2026-05-21T00:00:00Z',
    },
  ],
  total_count: 386,
  limit: HYDRO_MET_STATION_LIMIT,
  offset: 0,
}

const runtimeStationPage = {
  ...stationPage,
  items: [
    {
      station_id: 'qhh_forc_runtime_001',
      basin_version_id: 'basins_qhh_vbasins',
      station_name: 'QHH runtime forcing 001',
      longitude: 104.25,
      latitude: 31.5,
      elevation_m: 320,
      station_role: 'forcing',
      active_flag: true,
      properties_json: null,
      created_at: '2026-05-21T00:00:00Z',
    },
  ],
}

const riverSegments = {
  type: 'FeatureCollection',
  features: [
    {
      type: 'Feature',
      properties: {
        segment_id: 'seg-001',
        river_segment_id: 'seg-001',
        basin_version_id: 'basins_qhh_vbasins',
        river_network_version_id: 'basins_qhh_rivnet_vbasins',
        name: 'QHH segment 001',
        stream_order: 2,
        length_m: 1200,
      },
      geometry: { type: 'LineString', coordinates: [[104, 31], [105, 32]] },
    },
  ],
  total: 1633,
  feature_total: 1633,
  limit: HYDRO_MET_RIVER_SEGMENT_LIMIT,
  offset: 0,
} as const

function riverForecastResponse(
  overrides: Record<string, unknown> = {},
  seriesOverrides: Record<string, unknown> = {},
) {
  return {
    segment_id: 'seg-001',
    issue_time: '2026-05-21T00:00:00Z',
    unit: 'm3/s',
    frequency_thresholds: null,
    series: [
      {
        scenario_id: 'forecast_gfs_deterministic',
        source_id: 'GFS',
        cycle_time: '2026-05-21T00:00:00Z',
        available_lead_hours: 168,
        segment_role: 'future_7_days',
        points: [
          [Date.parse('2026-05-21T00:00:00Z'), 11],
          [Date.parse('2026-05-21T06:00:00Z'), 13],
        ],
        ...seriesOverrides,
      },
    ],
    ...overrides,
  }
}

function stationSeriesResponse(
  overrides: Partial<components['schemas']['StationSeriesResponse']> = {},
): components['schemas']['StationSeriesResponse'] {
  return {
    station_id: 'qhh_forc_001',
    station: {
      station_id: 'qhh_forc_001',
      basin_version_id: 'basins_qhh_vbasins',
      station_name: 'QHH forcing 001',
      longitude: 104,
      latitude: 31,
      elevation_m: 320,
      station_role: 'forcing',
      active_flag: true,
      properties_json: null,
      created_at: '2026-05-21T00:00:00Z',
    },
    forcing_version_id: 'forc_gfs_2026052100_basins_qhh_shud',
    model_id: 'basins_qhh_shud',
    source_id: 'GFS',
    cycle_time: '2026-05-21T00:00:00Z',
    valid_time_start: '2026-05-21T00:00:00Z',
    valid_time_end: '2026-05-21T02:00:00Z',
    limit: HYDRO_MET_STATION_SERIES_LIMIT,
    requested_from: null,
    requested_to: null,
    series: [],
    ...overrides,
  }
}

describe('hydro-met query state', () => {
  it('normalizes supported source and RFC3339 cycle', () => {
    const state = parseHydroMetQueryState('source=ifs&cycle=2026-05-21T08:00:00%2B08:00')

    expect(state.source).toBe('IFS')
    expect(state.cycle).toBe('2026-05-21T00:00:00.000Z')
    expect(state.validationReasons).toEqual([])
    expect(serializeHydroMetQueryState(state)).toBe('source=IFS&cycle=2026-05-21T00%3A00%3A00.000Z')
  })

  it('corrects unsupported source and malformed cycle without preserving bad values', () => {
    const state = parseHydroMetQueryState('source=ERA5&cycle=2026-02-30T00:00:00Z')

    expect(state.source).toBe('GFS')
    expect(state.cycle).toBeNull()
    expect(state.validationReasons).toHaveLength(2)
    expect(serializeHydroMetQueryState(state)).toBe('source=GFS')
    expect(needsHydroMetQueryReplacement('?source=ERA5&cycle=2026-02-30T00:00:00Z')).toBe(true)
  })

  it('parses and serializes complete strict handoff identity', () => {
    const state = parseHydroMetQueryState(
      'source=gfs&cycle_time=2026-05-21T08:00:00%2B08:00&run_id=qhh_gfs_2026052100_smoke&model_id=basins_qhh_shud',
    )

    expect(state.source).toBe('GFS')
    expect(state.cycle).toBe('2026-05-21T00:00:00.000Z')
    expect(state.strictIdentity).toEqual({
      source: 'GFS',
      cycleTime: '2026-05-21T00:00:00.000Z',
      runId: 'qhh_gfs_2026052100_smoke',
      modelId: 'basins_qhh_shud',
    })
    expect(state.strictIdentityError).toBeNull()
    expect(serializeHydroMetQueryState(state)).toBe(
      'source=GFS&cycle_time=2026-05-21T00%3A00%3A00.000Z&run_id=qhh_gfs_2026052100_smoke&model_id=basins_qhh_shud',
    )
  })

  it('keeps partial strict handoff invalid without normalizing to source-only browsing', () => {
    const state = parseHydroMetQueryState('source=GFS&run_id=qhh_gfs_2026052100_smoke')

    expect(state.strictIdentity).toBeNull()
    expect(state.strictIdentityError).toContain('缺少 cycle_time, model_id')
    expect(needsHydroMetQueryReplacement('?source=GFS&run_id=qhh_gfs_2026052100_smoke')).toBe(false)
  })

  it('parses, serializes, and merges the selected basin without a whitelist (#314)', () => {
    const state = parseHydroMetQueryState('source=GFS&basin=basins_heihe')
    expect(state.basin).toBe('basins_heihe')
    expect(serializeHydroMetQueryState(state)).toBe('source=GFS&basin=basins_heihe')

    const switched = mergeHydroMetQueryState(state, { basin: 'basins_qhh', cycle: null })
    expect(switched.basin).toBe('basins_qhh')
    expect(switched.cycle).toBeNull()

    const cleared = mergeHydroMetQueryState(state, { basin: null })
    expect(cleared.basin).toBeNull()
    expect(serializeHydroMetQueryState(cleared)).toBe('source=GFS')
  })

  it('treats basin as null when absent, preserving backend-default selection (#314)', () => {
    const state = parseHydroMetQueryState('source=GFS')
    expect(state.basin).toBeNull()
  })
})

describe('loadHydroMetBootstrap', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('loads latest product, station inventory, and river segment candidates from latest-product IDs', async () => {
    vi.mocked(client.GET).mockImplementation(async (path: string) => {
      if (path === '/api/v1/mvp/qhh/latest-product') return { data: success(latestProduct()), error: undefined } as never
      if (path === '/api/v1/met/stations') return { data: success(stationPage), error: undefined } as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return { data: success(riverSegments), error: undefined } as never
      return { data: success({}), error: undefined } as never
    })

    const result = await loadHydroMetBootstrap({ source: 'GFS', cycle: null })

    expect(result.status).toBe('ready')
    expect(result.stations).toHaveLength(1)
    expect(result.riverSegments).toHaveLength(1)
    expect(client.GET).toHaveBeenCalledWith('/api/v1/mvp/qhh/latest-product', {
      params: { query: { source: 'GFS' } },
    })
    expect(client.GET).toHaveBeenCalledWith('/api/v1/met/stations', {
      params: { query: { model_id: 'basins_qhh_shud', basin_version_id: 'basins_qhh_vbasins', limit: HYDRO_MET_STATION_LIMIT, offset: 0 } },
    })
    expect(client.GET).toHaveBeenCalledWith('/api/v1/basin-versions/{basin_version_id}/river-segments', {
      params: {
        path: { basin_version_id: 'basins_qhh_vbasins' },
        query: {
          river_network_version_id: 'basins_qhh_rivnet_vbasins',
          limit: HYDRO_MET_RIVER_SEGMENT_LIMIT,
          offset: 0,
        },
      },
    })
    const serializedCalls = JSON.stringify(vi.mocked(client.GET).mock.calls)
    expect(serializedCalls).not.toContain('manual')
    expect(serializedCalls).not.toContain('run_id')
    expect(serializedCalls).not.toContain('forcing_version_id')
  })

  it('loads strict latest product with source, cycle_time, run_id, and model_id', async () => {
    const calls: Array<{ path: string; query?: Record<string, unknown> }> = []
    vi.mocked(client.GET).mockImplementation(async (path: string, options?: { params?: { query?: Record<string, unknown> } }) => {
      calls.push({ path, query: options?.params?.query })
      if (path === '/api/v1/mvp/qhh/latest-product') return { data: success(latestProduct()), error: undefined } as never
      if (path === '/api/v1/met/stations') return { data: success(stationPage), error: undefined } as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return { data: success(riverSegments), error: undefined } as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const result = await loadHydroMetBootstrap({
      source: 'GFS',
      cycle: '2026-05-21T00:00:00.000Z',
      strictIdentity: {
        source: 'GFS',
        cycleTime: '2026-05-21T00:00:00.000Z',
        runId: 'qhh_gfs_2026052100_smoke',
        modelId: 'basins_qhh_shud',
      },
    })

    expect(result.status).toBe('ready')
    expect(calls[0]).toEqual({
      path: '/api/v1/mvp/qhh/latest-product',
      query: {
        source: 'GFS',
        cycle_time: '2026-05-21T00:00:00.000Z',
        run_id: 'qhh_gfs_2026052100_smoke',
        model_id: 'basins_qhh_shud',
      },
    })
  })

  it('threads basinId into latest-product query and derives downstream from the returned product (#314)', async () => {
    const calls: Array<{ path: string; query?: Record<string, unknown> }> = []
    vi.mocked(client.GET).mockImplementation(async (path: string, options?: { params?: { query?: Record<string, unknown> } }) => {
      calls.push({ path, query: options?.params?.query })
      if (path === '/api/v1/mvp/qhh/latest-product') {
        return {
          data: success(
            latestProduct({
              basin_id: 'basins_heihe',
              model_id: 'basins_heihe_shud',
              basin_version_id: 'basins_heihe_vbasins',
              river_network_version_id: 'basins_heihe_rivnet_vbasins',
            }),
          ),
          error: undefined,
        } as never
      }
      if (path === '/api/v1/met/stations') return { data: success(stationPage), error: undefined } as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return { data: success(riverSegments), error: undefined } as never
      throw new Error(`Unexpected GET ${path}`)
    })

    const result = await loadHydroMetBootstrap({ source: 'GFS', cycle: null, basinId: 'basins_heihe' })

    expect(result.status).toBe('ready')
    expect(calls[0]).toEqual({
      path: '/api/v1/mvp/qhh/latest-product',
      query: { source: 'GFS', basin_id: 'basins_heihe' },
    })
    // Downstream params are derived from the returned product's identity, not hand-input.
    expect(client.GET).toHaveBeenCalledWith('/api/v1/met/stations', {
      params: { query: { model_id: 'basins_heihe_shud', basin_version_id: 'basins_heihe_vbasins', limit: HYDRO_MET_STATION_LIMIT, offset: 0 } },
    })
    expect(client.GET).toHaveBeenCalledWith('/api/v1/basin-versions/{basin_version_id}/river-segments', {
      params: {
        path: { basin_version_id: 'basins_heihe_vbasins' },
        query: {
          river_network_version_id: 'basins_heihe_rivnet_vbasins',
          limit: HYDRO_MET_RIVER_SEGMENT_LIMIT,
          offset: 0,
        },
      },
    })
  })

  it('combines basinId with strict identity in the latest-product query (#314)', async () => {
    const calls: Array<{ path: string; query?: Record<string, unknown> }> = []
    vi.mocked(client.GET).mockImplementation(async (path: string, options?: { params?: { query?: Record<string, unknown> } }) => {
      calls.push({ path, query: options?.params?.query })
      if (path === '/api/v1/mvp/qhh/latest-product') return { data: success(latestProduct()), error: undefined } as never
      if (path === '/api/v1/met/stations') return { data: success(stationPage), error: undefined } as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return { data: success(riverSegments), error: undefined } as never
      throw new Error(`Unexpected GET ${path}`)
    })

    await loadHydroMetBootstrap({
      source: 'GFS',
      cycle: '2026-05-21T00:00:00.000Z',
      basinId: 'basins_qhh',
      strictIdentity: {
        source: 'GFS',
        cycleTime: '2026-05-21T00:00:00.000Z',
        runId: 'qhh_gfs_2026052100_smoke',
        modelId: 'basins_qhh_shud',
      },
    })

    expect(calls[0]).toEqual({
      path: '/api/v1/mvp/qhh/latest-product',
      query: {
        source: 'GFS',
        cycle_time: '2026-05-21T00:00:00.000Z',
        run_id: 'qhh_gfs_2026052100_smoke',
        model_id: 'basins_qhh_shud',
        basin_id: 'basins_qhh',
      },
    })
  })

  it('omits basin_id when no basin is selected, preserving backend-default behaviour (#314)', async () => {
    const calls: Array<{ path: string; query?: Record<string, unknown> }> = []
    vi.mocked(client.GET).mockImplementation(async (path: string, options?: { params?: { query?: Record<string, unknown> } }) => {
      calls.push({ path, query: options?.params?.query })
      if (path === '/api/v1/mvp/qhh/latest-product') return { data: success(latestProduct()), error: undefined } as never
      if (path === '/api/v1/met/stations') return { data: success(stationPage), error: undefined } as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return { data: success(riverSegments), error: undefined } as never
      throw new Error(`Unexpected GET ${path}`)
    })

    await loadHydroMetBootstrap({ source: 'GFS', cycle: null, basinId: null })

    expect(calls[0]).toEqual({ path: '/api/v1/mvp/qhh/latest-product', query: { source: 'GFS' } })
    expect(JSON.stringify(calls[0])).not.toContain('basin_id')
  })

  it('blocks downstream bootstrap when strict latest-product identity mismatches', async () => {
    vi.mocked(client.GET).mockResolvedValueOnce({
      data: success(latestProduct({ run_id: 'other-run' })),
      error: undefined,
    } as never)

    const result = await loadHydroMetBootstrap({
      source: 'GFS',
      cycle: '2026-05-21T00:00:00.000Z',
      strictIdentity: {
        source: 'GFS',
        cycleTime: '2026-05-21T00:00:00.000Z',
        runId: 'qhh_gfs_2026052100_smoke',
        modelId: 'basins_qhh_shud',
      },
    })

    expect(result.status).toBe('strict-identity-mismatch')
    expect(result.latestReasons.join(' ')).toContain('run_id=other-run')
    expect(vi.mocked(client.GET).mock.calls.map(([path]) => path)).toEqual(['/api/v1/mvp/qhh/latest-product'])
  })

  it('normalizes runtime-shaped station inventory coordinates without losing river candidates', async () => {
    vi.mocked(client.GET).mockImplementation(async (path: string) => {
      if (path === '/api/v1/mvp/qhh/latest-product') return { data: success(latestProduct()), error: undefined } as never
      if (path === '/api/v1/met/stations') return { data: success(runtimeStationPage), error: undefined } as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') return { data: success(riverSegments), error: undefined } as never
      return { data: success({}), error: undefined } as never
    })

    const result = await loadHydroMetBootstrap({ source: 'GFS', cycle: null })

    expect(result.status).toBe('ready')
    expect(result.stations[0]).toMatchObject({
      station_id: 'qhh_forc_runtime_001',
      geom: { type: 'Point', coordinates: [104.25, 31.5] },
    })
    expect(result.riverSegments).toHaveLength(1)
  })

  it('does not load downstream candidates when requested cycle differs from latest product', async () => {
    vi.mocked(client.GET).mockResolvedValue({ data: success(latestProduct()), error: undefined } as never)

    const result = await loadHydroMetBootstrap({ source: 'GFS', cycle: '2026-05-20T00:00:00.000Z' })

    expect(result.status).toBe('cycle-unavailable')
    expect(result.latestReasons.join(' ')).toContain('避免混用产品')
    expect(vi.mocked(client.GET).mock.calls.map(([path]) => path)).toEqual(['/api/v1/mvp/qhh/latest-product'])
  })

  it('reports latest unavailable and incomplete products before station or river requests', async () => {
    vi.mocked(client.GET).mockResolvedValueOnce({
      data: success(latestProduct({
        status: 'unavailable',
        availability: {
          ready: false,
          unavailable_reasons: [{ code: 'NO_FORCING', message: 'forcing version missing' }],
          quality_flags: [],
          quality_notes: [],
        },
      })),
      error: undefined,
    } as never)

    const unavailable = await loadHydroMetBootstrap({ source: 'GFS', cycle: null })

    expect(unavailable.status).toBe('latest-unavailable')
    expect(unavailable.latestReasons.join(' ')).toContain('NO_FORCING')
    expect(vi.mocked(client.GET).mock.calls.map(([path]) => path)).toEqual(['/api/v1/mvp/qhh/latest-product'])

    vi.clearAllMocks()
    vi.mocked(client.GET).mockResolvedValueOnce({
      data: success(latestProduct({ river_network_version_id: '', segment_count: 0 })),
      error: undefined,
    } as never)

    const incomplete = await loadHydroMetBootstrap({ source: 'GFS', cycle: null })

    expect(incomplete.status).toBe('latest-incomplete')
    expect(incomplete.latestReasons.join(' ')).toContain('river_network_version_id 缺失')
    expect(incomplete.latestReasons.join(' ')).toContain('segment_count 不可展示')
    expect(vi.mocked(client.GET).mock.calls.map(([path]) => path)).toEqual(['/api/v1/mvp/qhh/latest-product'])
  })

  it('keeps station and river partial failures separate without failing the whole bootstrap', async () => {
    vi.mocked(client.GET).mockImplementation(async (path: string) => {
      if (path === '/api/v1/mvp/qhh/latest-product') return { data: success(latestProduct()), error: undefined } as never
      if (path === '/api/v1/met/stations') return { data: undefined, error: { error: { message: 'station db timeout' } } } as never
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') {
        return { data: undefined, error: { error: { message: 'river db timeout' } } } as never
      }
      return { data: success({}), error: undefined } as never
    })

    const result = await loadHydroMetBootstrap({ source: 'GFS', cycle: null })

    expect(result.status).toBe('ready')
    expect(result.stationError).toBe('station db timeout')
    expect(result.riverError).toBe('river db timeout')
    expect(result.stations).toEqual([])
    expect(result.riverSegments).toEqual([])
  })

  it('redacts hydro-met UI-bound backend messages while keeping error labels', async () => {
    const unsafeMessage =
      'ERR_QHH failed opening s3://key:secret@bucket/private?token=abc#frag from file:///volume/data/nwm/Basins/qhh?sig=x#frag and /volume/data/nwm/Basins/qhh plus C:\\nwm\\Basins\\qhh'
    vi.mocked(client.GET).mockResolvedValueOnce({
      data: success(latestProduct({
        status: 'unavailable',
        availability: {
          ready: false,
          unavailable_reasons: [{ code: 'NO_READY_PRODUCT', message: unsafeMessage }],
          quality_flags: [],
          quality_notes: [],
        },
      })),
      error: undefined,
    } as never)

    const result = await loadHydroMetBootstrap({ source: 'GFS', cycle: null })
    const rendered = result.latestReasons.join(' ')

    expect(rendered).toContain('NO_READY_PRODUCT')
    expect(rendered).toContain('ERR_QHH')
    expect(rendered).toContain('s3://bucket/private')
    expect(rendered).not.toContain('key:secret')
    expect(rendered).not.toContain('token=abc')
    expect(rendered).not.toContain('#frag')
    expect(rendered).not.toContain('file://')
    expect(rendered).not.toContain('/volume/data/nwm/Basins/qhh')
    expect(rendered).not.toContain('C:\\nwm\\Basins\\qhh')

    expect(sanitizeHydroMetMessage(unsafeMessage)).toContain('s3://bucket/private')
  })
})

describe('loadHydroMetStationSeries', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls the generated station-series API with latest-product forcing version, six variables, and bounded limit', async () => {
    vi.mocked(client.GET).mockResolvedValueOnce({
      data: success(stationSeriesResponse()),
      error: undefined,
    } as never)

    const product = latestProduct()
    const response = await loadHydroMetStationSeries({
      product,
      station: { station_id: 'qhh_forc_001' },
      limit: 9999,
    })

    expect(response.station_id).toBe('qhh_forc_001')
    expect(client.GET).toHaveBeenCalledWith('/api/v1/met/stations/{station_id}/series', {
      params: {
        path: { station_id: 'qhh_forc_001' },
        query: {
          forcing_version_id: product.forcing_version_id,
          variables: [...HYDRO_MET_STATION_VARIABLES],
          limit: HYDRO_MET_STATION_SERIES_LIMIT,
        },
      },
    })
    expect(JSON.stringify(vi.mocked(client.GET).mock.calls)).not.toContain('/api/v1/forecast')
  })

  it('keeps station-series limit bounded and preserves typed API error messages', async () => {
    expect(boundedHydroMetStationSeriesLimit(0)).toBe(1)
    expect(boundedHydroMetStationSeriesLimit(12.7)).toBe(12)
    expect(boundedHydroMetStationSeriesLimit(5000)).toBe(HYDRO_MET_STATION_SERIES_LIMIT)
    expect(boundedHydroMetStationSeriesLimit(undefined)).toBe(HYDRO_MET_STATION_SERIES_LIMIT)

    vi.mocked(client.GET).mockResolvedValueOnce({
      data: undefined,
      error: { error: { message: 'station unavailable' } },
    } as never)

    await expect(loadHydroMetStationSeries({
      product: latestProduct(),
      station: { station_id: 'qhh_forc_001' },
    })).rejects.toThrow('station unavailable')
  })

  it('reports station-series identity mismatches without silently switching product identity', () => {
    const messages = validateHydroMetStationSeriesIdentity(
      stationSeriesResponse({
        station_id: 'qhh_forc_002',
        forcing_version_id: 'other-forcing',
        source_id: 'IFS',
        cycle_time: '2026-05-21T12:00:00Z',
      }),
      latestProduct(),
      'qhh_forc_001',
    )

    expect(messages.join(' ')).toContain('station_id=qhh_forc_002')
    expect(messages.join(' ')).toContain('forcing_version_id=other-forcing')
    expect(messages.join(' ')).toContain('source_id=IFS')
    expect(messages.join(' ')).toContain('cycle_time=2026-05-21T12:00:00.000Z')
  })
})

describe('loadHydroMetRiverForecast', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls the generated forecast-series API with q_down, selected segment, product cycle, and matching GFS scenario', async () => {
    vi.mocked(client.GET).mockResolvedValueOnce({
      data: success(riverForecastResponse()),
      error: undefined,
    } as never)

    const product = latestProduct()
    const response = await loadHydroMetRiverForecast({
      product,
      segment: {
        river_segment_id: 'seg-001',
        segment_id: 'seg-001',
        basin_version_id: product.basin_version_id,
        river_network_version_id: product.river_network_version_id,
        name: 'QHH segment 001',
      },
    })

    expect(response).toMatchObject({ segment_id: 'seg-001', unit: 'm3/s' })
    expect(hydroMetRiverScenarioForSource('GFS')).toBe('forecast_gfs_deterministic')
    expect(client.GET).toHaveBeenCalledWith(
      '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series',
      {
        params: {
          path: {
            basin_version_id: product.basin_version_id,
            segment_id: 'seg-001',
          },
          query: {
            river_network_version_id: product.river_network_version_id,
            issue_time: '2026-05-21T00:00:00.000Z',
            variables: HYDRO_MET_RIVER_FORECAST_VARIABLE,
            scenarios: 'forecast_gfs_deterministic',
            include_analysis: false,
          },
        },
      },
    )
    expect(JSON.stringify(vi.mocked(client.GET).mock.calls)).not.toContain('forcing_version_id')
  })

  it('preserves IFS source/scenario and validates shorter actual horizon without synthetic padding', async () => {
    const product = latestProduct({
      source_id: 'IFS',
      cycle_time: '2026-05-21T06:00:00Z',
      river_valid_time_end: '2026-05-27T06:00:00Z',
      valid_time_end: '2026-05-27T06:00:00Z',
      available_horizon_hours: 144,
      expected_horizon_hours: 168,
      shorter_horizon: true,
    })
    const payload = riverForecastResponse(
      { issue_time: '2026-05-21T06:00:00Z' },
      {
        scenario_id: 'forecast_ifs_deterministic',
        source_id: 'IFS',
        cycle_time: '2026-05-21T06:00:00Z',
        available_lead_hours: 144,
        points: [
          [Date.parse('2026-05-21T06:00:00Z'), 11],
          [Date.parse('2026-05-27T06:00:00Z'), 13],
        ],
      },
    )

    vi.mocked(client.GET).mockResolvedValueOnce({
      data: success(payload),
      error: undefined,
    } as never)

    const segment = {
      river_segment_id: 'seg-001',
      segment_id: 'seg-001',
      basin_version_id: product.basin_version_id,
      river_network_version_id: product.river_network_version_id,
      name: 'QHH segment 001',
    }
    const response = await loadHydroMetRiverForecast({ product, segment })
    const validation = validateHydroMetRiverForecastForChart(response, product, segment)

    expect(hydroMetRiverScenarioForSource('IFS')).toBe('forecast_ifs_deterministic')
    expect(client.GET).toHaveBeenCalledWith(
      '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series',
      expect.objectContaining({
        params: expect.objectContaining({
          query: expect.objectContaining({
            scenarios: 'forecast_ifs_deterministic',
            variables: 'q_down',
          }),
        }),
      }),
    )
    expect(validation.ok).toBe(true)
    if (validation.ok) {
      expect(validation.horizonShorter).toBe(true)
      expect(validation.horizonLabel).toContain('144h')
      expect(validation.renderedPoints).toHaveLength(2)
      expect(validation.validTimeEnd).toBe('2026-05-27T06:00:00.000Z')
    }
  })

  it('returns explicit invalid states for empty, malformed, or mismatched q_down forecast responses', () => {
    const product = latestProduct()
    const segment = {
      river_segment_id: 'seg-001',
      segment_id: 'seg-001',
      basin_version_id: product.basin_version_id,
      river_network_version_id: product.river_network_version_id,
      name: 'QHH segment 001',
    }

    expect(validateHydroMetRiverForecastForChart(riverForecastResponse({}, { points: [] }), product, segment)).toMatchObject({
      ok: false,
      messages: expect.arrayContaining([expect.stringContaining('没有可绘制点')]),
    })
    expect(validateHydroMetRiverForecastForChart({ segment_id: 'seg-001', unit: 'm3/s', series: [{ scenario_id: 'forecast_gfs_deterministic' }] } as never, product, segment)).toMatchObject({
      ok: false,
      messages: expect.arrayContaining([expect.stringContaining('points 缺失或格式无效')]),
    })
    expect(validateHydroMetRiverForecastForChart(riverForecastResponse({ variable: 'not_q_down' }), product, segment)).toMatchObject({
      ok: false,
      messages: expect.arrayContaining([expect.stringContaining('不是 q_down')]),
    })
  })

  it('rejects q_down forecast responses with stale or missing cycle identity proof', () => {
    const product = latestProduct()
    const segment = {
      river_segment_id: 'seg-001',
      segment_id: 'seg-001',
      basin_version_id: product.basin_version_id,
      river_network_version_id: product.river_network_version_id,
      name: 'QHH segment 001',
    }

    expect(validateHydroMetRiverForecastForChart(
      riverForecastResponse({ issue_time: '2026-05-20T00:00:00Z' }, { cycle_time: undefined }),
      product,
      segment,
    )).toMatchObject({
      ok: false,
      messages: expect.arrayContaining([expect.stringContaining('issue_time=2026-05-20T00:00:00.000Z')]),
    })

    expect(validateHydroMetRiverForecastForChart(
      riverForecastResponse({ issue_time: undefined }, { cycle_time: undefined }),
      product,
      segment,
    )).toMatchObject({
      ok: false,
      messages: expect.arrayContaining([expect.stringContaining('缺少与 latest-product 2026-05-21T00:00:00.000Z 匹配的 cycle identity')]),
    })

    expect(validateHydroMetRiverForecastForChart(
      riverForecastResponse({}, { cycle_time: '2026-05-20T00:00:00Z' }),
      product,
      segment,
    )).toMatchObject({
      ok: false,
      messages: expect.arrayContaining([expect.stringContaining('series[0].cycle_time=2026-05-20T00:00:00.000Z')]),
    })
  })

  it('rejects finite q_down timestamps outside the JavaScript Date range', () => {
    const product = latestProduct()
    const segment = {
      river_segment_id: 'seg-001',
      segment_id: 'seg-001',
      basin_version_id: product.basin_version_id,
      river_network_version_id: product.river_network_version_id,
      name: 'QHH segment 001',
    }

    expect(validateHydroMetRiverForecastForChart(
      riverForecastResponse({}, { points: [[8640000000000001, 1]] }),
      product,
      segment,
    )).toMatchObject({
      ok: false,
      messages: expect.arrayContaining([expect.stringContaining('超出 JavaScript Date 可表示范围')]),
    })
  })

  it('builds request keys from product and segment identity', () => {
    const product = latestProduct()
    expect(riverForecastRequestKey(product, 'seg-001')).toBe(
      'basins_qhh_vbasins|basins_qhh_rivnet_vbasins|GFS|2026-05-21T00:00:00.000Z|seg-001',
    )
  })
})

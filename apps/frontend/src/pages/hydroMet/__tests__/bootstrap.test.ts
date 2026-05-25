import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import {
  HYDRO_MET_RIVER_SEGMENT_LIMIT,
  HYDRO_MET_STATION_LIMIT,
  loadHydroMetBootstrap,
  type QhhLatestProduct,
} from '@/pages/hydroMet/bootstrap'
import {
  needsHydroMetQueryReplacement,
  parseHydroMetQueryState,
  serializeHydroMetQueryState,
} from '@/lib/hydroMet/queryState'

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
      params: { query: { model_id: 'basins_qhh_shud', limit: HYDRO_MET_STATION_LIMIT, offset: 0 } },
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
})

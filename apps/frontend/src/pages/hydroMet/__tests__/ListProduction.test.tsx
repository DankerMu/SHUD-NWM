import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { ReadyHydroMetContent } from '@/pages/hydroMet/HydroMetPage'
import type {
  HydroMetBootstrapResult,
  HydroMetRiverSegmentCollection,
  HydroMetStation,
  HydroMetStationPage,
  QhhLatestProduct,
} from '@/pages/hydroMet/bootstrap'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn(),
  },
}))

vi.mock('echarts-for-react/lib/core', () => ({
  default: () => null,
}))

function success<T>(data: T) {
  return { status: 'success', data }
}

function product(overrides: Partial<QhhLatestProduct> = {}): QhhLatestProduct {
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

function station(id: string, name: string): HydroMetStation {
  return {
    station_id: id,
    basin_version_id: 'basins_qhh_vbasins',
    station_name: name,
    geom: { type: 'Point', coordinates: [104.1, 31.2] },
    elevation_m: 320,
    station_role: 'forcing',
    active_flag: true,
    properties_json: null,
    created_at: '2026-05-21T00:00:00Z',
  }
}

type StationFilterAvailability = {
  search?: boolean
  variables?: boolean
  qc_status?: boolean
}

function stationPage(
  items: HydroMetStation[],
  options: { totalCount?: number; limit?: number; offset?: number; available?: StationFilterAvailability } = {},
): HydroMetStationPage {
  const available = options.available ?? {}
  return {
    items,
    total_count: options.totalCount ?? items.length,
    limit: options.limit ?? 500,
    offset: options.offset ?? 0,
    filters: {
      applied: {},
      available: {
        search: available.search ?? true,
        variables: available.variables ?? true,
        qc_status: available.qc_status ?? false,
      },
      qc_status: { available: available.qc_status ?? false, reason: null, requested: null },
    },
  }
}

function riverFeature(id: string, name: string, streamOrder: number | undefined) {
  return {
    type: 'Feature' as const,
    properties: {
      segment_id: id,
      river_segment_id: id,
      basin_version_id: 'basins_qhh_vbasins',
      river_network_version_id: 'basins_qhh_rivnet_vbasins',
      name,
      stream_order: streamOrder as number,
    },
    geometry: { type: 'LineString' as const, coordinates: [[104, 31], [105, 32]] },
  }
}

function riverCollection(
  features: ReturnType<typeof riverFeature>[],
  options: { total?: number; limit?: number; offset?: number } = {},
): HydroMetRiverSegmentCollection {
  return {
    type: 'FeatureCollection',
    features,
    total: options.total ?? features.length,
    feature_total: options.total ?? features.length,
    limit: options.limit ?? 250,
    offset: options.offset ?? 0,
  }
}

function bootstrapResult(overrides: Partial<HydroMetBootstrapResult> = {}): HydroMetBootstrapResult {
  const page = stationPage([station('qhh_forc_001', 'QHH forcing 001')], { totalCount: 386 })
  const collection = riverCollection([riverFeature('seg-001', 'QHH segment 001', 2)], { total: 1633 })
  return {
    status: 'ready',
    source: 'GFS',
    cycle: null,
    product: product(),
    stations: page.items,
    riverSegments: collection.features,
    stationPage: page,
    riverSegmentCollection: collection,
    latestReasons: [],
    stationError: null,
    riverError: null,
    ...overrides,
  }
}

type ClientQuery = Record<string, unknown> | undefined

function lastQueryFor(path: string): ClientQuery {
  const calls = vi.mocked(client.GET).mock.calls.filter(([callPath]) => callPath === path)
  const last = calls[calls.length - 1]
  return (last?.[1] as { params?: { query?: Record<string, unknown> } } | undefined)?.params?.query
}

function defaultClientMock() {
  vi.mocked(client.GET).mockImplementation(async (path: string) => {
    if (path === '/api/v1/met/stations') {
      return { data: success(stationPage([station('qhh_forc_002', 'North Ridge')], { totalCount: 1 })), error: undefined } as never
    }
    if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') {
      return { data: success(riverCollection([riverFeature('seg-002', 'High order', 7)], { total: 1 })), error: undefined } as never
    }
    return { data: success({}), error: undefined } as never
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  defaultClientMock()
})

afterEach(() => {
  cleanup()
})

describe('河段列表走后端 search/分页 (#315 §5.2)', () => {
  // Scenario: 搜索定位河段 —— 后端 search 过滤
  it('河段 search 走后端参数（带 search/limit/offset，分页非全量）', async () => {
    const user = userEvent.setup()
    const result = bootstrapResult()
    render(<ReadyHydroMetContent result={result} product={result.product!} />)

    await user.type(screen.getByLabelText('搜索河段'), 'High')

    await waitFor(() => {
      const query = lastQueryFor('/api/v1/basin-versions/{basin_version_id}/river-segments')
      expect(query?.search).toBe('High')
    })
    const query = lastQueryFor('/api/v1/basin-versions/{basin_version_id}/river-segments')
    // 分页参数走后端，limit 为单页上限而非全量。
    expect(query?.limit).toBe(250)
    expect(query?.offset).toBe(0)
    // strict identity：river_network_version_id 派生自 product，不手输。
    expect(query?.river_network_version_id).toBe('basins_qhh_rivnet_vbasins')
    await screen.findByText('High order')
  })

  // Scenario: 分页不全量加载 —— 后端 limit/offset
  it('河段分页用后端 offset 翻页，不一次拉全量', async () => {
    const user = userEvent.setup()
    const result = bootstrapResult({
      riverSegmentCollection: riverCollection([riverFeature('seg-001', 'QHH segment 001', 2)], {
        total: 1633,
        limit: 250,
        offset: 0,
      }),
    })
    render(<ReadyHydroMetContent result={result} product={result.product!} />)

    expect(screen.getByTestId('hydro-met-river-pagination')).toHaveTextContent('共 1633')
    await user.click(screen.getByTestId('hydro-met-river-pagination-next'))

    await waitFor(() => {
      const query = lastQueryFor('/api/v1/basin-versions/{basin_version_id}/river-segments')
      expect(query?.offset).toBe(250)
    })
    expect(lastQueryFor('/api/v1/basin-versions/{basin_version_id}/river-segments')?.limit).toBe(250)
  })

  // Scenario: stream-order 过滤（字段可用时）
  it('stream_order 控件在底层含字段时显示并走后端 stream_order_min/max', async () => {
    const user = userEvent.setup()
    const result = bootstrapResult()
    render(<ReadyHydroMetContent result={result} product={result.product!} />)

    expect(screen.getByTestId('hydro-met-river-stream-order-filter')).toBeInTheDocument()
    await user.type(screen.getByTestId('hydro-met-river-stream-order-min'), '3')

    await waitFor(() => {
      const query = lastQueryFor('/api/v1/basin-versions/{basin_version_id}/river-segments')
      expect(query?.stream_order_min).toBe(3)
    })
  })

  // Scenario: stream-order 过滤（字段缺失时）—— 不可用呈现
  it('底层无 stream_order 字段时隐藏控件并标注不可用（不报错）', () => {
    const result = bootstrapResult({
      riverSegments: [riverFeature('seg-001', 'QHH segment 001', undefined)],
      riverSegmentCollection: riverCollection([riverFeature('seg-001', 'QHH segment 001', undefined)], { total: 1 }),
    })
    render(<ReadyHydroMetContent result={result} product={result.product!} />)

    expect(screen.queryByTestId('hydro-met-river-stream-order-filter')).toBeNull()
    expect(screen.getByTestId('hydro-met-river-stream-order-unavailable')).toHaveTextContent('不可用')
  })
})

describe('站点列表走后端 search/variable 筛选 (#315 §5.3)', () => {
  // Scenario: 站点搜索 —— 后端 search
  it('站点 search 走后端参数（带 search/limit/offset，服务端筛选）', async () => {
    const user = userEvent.setup()
    const result = bootstrapResult()
    render(<ReadyHydroMetContent result={result} product={result.product!} />)

    await user.type(screen.getByLabelText('搜索气象站点'), 'North')

    await waitFor(() => {
      const query = lastQueryFor('/api/v1/met/stations')
      expect(query?.search).toBe('North')
    })
    const query = lastQueryFor('/api/v1/met/stations')
    expect(query?.limit).toBe(500)
    expect(query?.offset).toBe(0)
    // 服务端筛选：列表展示后端返回结果，不是客户端 filter 整页。
    await screen.findByText('North Ridge')
  })

  // Scenario: 按变量覆盖筛选
  it('variable 覆盖筛选走后端 variables 参数（字段可用时显示控件）', async () => {
    const user = userEvent.setup()
    const result = bootstrapResult()
    render(<ReadyHydroMetContent result={result} product={result.product!} />)

    expect(screen.getByTestId('hydro-met-station-variable-filter')).toBeInTheDocument()
    await user.click(screen.getByTestId('hydro-met-station-variable-PRCP'))

    await waitFor(() => {
      const query = lastQueryFor('/api/v1/met/stations')
      expect(query?.variables).toEqual(['PRCP'])
    })
  })

  // Scenario: variable 筛选字段不可用时（model_id 缺失）隐藏控件
  it('variables 不可用时（filters.available.variables=false）隐藏变量筛选控件', () => {
    const page = stationPage([station('qhh_forc_001', 'QHH forcing 001')], {
      totalCount: 1,
      available: { variables: false },
    })
    const result = bootstrapResult({ stationPage: page, stations: page.items })
    render(<ReadyHydroMetContent result={result} product={result.product!} />)

    expect(screen.queryByTestId('hydro-met-station-variable-filter')).toBeNull()
    expect(screen.getByTestId('hydro-met-station-variable-unavailable')).toHaveTextContent('不可用')
  })

  // Scenario: QC 筛选（字段不可用时）—— 不可用呈现
  it('qc_status 字段不可用时标注不可用（不报错，不渲染 QC 控件）', () => {
    const result = bootstrapResult()
    render(<ReadyHydroMetContent result={result} product={result.product!} />)

    expect(screen.getByTestId('hydro-met-station-qc-unavailable')).toHaveTextContent('不可用')
  })

  // Scenario: QC 筛选（字段可用时）—— 不显示不可用提示
  it('qc_status 字段可用时不渲染不可用提示', () => {
    const page = stationPage([station('qhh_forc_001', 'QHH forcing 001')], {
      totalCount: 1,
      available: { qc_status: true },
    })
    const result = bootstrapResult({ stationPage: page, stations: page.items })
    render(<ReadyHydroMetContent result={result} product={result.product!} />)

    expect(screen.queryByTestId('hydro-met-station-qc-unavailable')).toBeNull()
  })
})

describe('strict identity 前端一致性 (#315 §5.5 红线)', () => {
  // Scenario: 请求参数派生自同一产品身份
  it('河段/站点 search 请求的 identity 全部派生自 product，不手输', async () => {
    const user = userEvent.setup()
    const result = bootstrapResult()
    render(<ReadyHydroMetContent result={result} product={result.product!} />)

    await user.type(screen.getByLabelText('搜索气象站点'), 'North')
    await user.type(screen.getByLabelText('搜索河段'), 'High')

    await waitFor(() => {
      expect(lastQueryFor('/api/v1/met/stations')?.search).toBe('North')
      expect(lastQueryFor('/api/v1/basin-versions/{basin_version_id}/river-segments')?.search).toBe('High')
    })

    const stationQuery = lastQueryFor('/api/v1/met/stations')
    expect(stationQuery?.model_id).toBe('basins_qhh_shud')
    expect(stationQuery?.basin_version_id).toBe('basins_qhh_vbasins')

    const riverCall = vi.mocked(client.GET).mock.calls
      .filter(([path]) => path === '/api/v1/basin-versions/{basin_version_id}/river-segments')
      .at(-1)
    const riverPath = (riverCall?.[1] as { params?: { path?: Record<string, unknown> } } | undefined)?.params?.path
    expect(riverPath?.basin_version_id).toBe('basins_qhh_vbasins')
    expect(lastQueryFor('/api/v1/basin-versions/{basin_version_id}/river-segments')?.river_network_version_id)
      .toBe('basins_qhh_rivnet_vbasins')

    // 红线：请求中不得出现手输 identity token / 假数据标记。
    const serialized = JSON.stringify(vi.mocked(client.GET).mock.calls)
    expect(serialized).not.toContain('manual')
    expect(serialized).not.toContain('fake')
  })

  it('不在 search/筛选请求中夹带额外后端 identity 参数（run_id/forcing_version_id）', async () => {
    const user = userEvent.setup()
    const result = bootstrapResult()
    render(<ReadyHydroMetContent result={result} product={result.product!} />)

    await user.type(screen.getByLabelText('搜索气象站点'), 'North')
    await waitFor(() => expect(lastQueryFor('/api/v1/met/stations')?.search).toBe('North'))

    const stationQuery = lastQueryFor('/api/v1/met/stations') ?? {}
    expect(Object.keys(stationQuery)).not.toContain('run_id')
    expect(Object.keys(stationQuery)).not.toContain('forcing_version_id')
    expect(Object.keys(stationQuery)).not.toContain('river_network_version_id')
  })
})

describe('列表诚实降级 (#315)', () => {
  it('站点 search 无匹配时展示诚实空态，不绘制假站点', async () => {
    const user = userEvent.setup()
    vi.mocked(client.GET).mockImplementation(async (path: string) => {
      if (path === '/api/v1/met/stations') {
        return { data: success(stationPage([], { totalCount: 0 })), error: undefined } as never
      }
      if (path === '/api/v1/basin-versions/{basin_version_id}/river-segments') {
        return { data: success(riverCollection([])), error: undefined } as never
      }
      return { data: success({}), error: undefined } as never
    })
    const result = bootstrapResult()
    render(<ReadyHydroMetContent result={result} product={result.product!} />)

    await user.type(screen.getByLabelText('搜索气象站点'), 'no-match')
    await waitFor(() => expect(screen.getByTestId('hydro-met-station-no-results')).toBeInTheDocument())
    expect(within(screen.getByTestId('hydro-met-station-panel')).queryByTestId('hydro-met-station-row')).toBeNull()
  })
})

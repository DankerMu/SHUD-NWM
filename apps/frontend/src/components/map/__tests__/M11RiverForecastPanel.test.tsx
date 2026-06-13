import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { M11RiverForecastPanel, type M11RiverPopupSegment } from '@/components/map/M11RiverForecastPanel'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import { fetchHydroMetLatestProduct, type QhhLatestProduct } from '@/pages/hydroMet/bootstrap'

vi.mock('@/api/client', () => ({ client: { GET: vi.fn() } }))

// 面板按源解析 latest-product（GFS+IFS 各一次），mock 它隔离双源/honest 逻辑。
vi.mock('@/pages/hydroMet/bootstrap', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/hydroMet/bootstrap')>()
  return { ...actual, fetchHydroMetLatestProduct: vi.fn() }
})

vi.mock('echarts-for-react/lib/core', () => ({
  default: ({ option }: { option: unknown }) => <pre data-testid="mock-forecast-echarts">{JSON.stringify(option)}</pre>,
}))
vi.mock('@/components/charts/echartsCore', () => ({ echarts: {} }))

function success<T>(data: T) {
  return { status: 'success', data }
}

function product(source: HydroMetSource, overrides: Partial<QhhLatestProduct> = {}): QhhLatestProduct {
  return {
    basin_id: 'basins_qhh',
    model_id: 'basins_qhh_shud',
    basin_version_id: 'bv-1',
    river_network_version_id: 'rn-1',
    source_id: source,
    cycle_time: '2026-05-21T00:00:00Z',
    run_id: 'run-1',
    forcing_version_id: 'forc-1',
    station_count: 10,
    expected_station_count: 10,
    segment_count: 20,
    expected_segment_count: 20,
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
      return_period_status: 'unavailable',
      return_period_reasons: [],
    },
    quality: {
      station_sample_count: 1,
      river_sample_count: 1,
      required_station_variables: ['PRCP', 'TEMP', 'RH', 'wind', 'Rn', 'Press'],
      station_variable_coverage: [],
      candidate_limit: 20,
      search_limit: 20,
      context_limit: 20,
      query_indexes: [],
    },
    ...overrides,
  } as QhhLatestProduct
}

const segment: M11RiverPopupSegment = {
  river_segment_id: 'seg-009',
  segment_id: 'seg-009',
  river_network_version_id: 'rn-1',
  basin_version_id: 'bv-1',
  name: 'Main Stem 009',
}

function forecastFor(source: HydroMetSource, riverSegmentId = 'seg-009') {
  const scenario = source === 'IFS' ? 'forecast_ifs_deterministic' : 'forecast_gfs_deterministic'
  const base = source === 'IFS' ? 4000 : 3225
  return {
    river_segment_id: riverSegmentId,
    issue_time: '2026-05-21T00:00:00Z',
    variable: 'q_down',
    unit: 'm3/s',
    series: [
      {
        scenario_id: scenario,
        source_id: source,
        cycle_time: '2026-05-21T00:00:00Z',
        points: [
          { valid_time: '2026-05-21T06:00:00Z', value: base },
          { valid_time: '2026-05-21T12:00:00Z', value: base + 75 },
        ],
      },
    ],
  }
}

// client.GET 按 scenarios query 区分源返回；ifsSegmentId 可注入坏 segment 触发 IFS ok:false。
function mockForecastBySource(opts: { ifsSegmentId?: string } = {}) {
  vi.mocked(client.GET).mockImplementation((async (_path: string, init: { params: { query: { scenarios: string } } }) => {
    const isIfs = init.params.query.scenarios.includes('ifs')
    const body = isIfs ? forecastFor('IFS', opts.ifsSegmentId ?? 'seg-009') : forecastFor('GFS')
    return { data: success(body), error: undefined }
  }) as never)
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(fetchHydroMetLatestProduct).mockImplementation((async ({ source }: { source: HydroMetSource }) => product(source)) as never)
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('M11RiverForecastPanel', () => {
  it('renders GFS + IFS on one axis in a side panel with wheel zoom, no source switch', async () => {
    mockForecastBySource()
    render(<M11RiverForecastPanel basinId="basins_qhh" segment={segment} />)

    expect(screen.getByTestId('m11-river-forecast-panel')).toBeInTheDocument()
    await screen.findByTestId('m11-river-panel-chart')
    const option = screen.getByTestId('mock-forecast-echarts').textContent ?? ''
    // 双源同图：GFS(3225) 与 IFS(4000) 两条 series 同时渲染
    expect(option).toContain('3225')
    expect(option).toContain('4000')
    // 滚轮缩放时间轴
    expect(option).toContain('"dataZoom"')
    // 不再有 GFS/IFS 切换控件（不做切换）
    expect(screen.queryByTestId('m11-popup-source-controls')).not.toBeInTheDocument()
    // 两源各解析一次 latest-product
    expect(fetchHydroMetLatestProduct).toHaveBeenCalledWith(expect.objectContaining({ source: 'GFS', basinId: 'basins_qhh' }))
    expect(fetchHydroMetLatestProduct).toHaveBeenCalledWith(expect.objectContaining({ source: 'IFS', basinId: 'basins_qhh' }))
  })

  it('still draws the valid source and lists the failed one when one source fails validation', async () => {
    mockForecastBySource({ ifsSegmentId: 'seg-OTHER' }) // IFS 响应 segment 不匹配 → ok:false
    render(<M11RiverForecastPanel basinId="basins_qhh" segment={segment} />)

    await screen.findByTestId('m11-river-panel-chart')
    const option = screen.getByTestId('mock-forecast-echarts').textContent ?? ''
    expect(option).toContain('3225') // GFS 仍绘制
    expect(option).not.toContain('4000') // IFS 未绘制
    expect(screen.getByTestId('m11-river-panel-partial')).toHaveTextContent('IFS')
  })

  it('shows honest empty state and resolves no product when basinId=null', () => {
    render(<M11RiverForecastPanel basinId={null} segment={segment} />)

    expect(screen.getByTestId('m11-river-panel-empty')).toHaveTextContent('请选择流域')
    expect(screen.queryByTestId('mock-forecast-echarts')).not.toBeInTheDocument()
    expect(vi.mocked(fetchHydroMetLatestProduct)).not.toHaveBeenCalled()
  })
})

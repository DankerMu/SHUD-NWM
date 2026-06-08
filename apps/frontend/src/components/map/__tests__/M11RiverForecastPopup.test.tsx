import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { M11RiverForecastPopup, type M11RiverPopupSegment } from '@/components/map/M11RiverForecastPopup'
import { fetchHydroMetLatestProduct, type QhhLatestProduct } from '@/pages/hydroMet/bootstrap'

vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

// 弹窗只取 latest-product（不拉 stations/river-segments）；mock 它隔离曲线/honest 逻辑。
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

function product(overrides: Partial<QhhLatestProduct> = {}): QhhLatestProduct {
  return {
    basin_id: 'basins_qhh',
    model_id: 'basins_qhh_shud',
    basin_version_id: 'bv-1',
    river_network_version_id: 'rn-1',
    source_id: 'GFS',
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

function mockForecast(responseBody: Record<string, unknown>) {
  vi.mocked(client.GET).mockResolvedValue({ data: success(responseBody), error: undefined } as never)
}

const validForecast = {
  river_segment_id: 'seg-009',
  issue_time: '2026-05-21T00:00:00Z',
  variable: 'q_down',
  unit: 'm3/s',
  series: [
    {
      scenario_id: 'forecast_gfs_deterministic',
      source_id: 'GFS',
      cycle_time: '2026-05-21T00:00:00Z',
      points: [
        { valid_time: '2026-05-21T06:00:00Z', value: 3225 },
        { valid_time: '2026-05-21T12:00:00Z', value: 3300 },
      ],
    },
  ],
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(fetchHydroMetLatestProduct).mockResolvedValue(product())
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('M11RiverForecastPopup', () => {
  it('renders the q_down forecast chart in a glass popup when validation passes', async () => {
    mockForecast(validForecast)
    render(<M11RiverForecastPopup basinId="basins_qhh" initialSource="GFS" segment={segment} />)

    // 玻璃容器 + 弹窗内 source 控件渲染
    expect(screen.getByTestId('m11-river-popup')).toBeInTheDocument()
    expect(screen.getByTestId('m11-popup-source-controls')).toBeInTheDocument()
    expect(await screen.findByTestId('m11-river-popup-loaded')).toBeInTheDocument()
    const option = screen.getByTestId('mock-forecast-echarts').textContent ?? ''
    expect(option).toContain('q_down river discharge')
    expect(option).toContain('3225')
    expect(screen.getByTestId('m11-river-popup-horizon')).toBeInTheDocument()
    // q_down 单变量标注（产品唯一预报变量）
    expect(screen.getByTestId('m11-river-popup-variable')).toHaveTextContent('q_down')
  })

  it('does NOT render a return-period section in the discharge popup', async () => {
    mockForecast(validForecast)
    render(<M11RiverForecastPopup basinId="basins_qhh" initialSource="GFS" segment={segment} />)

    await screen.findByTestId('m11-river-popup-loaded')
    expect(screen.queryByTestId('hydro-met-return-period-section')).not.toBeInTheDocument()
  })

  it('only resolves latest-product (no stations/river-segments bootstrap) for speed', async () => {
    mockForecast(validForecast)
    render(<M11RiverForecastPopup basinId="basins_qhh" initialSource="GFS" segment={segment} />)

    await screen.findByTestId('m11-river-popup-loaded')
    expect(fetchHydroMetLatestProduct).toHaveBeenCalledWith(expect.objectContaining({ source: 'GFS', basinId: 'basins_qhh' }))
  })

  it('lists the real cycle (single item) in the issue-time selector', async () => {
    mockForecast(validForecast)
    render(<M11RiverForecastPopup basinId="basins_qhh" initialSource="GFS" segment={segment} />)

    await screen.findByTestId('m11-river-popup-loaded')
    const issueTime = screen.getByTestId('m11-popup-issue-time') as HTMLSelectElement
    const options = Array.from(issueTime.querySelectorAll('option')).map((o) => o.value)
    expect(options).toEqual(['2026-05-21T00:00:00Z'])
  })

  it('lists all real cycles from available_issue_times and refetches with the chosen cycle', async () => {
    const cycles = ['2026-05-21T00:00:00Z', '2026-05-20T12:00:00Z', '2026-05-20T00:00:00Z']
    vi.mocked(fetchHydroMetLatestProduct).mockResolvedValue(product({ available_issue_times: cycles } as Partial<QhhLatestProduct>))
    mockForecast(validForecast)
    const user = userEvent.setup()
    render(<M11RiverForecastPopup basinId="basins_qhh" initialSource="GFS" segment={segment} />)

    await screen.findByTestId('m11-river-popup-loaded')
    const issueTime = screen.getByTestId('m11-popup-issue-time') as HTMLSelectElement
    expect(Array.from(issueTime.querySelectorAll('option')).map((o) => o.value)).toEqual(cycles)

    await user.selectOptions(issueTime, '2026-05-20T12:00:00Z')
    await waitFor(() =>
      expect(fetchHydroMetLatestProduct).toHaveBeenLastCalledWith(
        expect.objectContaining({ source: 'GFS', basinId: 'basins_qhh', cycle: '2026-05-20T12:00:00Z' }),
      ),
    )
  })

  it('shows current and peak discharge KPIs derived from rendered points', async () => {
    mockForecast(validForecast)
    render(<M11RiverForecastPopup basinId="basins_qhh" initialSource="GFS" segment={segment} />)

    await screen.findByTestId('m11-river-popup-loaded')
    expect(screen.getByTestId('m11-river-popup-kpi-current')).toHaveTextContent('3225')
    expect(screen.getByTestId('m11-river-popup-kpi-peak')).toHaveTextContent('3300')
  })

  it('refetches with the new source when the user switches GFS to IFS', async () => {
    const user = userEvent.setup()
    mockForecast(validForecast)
    render(<M11RiverForecastPopup basinId="basins_qhh" initialSource="GFS" segment={segment} />)

    await screen.findByTestId('m11-river-popup-loaded')
    expect(fetchHydroMetLatestProduct).toHaveBeenLastCalledWith(expect.objectContaining({ source: 'GFS', basinId: 'basins_qhh' }))

    vi.mocked(fetchHydroMetLatestProduct).mockResolvedValue(product({ source_id: 'IFS', cycle_time: '2026-05-21T12:00:00Z' }))
    await user.click(screen.getByTestId('m11-popup-source-IFS'))
    await waitFor(() =>
      expect(fetchHydroMetLatestProduct).toHaveBeenLastCalledWith(expect.objectContaining({ source: 'IFS', basinId: 'basins_qhh' })),
    )
  })

  it('shows reasons and does NOT draw a curve when forecast fails validation (ok:false)', async () => {
    mockForecast({ ...validForecast, river_segment_id: 'seg-OTHER' })
    render(<M11RiverForecastPopup basinId="basins_qhh" initialSource="GFS" segment={segment} />)

    expect(await screen.findByTestId('m11-river-popup-invalid')).toBeInTheDocument()
    expect(screen.queryByTestId('mock-forecast-echarts')).not.toBeInTheDocument()
  })

  it('shows honest empty state and never resolves a product when basinId=null', () => {
    render(<M11RiverForecastPopup basinId={null} initialSource={null} segment={segment} />)

    expect(screen.getByTestId('m11-river-popup-no-product')).toHaveTextContent('请选择流域')
    expect(screen.queryByTestId('mock-forecast-echarts')).not.toBeInTheDocument()
    expect(vi.mocked(fetchHydroMetLatestProduct)).not.toHaveBeenCalled()
    // 起报选择器诚实空态（无解析产品 → 无起报时间）
    expect(screen.getByTestId('m11-popup-issue-time-empty')).toBeInTheDocument()
  })
})

import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { M11RiverForecastPopup, type M11RiverPopupSegment } from '@/components/map/M11RiverForecastPopup'
import type { QhhLatestProduct } from '@/pages/hydroMet/bootstrap'

vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

// echarts 在无头 WebGL 不可用：mock 成把 option 序列化进 DOM 以便断言曲线渲染。
vi.mock('echarts-for-react/lib/core', () => ({
  default: ({ option }: { option: unknown }) => (
    <pre data-testid="mock-forecast-echarts">{JSON.stringify(option)}</pre>
  ),
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
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('M11RiverForecastPopup', () => {
  it('renders the q_down forecast chart when forecast passes identity + chart validation', async () => {
    mockForecast(validForecast)
    render(<M11RiverForecastPopup product={product()} segment={segment} />)

    expect(await screen.findByTestId('m11-river-popup-loaded')).toBeInTheDocument()
    const option = screen.getByTestId('mock-forecast-echarts').textContent ?? ''
    expect(option).toContain('q_down river discharge')
    expect(option).toContain('3225')
    expect(screen.getByTestId('m11-river-popup-horizon')).toBeInTheDocument()
  })

  it('shows reasons and does NOT draw a curve when forecast fails validation (ok:false)', async () => {
    // 缺 segment 身份匹配：response river_segment_id 与选中不一致 → ok:false。
    mockForecast({ ...validForecast, river_segment_id: 'seg-OTHER' })
    render(<M11RiverForecastPopup product={product()} segment={segment} />)

    expect(await screen.findByTestId('m11-river-popup-invalid')).toBeInTheDocument()
    expect(screen.queryByTestId('mock-forecast-echarts')).not.toBeInTheDocument()
  })

  it('gates return-period via productReady: shows unavailable when product not ready', async () => {
    mockForecast(validForecast)
    const notReady = product({
      availability: {
        ready: false,
        unavailable_reasons: [],
        quality_flags: [],
        quality_notes: [],
        return_period_status: 'ready',
        return_period_reasons: [],
      },
    })
    render(<M11RiverForecastPopup product={notReady} segment={segment} />)

    await waitFor(() => expect(screen.getByTestId('hydro-met-return-period-section')).toBeInTheDocument())
    expect(screen.getByTestId('hydro-met-return-period-unavailable')).toBeInTheDocument()
  })

  it('renders return-period three-state (ready) when product ready + status ready', async () => {
    mockForecast(validForecast)
    const ready = product({
      availability: {
        ready: true,
        unavailable_reasons: [],
        quality_flags: [],
        quality_notes: [],
        return_period_status: 'ready',
        return_period_reasons: [],
      },
    })
    render(<M11RiverForecastPopup product={ready} segment={segment} />)

    expect(await screen.findByTestId('hydro-met-return-period-section')).toBeInTheDocument()
    expect(screen.queryByTestId('hydro-met-return-period-unavailable')).not.toBeInTheDocument()
  })

  it('shows honest empty state and does not call the API when product=null (best unresolved)', async () => {
    render(<M11RiverForecastPopup product={null} segment={segment} productReason="等待 Best Available 解析" />)

    expect(screen.getByTestId('m11-river-popup-no-product')).toHaveTextContent('等待 Best Available 解析')
    expect(screen.queryByTestId('mock-forecast-echarts')).not.toBeInTheDocument()
    expect(vi.mocked(client.GET)).not.toHaveBeenCalled()
  })
})

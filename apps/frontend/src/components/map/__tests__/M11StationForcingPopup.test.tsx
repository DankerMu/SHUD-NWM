import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { M11StationForcingPopup, type M11StationPopupStation } from '@/components/map/M11StationForcingPopup'
import { HYDRO_MET_STATION_VARIABLES } from '@/lib/hydroMet/stationSeries'
import { fetchHydroMetLatestProduct, type QhhLatestProduct } from '@/pages/hydroMet/bootstrap'

vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

vi.mock('@/pages/hydroMet/bootstrap', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/hydroMet/bootstrap')>()
  return { ...actual, fetchHydroMetLatestProduct: vi.fn() }
})

vi.mock('echarts-for-react/lib/core', () => ({
  default: ({ option }: { option: unknown }) => <pre data-testid="mock-station-echarts">{JSON.stringify(option)}</pre>,
}))
vi.mock('@/components/charts/echartsCore', () => ({ echarts: {} }))

function success<T>(data: T) {
  return { status: 'success', data }
}

function product(overrides: Partial<QhhLatestProduct> = {}): QhhLatestProduct {
  return {
    basin_id: 'basins_qhh',
    model_id: 'm-1',
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
      required_station_variables: [...HYDRO_MET_STATION_VARIABLES],
      station_variable_coverage: [],
      candidate_limit: 20,
      search_limit: 20,
      context_limit: 20,
      query_indexes: [],
    },
    ...overrides,
  } as QhhLatestProduct
}


const station: M11StationPopupStation = { station_id: 'qhh_forc_001', station_name: 'QHH forcing 001' }

function metadata() {
  return {
    limit: 240,
    returned_points: 2,
    requested_from: '2026-05-21T00:00:00Z',
    requested_to: '2026-05-22T00:00:00Z',
    returned_from: '2026-05-21T06:00:00Z',
    returned_to: '2026-05-21T12:00:00Z',
    truncated: false,
  }
}

function seriesResponse(overrides: Record<string, unknown> = {}) {
  return {
    station_id: 'qhh_forc_001',
    station: { station_id: 'qhh_forc_001', basin_version_id: 'bv-1' },
    forcing_version_id: 'forc-1',
    model_id: 'm-1',
    source_id: 'GFS',
    cycle_time: '2026-05-21T00:00:00Z',
    valid_time_start: '2026-05-21T06:00:00Z',
    valid_time_end: '2026-05-21T12:00:00Z',
    limit: 240,
    series: HYDRO_MET_STATION_VARIABLES.map((variable) => ({
      variable,
      unit: 'mm',
      source_id: 'GFS',
      cycle_time: '2026-05-21T00:00:00Z',
      truncated: false,
      metadata: metadata(),
      points: [
        { valid_time: '2026-05-21T06:00:00Z', value: 1.2, quality_flag: 'ok' },
        { valid_time: '2026-05-21T12:00:00Z', value: 2.4, quality_flag: 'ok' },
      ],
    })),
    ...overrides,
  }
}

function mockSeries(body: Record<string, unknown>) {
  vi.mocked(client.GET).mockResolvedValue({ data: success(body), error: undefined } as never)
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(fetchHydroMetLatestProduct).mockResolvedValue(product())
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('M11StationForcingPopup', () => {
  it('renders six forcing variable charts in a glass popup when identity matches', async () => {
    mockSeries(seriesResponse())
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(screen.getByTestId('m11-station-popup')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-selector')).toBeInTheDocument()
    expect(await screen.findByTestId('m11-station-popup-loaded')).toBeInTheDocument()
    expect(client.GET).toHaveBeenCalledWith('/api/v1/met/stations/{station_id}/series', expect.objectContaining({
      params: expect.objectContaining({
        query: expect.objectContaining({
          forcing_version_id: 'forc-1',
          model_id: 'm-1',
          source_id: 'GFS',
          cycle_time: '2026-05-21T00:00:00Z',
        }),
      }),
    }))
    for (const variable of HYDRO_MET_STATION_VARIABLES) {
      expect(screen.getByTestId(`m11-station-variable-${variable}-chart`)).toBeInTheDocument()
    }
    expect(screen.getAllByTestId('mock-station-echarts')).toHaveLength(HYDRO_MET_STATION_VARIABLES.length)
  })

  it('changes the displayed curve set when a variable is toggled off', async () => {
    const user = userEvent.setup()
    mockSeries(seriesResponse())
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    await screen.findByTestId('m11-station-popup-loaded')
    expect(screen.getAllByTestId('mock-station-echarts')).toHaveLength(HYDRO_MET_STATION_VARIABLES.length)

    // 关掉 PRCP → 只显其余五个变量曲线
    await user.click(screen.getByTestId('m11-station-variable-toggle-PRCP'))
    expect(screen.queryByTestId('m11-station-variable-PRCP-chart')).not.toBeInTheDocument()
    expect(screen.getAllByTestId('mock-station-echarts')).toHaveLength(HYDRO_MET_STATION_VARIABLES.length - 1)

    // 重新开启 PRCP → 恢复全部
    await user.click(screen.getByTestId('m11-station-variable-toggle-PRCP'))
    expect(screen.getByTestId('m11-station-variable-PRCP-chart')).toBeInTheDocument()
    expect(screen.getAllByTestId('mock-station-echarts')).toHaveLength(HYDRO_MET_STATION_VARIABLES.length)
  })

  it('refetches with the new source when the user switches GFS to IFS', async () => {
    const user = userEvent.setup()
    mockSeries(seriesResponse())
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    await screen.findByTestId('m11-station-popup-loaded')
    expect(fetchHydroMetLatestProduct).toHaveBeenLastCalledWith(expect.objectContaining({ source: 'GFS', basinId: 'basins_qhh' }))

    vi.mocked(fetchHydroMetLatestProduct).mockResolvedValue(product({ source_id: 'IFS' }))
    await user.click(screen.getByTestId('m11-popup-source-IFS'))
    await waitFor(() =>
      expect(fetchHydroMetLatestProduct).toHaveBeenLastCalledWith(expect.objectContaining({ source: 'IFS', basinId: 'basins_qhh' })),
    )
  })

  it('shows identity-mismatch empty state and draws no curve when station_id mismatches', async () => {
    mockSeries(seriesResponse({ station_id: 'OTHER' }))
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-identity-mismatch')).toBeInTheDocument()
    expect(screen.queryByTestId('mock-station-echarts')).not.toBeInTheDocument()
  })

  it('shows honest empty state and never resolves a product when basinId=null', () => {
    render(<M11StationForcingPopup basinId={null} initialSource={null} station={station} />)

    expect(screen.getByTestId('m11-station-popup-no-product')).toHaveTextContent('请选择流域')
    expect(screen.queryByTestId('mock-station-echarts')).not.toBeInTheDocument()
    expect(vi.mocked(fetchHydroMetLatestProduct)).not.toHaveBeenCalled()
  })

  it('rejects the variable (no echarts) when any point is malformed/NaN', async () => {
    const body = seriesResponse()
    const series = (body.series as Record<string, unknown>[])[0]
    series.points = [
      { valid_time: '2026-05-21T06:00:00Z', value: 1.2, quality_flag: 'ok' },
      { valid_time: '2026-05-21T12:00:00Z', value: Number.NaN, quality_flag: 'ok' },
    ]
    mockSeries(body)
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-loaded')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-invalid')).toBeInTheDocument()
    expect(screen.queryByTestId('m11-station-variable-PRCP-chart')).not.toBeInTheDocument()
    expect(screen.getAllByTestId('mock-station-echarts')).toHaveLength(HYDRO_MET_STATION_VARIABLES.length - 1)
  })

  it('gates on missing unit (no echarts) when unit is null', async () => {
    const body = seriesResponse()
    const series = (body.series as Record<string, unknown>[])[0]
    series.unit = null
    mockSeries(body)
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-loaded')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-missing-unit')).toBeInTheDocument()
    expect(screen.queryByTestId('m11-station-variable-PRCP-chart')).not.toBeInTheDocument()
    expect(screen.getAllByTestId('mock-station-echarts')).toHaveLength(HYDRO_MET_STATION_VARIABLES.length - 1)
  })

  it('rejects the variable (no echarts) when metadata is malformed', async () => {
    const body = seriesResponse()
    const series = (body.series as Record<string, unknown>[])[0]
    series.metadata = { ...metadata(), returned_points: -1, returned_from: 'not-a-time' }
    mockSeries(body)
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-loaded')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-invalid')).toBeInTheDocument()
    expect(screen.queryByTestId('m11-station-variable-PRCP-chart')).not.toBeInTheDocument()
    expect(screen.getAllByTestId('mock-station-echarts')).toHaveLength(HYDRO_MET_STATION_VARIABLES.length - 1)
  })

  it('renders echarts and discloses truncation/cap when series is truncated', async () => {
    const body = seriesResponse()
    const series = (body.series as Record<string, unknown>[])[0]
    series.truncated = true
    series.metadata = { ...metadata(), returned_points: 1000, truncated: true }
    mockSeries(body)
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-loaded')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-chart')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-truncated')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-capped')).toBeInTheDocument()
  })
})

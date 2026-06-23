import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { formatIssueTime } from '@/components/map/M11PopupChrome'
import { M11StationForcingPopup, type M11StationPopupStation } from '@/components/map/M11StationForcingPopup'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import { HYDRO_MET_STATION_SERIES_API_TUPLE_LIMIT, HYDRO_MET_STATION_VARIABLES } from '@/lib/hydroMet/stationSeries'
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

const DEFAULT_CYCLE = '2026-05-21T00:00:00Z'
const RETAINED_OUT_CYCLE = '2026-05-20T00:00:00Z'

const station: M11StationPopupStation = { station_id: 'qhh_forc_001', station_name: 'QHH forcing 001' }

type SeriesQuery = {
  forcing_version_id?: unknown
  model_id?: unknown
  source_id?: unknown
  cycle_time?: unknown
  variables?: unknown
  limit?: unknown
}

function success<T>(data: T) {
  return { status: 'success', data }
}

function forcingVersionFor(source: HydroMetSource, cycle = DEFAULT_CYCLE) {
  const suffix = cycle === RETAINED_OUT_CYCLE ? 'old' : 'latest'
  return `forc-${source.toLowerCase()}-${suffix}`
}

function product(overrides: Partial<QhhLatestProduct> = {}): QhhLatestProduct {
  return {
    basin_id: 'basins_qhh',
    model_id: 'm-1',
    basin_version_id: 'bv-1',
    river_network_version_id: 'rn-1',
    source_id: 'GFS',
    cycle_time: DEFAULT_CYCLE,
    run_id: 'run-gfs-latest',
    forcing_version_id: forcingVersionFor('GFS'),
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

function productFor(source: HydroMetSource, cycle = DEFAULT_CYCLE, issueTimes = [DEFAULT_CYCLE]) {
  return product({
    source_id: source,
    cycle_time: cycle,
    run_id: `run-${source.toLowerCase()}-${cycle === RETAINED_OUT_CYCLE ? 'old' : 'latest'}`,
    forcing_version_id: forcingVersionFor(source, cycle),
    available_issue_times: issueTimes,
  })
}

function metadata(overrides: Record<string, unknown> = {}) {
  return {
    limit: HYDRO_MET_STATION_SERIES_API_TUPLE_LIMIT,
    returned_points: 2,
    requested_from: '2026-05-21T00:00:00Z',
    requested_to: '2026-05-22T00:00:00Z',
    returned_from: '2026-05-21T06:00:00Z',
    returned_to: '2026-05-21T12:00:00Z',
    truncated: false,
    ...overrides,
  }
}

function seriesOverridesFromQuery(query: SeriesQuery) {
  const overrides: Record<string, unknown> = {}
  if (typeof query.cycle_time === 'string') overrides.cycle_time = query.cycle_time
  if (typeof query.forcing_version_id === 'string') overrides.forcing_version_id = query.forcing_version_id
  if (typeof query.model_id === 'string') overrides.model_id = query.model_id
  return overrides
}

function seriesResponseFor(source: HydroMetSource, overrides: Record<string, unknown> = {}) {
  const cycle = typeof overrides.cycle_time === 'string' ? overrides.cycle_time : DEFAULT_CYCLE
  const forcingVersion =
    typeof overrides.forcing_version_id === 'string' ? overrides.forcing_version_id : forcingVersionFor(source, cycle)
  const modelId = typeof overrides.model_id === 'string' ? overrides.model_id : 'm-1'
  return {
    station_id: station.station_id,
    station: { station_id: station.station_id, basin_version_id: 'bv-1' },
    forcing_version_id: forcingVersion,
    model_id: modelId,
    source_id: source,
    cycle_time: cycle,
    valid_time_start: '2026-05-21T06:00:00Z',
    valid_time_end: '2026-05-21T12:00:00Z',
    limit: HYDRO_MET_STATION_SERIES_API_TUPLE_LIMIT,
    series: HYDRO_MET_STATION_VARIABLES.map((variable) => ({
      variable,
      unit: 'mm',
      source_id: source,
      cycle_time: cycle,
      truncated: false,
      metadata: metadata(),
      points: [
        { valid_time: '2026-05-21T06:00:00Z', value: source === 'GFS' ? 1.2 : 1.6, quality_flag: 'ok' },
        { valid_time: '2026-05-21T12:00:00Z', value: source === 'GFS' ? 2.4 : 2.8, quality_flag: 'ok' },
      ],
    })),
    ...overrides,
  }
}

function getSeriesQuery(init: unknown) {
  return (init as { params: { query: SeriesQuery } }).params.query
}

function sourceFromQuery(query: SeriesQuery): HydroMetSource {
  return query.source_id === 'IFS' ? 'IFS' : 'GFS'
}

function mockLatestProducts(issueTimes = [DEFAULT_CYCLE]) {
  vi.mocked(fetchHydroMetLatestProduct).mockImplementation(async (request) => {
    const cycle = request.cycle ?? issueTimes[0] ?? DEFAULT_CYCLE
    return productFor(request.source, cycle, issueTimes)
  })
}

function mockSeriesBySource(
  builder: (source: HydroMetSource, query: SeriesQuery) => Record<string, unknown> = (source, query) =>
    seriesResponseFor(source, seriesOverridesFromQuery(query)),
) {
  vi.mocked(client.GET).mockImplementation(async (_path, init) => {
    const query = getSeriesQuery(init)
    const source = sourceFromQuery(query)
    return { data: success(builder(source, query)), error: undefined } as never
  })
}

function stationSeriesDiskMiss() {
  return {
    error: {
      code: 'STATION_FORCING_FILE_NOT_FOUND',
      message: 'Station forcing file not found.',
      details: {
        station_id: station.station_id,
        expected_path: 'forcing/gfs/2026052000/basins_qhh_v1/m-1/shud/qhh_forc_001.csv',
      },
    },
  }
}

function prcpSeries(body: Record<string, unknown>) {
  return (body.series as Record<string, unknown>[]).find((series) => series.variable === 'PRCP') as Record<string, unknown>
}

const originalViewport = { width: window.innerWidth, height: window.innerHeight }

function setViewport(width: number, height: number) {
  Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: width })
  Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: height })
  window.dispatchEvent(new Event('resize'))
}

function panelPosition(panel: HTMLElement) {
  return {
    x: Number.parseFloat(panel.style.left),
    y: Number.parseFloat(panel.style.top),
  }
}

async function positionedPanel(testId: string) {
  const panel = await screen.findByTestId(testId)
  await waitFor(() => expect(Number.isFinite(panelPosition(panel).x)).toBe(true))
  return panel
}

function rect(left: number, top: number, width: number, height: number): DOMRect {
  return {
    x: left,
    y: top,
    left,
    top,
    width,
    height,
    right: left + width,
    bottom: top + height,
    toJSON: () => ({}),
  } as DOMRect
}

function expectRectInside(container: DOMRect, target: DOMRect) {
  expect(target.left).toBeGreaterThanOrEqual(container.left)
  expect(target.top).toBeGreaterThanOrEqual(container.top)
  expect(target.right).toBeLessThanOrEqual(container.right)
  expect(target.bottom).toBeLessThanOrEqual(container.bottom)
}

function mockCurveWindowRects({
  containerTestId,
  panelTestId,
  handleTestId,
  closeLabel,
  panelWidth,
  panelHeight,
}: {
  containerTestId: string
  panelTestId: string
  handleTestId: string
  closeLabel: string
  panelWidth: number
  panelHeight: number
}) {
  const original = HTMLElement.prototype.getBoundingClientRect
  return vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function getBoundingClientRect(this: HTMLElement) {
    const testId = this.getAttribute('data-testid')
    const panel = document.querySelector(`[data-testid="${panelTestId}"]`) as HTMLElement | null
    const current = panel ? panelPosition(panel) : { x: 0, y: 0 }
    const left = Number.isFinite(current.x) ? current.x : 0
    const top = Number.isFinite(current.y) ? current.y : 0

    if (testId === containerTestId) return rect(0, 0, 900, 600)
    if (testId === panelTestId) return rect(left, top, panelWidth, panelHeight)
    if (testId === handleTestId) return rect(left, top, panelWidth, 72)
    if (this.getAttribute('aria-label') === closeLabel) return rect(left + panelWidth - 44, top + 12, 28, 28)
    return original.call(this)
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  setViewport(originalViewport.width, originalViewport.height)
  mockLatestProducts()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('M11StationForcingPopup', () => {
  it('waits honestly without station-series requests when no concrete GFS/IFS source is available', () => {
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource={null} station={station} />)

    expect(screen.getByTestId('m11-station-popup-no-product')).toHaveTextContent('等待 GFS/IFS 源解析')
    expect(fetchHydroMetLatestProduct).not.toHaveBeenCalled()
    expect(vi.mocked(client.GET)).not.toHaveBeenCalled()
  })

  it('keeps compare GFS+IFS resolved state on the existing dual-source station-series path', async () => {
    mockSeriesBySource()
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS+IFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-loaded')).toBeInTheDocument()
    expect(fetchHydroMetLatestProduct).toHaveBeenCalledWith(expect.objectContaining({ source: 'GFS', basinId: 'basins_qhh' }))
    expect(fetchHydroMetLatestProduct).toHaveBeenCalledWith(expect.objectContaining({ source: 'IFS', basinId: 'basins_qhh' }))
  })

  it('renders one selected forcing variable chart with GFS and IFS on the same axis', async () => {
    mockSeriesBySource()
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(screen.getByTestId('m11-station-popup')).toHaveClass('aspect-video')
    expect(screen.getByTestId('m11-station-popup')).toHaveAttribute('data-m11-curve-window-kind', 'station')
    expect(screen.getByText('QHH 代站 1')).toBeInTheDocument()
    expect(screen.getByText('站点 ID qhh_forc_001')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-selector')).toBeInTheDocument()
    expect(await screen.findByTestId('m11-station-popup-loaded')).toBeInTheDocument()
    expect(screen.queryByTestId('m11-popup-source-controls')).not.toBeInTheDocument()

    expect(fetchHydroMetLatestProduct).toHaveBeenCalledTimes(2)
    expect(fetchHydroMetLatestProduct).toHaveBeenCalledWith(expect.objectContaining({ source: 'GFS', basinId: 'basins_qhh' }))
    expect(fetchHydroMetLatestProduct).toHaveBeenCalledWith(expect.objectContaining({ source: 'IFS', basinId: 'basins_qhh' }))

    const queries = vi.mocked(client.GET).mock.calls.map(([, init]) => getSeriesQuery(init))
    expect(queries.map((query) => query.source_id).sort()).toEqual(['GFS', 'IFS'])
    for (const query of queries) {
      expect(query).toEqual(expect.objectContaining({
        model_id: 'm-1',
        variables: [...HYDRO_MET_STATION_VARIABLES],
        limit: HYDRO_MET_STATION_SERIES_API_TUPLE_LIMIT,
      }))
      const source = sourceFromQuery(query)
      expect(query.forcing_version_id).toBe(forcingVersionFor(source))
    }

    expect(HYDRO_MET_STATION_VARIABLES).toEqual(['PRCP', 'TEMP', 'RH', 'wind', 'Rn'])
    expect(screen.getByTestId('m11-station-variable-PRCP-chart')).toBeInTheDocument()
    for (const variable of HYDRO_MET_STATION_VARIABLES.filter((variable) => variable !== 'PRCP')) {
      expect(screen.queryByTestId(`m11-station-variable-${variable}-chart`)).not.toBeInTheDocument()
    }

    const chart = screen.getByTestId('mock-station-echarts')
    expect(screen.getAllByTestId('mock-station-echarts')).toHaveLength(1)
    expect(chart).toHaveTextContent('"name":"GFS"')
    expect(chart).toHaveTextContent('"name":"IFS"')
  })

  it('keeps station tabs and issue-time controls from starting drag while narrow viewport clamp stays reachable', async () => {
    setViewport(360, 500)
    const cycles = [DEFAULT_CYCLE, RETAINED_OUT_CYCLE]
    mockLatestProducts(cycles)
    mockSeriesBySource()
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    const panel = await positionedPanel('m11-station-popup')
    const initial = panelPosition(panel)
    expect(initial.x).toBe(12)
    expect(initial.y).toBeGreaterThanOrEqual(12)
    await screen.findByTestId('m11-station-popup-loaded')

    fireEvent.pointerDown(screen.getByTestId('m11-station-variable-toggle-TEMP'), { button: 0, clientX: 40, clientY: 150 })
    fireEvent.pointerMove(window, { clientX: 250, clientY: 220 })
    fireEvent.pointerUp(window)
    expect(panelPosition(panel)).toEqual(initial)

    fireEvent.pointerDown(screen.getByTestId('m11-popup-issue-time'), { button: 0, clientX: 40, clientY: 100 })
    fireEvent.pointerMove(window, { clientX: 250, clientY: 240 })
    fireEvent.pointerUp(window)
    expect(panelPosition(panel)).toEqual(initial)

    const handle = screen.getByTestId('m11-station-popup-drag-handle')
    fireEvent.pointerDown(handle, { button: 0, clientX: initial.x + 24, clientY: initial.y + 24 })
    fireEvent.pointerMove(window, { clientX: 4000, clientY: 4000 })
    fireEvent.pointerUp(window)
    expect(panelPosition(panel).x).toBe(12)
    expect(panelPosition(panel).y).toBeCloseTo(500 - Math.min((360 - 24) * 9 / 16, 500 - 24) - 12)
  })

  it('keeps the station window, drag handle, and close button inside a nonzero map container', async () => {
    setViewport(900, 600)
    mockSeriesBySource()
    const rectSpy = mockCurveWindowRects({
      containerTestId: 'm11-map-container',
      panelTestId: 'm11-station-popup',
      handleTestId: 'm11-station-popup-drag-handle',
      closeLabel: '关闭弹窗',
      panelWidth: 378,
      panelHeight: 213,
    })

    try {
      render(
        <section data-testid="m11-map-container">
          <M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} onClose={() => undefined} />
        </section>,
      )

      const panel = await positionedPanel('m11-station-popup')
      const handle = screen.getByTestId('m11-station-popup-drag-handle')
      const close = screen.getByLabelText('关闭弹窗')
      const initial = panelPosition(panel)

      fireEvent.pointerDown(handle, { button: 0, clientX: initial.x + 24, clientY: initial.y + 24 })
      fireEvent.pointerMove(window, { clientX: 5000, clientY: 5000 })
      fireEvent.pointerUp(window)

      const containerRect = screen.getByTestId('m11-map-container').getBoundingClientRect()
      expectRectInside(containerRect, panel.getBoundingClientRect())
      expectRectInside(containerRect, handle.getBoundingClientRect())
      expectRectInside(containerRect, close.getBoundingClientRect())
    } finally {
      rectSpy.mockRestore()
    }
  })

  it('resets station window placement when the station identity changes and reflects active z-index', async () => {
    mockSeriesBySource()
    const { rerender } = render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} active={false} />)

    const panel = await positionedPanel('m11-station-popup')
    const initial = panelPosition(panel)
    expect(panel.style.zIndex).toBe('132')
    const handle = screen.getByTestId('m11-station-popup-drag-handle')
    fireEvent.pointerDown(handle, { button: 0, clientX: initial.x + 24, clientY: initial.y + 24 })
    fireEvent.pointerMove(window, { clientX: initial.x + 220, clientY: initial.y + 140 })
    fireEvent.pointerUp(window)
    expect(panelPosition(panel)).not.toEqual(initial)

    rerender(
      <M11StationForcingPopup
        basinId="basins_qhh"
        initialSource="GFS"
        station={{ station_id: 'qhh_forc_002', station_name: 'North Ridge station' }}
        active
      />,
    )

    await waitFor(() => expect(panelPosition(panel)).toEqual(initial))
    expect(panel.style.zIndex).toBe('142')
  })

  it('switches the selected variable without adding another chart panel', async () => {
    const user = userEvent.setup()
    mockSeriesBySource()
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    await screen.findByTestId('m11-station-popup-loaded')
    expect(screen.getByTestId('m11-station-variable-PRCP-chart')).toBeInTheDocument()
    expect(screen.getAllByTestId('mock-station-echarts')).toHaveLength(1)

    await user.click(screen.getByTestId('m11-station-variable-toggle-TEMP'))
    expect(screen.queryByTestId('m11-station-variable-PRCP-chart')).not.toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-TEMP-chart')).toBeInTheDocument()
    expect(screen.getAllByTestId('mock-station-echarts')).toHaveLength(1)
    expect(screen.getByTestId('m11-station-variable-toggle-TEMP')).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId('m11-station-variable-toggle-PRCP')).toHaveAttribute('aria-selected', 'false')
  })

  it('keeps the available source plotted when the other source is outside the retained disk window', async () => {
    const user = userEvent.setup()
    mockLatestProducts([DEFAULT_CYCLE, RETAINED_OUT_CYCLE])
    vi.mocked(client.GET).mockImplementation(async (_path, init) => {
      const query = getSeriesQuery(init)
      const source = sourceFromQuery(query)
      if (source === 'GFS' && query.cycle_time === RETAINED_OUT_CYCLE) {
        return { data: undefined, error: stationSeriesDiskMiss() } as never
      }
      return { data: success(seriesResponseFor(source, seriesOverridesFromQuery(query))), error: undefined } as never
    })

    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)
    await screen.findByTestId('m11-station-popup-loaded')

    await user.click(screen.getByTestId('m11-popup-issue-time'))
    const content = await screen.findByTestId('m11-popup-issue-time-content')
    expect(content).toHaveClass('bg-slate-950/95')
    await user.click(screen.getByRole('option', { name: formatIssueTime(RETAINED_OUT_CYCLE) }))

    await waitFor(() => {
      expect(screen.getByTestId('m11-station-popup-partial')).toHaveTextContent('GFS：该起报 05-20 00:00 UTC')
    })
    const retainedCycleQueries = vi.mocked(client.GET).mock.calls
      .map(([, init]) => getSeriesQuery(init))
      .filter((query) => query.cycle_time === RETAINED_OUT_CYCLE)
    for (const source of ['GFS', 'IFS'] satisfies HydroMetSource[]) {
      expect(retainedCycleQueries.find((query) => sourceFromQuery(query) === source)).toEqual(
        expect.objectContaining({ cycle_time: RETAINED_OUT_CYCLE }),
      )
    }
    expect(screen.getByTestId('m11-station-popup-partial')).toHaveTextContent('磁盘保留窗口')
    expect(screen.getByTestId('m11-station-variable-PRCP-chart')).toBeInTheDocument()
    expect(screen.queryByTestId('m11-station-variable-toggle-Press')).not.toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: 'Press' })).not.toBeInTheDocument()
    const chart = screen.getByTestId('mock-station-echarts')
    expect(chart).not.toHaveTextContent('"name":"GFS"')
    expect(chart).toHaveTextContent('"name":"IFS"')
  })

  it('keeps a stale selected cycle honest and reloads both source identities when latest is selected', async () => {
    const user = userEvent.setup()
    const initialCycles = [DEFAULT_CYCLE, RETAINED_OUT_CYCLE]
    vi.mocked(fetchHydroMetLatestProduct).mockImplementation(async (request) => {
      if (request.cycle === RETAINED_OUT_CYCLE) return productFor(request.source, DEFAULT_CYCLE, [DEFAULT_CYCLE])
      const issueTimes = request.cycle === DEFAULT_CYCLE ? [DEFAULT_CYCLE] : initialCycles
      return productFor(request.source, DEFAULT_CYCLE, issueTimes)
    })
    mockSeriesBySource()

    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)
    await screen.findByTestId('m11-station-variable-PRCP-chart')

    await user.click(screen.getByTestId('m11-popup-issue-time'))
    await user.click(screen.getByRole('option', { name: formatIssueTime(RETAINED_OUT_CYCLE) }))

    const empty = await screen.findByTestId('m11-station-popup-empty')
    expect(empty).toHaveTextContent(/起报.*已不可用/)
    expect(screen.getByTestId('m11-popup-issue-time')).toHaveTextContent(formatIssueTime(RETAINED_OUT_CYCLE))

    await user.click(screen.getByTestId('m11-popup-issue-time'))
    const staleOption = screen.getByRole('option', { name: /05-20 00:00 UTC.*磁盘保留不可用/ })
    expect(staleOption).toHaveAttribute('aria-disabled', 'true')

    vi.mocked(fetchHydroMetLatestProduct).mockClear()
    vi.mocked(client.GET).mockClear()
    await user.click(screen.getByRole('option', { name: formatIssueTime(DEFAULT_CYCLE) }))

    await waitFor(() => {
      expect(fetchHydroMetLatestProduct).toHaveBeenCalledWith(expect.objectContaining({ source: 'GFS', cycle: DEFAULT_CYCLE }))
      expect(fetchHydroMetLatestProduct).toHaveBeenCalledWith(expect.objectContaining({ source: 'IFS', cycle: DEFAULT_CYCLE }))
      expect(client.GET).toHaveBeenCalledTimes(2)
    })

    const queries = vi.mocked(client.GET).mock.calls.map(([, init]) => getSeriesQuery(init))
    for (const source of ['GFS', 'IFS'] satisfies HydroMetSource[]) {
      expect(queries.find((query) => sourceFromQuery(query) === source)).toEqual(
        expect.objectContaining({
          cycle_time: DEFAULT_CYCLE,
          forcing_version_id: forcingVersionFor(source, DEFAULT_CYCLE),
          model_id: 'm-1',
        }),
      )
    }
    expect(screen.getByTestId('m11-station-variable-PRCP-chart')).toBeInTheDocument()
  })

  it('keeps issue-time choices recoverable when both downstream station-series paths fail after latest-product', async () => {
    const user = userEvent.setup()
    const cycles = [DEFAULT_CYCLE, RETAINED_OUT_CYCLE]
    mockLatestProducts(cycles)
    vi.mocked(client.GET).mockResolvedValue({
      data: undefined,
      error: { error: { code: 'STATION_SERIES_UNAVAILABLE', message: 'station-series failed' } },
    } as never)

    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    const empty = await screen.findByTestId('m11-station-popup-empty')
    expect(empty).toHaveTextContent('GFS')
    expect(empty).toHaveTextContent('IFS')
    expect(screen.getByTestId('m11-popup-issue-time')).toHaveTextContent(formatIssueTime(DEFAULT_CYCLE))

    await user.click(screen.getByTestId('m11-popup-issue-time'))
    expect(screen.getAllByRole('option')).toHaveLength(cycles.length)

    vi.mocked(fetchHydroMetLatestProduct).mockClear()
    await user.click(screen.getByRole('option', { name: formatIssueTime(RETAINED_OUT_CYCLE) }))
    await waitFor(() => {
      expect(fetchHydroMetLatestProduct).toHaveBeenCalledWith(expect.objectContaining({ source: 'GFS', cycle: RETAINED_OUT_CYCLE }))
      expect(fetchHydroMetLatestProduct).toHaveBeenCalledWith(expect.objectContaining({ source: 'IFS', cycle: RETAINED_OUT_CYCLE }))
    })
    await screen.findByTestId('m11-station-popup-empty')

    await user.click(screen.getByTestId('m11-popup-issue-time'))
    vi.mocked(fetchHydroMetLatestProduct).mockClear()
    vi.mocked(client.GET).mockClear()
    await user.click(screen.getByRole('option', { name: formatIssueTime(DEFAULT_CYCLE) }))

    await waitFor(() => {
      expect(fetchHydroMetLatestProduct).toHaveBeenCalledWith(expect.objectContaining({ source: 'GFS', cycle: DEFAULT_CYCLE }))
      expect(fetchHydroMetLatestProduct).toHaveBeenCalledWith(expect.objectContaining({ source: 'IFS', cycle: DEFAULT_CYCLE }))
      expect(client.GET).toHaveBeenCalledTimes(2)
    })

    const queries = vi.mocked(client.GET).mock.calls.map(([, init]) => getSeriesQuery(init))
    for (const source of ['GFS', 'IFS'] satisfies HydroMetSource[]) {
      expect(queries.find((query) => sourceFromQuery(query) === source)).toEqual(
        expect.objectContaining({
          cycle_time: DEFAULT_CYCLE,
          forcing_version_id: forcingVersionFor(source, DEFAULT_CYCLE),
          model_id: 'm-1',
        }),
      )
    }
  })

  it('shows an empty state and draws no curve when station_id mismatches for both sources', async () => {
    mockSeriesBySource((source, query) => seriesResponseFor(source, { ...seriesOverridesFromQuery(query), station_id: 'OTHER' }))
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-empty')).toHaveTextContent('station_id=OTHER')
    expect(screen.queryByTestId('mock-station-echarts')).not.toBeInTheDocument()
  })

  it('shows an empty state and draws no curve when model_id mismatches for both sources', async () => {
    mockSeriesBySource((source, query) => seriesResponseFor(source, { ...seriesOverridesFromQuery(query), model_id: 'other-model' }))
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-empty')).toHaveTextContent('model_id=other-model')
    expect(screen.queryByTestId('mock-station-echarts')).not.toBeInTheDocument()
  })

  it('shows an empty state and draws no curve when cycle_time is missing for both sources', async () => {
    mockSeriesBySource((source, query) => {
      const body = seriesResponseFor(source, seriesOverridesFromQuery(query))
      delete body.cycle_time
      return body
    })
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-empty')).toHaveTextContent('cycle_time 元数据格式无效')
    expect(screen.queryByTestId('mock-station-echarts')).not.toBeInTheDocument()
  })

  it('shows honest empty state and never resolves a product when basinId=null', () => {
    render(<M11StationForcingPopup basinId={null} initialSource={null} station={station} />)

    expect(screen.getByTestId('m11-station-popup-no-product')).toHaveTextContent('请选择流域')
    expect(screen.queryByTestId('mock-station-echarts')).not.toBeInTheDocument()
    expect(vi.mocked(fetchHydroMetLatestProduct)).not.toHaveBeenCalled()
  })

  it('rejects the selected variable when any point is malformed/NaN for both sources', async () => {
    mockSeriesBySource((source, query) => {
      const body = seriesResponseFor(source, seriesOverridesFromQuery(query))
      prcpSeries(body).points = [
        { valid_time: '2026-05-21T06:00:00Z', value: 1.2, quality_flag: 'ok' },
        { valid_time: '2026-05-21T12:00:00Z', value: Number.NaN, quality_flag: 'ok' },
      ]
      return body
    })
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-empty')).toHaveTextContent('value 不是有限数值')
    expect(screen.queryByTestId('m11-station-variable-PRCP-chart')).not.toBeInTheDocument()
    expect(screen.queryByTestId('mock-station-echarts')).not.toBeInTheDocument()
  })

  it('gates the selected variable when unit is null for both sources', async () => {
    mockSeriesBySource((source, query) => {
      const body = seriesResponseFor(source, seriesOverridesFromQuery(query))
      prcpSeries(body).unit = null
      return body
    })
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-empty')).toHaveTextContent('缺少 unit 元数据')
    expect(screen.queryByTestId('m11-station-variable-PRCP-chart')).not.toBeInTheDocument()
    expect(screen.queryByTestId('mock-station-echarts')).not.toBeInTheDocument()
  })

  it('rejects the selected variable when metadata is malformed for both sources', async () => {
    mockSeriesBySource((source, query) => {
      const body = seriesResponseFor(source, seriesOverridesFromQuery(query))
      prcpSeries(body).metadata = { ...metadata(), returned_points: -1, returned_from: 'not-a-time' }
      return body
    })
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-empty')).toHaveTextContent('metadata.returned_points')
    expect(screen.queryByTestId('m11-station-variable-PRCP-chart')).not.toBeInTheDocument()
    expect(screen.queryByTestId('mock-station-echarts')).not.toBeInTheDocument()
  })

  it('renders echarts and discloses source-specific truncation/cap badges when series is truncated', async () => {
    mockSeriesBySource((source, query) => {
      const body = seriesResponseFor(source, seriesOverridesFromQuery(query))
      const series = prcpSeries(body)
      series.truncated = true
      series.metadata = { ...metadata(), returned_points: 1000, truncated: true }
      return body
    })
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-loaded')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-chart')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-GFS-truncated')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-GFS-capped')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-IFS-truncated')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-IFS-capped')).toBeInTheDocument()
  })

  it('locally caps long selected-variable series without marking API truncation when metadata is complete', async () => {
    mockSeriesBySource((source, query) => {
      const body = seriesResponseFor(source, seriesOverridesFromQuery(query))
      const points = Array.from({ length: 280 }, (_, index) => ({
        valid_time: new Date(Date.UTC(2026, 4, 21, index)).toISOString(),
        value: source === 'GFS' ? index : index + 0.5,
        quality_flag: 'ok',
      }))
      const series = prcpSeries(body)
      series.points = points
      series.truncated = false
      series.metadata = {
        ...metadata(),
        returned_points: points.length,
        returned_from: points[0].valid_time,
        returned_to: points[points.length - 1].valid_time,
        truncated: false,
      }
      return body
    })
    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)

    expect(await screen.findByTestId('m11-station-popup-loaded')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-GFS-capped')).toBeInTheDocument()
    expect(screen.getByTestId('m11-station-variable-PRCP-IFS-capped')).toBeInTheDocument()
    expect(screen.queryByTestId('m11-station-variable-PRCP-GFS-truncated')).not.toBeInTheDocument()
    expect(screen.queryByTestId('m11-station-variable-PRCP-IFS-truncated')).not.toBeInTheDocument()
  })
})

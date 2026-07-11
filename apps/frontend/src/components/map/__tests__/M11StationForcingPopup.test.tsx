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
    run_status: 'published',
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

  it('keeps the dragged station window placement when the station identity changes and reflects active z-index', async () => {
    mockSeriesBySource()
    const { rerender } = render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} active={false} />)

    const panel = await positionedPanel('m11-station-popup')
    const initial = panelPosition(panel)
    expect(panel.style.zIndex).toBe('132')
    const handle = screen.getByTestId('m11-station-popup-drag-handle')
    fireEvent.pointerDown(handle, { button: 0, clientX: initial.x + 24, clientY: initial.y + 24 })
    fireEvent.pointerMove(window, { clientX: initial.x + 220, clientY: initial.y + 140 })
    fireEvent.pointerUp(window)
    const dragged = panelPosition(panel)
    expect(dragged).not.toEqual(initial)

    rerender(
      <M11StationForcingPopup
        basinId="basins_qhh"
        initialSource="GFS"
        station={{ station_id: 'qhh_forc_002', station_name: 'North Ridge station' }}
        active
      />,
    )

    expect(screen.getByText('North Ridge station')).toBeInTheDocument()
    await waitFor(() => expect(panelPosition(panel)).toEqual(dragged))
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

  // Epic #992 SUB-6 §3.1 (P4) — the frontend resolves the live `model_id`
  // from the currently selected latest product at request time
  // (`stationSeriesProductIdentity` uses `product.model_id`). After a
  // cutover, a live (latest-product) request carries the newly active
  // `model_id`, not the pre-cutover one.
  it('carries the newly active model_id in the live station-series request after a cutover', async () => {
    vi.mocked(fetchHydroMetLatestProduct).mockImplementation(async (request) => {
      const cycle = request.cycle ?? DEFAULT_CYCLE
      return product({
        source_id: request.source,
        cycle_time: cycle,
        run_id: `run-${request.source.toLowerCase()}-latest`,
        forcing_version_id: forcingVersionFor(request.source, cycle),
        available_issue_times: [DEFAULT_CYCLE],
        model_id: 'm-new-post-cutover',
      })
    })
    mockSeriesBySource()

    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)
    await screen.findByTestId('m11-station-popup-loaded')

    const queries = vi.mocked(client.GET).mock.calls.map(([, init]) => getSeriesQuery(init))
    expect(queries).toHaveLength(2)
    expect(queries.map((query) => query.source_id).sort()).toEqual(['GFS', 'IFS'])
    for (const query of queries) {
      // The live request carries the newly active model_id sourced from
      // fetchHydroMetLatestProduct at request time.
      expect(query.model_id).toBe('m-new-post-cutover')
      // Negative: the pre-cutover default (`m-1` from `product()`) MUST
      // NOT leak — a cached / captured pin would fail this check.
      expect(query.model_id).not.toBe('m-1')
    }
  })

  // Epic #992 SUB-6 §3.1 (P5) — the popup does not reuse a pre-cutover
  // `model_id` across two live station-series requests when the latest
  // product changes (there is no client-side cache; each loadSource
  // executes a fresh `fetchHydroMetLatestProduct`).
  it('does not reuse a pre-cutover model_id across two live station-series requests when the latest product changes', async () => {
    const user = userEvent.setup()
    const cycles = [DEFAULT_CYCLE, RETAINED_OUT_CYCLE]
    vi.mocked(fetchHydroMetLatestProduct).mockImplementation(async (request) => {
      const cycle = request.cycle ?? DEFAULT_CYCLE
      // Simulate a cutover across the two cycles: DEFAULT_CYCLE resolves
      // to the pre-cutover model, RETAINED_OUT_CYCLE resolves to the
      // post-cutover model. The point is that the popup requests each
      // model_id fresh from the latest product per loadSource cycle.
      const modelId = cycle === RETAINED_OUT_CYCLE ? 'm-new' : 'm-old'
      return product({
        source_id: request.source,
        cycle_time: cycle,
        run_id: `run-${request.source.toLowerCase()}-${cycle === RETAINED_OUT_CYCLE ? 'new' : 'old'}`,
        forcing_version_id: forcingVersionFor(request.source, cycle),
        available_issue_times: cycles,
        model_id: modelId,
      })
    })
    mockSeriesBySource()

    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)
    await screen.findByTestId('m11-station-popup-loaded')

    const preFlipQueries = vi.mocked(client.GET).mock.calls.map(([, init]) => getSeriesQuery(init))
    expect(preFlipQueries).toHaveLength(2)
    for (const query of preFlipQueries) {
      expect(query.model_id).toBe('m-old')
      expect(query.cycle_time).toBe(DEFAULT_CYCLE)
    }

    vi.mocked(client.GET).mockClear()
    await user.click(screen.getByTestId('m11-popup-issue-time'))
    await user.click(screen.getByRole('option', { name: formatIssueTime(RETAINED_OUT_CYCLE) }))

    await waitFor(() => expect(client.GET).toHaveBeenCalledTimes(2))
    const postFlipQueries = vi.mocked(client.GET).mock.calls.map(([, init]) => getSeriesQuery(init))
    for (const query of postFlipQueries) {
      // The second loadSource cycle carries the NEW model_id — the
      // popup did not reuse the pre-cutover `m-old` value from the
      // earlier request.
      expect(query.model_id).toBe('m-new')
      expect(query.cycle_time).toBe(RETAINED_OUT_CYCLE)
      expect(query.model_id).not.toBe('m-old')
    }
  })

  // Epic #992 SUB-3 §2.1 (T1) — post-cutover new-pin-on-old-cycle: with BOTH
  // GFS and IFS returning STATION_FORCING_FILE_NOT_FOUND (SUB-4 desensitized
  // shape, details = {station_id} only) for a pre-cutover cycle, the popup
  // renders the retention-specific empty state (`retainedDiskMissMessage`),
  // draws NO chart, raises NO generic chart-failure message, and issues NO
  // additional client.GET call beyond the 2 series requests for the selected
  // cycle — proving absence of any DB/archive/fallback endpoint hit.
  it('renders retention empty state and adds no fallback client.GET when both GFS+IFS miss on a pre-cutover cycle', async () => {
    const user = userEvent.setup()
    const cycles = [DEFAULT_CYCLE, RETAINED_OUT_CYCLE]
    mockLatestProducts(cycles)
    vi.mocked(client.GET).mockImplementation(async (_path, init) => {
      const query = getSeriesQuery(init)
      const source = sourceFromQuery(query)
      if (query.cycle_time === RETAINED_OUT_CYCLE) {
        // SUB-4 desensitized shape: details = { station_id } only (no
        // expected_path, no (basin_version_id, model_id, source_id, cycle_time)
        // tuple).
        return {
          data: undefined,
          error: {
            error: {
              code: 'STATION_FORCING_FILE_NOT_FOUND',
              message: 'Station forcing file not found.',
              details: { station_id: station.station_id },
            },
          },
        } as never
      }
      return { data: success(seriesResponseFor(source, seriesOverridesFromQuery(query))), error: undefined } as never
    })

    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)
    // Initial mount at DEFAULT_CYCLE succeeds — chart renders.
    await screen.findByTestId('m11-station-variable-PRCP-chart')

    // Clear counters so we can assert exactly the retention-cycle refresh
    // client.GET count (proving no fallback endpoint fires).
    vi.mocked(client.GET).mockClear()

    await user.click(screen.getByTestId('m11-popup-issue-time'))
    await user.click(screen.getByRole('option', { name: formatIssueTime(RETAINED_OUT_CYCLE) }))

    const empty = await screen.findByTestId('m11-station-popup-empty')
    const cycleLabel = formatIssueTime(RETAINED_OUT_CYCLE)
    // Retention-specific empty-state text (retainedDiskMissMessage shape) for
    // BOTH sources — proves the retention branch was taken.
    expect(empty).toHaveTextContent(`GFS：该起报 ${cycleLabel} 的 station-series 已不在当前磁盘保留窗口内`)
    expect(empty).toHaveTextContent(`IFS：该起报 ${cycleLabel} 的 station-series 已不在当前磁盘保留窗口内`)
    // Negative: the generic chart-failure fallback path
    // (`${source}：${formatHydroMetStationSeriesMessage(error, 'station-series 不可用')}`)
    // MUST NOT be taken.
    expect(empty).not.toHaveTextContent('station-series 不可用')

    // No chart drawn — no synthetic points, no partial-chart fallback.
    for (const variable of HYDRO_MET_STATION_VARIABLES) {
      expect(screen.queryByTestId(`m11-station-variable-${variable}-chart`)).not.toBeInTheDocument()
    }
    expect(screen.queryByTestId('mock-station-echarts')).not.toBeInTheDocument()

    // No fallback endpoint hit: exactly the 2 series requests for the
    // retention cycle (GFS + IFS). Any DB/archive/history/synthetic-points
    // fallback would show up as extra client.GET calls here.
    await waitFor(() => expect(client.GET).toHaveBeenCalledTimes(2))
    // Endpoint-path allow-list: a same-count mutation that swaps one series
    // request for a DB/archive fallback (e.g.
    // `/api/v1/history/station-archive`) would keep count=2 but hit a
    // different path. Assert every call targets the station-series endpoint.
    const paths = vi.mocked(client.GET).mock.calls.map(([path]) => path)
    expect(paths).toEqual([
      '/api/v1/met/stations/{station_id}/series',
      '/api/v1/met/stations/{station_id}/series',
    ])
    const queries = vi.mocked(client.GET).mock.calls.map(([, init]) => getSeriesQuery(init))
    expect(queries.map((query) => query.source_id).sort()).toEqual(['GFS', 'IFS'])
    for (const query of queries) {
      expect(query.cycle_time).toBe(RETAINED_OUT_CYCLE)
    }
  })

  // Epic #992 SUB-3 §2.1 (T2, picker-catalog-only mechanical property) —
  // M11IssueTimeSelect (M11PopupChrome.tsx:97-127) offers only cycles from the
  // issueTimes prop derived from product.available_issue_times. A pre-cutover
  // cycle NOT in available_issue_times MUST NOT be added to the picker options
  // by any synthesis path.
  it('picker offers only catalog-provided cycles; never synthesizes a pre-cutover option', async () => {
    const user = userEvent.setup()
    // Catalog offers ONLY DEFAULT_CYCLE — no pre-cutover option.
    mockLatestProducts([DEFAULT_CYCLE])
    mockSeriesBySource()

    render(<M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />)
    await screen.findByTestId('m11-station-popup-loaded')

    await user.click(screen.getByTestId('m11-popup-issue-time'))
    await screen.findByTestId('m11-popup-issue-time-content')

    const options = screen.getAllByRole('option')
    expect(options).toHaveLength(1)
    expect(options[0]).toHaveTextContent(formatIssueTime(DEFAULT_CYCLE))
    // Negative: the picker MUST NOT invent a pre-cutover option outside the
    // catalog. RETAINED_OUT_CYCLE was NOT in `available_issue_times` and MUST
    // NOT appear anywhere in the option list (as either a normal or a
    // retained-out marking).
    expect(
      screen.queryByRole('option', { name: new RegExp(formatIssueTime(RETAINED_OUT_CYCLE)) }),
    ).not.toBeInTheDocument()
    // Negative: no option carries the picker's retention-unavailable marker
    // (which would only render if a synthesis path had added one).
    expect(screen.queryByRole('option', { name: /磁盘保留不可用/ })).not.toBeInTheDocument()
  })

  // Epic #992 SUB-3 §2.1 (T3, per-session no-persistence mechanical property)
  // — after clicking a retained-out cycle and observing the retention warning,
  // unmount + remount the popup for the SAME station; the retained-out marking
  // MUST NOT persist. The retention path writes to NEITHER localStorage NOR
  // sessionStorage — the marking is per-cycle, per-session popup state only.
  it('does not persist the retained-out marking across unmount/remount and writes no storage on the retention path', async () => {
    const user = userEvent.setup()
    const cycles = [DEFAULT_CYCLE, RETAINED_OUT_CYCLE]
    mockLatestProducts(cycles)
    vi.mocked(client.GET).mockImplementation(async (_path, init) => {
      const query = getSeriesQuery(init)
      const source = sourceFromQuery(query)
      if (query.cycle_time === RETAINED_OUT_CYCLE) {
        return {
          data: undefined,
          error: {
            error: {
              code: 'STATION_FORCING_FILE_NOT_FOUND',
              message: 'Station forcing file not found.',
              details: { station_id: station.station_id },
            },
          },
        } as never
      }
      return { data: success(seriesResponseFor(source, seriesOverridesFromQuery(query))), error: undefined } as never
    })

    // Storage.prototype spy is required: instance-level `vi.spyOn(window.sessionStorage, 'setItem')`
    // does not intercept in the jsdom/happy-dom env (the method lives on the native
    // Storage prototype). A single prototype spy covers both localStorage and
    // sessionStorage since they share the prototype.
    const storageSetItem = vi.spyOn(Storage.prototype, 'setItem')
    try {
      const { unmount } = render(
        <M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />,
      )
      // Settle initial chart render (drains any userEvent/Radix internal
      // setItem noise from the mount phase) before establishing the delta
      // baseline for the retention click.
      await screen.findByTestId('m11-station-variable-PRCP-chart')

      // Delta baseline: reset the setItem call log so any subsequent call
      // MUST come from the retention path itself. A persistence mutation
      // using a generic cache key (e.g. `nhms-popup-cache-v3`) that would
      // evade a keyword filter is caught by this bare-count assertion.
      storageSetItem.mockClear()

      await user.click(screen.getByTestId('m11-popup-issue-time'))
      await user.click(screen.getByRole('option', { name: formatIssueTime(RETAINED_OUT_CYCLE) }))

      // Retention warning observed for the retained-out cycle.
      const empty = await screen.findByTestId('m11-station-popup-empty')
      expect(empty).toHaveTextContent('磁盘保留窗口内')

      // Bare delta assertion: the retention path itself writes NO storage
      // (any key — retention-related or otherwise).
      expect(storageSetItem).not.toHaveBeenCalled()

      // Unmount and remount for the SAME station: the retained-out marking
      // must NOT carry over. The fresh mount defaults to the catalog latest
      // cycle (DEFAULT_CYCLE) and does NOT render the retention empty state.
      unmount()

      // Second delta baseline: any remount-triggered setItem must come from
      // fresh mount logic, not carry state through storage — bare-count
      // assertion catches persistence via any key.
      storageSetItem.mockClear()

      render(
        <M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={station} />,
      )
      await screen.findByTestId('m11-station-variable-PRCP-chart')
      // Fresh mount: no retention empty state lingering.
      expect(screen.queryByTestId('m11-station-popup-empty')).not.toBeInTheDocument()
      // Picker defaults to DEFAULT_CYCLE (catalog latest), not the previously
      // clicked retained-out cycle.
      expect(screen.getByTestId('m11-popup-issue-time')).toHaveTextContent(formatIssueTime(DEFAULT_CYCLE))

      // Post-remount, no storage was written at all — the retention marking
      // truly does not persist via ANY key.
      expect(storageSetItem).not.toHaveBeenCalled()
    } finally {
      storageSetItem.mockRestore()
    }
  })
})

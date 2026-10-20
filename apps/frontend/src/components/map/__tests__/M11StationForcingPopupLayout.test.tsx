import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { M11StationForcingPopup } from '@/components/map/M11StationForcingPopup'
import { HYDRO_MET_STATION_SERIES_API_TUPLE_LIMIT, HYDRO_MET_STATION_VARIABLES } from '@/lib/hydroMet/stationSeries'
import { fetchHydroMetLatestProduct } from '@/stores/hydroMetBootstrap'

import {
  DEFAULT_CYCLE,
  RETAINED_OUT_CYCLE,
  station,
  forcingVersionFor,
  getSeriesQuery,
  apiGetCalls,
  sourceFromQuery,
  mockLatestProducts,
  mockSeriesBySource,
  originalViewport,
  setViewport,
  panelPosition,
  positionedPanel,
  expectRectInside,
  mockCurveWindowRects,
} from './m11StationForcingPopupFixtures'

vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

vi.mock('@/stores/hydroMetBootstrap', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/stores/hydroMetBootstrap')>()
  return { ...actual, fetchHydroMetLatestProduct: vi.fn() }
})

vi.mock('echarts-for-react/lib/core', () => ({
  default: ({ option }: { option: unknown }) => <pre data-testid="mock-station-echarts">{JSON.stringify(option)}</pre>,
}))
vi.mock('@/components/charts/echartsCore', () => ({ echarts: {} }))

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
    expect(screen.queryByRole('button', { name: /GFS|IFS/ })).not.toBeInTheDocument()

    expect(fetchHydroMetLatestProduct).toHaveBeenCalledTimes(2)
    expect(fetchHydroMetLatestProduct).toHaveBeenCalledWith(expect.objectContaining({ source: 'GFS', basinId: 'basins_qhh' }))
    expect(fetchHydroMetLatestProduct).toHaveBeenCalledWith(expect.objectContaining({ source: 'IFS', basinId: 'basins_qhh' }))

    const queries = apiGetCalls().map(([, init]) => getSeriesQuery(init))
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
})

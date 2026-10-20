import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { formatIssueTime } from '@/components/map/M11PopupChrome'
import { M11StationForcingPopup } from '@/components/map/M11StationForcingPopup'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import { fetchHydroMetLatestProduct } from '@/stores/hydroMetBootstrap'

import {
  DEFAULT_CYCLE,
  RETAINED_OUT_CYCLE,
  station,
  success,
  forcingVersionFor,
  productFor,
  seriesOverridesFromQuery,
  seriesResponseFor,
  getSeriesQuery,
  apiGetCalls,
  sourceFromQuery,
  mockLatestProducts,
  mockSeriesBySource,
  stationSeriesDiskMiss,
  originalViewport,
  setViewport,
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
    const retainedCycleQueries = apiGetCalls()
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
    expect(screen.getByTestId('m11-popup-issue-time')).toHaveAttribute('title', formatIssueTime(DEFAULT_CYCLE)) // 普通时次：title 即普通标签

    await user.click(screen.getByTestId('m11-popup-issue-time'))
    await user.click(screen.getByRole('option', { name: formatIssueTime(RETAINED_OUT_CYCLE) }))

    const empty = await screen.findByTestId('m11-station-popup-empty')
    expect(empty).toHaveTextContent(/起报.*已不可用/)
    expect(screen.getByTestId('m11-popup-issue-time')).toHaveTextContent(formatIssueTime(RETAINED_OUT_CYCLE))
    // 触发器里的长标签会被截断，完整标签留在 title 上。
    expect(screen.getByTestId('m11-popup-issue-time')).toHaveAttribute('title', `${formatIssueTime(RETAINED_OUT_CYCLE)} · 磁盘保留不可用`)

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

    const queries = apiGetCalls().map(([, init]) => getSeriesQuery(init))
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

    const queries = apiGetCalls().map(([, init]) => getSeriesQuery(init))
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
})

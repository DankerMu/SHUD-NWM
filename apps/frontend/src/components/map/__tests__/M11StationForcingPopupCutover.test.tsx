import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { formatIssueTime } from '@/components/map/M11PopupChrome'
import { M11StationForcingPopup } from '@/components/map/M11StationForcingPopup'
import { HYDRO_MET_STATION_VARIABLES } from '@/lib/hydroMet/stationSeries'
import { fetchHydroMetLatestProduct } from '@/stores/hydroMetBootstrap'

import {
  DEFAULT_CYCLE,
  RETAINED_OUT_CYCLE,
  station,
  success,
  forcingVersionFor,
  product,
  seriesOverridesFromQuery,
  seriesResponseFor,
  getSeriesQuery,
  apiGetCalls,
  sourceFromQuery,
  mockLatestProducts,
  mockSeriesBySource,
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

    const queries = apiGetCalls().map(([, init]) => getSeriesQuery(init))
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

    const preFlipQueries = apiGetCalls().map(([, init]) => getSeriesQuery(init))
    expect(preFlipQueries).toHaveLength(2)
    for (const query of preFlipQueries) {
      expect(query.model_id).toBe('m-old')
      expect(query.cycle_time).toBe(DEFAULT_CYCLE)
    }

    vi.mocked(client.GET).mockClear()
    await user.click(screen.getByTestId('m11-popup-issue-time'))
    await user.click(screen.getByRole('option', { name: formatIssueTime(RETAINED_OUT_CYCLE) }))

    await waitFor(() => expect(client.GET).toHaveBeenCalledTimes(2))
    const postFlipQueries = apiGetCalls().map(([, init]) => getSeriesQuery(init))
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
    const paths = apiGetCalls().map(([path]) => path)
    expect(paths).toEqual([
      '/api/v1/met/stations/{station_id}/series',
      '/api/v1/met/stations/{station_id}/series',
    ])
    const queries = apiGetCalls().map(([, init]) => getSeriesQuery(init))
    expect(queries.map((query) => query.source_id).sort()).toEqual(['GFS', 'IFS'])
    for (const query of queries) {
      expect(query.cycle_time).toBe(RETAINED_OUT_CYCLE)
    }
  })

  // Epic #992 SUB-3 §2.1 (T2, picker-catalog-only mechanical property) —
  // M11IssueTimeSelect offers the cycles from the issueTimes prop derived from
  // product.available_issue_times, plus at most the currently selected cycle
  // when it is absent from that prop (its `retainedIssueTime`). A pre-cutover
  // cycle that is neither in available_issue_times nor selected MUST NOT be
  // added to the picker options by any synthesis path.
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

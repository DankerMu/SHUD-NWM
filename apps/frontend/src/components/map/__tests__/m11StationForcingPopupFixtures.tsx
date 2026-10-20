import { screen, waitFor } from '@testing-library/react'
import { expect, vi } from 'vitest'

import { client } from '@/api/client'
import type { M11StationPopupStation } from '@/components/map/M11StationForcingPopup'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import { HYDRO_MET_STATION_SERIES_API_TUPLE_LIMIT, HYDRO_MET_STATION_VARIABLES } from '@/lib/hydroMet/stationSeries'
import { fetchHydroMetLatestProduct, type QhhLatestProduct } from '@/stores/hydroMetBootstrap'

export const DEFAULT_CYCLE = '2026-05-21T00:00:00Z'
export const RETAINED_OUT_CYCLE = '2026-05-20T00:00:00Z'

export const station: M11StationPopupStation = { station_id: 'qhh_forc_001', station_name: 'QHH forcing 001' }

export type SeriesQuery = {
  forcing_version_id?: unknown
  model_id?: unknown
  source_id?: unknown
  cycle_time?: unknown
  variables?: unknown
  limit?: unknown
}

export function success<T>(data: T) {
  return { status: 'success', data }
}

export function forcingVersionFor(source: HydroMetSource, cycle = DEFAULT_CYCLE) {
  const suffix = cycle === RETAINED_OUT_CYCLE ? 'old' : 'latest'
  return `forc-${source.toLowerCase()}-${suffix}`
}

export function product(overrides: Partial<QhhLatestProduct> = {}): QhhLatestProduct {
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

export function productFor(source: HydroMetSource, cycle = DEFAULT_CYCLE, issueTimes = [DEFAULT_CYCLE]) {
  return product({
    source_id: source,
    cycle_time: cycle,
    run_id: `run-${source.toLowerCase()}-${cycle === RETAINED_OUT_CYCLE ? 'old' : 'latest'}`,
    forcing_version_id: forcingVersionFor(source, cycle),
    available_issue_times: issueTimes,
  })
}

export function metadata(overrides: Record<string, unknown> = {}) {
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

export function seriesOverridesFromQuery(query: SeriesQuery) {
  const overrides: Record<string, unknown> = {}
  if (typeof query.cycle_time === 'string') overrides.cycle_time = query.cycle_time
  if (typeof query.forcing_version_id === 'string') overrides.forcing_version_id = query.forcing_version_id
  if (typeof query.model_id === 'string') overrides.model_id = query.model_id
  return overrides
}

export function seriesResponseFor(source: HydroMetSource, overrides: Record<string, unknown> = {}) {
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

export function getSeriesQuery(init: unknown) {
  return (init as { params: { query: SeriesQuery } }).params.query
}

// `client.GET` 是 openapi-fetch 的泛型重载函数，`vi.mocked(...).mock.calls` 因此推断成 `never[]`，
// 无法直接解构。按调用实参的运行时形状（[path, init]）读成 `unknown[][]` 后交给下游的
// `unknown` 入参守卫处理。
export function apiGetCalls(): unknown[][] {
  return vi.mocked(client.GET).mock.calls
}

export function sourceFromQuery(query: SeriesQuery): HydroMetSource {
  return query.source_id === 'IFS' ? 'IFS' : 'GFS'
}

export function mockLatestProducts(issueTimes = [DEFAULT_CYCLE]) {
  vi.mocked(fetchHydroMetLatestProduct).mockImplementation(async (request) => {
    const cycle = request.cycle ?? issueTimes[0] ?? DEFAULT_CYCLE
    return productFor(request.source, cycle, issueTimes)
  })
}

export function mockSeriesBySource(
  builder: (source: HydroMetSource, query: SeriesQuery) => Record<string, unknown> = (source, query) =>
    seriesResponseFor(source, seriesOverridesFromQuery(query)),
) {
  vi.mocked(client.GET).mockImplementation(async (_path, init) => {
    const query = getSeriesQuery(init)
    const source = sourceFromQuery(query)
    return { data: success(builder(source, query)), error: undefined } as never
  })
}

export function stationSeriesDiskMiss() {
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

export function prcpSeries(body: Record<string, unknown>) {
  return (body.series as Record<string, unknown>[]).find((series) => series.variable === 'PRCP') as Record<string, unknown>
}

export const originalViewport = { width: window.innerWidth, height: window.innerHeight }

export function setViewport(width: number, height: number) {
  Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: width })
  Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: height })
  window.dispatchEvent(new Event('resize'))
}

export function panelPosition(panel: HTMLElement) {
  return {
    x: Number.parseFloat(panel.style.left),
    y: Number.parseFloat(panel.style.top),
  }
}

export async function positionedPanel(testId: string) {
  const panel = await screen.findByTestId(testId)
  await waitFor(() => expect(Number.isFinite(panelPosition(panel).x)).toBe(true))
  return panel
}

export function rect(left: number, top: number, width: number, height: number): DOMRect {
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

export function expectRectInside(container: DOMRect, target: DOMRect) {
  expect(target.left).toBeGreaterThanOrEqual(container.left)
  expect(target.top).toBeGreaterThanOrEqual(container.top)
  expect(target.right).toBeLessThanOrEqual(container.right)
  expect(target.bottom).toBeLessThanOrEqual(container.bottom)
}

export function mockCurveWindowRects({
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

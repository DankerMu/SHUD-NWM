import { render, screen } from '@testing-library/react'
import type { EChartsInstance } from 'echarts-for-react/lib/types'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { M11StationForcingPopup } from '@/components/map/M11StationForcingPopup'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import { HYDRO_MET_STATION_SERIES_API_TUPLE_LIMIT, HYDRO_MET_STATION_VARIABLES } from '@/lib/hydroMet/stationSeries'
import { fetchHydroMetLatestProduct, type QhhLatestProduct } from '@/pages/hydroMet/bootstrap'

/**
 * 气象代站图表的“画布跟随容器”接线（openspec mobile-responsive-display task 4.5 用例 (i)）。
 * 只钉接线：图表把 `onChartReady` 交给封装库，就绪后观察实例的 dom、尺寸不一致时重排、卸载时断开。
 * 观察器的其余行为由 `ForecastChartFillResize.test.tsx` 经同一个 hook 钉住。
 */

const { coreProps } = vi.hoisted(() => ({ coreProps: vi.fn() }))

vi.mock('@/api/client', () => ({
  client: { GET: vi.fn() },
}))

vi.mock('@/pages/hydroMet/bootstrap', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/hydroMet/bootstrap')>()
  return { ...actual, fetchHydroMetLatestProduct: vi.fn() }
})

vi.mock('@/components/charts/echartsCore', () => ({ echarts: {} }))

vi.mock('echarts-for-react/lib/core', () => ({
  default: (props: { onChartReady?: (instance: EChartsInstance) => void }) => {
    coreProps(props)
    return <div data-testid="mock-station-echarts" />
  },
}))

/** 可控的 ResizeObserver：回调由测试同步触发。 */
class ControllableResizeObserver implements ResizeObserver {
  static instances: ControllableResizeObserver[] = []

  readonly observe = vi.fn<(target: Element) => void>()
  readonly unobserve = vi.fn<(target: Element) => void>()
  readonly disconnect = vi.fn()

  constructor(private readonly callback: ResizeObserverCallback) {
    ControllableResizeObserver.instances.push(this)
  }

  trigger() {
    this.callback([], this)
  }
}

const CYCLE = '2026-05-21T00:00:00Z'
const STATION_ID = 'qhh_forc_001'

function productFor(source: HydroMetSource): QhhLatestProduct {
  return {
    basin_id: 'basins_qhh',
    model_id: 'm-1',
    basin_version_id: 'bv-1',
    river_network_version_id: 'rn-1',
    source_id: source,
    cycle_time: CYCLE,
    run_id: `run-${source.toLowerCase()}`,
    forcing_version_id: `forc-${source.toLowerCase()}`,
    available_issue_times: [CYCLE],
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
    availability: { ready: true, unavailable_reasons: [], quality_flags: [], quality_notes: [] },
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
  } as QhhLatestProduct
}

function seriesResponseFor(source: HydroMetSource) {
  return {
    station_id: STATION_ID,
    station: { station_id: STATION_ID, basin_version_id: 'bv-1' },
    forcing_version_id: `forc-${source.toLowerCase()}`,
    model_id: 'm-1',
    source_id: source,
    cycle_time: CYCLE,
    valid_time_start: '2026-05-21T06:00:00Z',
    valid_time_end: '2026-05-21T12:00:00Z',
    limit: HYDRO_MET_STATION_SERIES_API_TUPLE_LIMIT,
    series: HYDRO_MET_STATION_VARIABLES.map((variable) => ({
      variable,
      unit: 'mm',
      source_id: source,
      cycle_time: CYCLE,
      truncated: false,
      metadata: {
        limit: HYDRO_MET_STATION_SERIES_API_TUPLE_LIMIT,
        returned_points: 2,
        requested_from: '2026-05-21T00:00:00Z',
        requested_to: '2026-05-22T00:00:00Z',
        returned_from: '2026-05-21T06:00:00Z',
        returned_to: '2026-05-21T12:00:00Z',
        truncated: false,
      },
      points: [
        { valid_time: '2026-05-21T06:00:00Z', value: 1.2, quality_flag: 'ok' },
        { valid_time: '2026-05-21T12:00:00Z', value: 2.4, quality_flag: 'ok' },
      ],
    })),
  }
}

/** 假 ECharts 实例：容器 640×360，实例（画布）320×180——两者不一致。 */
function mismatchedChart() {
  const dom = document.createElement('div')
  Object.defineProperty(dom, 'clientWidth', { configurable: true, get: () => 640 })
  Object.defineProperty(dom, 'clientHeight', { configurable: true, get: () => 360 })
  const resize = vi.fn()
  const instance = {
    getDom: () => dom,
    getWidth: () => 320,
    getHeight: () => 180,
    isDisposed: () => false,
    resize,
  } as unknown as EChartsInstance
  return { dom, instance, resize }
}

describe('M11StationForcingPopup chart container following', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ControllableResizeObserver.instances = []
    vi.stubGlobal('ResizeObserver', ControllableResizeObserver)
    vi.mocked(fetchHydroMetLatestProduct).mockImplementation(async (request) => productFor(request.source))
    vi.mocked(client.GET).mockImplementation(async (_path, init) => {
      const query = (init as { params: { query: { source_id?: unknown } } }).params.query
      const source: HydroMetSource = query.source_id === 'IFS' ? 'IFS' : 'GFS'
      return { data: { status: 'success', data: seriesResponseFor(source) }, error: undefined } as never
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('(i) hands onChartReady to the chart wrapper, follows the chart element and disconnects on unmount', async () => {
    const { unmount } = render(
      <M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={{ station_id: STATION_ID, station_name: 'QHH forcing 001' }} />,
    )
    await screen.findByTestId('m11-station-popup-loaded')
    expect(screen.getByTestId('m11-station-panel-chart')).toContainElement(screen.getByTestId('mock-station-echarts'))

    const props = coreProps.mock.lastCall?.[0] as { onChartReady?: (instance: EChartsInstance) => void } | undefined
    expect(props, 'the chart wrapper should have been rendered').toBeDefined()
    const onChartReady = props?.onChartReady
    expect(onChartReady, 'the station chart should pass onChartReady').toBeTypeOf('function')
    expect(ControllableResizeObserver.instances).toHaveLength(0)

    const chart = mismatchedChart()
    onChartReady?.(chart.instance)
    expect(ControllableResizeObserver.instances).toHaveLength(1)
    const observer = ControllableResizeObserver.instances[0]
    expect(observer.observe).toHaveBeenCalledTimes(1)
    expect(observer.observe).toHaveBeenCalledWith(chart.dom)
    expect(chart.resize).not.toHaveBeenCalled()

    observer.trigger()
    expect(chart.resize).toHaveBeenCalledTimes(1)
    expect(chart.resize).toHaveBeenCalledWith({ width: 'auto', height: 'auto' })
    expect(observer.disconnect).not.toHaveBeenCalled()

    unmount()
    expect(observer.disconnect).toHaveBeenCalled()
  })
})

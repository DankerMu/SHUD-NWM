import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { M11StationForcingPopup } from '@/components/map/M11StationForcingPopup'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import { HYDRO_MET_STATION_SERIES_API_TUPLE_LIMIT } from '@/lib/hydroMet/stationSeries'
import { fetchHydroMetLatestProduct, type QhhLatestProduct } from '@/pages/hydroMet/bootstrap'
import { installMobileFormMatchMedia } from '@/test/mobileFormMatchMedia'

/**
 * 气象代站曲线的触屏缩放配置与图表区只读属性（openspec mobile-responsive-display task 4.8，design.md D12）。
 * 用例 (j)–(m) 对应 tasks.md 里 #2809 的 Triage。图表库封装被替换成记录 props 的桩：配置看面板交出去的
 * `option`，事件经它收到的 `onEvents` 以封装库的调用方式 `(事件参数, 实例)` 驱动。真实手势由 e2e 断言。
 */

vi.mock('@/api/client', () => ({ client: { GET: vi.fn() } }))

vi.mock('@/pages/hydroMet/bootstrap', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/hydroMet/bootstrap')>()
  return { ...actual, fetchHydroMetLatestProduct: vi.fn() }
})

const { coreProps, coreMounts } = vi.hoisted(() => ({ coreProps: vi.fn(), coreMounts: { count: 0 } }))

vi.mock('@/components/charts/echartsCore', () => ({ echarts: {} }))

vi.mock('echarts-for-react/lib/core', async () => {
  const { useEffect } = await import('react')
  return {
    default: (props: Record<string, unknown>) => {
      coreProps(props)
      // 挂载次数 = 图表实例被创建的次数。
      useEffect(() => {
        coreMounts.count += 1
      }, [])
      return <div data-testid="mock-station-echarts" />
    },
  }
})

/** 改动前的 inside dataZoom 字面值（抄自改动前的源码，不读产品常量）。 */
const DESKTOP_DATA_ZOOM = [{ type: 'inside', xAxisIndex: 0, filterMode: 'none' }]
const CHART_TEST_ID = 'm11-station-panel-chart'
const CYCLE = '2026-05-21T00:00:00Z'
const STATION_ID = 'qhh_forc_001'
/** 响应里带的要素：两个源都没有 `Rn`，切到它即进入“无可绘制序列”的空态。 */
const SERVED_VARIABLES = ['PRCP', 'TEMP', 'RH', 'wind'] as const
const MISSING_VARIABLE = 'Rn'

type ChartEventHandler = (event: unknown, instance: unknown) => void
interface CapturedCoreProps {
  option: { dataZoom?: unknown; series: Array<{ data: unknown }> }
  onEvents?: Record<string, ChartEventHandler>
}

function capturedProps(): CapturedCoreProps {
  const props = coreProps.mock.lastCall?.[0] as CapturedCoreProps | undefined
  if (!props) throw new Error('echarts core was not rendered')
  return props
}

function chartEvents() {
  const events = capturedProps().onEvents
  if (!events) throw new Error('the station chart did not hand onEvents to the chart wrapper')
  return events
}

/** 假图表实例：缩放窗口由测试直接改写，`getOption` 每次读当前值。 */
function fakeChart(zoomWindow: { start: number; end: number }) {
  return {
    window: zoomWindow,
    instance: {
      getOption: () => ({ dataZoom: [{ ...zoomWindow }] }),
      isDisposed: () => false,
    },
  }
}

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

/** 每个要素的数值不同：切换要素后交给图表的序列数据必然不同。 */
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
    series: SERVED_VARIABLES.map((variable, index) => ({
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
        { valid_time: '2026-05-21T06:00:00Z', value: 1.2 + index * 10, quality_flag: 'ok' },
        { valid_time: '2026-05-21T12:00:00Z', value: 2.4 + index * 10, quality_flag: 'ok' },
      ],
    })),
  }
}

async function renderLoadedPopup() {
  const view = render(
    <M11StationForcingPopup basinId="basins_qhh" initialSource="GFS" station={{ station_id: STATION_ID, station_name: 'QHH forcing 001' }} />,
  )
  const chart = await screen.findByTestId(CHART_TEST_ID)
  return { ...view, chart }
}

function selectVariable(variable: string) {
  fireEvent.click(screen.getByTestId(`m11-station-variable-toggle-${variable}`))
}

let form: ReturnType<typeof installMobileFormMatchMedia> | undefined

beforeEach(() => {
  vi.clearAllMocks()
  coreMounts.count = 0
  vi.mocked(fetchHydroMetLatestProduct).mockImplementation(async (request) => productFor(request.source))
  vi.mocked(client.GET).mockImplementation(async (_path, init) => {
    const query = (init as { params: { query: { source_id?: unknown } } }).params.query
    const source: HydroMetSource = query.source_id === 'IFS' ? 'IFS' : 'GFS'
    return { data: { status: 'success', data: seriesResponseFor(source) }, error: undefined } as never
  })
})

afterEach(() => {
  form?.restore()
  form = undefined
})

describe('M11StationForcingPopup 的触屏缩放', () => {
  it('(j) 桌面形态：dataZoom 与改动前的字面值深相等，属性为初值', async () => {
    const { chart } = await renderLoadedPopup()

    expect(capturedProps().option.dataZoom).toStrictEqual(DESKTOP_DATA_ZOOM)
    expect(chart).toHaveAttribute('data-zoom-start', '0')
    expect(chart).toHaveAttribute('data-zoom-end', '100')
    expect(chart).toHaveAttribute('data-tooltip-visible', 'false')
    expect(chart).toContainElement(screen.getByTestId('mock-station-echarts'))
  })

  for (const landscape of [false, true]) {
    it(`(j) 移动形态${landscape ? '（矮视口横屏）' : ''}：与桌面字面值只差 preventDefaultMouseMove: false 一个键`, async () => {
      form = installMobileFormMatchMedia(true, landscape)
      const { chart } = await renderLoadedPopup()

      expect(capturedProps().option.dataZoom).toStrictEqual([{ ...DESKTOP_DATA_ZOOM[0], preventDefaultMouseMove: false }])
      expect(chart).toHaveAttribute('data-zoom-start', '0')
      expect(chart).toHaveAttribute('data-zoom-end', '100')
      expect(chart).toHaveAttribute('data-tooltip-visible', 'false')
    })
  }

  it('(j) 竖屏 ↔ 矮视口横屏不换图表配置（旋转不重置缩放），移动 → 桌面才换回桌面字面值', async () => {
    form = installMobileFormMatchMedia(true, false)
    await renderLoadedPopup()
    const portraitOption = capturedProps().option

    act(() => form!.setForm({ mobile: true, landscape: true }))
    expect(capturedProps().option).toBe(portraitOption)

    act(() => form!.setForm({ mobile: true, landscape: false }))
    expect(capturedProps().option).toBe(portraitOption)

    act(() => form!.setForm({ mobile: false, landscape: false }))
    expect(capturedProps().option).not.toBe(portraitOption)
    expect(capturedProps().option.dataZoom).toStrictEqual(DESKTOP_DATA_ZOOM)
  })

  it('(k) 缩放事件 → 属性取实例当前窗口（不信事件载荷）；切换要素（配置重设）后回到 0 / 100；onEvents 引用稳定', async () => {
    const { chart } = await renderLoadedPopup()
    const events = chartEvents()
    const fake = fakeChart({ start: 20, end: 60 })

    // 事件载荷故意与实例不一致：属性必须跟实例走。
    act(() => events.datazoom({ start: 1, end: 2 }, fake.instance))
    expect(chart).toHaveAttribute('data-zoom-start', '20')
    expect(chart).toHaveAttribute('data-zoom-end', '60')

    fake.window.start = 35.5
    fake.window.end = 75.5
    act(() => chartEvents().datazoom({}, fake.instance))
    expect(chart).toHaveAttribute('data-zoom-start', '35.5')
    expect(chart).toHaveAttribute('data-zoom-end', '75.5')
    // 属性更新引起的重渲染不得换掉事件表：引用一变封装库就整组解绑重绑。
    expect(chartEvents()).toBe(events)

    // 切换要素 -> 新序列 -> 配置整体重设：图表库把窗口打回全范围，但不发 datazoom。
    const optionBefore = capturedProps().option
    fake.window.start = 0
    fake.window.end = 100
    selectVariable('TEMP')
    expect(capturedProps().option).not.toBe(optionBefore)
    expect(capturedProps().option.series[0].data).not.toEqual(optionBefore.series[0].data)

    // 同一个图表区、同一个图表实例（没有销毁重建）。
    expect(screen.getByTestId(CHART_TEST_ID)).toBe(chart)
    expect(coreMounts.count).toBe(1)
    expect(chart).toHaveAttribute('data-zoom-start', '0')
    expect(chart).toHaveAttribute('data-zoom-end', '100')
    expect(chartEvents()).toBe(events)
  })

  it('(k) 配置没有重设时属性不动（复位读的是实例，不是写死的初值）', async () => {
    form = installMobileFormMatchMedia(true, false)
    const { chart } = await renderLoadedPopup()
    const fake = fakeChart({ start: 20, end: 60 })
    act(() => chartEvents().datazoom({}, fake.instance))

    // 旋转：仍是移动形态，配置不变。
    act(() => form!.setForm({ mobile: true, landscape: true }))
    expect(chart).toHaveAttribute('data-zoom-start', '20')
    expect(chart).toHaveAttribute('data-zoom-end', '60')
  })

  it('(l) 切到没有可绘制序列的要素再切回来（图表销毁重建）：属性从 0 / 100 / "false" 重新开始', async () => {
    const { chart } = await renderLoadedPopup()
    // 旧实例停在缩放过的窗口上，且 tooltip 可见；实例销毁不发 datazoom / hidetip。
    const fake = fakeChart({ start: 20, end: 60 })
    act(() => chartEvents().datazoom({}, fake.instance))
    act(() => chartEvents().showtip({}, fake.instance))
    expect(chart).toHaveAttribute('data-zoom-start', '20')
    expect(chart).toHaveAttribute('data-zoom-end', '60')
    expect(chart).toHaveAttribute('data-tooltip-visible', 'true')
    expect(coreMounts.count).toBe(1)

    selectVariable(MISSING_VARIABLE)
    expect(screen.getByTestId('m11-station-popup-empty')).toBeInTheDocument()
    expect(screen.queryByTestId(CHART_TEST_ID)).not.toBeInTheDocument()
    expect(screen.queryByTestId('mock-station-echarts')).not.toBeInTheDocument()

    selectVariable('PRCP')
    const recreated = screen.getByTestId(CHART_TEST_ID)
    expect(coreMounts.count, '图表应被重新创建').toBe(2)
    expect(recreated).toHaveAttribute('data-zoom-start', '0')
    expect(recreated).toHaveAttribute('data-zoom-end', '100')
    expect(recreated).toHaveAttribute('data-tooltip-visible', 'false')
  })

  it('(m) tooltip 显示 / 隐藏事件 → data-tooltip-visible 翻转', async () => {
    const { chart } = await renderLoadedPopup()
    const events = chartEvents()
    expect(chart).toHaveAttribute('data-tooltip-visible', 'false')

    act(() => events.showtip({}, {}))
    expect(chart).toHaveAttribute('data-tooltip-visible', 'true')

    act(() => events.hidetip({}, {}))
    expect(chart).toHaveAttribute('data-tooltip-visible', 'false')
    expect(chartEvents()).toBe(events)
  })
})

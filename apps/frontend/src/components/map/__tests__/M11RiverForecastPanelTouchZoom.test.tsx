import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { M11RiverForecastPanel, type M11RiverPopupSegment } from '@/components/map/M11RiverForecastPanel'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import { fetchHydroMetLatestProduct, type QhhLatestProduct } from '@/pages/hydroMet/bootstrap'
import { installMobileFormMatchMedia } from '@/test/mobileFormMatchMedia'

/**
 * 河段曲线的触屏缩放配置、提示文案与图表区只读属性（openspec mobile-responsive-display task 4.7，design.md D12）。
 * 用例 (j)(l)(m) 对应 tasks.md 里 #2808 的 Triage。图表库封装被替换成记录 props 的桩：配置看面板交出去的
 * `option`，事件经它收到的 `onEvents` 以封装库的调用方式 `(事件参数, 实例)` 驱动。真实手势由 e2e 断言。
 */

vi.mock('@/api/client', () => ({ client: { GET: vi.fn() } }))

vi.mock('@/pages/hydroMet/bootstrap', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/hydroMet/bootstrap')>()
  return { ...actual, fetchHydroMetLatestProduct: vi.fn() }
})

const { coreProps } = vi.hoisted(() => ({ coreProps: vi.fn() }))

vi.mock('echarts-for-react/lib/core', () => ({
  default: (props: Record<string, unknown>) => {
    coreProps(props)
    return <div data-testid="mock-forecast-echarts" />
  },
}))
vi.mock('@/components/charts/echartsCore', () => ({ echarts: {} }))

/** 改动前的 inside dataZoom 字面值（抄自改动前的源码，不读产品常量）。 */
const DESKTOP_DATA_ZOOM = [{ type: 'inside', zoomOnMouseWheel: true, moveOnMouseMove: false, moveOnMouseWheel: false, filterMode: 'none' }]
const CHART_TEST_ID = 'm11-river-panel-chart'
const HINT_TEST_ID = 'm11-river-panel-zoom-hint'

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
  if (!events) throw new Error('the panel did not hand onEvents to the chart')
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

function product(source: HydroMetSource): QhhLatestProduct {
  return {
    basin_id: 'basins_qhh',
    model_id: 'basins_qhh_shud',
    basin_version_id: 'bv-1',
    river_network_version_id: 'rn-1',
    source_id: source,
    cycle_time: '2026-05-21T00:00:00Z',
    run_id: 'run-1',
    forcing_version_id: 'forc-1',
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

const segment: M11RiverPopupSegment = {
  river_segment_id: 'seg-009',
  segment_id: 'seg-009',
  river_network_version_id: 'rn-1',
  basin_version_id: 'bv-1',
  name: 'Main Stem 009',
}
const otherSegment: M11RiverPopupSegment = { ...segment, river_segment_id: 'seg-010', segment_id: 'seg-010', name: 'Main Stem 010' }

/** forecast-series 按请求的河段与源应答（身份回显，过得了面板的契约校验）。 */
function mockForecastSeries() {
  vi.mocked(client.GET).mockImplementation((async (
    _path: string,
    init: { params: { path: { segment_id: string }; query: { scenarios: string } } },
  ) => {
    const source: HydroMetSource = init.params.query.scenarios.includes('ifs') ? 'IFS' : 'GFS'
    const base = (source === 'IFS' ? 4000 : 3225) + (init.params.path.segment_id === segment.river_segment_id ? 0 : 500)
    return {
      data: {
        status: 'success',
        data: {
          river_segment_id: init.params.path.segment_id,
          issue_time: '2026-05-21T00:00:00Z',
          variable: 'q_down',
          unit: 'm3/s',
          series: [
            {
              scenario_id: source === 'IFS' ? 'forecast_ifs_deterministic' : 'forecast_gfs_deterministic',
              source_id: source,
              cycle_time: '2026-05-21T00:00:00Z',
              points: [
                { valid_time: '2026-05-21T06:00:00Z', value: base },
                { valid_time: '2026-05-21T12:00:00Z', value: base + 75 },
              ],
            },
          ],
        },
      },
      error: undefined,
    }
  }) as never)
}

async function renderLoadedPanel() {
  const view = render(<M11RiverForecastPanel basinId="basins_qhh" segment={segment} />)
  const chart = await screen.findByTestId(CHART_TEST_ID)
  return { ...view, chart }
}

let form: ReturnType<typeof installMobileFormMatchMedia> | undefined

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(fetchHydroMetLatestProduct).mockImplementation((async ({ source }: { source: HydroMetSource }) => product(source)) as never)
  mockForecastSeries()
})

afterEach(() => {
  form?.restore()
  form = undefined
})

describe('M11RiverForecastPanel 的触屏缩放', () => {
  it('(j) 桌面形态：dataZoom 与改动前的字面值深相等，提示文案为「滚轮缩放时间轴」', async () => {
    const { chart } = await renderLoadedPanel()

    expect(capturedProps().option.dataZoom).toStrictEqual(DESKTOP_DATA_ZOOM)
    const hint = screen.getByTestId(HINT_TEST_ID)
    expect(hint).toHaveTextContent(/^滚轮缩放时间轴$/)
    expect(hint).toHaveClass('ml-auto', 'text-[10px]', 'text-slate-500')
    expect(chart).toHaveAttribute('data-zoom-start', '0')
    expect(chart).toHaveAttribute('data-zoom-end', '100')
    expect(chart).toHaveAttribute('data-tooltip-visible', 'false')
  })

  for (const landscape of [false, true]) {
    it(`(j) 移动形态${landscape ? '（矮视口横屏）' : ''}：与桌面字面值只差 moveOnMouseMove 与 preventDefaultMouseMove 两个键，提示文案为「双指缩放时间轴」`, async () => {
      form = installMobileFormMatchMedia(true, landscape)
      const { chart } = await renderLoadedPanel()

      expect(capturedProps().option.dataZoom).toStrictEqual([
        { ...DESKTOP_DATA_ZOOM[0], moveOnMouseMove: true, preventDefaultMouseMove: false },
      ])
      const hint = screen.getByTestId(HINT_TEST_ID)
      expect(hint).toHaveTextContent(/^双指缩放时间轴$/)
      expect(hint).toHaveClass('ml-auto', 'text-[10px]', 'text-slate-500')
      expect(chart).toHaveAttribute('data-zoom-start', '0')
      expect(chart).toHaveAttribute('data-zoom-end', '100')
    })
  }

  it('(j) 竖屏 ↔ 矮视口横屏不换图表配置（旋转不重置缩放），移动 → 桌面才换回桌面字面值', async () => {
    form = installMobileFormMatchMedia(true, false)
    await renderLoadedPanel()
    const portraitOption = capturedProps().option

    act(() => form!.setForm({ mobile: true, landscape: true }))
    expect(capturedProps().option).toBe(portraitOption)

    act(() => form!.setForm({ mobile: false, landscape: false }))
    expect(capturedProps().option).not.toBe(portraitOption)
    expect(capturedProps().option.dataZoom).toStrictEqual(DESKTOP_DATA_ZOOM)
    expect(screen.getByTestId(HINT_TEST_ID)).toHaveTextContent(/^滚轮缩放时间轴$/)
  })

  it('(l) 缩放事件 → 属性取实例当前窗口（不信事件载荷）；数据换新（配置重设）后回到 0 / 100', async () => {
    const { chart, rerender } = await renderLoadedPanel()
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

    // 换河段 -> 新数据 -> 配置整体重设：图表库把窗口打回全范围，但不发 datazoom。
    const optionBefore = capturedProps().option
    fake.window.start = 0
    fake.window.end = 100
    rerender(<M11RiverForecastPanel basinId="basins_qhh" segment={otherSegment} />)
    await waitFor(() => expect(capturedProps().option.series[0].data).not.toEqual(optionBefore.series[0].data))

    const reloadedChart = screen.getByTestId(CHART_TEST_ID)
    expect(reloadedChart).toHaveAttribute('data-zoom-start', '0')
    expect(reloadedChart).toHaveAttribute('data-zoom-end', '100')
    expect(chartEvents()).toBe(events)
  })

  it('(l) 配置没有重设时属性不动（复位读的是实例，不是写死的初值）', async () => {
    form = installMobileFormMatchMedia(true, false)
    const { chart } = await renderLoadedPanel()
    const fake = fakeChart({ start: 20, end: 60 })
    act(() => chartEvents().datazoom({}, fake.instance))

    // 旋转：仍是移动形态，配置不变。
    act(() => form!.setForm({ mobile: true, landscape: true }))
    expect(chart).toHaveAttribute('data-zoom-start', '20')
    expect(chart).toHaveAttribute('data-zoom-end', '60')
  })

  it('(m) tooltip 显示 / 隐藏事件 → data-tooltip-visible 翻转', async () => {
    const { chart } = await renderLoadedPanel()
    const events = chartEvents()
    expect(chart).toHaveAttribute('data-tooltip-visible', 'false')

    act(() => events.showtip({}, {}))
    expect(chart).toHaveAttribute('data-tooltip-visible', 'true')

    act(() => events.hidetip({}, {}))
    expect(chart).toHaveAttribute('data-tooltip-visible', 'false')
    expect(chartEvents()).toBe(events)
  })
})

import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ForecastChart } from '@/components/charts/ForecastChart'
import type { ForecastData, ForecastSeries } from '@/lib/forecastData'
import { FORECAST_CHART_POINT_BUDGET } from '@/lib/forecastRenderingBudget'

vi.mock('@/components/charts/echartsCore', () => ({
  echarts: {},
}))

// `coreProps` 留住原始 option：tooltip / 轴标签 formatter 是函数，JSON 序列化会丢掉它们。
const { coreProps } = vi.hoisted(() => ({ coreProps: vi.fn() }))

vi.mock('echarts-for-react/lib/core', () => ({
  default: (props: { option: unknown }) => {
    coreProps(props)
    return <pre data-testid="echarts-option">{JSON.stringify(props.option)}</pre>
  },
}))

interface TooltipParamInput {
  axisValue?: number
  marker?: string
  seriesName?: string
  value?: Array<number | string> | number
}

interface RawChartOption {
  legend?: unknown
  title?: unknown
  tooltip: { formatter: (params: TooltipParamInput | TooltipParamInput[]) => string }
  xAxis: { axisLabel: { formatter: (value: number) => string } }
  yAxis: { name: string }
}

function rawChartOption() {
  const props = coreProps.mock.lastCall?.[0] as { option: RawChartOption } | undefined
  if (!props) throw new Error('echarts core was not rendered')
  return props.option
}

const gfsAndIfs = () => [
  forecastSeries({ label: 'GFS' }),
  forecastSeries({ scenario: 'forecast_ifs_deterministic', source: 'IFS', label: 'IFS', color: '#34d399' }),
]

function forecastSeries(overrides: Partial<ForecastSeries>): ForecastSeries {
  return {
    scenario: 'forecast_gfs_deterministic',
    source: 'GFS',
    label: 'GFS 预报',
    color: '#22d3ee',
    cycleTime: '2026-05-03T00:00:00Z',
    availableLeadHours: 168,
    points: [
      { time: '2026-05-03T00:00:00Z', value: 1000 },
      { time: '2026-05-03T06:00:00Z', value: 1100 },
    ],
    ...overrides,
  }
}

function forecastData(series: ForecastSeries[]): ForecastData {
  return {
    segmentId: 'seg-1',
    issueTime: '2026-05-03T00:00:00Z',
    unit: 'm3/s',
    sourceAttribution: 'GFS, IFS',
    cycleAttribution: 'GFS: 05-03 00Z | IFS: 05-02 18Z',
    series,
  }
}

function renderedChartOption() {
  return JSON.parse(screen.getByTestId('echarts-option').textContent || '{}') as {
    series: Array<{
      name: string
      data?: Array<[number, number]>
      lineStyle: { color: string; type: string }
      markLine?: { data?: Array<{ name: string; xAxis: number }> }
    }>
  }
}

describe('ForecastChart comparison rendering', () => {
  it('renders GFS and IFS curves with the comparison colors', () => {
    render(
      <ForecastChart
        data={forecastData([
          forecastSeries({
            scenario: 'analysis_true_field',
            source: 'ERA5',
            label: '分析（ERA5）',
            color: '#34d399',
          }),
          forecastSeries({}),
          forecastSeries({
            scenario: 'forecast_ifs_deterministic',
            source: 'IFS',
            label: 'IFS 预报',
            color: '#34d399',
          }),
        ])}
      />,
    )

    const option = renderedChartOption()
    const analysis = option.series.find((series) => series.name === '分析（ERA5）')
    const gfs = option.series.find((series) => series.name === 'GFS 预报')
    const ifs = option.series.find((series) => series.name === 'IFS 预报')

    expect(analysis?.lineStyle).toMatchObject({ color: '#34d399', type: 'solid' })
    expect(gfs?.lineStyle).toMatchObject({ color: '#22d3ee', type: 'solid' })
    expect(ifs?.lineStyle).toMatchObject({ color: '#34d399', type: 'dashed' })
  })

  it('shows the IFS 6d annotation when available lead hours are 144', () => {
    const endpoint = Date.parse('2026-05-08T18:00:00Z')
    render(
      <ForecastChart
        data={forecastData([
          forecastSeries({
            scenario: 'forecast_ifs_deterministic',
            source: 'IFS',
            label: 'IFS 预报',
            color: '#34d399',
            cycleTime: '2026-05-02T18:00:00Z',
            availableLeadHours: 144,
            points: [
              { time: '2026-05-02T18:00:00Z', value: 900 },
              { time: '2026-05-08T18:00:00Z', value: 950 },
              { time: '2026-05-09T18:00:00Z', value: 990 },
            ],
          }),
        ])}
      />,
    )

    const ifs = renderedChartOption().series.find((series) => series.name === 'IFS 预报')
    expect(ifs?.markLine?.data).toContainEqual(expect.objectContaining({ name: 'IFS 6d', xAxis: endpoint }))
    expect(ifs?.data?.at(-1)?.[0]).toBe(endpoint)
  })

  it('defensively caps chart option points at the shared budget', () => {
    render(
      <ForecastChart
        data={forecastData([
          forecastSeries({
            points: Array.from({ length: FORECAST_CHART_POINT_BUDGET + 5 }, (_, index) => ({
              time: `2026-05-18T${String(index % 24).padStart(2, '0')}:00:00Z`,
              value: index,
            })),
          }),
        ])}
      />,
    )

    const option = renderedChartOption()
    const pointCount = option.series.reduce((total, series) => total + (series.data?.length ?? 0), 0)
    expect(pointCount).toBeLessThanOrEqual(FORECAST_CHART_POINT_BUDGET)
  })

  it('lists the time, then one line per series that has a value, with two decimals and the unit', () => {
    render(<ForecastChart data={forecastData(gfsAndIfs())} variant="compact" appearance="dark" />)

    const at = Date.parse('2026-05-03T06:00:00Z')
    const text = rawChartOption().tooltip.formatter([
      { axisValue: at, marker: '', seriesName: 'GFS', value: [at, 1234.5] },
      { axisValue: at, marker: '', seriesName: 'IFS', value: [at, 987.654] },
    ])
    expect(text).toBe(['时间: 2026-05-03 06:00', 'GFS: 1234.50 m³/s', 'IFS: 987.65 m³/s'].join('\n'))

    // 该时刻无值的曲线不占行。
    const oneValue = rawChartOption().tooltip.formatter([
      { axisValue: at, marker: '', seriesName: 'GFS', value: [at, 1234.5] },
      { axisValue: at, marker: '', seriesName: 'IFS', value: [at, Number.NaN] },
    ])
    expect(oneValue).toBe(['时间: 2026-05-03 06:00', 'GFS: 1234.50 m³/s'].join('\n'))

    // echarts 给出的系列色点（richText 标记）原样排在源名之前。
    const withMarker = rawChartOption().tooltip.formatter([{ axisValue: at, marker: '{marker0|}', seriesName: 'GFS', value: [at, 1] }])
    expect(withMarker.split('\n')[1]).toBe('{marker0|}GFS: 1.00 m³/s')

    // 时刻按 UTC 的 YYYY-MM-DD HH:mm：本地 getter 改成哨兵值，任何时区下本地/UTC 混用都会露出来。
    const local = [
      vi.spyOn(Date.prototype, 'getFullYear').mockReturnValue(1999),
      vi.spyOn(Date.prototype, 'getMonth').mockReturnValue(0),
      vi.spyOn(Date.prototype, 'getDate').mockReturnValue(1),
      vi.spyOn(Date.prototype, 'getHours').mockReturnValue(1),
      vi.spyOn(Date.prototype, 'getMinutes').mockReturnValue(1),
    ]
    try {
      const late = Date.parse('2026-12-31T23:45:00Z')
      const lateText = rawChartOption().tooltip.formatter([{ axisValue: late, marker: '', seriesName: 'GFS', value: [late, 1] }])
      expect(lateText.split('\n')[0]).toBe('时间: 2026-12-31 23:45')
    } finally {
      local.forEach((spy) => spy.mockRestore())
    }
  })

  it('labels the time axis in UTC as MM-DD HH:00 and names the value axis 流量 (m³/s)', () => {
    render(<ForecastChart data={forecastData(gfsAndIfs())} variant="compact" appearance="dark" />)

    const option = rawChartOption()
    // 23:00Z 在任何非 UTC 时区都会跨日或换小时；本地 getter 另被改成哨兵值，UTC 时区下也能抓到本地/UTC 的混用。
    const local = [
      vi.spyOn(Date.prototype, 'getMonth').mockReturnValue(0),
      vi.spyOn(Date.prototype, 'getDate').mockReturnValue(1),
      vi.spyOn(Date.prototype, 'getHours').mockReturnValue(1),
    ]
    try {
      expect(option.xAxis.axisLabel.formatter(Date.parse('2026-05-03T23:00:00Z'))).toBe('05-03 23:00')
      expect(option.xAxis.axisLabel.formatter(Date.parse('2026-12-31T06:30:00Z'))).toBe('12-31 06:00')
    } finally {
      local.forEach((spy) => spy.mockRestore())
    }
    expect(option.yAxis.name).toBe('流量 (m³/s)')
  })

  it('renders no echarts legend or title in the compact variant', () => {
    render(<ForecastChart data={forecastData(gfsAndIfs())} variant="compact" appearance="dark" />)

    const option = rawChartOption()
    expect(option.legend).toBeUndefined()
    expect(option.title).toBeUndefined()
  })
})

import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ForecastChart } from '@/components/charts/ForecastChart'
import type { ForecastData, ForecastSeries } from '@/lib/forecastData'
import { FORECAST_CHART_POINT_BUDGET } from '@/lib/forecastRenderingBudget'

vi.mock('@/components/charts/echartsCore', () => ({
  echarts: {},
}))

vi.mock('echarts-for-react/lib/core', () => ({
  default: ({ option }: { option: unknown }) => (
    <pre data-testid="echarts-option">{JSON.stringify(option)}</pre>
  ),
}))

function forecastSeries(overrides: Partial<ForecastSeries>): ForecastSeries {
  return {
    scenario: 'forecast_gfs_deterministic',
    source: 'GFS',
    role: 'future_7_days',
    isAnalysis: false,
    label: 'GFS 预报',
    color: '#ef7d22',
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
            role: 'past_3_days',
            isAnalysis: true,
            label: '分析（ERA5）',
            color: '#2266cc',
          }),
          forecastSeries({}),
          forecastSeries({
            scenario: 'forecast_ifs_deterministic',
            source: 'IFS',
            label: 'IFS 预报',
            color: '#2ca02c',
          }),
        ])}
      />,
    )

    const option = renderedChartOption()
    const analysis = option.series.find((series) => series.name === '分析（ERA5）')
    const gfs = option.series.find((series) => series.name === 'GFS 预报')
    const ifs = option.series.find((series) => series.name === 'IFS 预报')

    expect(analysis?.lineStyle).toMatchObject({ color: '#2266cc', type: 'solid' })
    expect(gfs?.lineStyle).toMatchObject({ color: '#ef7d22', type: 'solid' })
    expect(ifs?.lineStyle).toMatchObject({ color: '#2ca02c', type: 'dashed' })
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
            color: '#2ca02c',
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

  it('renders an over-budget state instead of passing oversized forecast arrays to ECharts', () => {
    render(
      <ForecastChart
        data={{
          ...forecastData([
            forecastSeries({
              points: Array.from({ length: FORECAST_CHART_POINT_BUDGET }, (_, index) => ({
                time: `2026-05-18T${String(index % 24).padStart(2, '0')}:00:00Z`,
                value: index,
              })),
            }),
          ]),
          pointBudgetStatus: {
            pointBudget: FORECAST_CHART_POINT_BUDGET,
            sourcePointCount: FORECAST_CHART_POINT_BUDGET + 1,
            retainedPointCount: FORECAST_CHART_POINT_BUDGET,
            seriesBudget: FORECAST_CHART_POINT_BUDGET,
            sourceSeriesCount: 1,
            retainedSeriesCount: 1,
            overBudget: true,
          },
        }}
      />,
    )

    expect(screen.getByRole('status')).toHaveTextContent('预报序列超出客户端渲染预算')
    expect(screen.queryByTestId('echarts-option')).not.toBeInTheDocument()
  })

  it('renders a degraded state for too many retained one-point series before building ECharts options', () => {
    render(
      <ForecastChart
        data={{
          ...forecastData(
            Array.from({ length: FORECAST_CHART_POINT_BUDGET }, (_, index) =>
              forecastSeries({
                points: [{ time: `2026-05-18T${String(index % 24).padStart(2, '0')}:00:00Z`, value: index }],
              }),
            ),
          ),
          pointBudgetStatus: {
            pointBudget: FORECAST_CHART_POINT_BUDGET,
            sourcePointCount: FORECAST_CHART_POINT_BUDGET + 1,
            retainedPointCount: FORECAST_CHART_POINT_BUDGET,
            seriesBudget: FORECAST_CHART_POINT_BUDGET,
            sourceSeriesCount: FORECAST_CHART_POINT_BUDGET + 1,
            retainedSeriesCount: FORECAST_CHART_POINT_BUDGET,
            overBudget: true,
          },
        }}
      />,
    )

    expect(screen.getByRole('status')).toHaveTextContent('预报序列超出客户端渲染预算')
    expect(screen.queryByTestId('echarts-option')).not.toBeInTheDocument()
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
})

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { ForecastChart } from '@/components/charts/ForecastChart'
import { ScenarioSelector } from '@/components/ScenarioSelector'
import { FORECAST_CHART_POINT_BUDGET } from '@/lib/forecastRenderingBudget'
import type { ForecastData, ForecastSeries } from '@/stores/forecast'
import { useForecastStore } from '@/stores/forecast'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn(),
  },
}))

vi.mock('@/components/charts/echartsCore', () => ({
  echarts: {},
}))

vi.mock('echarts-for-react/lib/core', () => ({
  default: ({ option }: { option: unknown }) => (
    <pre data-testid="echarts-option">{JSON.stringify(option)}</pre>
  ),
}))

function success<T>(data: T) {
  return { data: { status: 'success', data }, error: undefined }
}

function resetForecastStore(overrides: Partial<ReturnType<typeof useForecastStore.getState>> = {}) {
  useForecastStore.setState(
    {
      ...useForecastStore.getInitialState(),
      ...overrides,
    },
    true,
  )
}

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

describe('forecast comparison UI', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    resetForecastStore()
  })

  it('toggles scenarios and keeps at least one selected', async () => {
    const user = userEvent.setup()
    render(<ScenarioSelector />)

    const gfs = screen.getByLabelText('GFS scenario')
    const ifs = screen.getByLabelText('IFS scenario')

    expect(gfs).toBeChecked()
    expect(gfs).toBeDisabled()
    expect(ifs).not.toBeChecked()

    await user.click(ifs)
    expect(useForecastStore.getState().selectedScenarios).toEqual(['GFS', 'IFS'])

    await user.click(gfs)
    expect(useForecastStore.getState().selectedScenarios).toEqual(['IFS'])
    expect(screen.getByLabelText('IFS scenario')).toBeDisabled()

    useForecastStore.getState().toggleScenario('IFS')
    expect(useForecastStore.getState().selectedScenarios).toEqual(['IFS'])
  })

  it('passes selected scenarios in the forecast request', async () => {
    let query: Record<string, unknown> | undefined
    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const options = args[1] as { params?: { query?: Record<string, unknown> } }
      query = options.params?.query
      return success({
        segment_id: 'seg-1',
        issue_time: '2026-05-03T00:00:00Z',
        unit: 'm3/s',
        series: [],
        frequency_thresholds: { q2: 1, q5: 2, q10: 3, q20: 4, q50: 5, q100: 6 },
      }) as never
    })
    resetForecastStore({
      selectedSegment: { segmentId: 'seg-1', basinVersionId: 'basin-1', riverNetworkVersionId: 'rn-1' },
      selectedScenarios: ['GFS', 'IFS'],
    })

    await useForecastStore.getState().fetchForecast()

    expect(query).toMatchObject({ river_network_version_id: 'rn-1', scenarios: 'GFS,IFS', include_analysis: true })
  })

  it('uses route handoff source and cycle options without changing default forecast behavior', async () => {
    let query: Record<string, unknown> | undefined
    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const options = args[1] as { params?: { query?: Record<string, unknown> } }
      query = options.params?.query
      return success({
        segment_id: 'seg-1',
        issue_time: '2026-05-18T00:00:00Z',
        unit: 'm3/s',
        series: [],
        frequency_thresholds: null,
      }) as never
    })
    resetForecastStore({
      selectedSegment: { segmentId: 'seg-1', basinVersionId: 'basin-1', riverNetworkVersionId: 'rn-1' },
      selectedScenarios: ['GFS'],
    })

    await useForecastStore.getState().fetchForecast({
      source: 'ifs',
      issueTime: '2026-05-18T00:00:00.000Z',
      includeAnalysis: true,
    })

    expect(query).toMatchObject({
      issue_time: '2026-05-18T00:00:00.000Z',
      river_network_version_id: 'rn-1',
      scenarios: 'IFS',
      include_analysis: true,
    })
  })

  it('omits scenarios for source=best so the API can resolve best availability', async () => {
    let query: Record<string, unknown> | undefined
    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const options = args[1] as { params?: { query?: Record<string, unknown> } }
      query = options.params?.query
      return success({
        segment_id: 'seg-1',
        issue_time: '2026-05-18T00:00:00Z',
        unit: 'm3/s',
        series: [],
        frequency_thresholds: null,
      }) as never
    })
    resetForecastStore({
      selectedSegment: { segmentId: 'seg-1', basinVersionId: 'basin-1', riverNetworkVersionId: 'rn-1' },
      selectedScenarios: ['GFS'],
    })

    await useForecastStore.getState().fetchForecast({
      source: 'best',
      issueTime: '2026-05-18T00:00:00.000Z',
      includeAnalysis: true,
    })

    expect(query).toMatchObject({
      issue_time: '2026-05-18T00:00:00.000Z',
      river_network_version_id: 'rn-1',
      include_analysis: true,
    })
    expect(query).not.toHaveProperty('scenarios')
  })

  it('rejects forecast responses for sibling segment identities', async () => {
    vi.mocked(client.GET).mockResolvedValue(
      success({
        segment_id: 'seg-sibling',
        issue_time: '2026-05-18T00:00:00Z',
        unit: 'm3/s',
        series: [],
        frequency_thresholds: null,
      }) as never,
    )
    resetForecastStore({
      selectedSegment: { segmentId: 'seg-1', basinVersionId: 'basin-1', riverNetworkVersionId: 'rn-1' },
      selectedScenarios: ['GFS'],
    })

    await expect(useForecastStore.getState().fetchForecast({ source: 'gfs' })).rejects.toThrow('预报曲线响应与请求河段不匹配')
    expect(useForecastStore.getState()).toMatchObject({
      forecastData: null,
      loading: false,
    })
  })

  it('accepts ignored-context forecast responses even when a stale route context remains in the store', async () => {
    let query: Record<string, unknown> | undefined
    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const options = args[1] as { params?: { query?: Record<string, unknown> } }
      query = options.params?.query
      return success({
        segment_id: 'seg-1',
        issue_time: '2026-05-12T00:00:00Z',
        unit: 'm3/s',
        series: [
          {
            scenario_id: 'forecast_gfs_deterministic',
            source: 'GFS',
            segment_role: 'future_7_days',
            cycle_time: '2026-05-12T00:00:00.000Z',
            points: [['2026-05-12T03:00:00Z', 123]],
          },
        ],
        frequency_thresholds: null,
      }) as never
    })
    resetForecastStore({
      selectedSegment: { segmentId: 'seg-1', basinVersionId: 'basin-1', riverNetworkVersionId: 'rn-1' },
      selectedScenarios: ['IFS'],
      activeRequestContext: { source: 'ifs', issueTime: '2026-05-18T00:00:00.000Z' },
    })

    await useForecastStore.getState().fetchForecast({
      includeAnalysis: true,
      ignoreActiveRequestContext: true,
      source: 'gfs',
      issueTime: '2026-05-12T00:00:00.000Z',
    })

    expect(query).toMatchObject({
      issue_time: '2026-05-12T00:00:00.000Z',
      river_network_version_id: 'rn-1',
      scenarios: 'GFS',
      include_analysis: true,
    })
    expect(useForecastStore.getState()).toMatchObject({
      activeRequestContext: { source: 'ifs', issueTime: '2026-05-18T00:00:00.000Z' },
      forecastData: {
        segmentId: 'seg-1',
        issueTime: '2026-05-12T00:00:00Z',
        sourceAttribution: 'GFS',
      },
      loading: false,
    })
  })

  it('persists restored source and cycle as the active forecast request context', async () => {
    const queries: Array<Record<string, unknown> | undefined> = []
    vi.mocked(client.GET).mockImplementation(async (...args: unknown[]) => {
      const options = args[1] as { params?: { query?: Record<string, unknown> } }
      queries.push(options.params?.query)
      return success({
        segment_id: 'seg-1',
        issue_time: '2026-05-18T00:00:00Z',
        unit: 'm3/s',
        series: [],
        frequency_thresholds: null,
      }) as never
    })
    resetForecastStore({
      selectedSegment: { segmentId: 'seg-1', basinVersionId: 'basin-1', riverNetworkVersionId: 'rn-1' },
      selectedScenarios: ['GFS'],
    })

    useForecastStore.getState().setRequestContext({ source: 'compare', issueTime: '2026-05-18T00:00:00.000Z' })
    await useForecastStore.getState().fetchForecast({ includeAnalysis: true })
    useForecastStore.getState().toggleScenario('IFS')
    await useForecastStore.getState().fetchForecast({ includeAnalysis: true, useSelectedScenarios: true })

    expect(useForecastStore.getState().activeRequestContext).toMatchObject({ source: null, issueTime: '2026-05-18T00:00:00.000Z' })
    expect(queries).toEqual([
      expect.objectContaining({
        river_network_version_id: 'rn-1',
        issue_time: '2026-05-18T00:00:00.000Z',
        scenarios: 'GFS,IFS',
      }),
      expect.objectContaining({
        river_network_version_id: 'rn-1',
        issue_time: '2026-05-18T00:00:00.000Z',
        scenarios: 'GFS',
      }),
    ])
  })

  it('preserves spliced forecast thresholds from include-analysis responses', async () => {
    vi.mocked(client.GET).mockResolvedValue(
      success({
        river_segment_id: 'seg-1',
        issue_time: '2026-05-03T00:00:00Z',
        variable: 'discharge',
        unit: 'm3/s',
        frequency_thresholds: { Q2: 1, Q20: 4, Q100: 6 },
        segments: [],
      }) as never,
    )
    resetForecastStore({
      selectedSegment: { segmentId: 'seg-1', basinVersionId: 'basin-1', riverNetworkVersionId: 'rn-1' },
      selectedScenarios: ['GFS'],
    })

    await useForecastStore.getState().fetchForecast({ includeAnalysis: true })

    expect(useForecastStore.getState().forecastData?.frequencyThresholds).toEqual({
      Q2: 1,
      Q20: 4,
      Q100: 6,
    })
  })

  it('caps oversized forecast payloads during normalization', async () => {
    vi.mocked(client.GET).mockResolvedValue(
      success({
        segment_id: 'seg-1',
        issue_time: '2026-05-18T00:00:00Z',
        unit: 'm3/s',
        series: [
          {
            scenario_id: 'forecast_gfs_deterministic',
            source: 'GFS',
            segment_role: 'future_7_days',
            points: Array.from({ length: FORECAST_CHART_POINT_BUDGET + 5 }, (_, index) => [
              `2026-05-18T${String(index % 24).padStart(2, '0')}:00:00Z`,
              index,
            ]),
          },
        ],
        frequency_thresholds: null,
      }) as never,
    )
    resetForecastStore({
      selectedSegment: { segmentId: 'seg-1', basinVersionId: 'basin-1', riverNetworkVersionId: 'rn-1' },
      selectedScenarios: ['GFS'],
    })

    await useForecastStore.getState().fetchForecast({ source: 'gfs' })

    const forecast = useForecastStore.getState().forecastData
    expect(forecast?.pointBudgetStatus).toMatchObject({
      pointBudget: FORECAST_CHART_POINT_BUDGET,
      sourcePointCount: FORECAST_CHART_POINT_BUDGET + 5,
      retainedPointCount: FORECAST_CHART_POINT_BUDGET,
      seriesBudget: FORECAST_CHART_POINT_BUDGET,
      sourceSeriesCount: 1,
      retainedSeriesCount: 1,
      overBudget: true,
    })
    expect(forecast?.series[0]?.points).toHaveLength(FORECAST_CHART_POINT_BUDGET)
  })

  it('marks too many one-point series as over-budget and stops retaining extra series', async () => {
    vi.mocked(client.GET).mockResolvedValue(
      success({
        segment_id: 'seg-1',
        issue_time: '2026-05-18T00:00:00Z',
        unit: 'm3/s',
        series: Array.from({ length: FORECAST_CHART_POINT_BUDGET + 1 }, (_, index) => ({
          scenario_id: 'forecast_gfs_deterministic',
          source: 'GFS',
          segment_role: 'future_7_days',
          points: [[`2026-05-18T${String(index % 24).padStart(2, '0')}:00:00Z`, index]],
        })),
        frequency_thresholds: null,
      }) as never,
    )
    resetForecastStore({
      selectedSegment: { segmentId: 'seg-1', basinVersionId: 'basin-1', riverNetworkVersionId: 'rn-1' },
      selectedScenarios: ['GFS'],
    })

    await useForecastStore.getState().fetchForecast({ source: 'gfs' })

    const forecast = useForecastStore.getState().forecastData
    expect(forecast?.pointBudgetStatus).toMatchObject({
      pointBudget: FORECAST_CHART_POINT_BUDGET,
      sourcePointCount: FORECAST_CHART_POINT_BUDGET + 1,
      retainedPointCount: FORECAST_CHART_POINT_BUDGET,
      seriesBudget: FORECAST_CHART_POINT_BUDGET,
      sourceSeriesCount: FORECAST_CHART_POINT_BUDGET + 1,
      retainedSeriesCount: FORECAST_CHART_POINT_BUDGET,
      overBudget: true,
    })
    expect(forecast?.series).toHaveLength(FORECAST_CHART_POINT_BUDGET)
  })

  it('fails locally instead of issuing an unscoped forecast request', async () => {
    resetForecastStore({
      selectedSegment: { segmentId: 'seg-1', basinVersionId: 'basin-1', riverNetworkVersionId: '' },
      selectedScenarios: ['GFS'],
    })

    await expect(useForecastStore.getState().fetchForecast()).rejects.toThrow('缺少 river_network_version_id')

    expect(client.GET).not.toHaveBeenCalled()
    expect(useForecastStore.getState()).toMatchObject({
      error: '缺少 river_network_version_id，无法请求河段预报',
      forecastData: null,
      loading: false,
    })
  })

  it('renders GFS and IFS curves with the comparison colors', () => {
    render(
      <ForecastChart
        data={forecastData([
          forecastSeries({
            scenario: 'analysis_true_field',
            source: 'ERA5',
            role: 'past_7_days',
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
    expect(ifs?.data.at(-1)?.[0]).toBe(endpoint)
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

  it('shows unavailable text when IFS is selected but absent from the response', () => {
    resetForecastStore({
      selectedScenarios: ['GFS', 'IFS'],
      forecastData: forecastData([forecastSeries({})]),
    })

    render(<ScenarioSelector />)

    expect(screen.getByText('(暂无数据)')).toHaveClass('text-muted')
  })
})

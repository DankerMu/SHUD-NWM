import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { ForecastChart } from '@/components/charts/ForecastChart'
import { ScenarioSelector } from '@/components/ScenarioSelector'
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
      data: Array<[number, number]>
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
      selectedSegment: { segmentId: 'seg-1', basinVersionId: 'basin-1' },
      selectedScenarios: ['GFS', 'IFS'],
    })

    await useForecastStore.getState().fetchForecast()

    expect(query).toMatchObject({ scenarios: 'GFS,IFS', include_analysis: true })
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
      selectedSegment: { segmentId: 'seg-1', basinVersionId: 'basin-1' },
      selectedScenarios: ['GFS'],
    })

    await useForecastStore.getState().fetchForecast({
      source: 'ifs',
      issueTime: '2026-05-18T00:00:00.000Z',
      includeAnalysis: true,
    })

    expect(query).toMatchObject({
      issue_time: '2026-05-18T00:00:00.000Z',
      scenarios: 'IFS',
      include_analysis: true,
    })
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
      selectedSegment: { segmentId: 'seg-1', basinVersionId: 'basin-1' },
      selectedScenarios: ['GFS'],
    })

    await useForecastStore.getState().fetchForecast({ includeAnalysis: true })

    expect(useForecastStore.getState().forecastData?.frequencyThresholds).toEqual({
      Q2: 1,
      Q20: 4,
      Q100: 6,
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

  it('shows unavailable text when IFS is selected but absent from the response', () => {
    resetForecastStore({
      selectedScenarios: ['GFS', 'IFS'],
      forecastData: forecastData([forecastSeries({})]),
    })

    render(<ScenarioSelector />)

    expect(screen.getByText('(暂无数据)')).toHaveClass('text-muted')
  })
})

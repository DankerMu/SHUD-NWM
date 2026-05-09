import { create } from 'zustand'

import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'

export interface ForecastSegmentInfo {
  segmentId: string
  name?: string
  basinVersionId?: string
  riverNetworkVersionId?: string
  streamOrder?: number
}

export interface ForecastSeriesPoint {
  time: string | number
  value: number
}

export interface ForecastSeries {
  scenario: string
  source?: string
  role?: string
  isAnalysis: boolean
  label: string
  color: '#2266cc' | '#ef7d22'
  points: ForecastSeriesPoint[]
}

export interface ForecastData {
  segmentId: string
  issueTime: string | null
  unit: string
  series: ForecastSeries[]
  sourceAttribution: string
  frequencyThresholds?: components['schemas']['RiverSeriesResponse']['frequency_thresholds']
}

export interface FetchForecastOptions {
  includeAnalysis?: boolean
}

interface ForecastState {
  selectedSegment: ForecastSegmentInfo | null
  forecastData: ForecastData | null
  loading: boolean
  error: string | null
  includeAnalysis: boolean
  requestNonce: number
  selectSegment: (segment: ForecastSegmentInfo) => void
  fetchForecast: (options?: FetchForecastOptions) => Promise<void>
  clearSelection: () => void
  setLoading: (loading: boolean) => void
  setError: (error: string | null) => void
}

interface SplicedForecastPoint {
  valid_time?: string | number
  time?: string | number
  value?: number
}

interface SplicedForecastSegment {
  scenario?: string
  scenario_id?: string
  source?: string
  segment_role?: string
  role?: string
  data?: SplicedForecastPoint[]
}

interface SplicedForecastResponse {
  river_segment_id?: string
  segment_id?: string
  issue_time?: string
  unit?: string
  segments?: SplicedForecastSegment[]
}

type RiverSeriesResponse = components['schemas']['RiverSeriesResponse']
type ForecastApiPayload = RiverSeriesResponse | SplicedForecastResponse

function isAnalysisScenario(scenario: string) {
  return scenario === 'analysis_true_field' || scenario.toLowerCase().includes('analysis')
}

function buildSeriesLabel(isAnalysis: boolean, scenario: string, source?: string) {
  if (source) return `${isAnalysis ? '真实场' : '预报'} (${source})`
  if (isAnalysis) return '真实场'
  return scenario || '预报'
}

function normalizeSplicedResponse(payload: SplicedForecastResponse): ForecastData {
  const series = (payload.segments ?? []).map((segment): ForecastSeries => {
    const scenario = segment.scenario ?? segment.scenario_id ?? ''
    const source = segment.source
    const isAnalysis = isAnalysisScenario(scenario)
    return {
      scenario,
      source,
      role: segment.segment_role ?? segment.role,
      isAnalysis,
      label: buildSeriesLabel(isAnalysis, scenario, source),
      color: isAnalysis ? '#2266cc' : '#ef7d22',
      points: (segment.data ?? [])
        .filter((point) => point.value !== undefined && (point.valid_time !== undefined || point.time !== undefined))
        .map((point) => ({
          time: point.valid_time ?? point.time ?? '',
          value: Number(point.value),
        })),
    }
  })

  return {
    segmentId: payload.river_segment_id ?? payload.segment_id ?? '',
    issueTime: payload.issue_time ?? null,
    unit: payload.unit ?? 'm3/s',
    series,
    sourceAttribution: buildSourceAttribution(series),
  }
}

function normalizeRiverSeriesResponse(payload: RiverSeriesResponse): ForecastData {
  const series = (payload.series ?? []).map((segment): ForecastSeries => {
    const scenario = segment.scenario_id ?? 'forecast_gfs_deterministic'
    const isAnalysis = isAnalysisScenario(scenario)
    const source = isAnalysis ? undefined : 'GFS'
    return {
      scenario,
      source,
      role: segment.segment_role,
      isAnalysis,
      label: buildSeriesLabel(isAnalysis, scenario, source),
      color: isAnalysis ? '#2266cc' : '#ef7d22',
      points: (segment.points ?? [])
        .filter((point) => point.length >= 2)
        .map((point) => ({
          time: point[0],
          value: Number(point[1]),
        })),
    }
  })

  return {
    segmentId: payload.segment_id,
    issueTime: payload.issue_time ?? null,
    unit: payload.unit ?? 'm3/s',
    series,
    sourceAttribution: buildSourceAttribution(series),
    frequencyThresholds: payload.frequency_thresholds,
  }
}

function normalizeForecastPayload(payload: ForecastApiPayload): ForecastData {
  if ('segments' in payload && Array.isArray(payload.segments)) {
    return normalizeSplicedResponse(payload)
  }

  return normalizeRiverSeriesResponse(payload as RiverSeriesResponse)
}

function buildSourceAttribution(series: ForecastSeries[]) {
  const sources = series
    .filter((segment) => segment.source)
    .map((segment) => `${segment.isAnalysis ? 'analysis' : 'forecast'}: ${segment.source}`)
  return [...new Set(sources)].join('；')
}

async function fetchForecastSeries(segment: ForecastSegmentInfo, includeAnalysis: boolean) {
  if (!segment.basinVersionId) {
    throw new Error('缺少 basin_version_id，无法请求河段预报')
  }

  const query = {
    issue_time: 'latest',
    variables: 'q_down',
    scenarios: 'GFS',
    include_analysis: includeAnalysis,
  }

  const { data, error } = await client.GET(
    '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series',
    {
      params: {
        path: {
          basin_version_id: segment.basinVersionId,
          segment_id: segment.segmentId,
        },
        query,
      },
    },
  )

  if (error) throw new Error(getApiErrorMessage(error, '获取预报曲线失败'))
  return unwrapApiData<ForecastApiPayload>(data, '获取预报曲线失败')
}

export const useForecastStore = create<ForecastState>((set, get) => ({
  selectedSegment: null,
  forecastData: null,
  loading: false,
  error: null,
  includeAnalysis: true,
  requestNonce: 0,
  selectSegment: (segment) =>
    set({
      selectedSegment: segment,
      forecastData: null,
      loading: false,
      error: null,
    }),
  fetchForecast: async (options) => {
    const segment = get().selectedSegment
    if (!segment) return

    const includeAnalysis = options?.includeAnalysis ?? get().includeAnalysis
    const requestedSegmentId = segment.segmentId
    const requestNonce = get().requestNonce + 1
    set({ loading: true, error: null, forecastData: null, includeAnalysis, requestNonce })

    try {
      const payload = await fetchForecastSeries(segment, includeAnalysis)
      const state = get()
      if (state.requestNonce !== requestNonce || state.selectedSegment?.segmentId !== requestedSegmentId) return

      set({
        forecastData: normalizeForecastPayload(payload),
        loading: false,
        error: null,
      })
    } catch (error) {
      const state = get()
      if (state.requestNonce !== requestNonce || state.selectedSegment?.segmentId !== requestedSegmentId) return
      const message = getApiErrorMessage(error, '获取预报曲线失败')
      set({ error: message, loading: false, forecastData: null })
      throw error
    }
  },
  clearSelection: () =>
    set({
      selectedSegment: null,
      forecastData: null,
      loading: false,
      error: null,
    }),
  setLoading: (loading) => set({ loading }),
  setError: (error) => set({ error }),
}))

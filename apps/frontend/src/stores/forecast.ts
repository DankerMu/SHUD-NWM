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
  color: '#2266cc' | '#ef7d22' | '#2ca02c'
  cycleTime?: string | null
  availableLeadHours?: number | null
  points: ForecastSeriesPoint[]
}

export interface ForecastData {
  segmentId: string
  issueTime: string | null
  unit: string
  series: ForecastSeries[]
  sourceAttribution: string
  cycleAttribution: string
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
  selectedScenarios: string[]
  requestNonce: number
  selectSegment: (segment: ForecastSegmentInfo) => void
  toggleScenario: (scenario: string) => void
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
  source_id?: string
  segment_role?: string
  role?: string
  cycle_time?: string | null
  available_lead_hours?: number | null
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
type RiverSeriesSegment = RiverSeriesResponse['series'][number] & {
  source?: string
  source_id?: string
  cycle_time?: string | null
  available_lead_hours?: number | null
}
type ForecastApiPayload = RiverSeriesResponse | SplicedForecastResponse

function isAnalysisScenario(scenario: string) {
  return scenario === 'analysis_true_field' || scenario.toLowerCase().includes('analysis')
}

function inferSource(scenario: string, source?: string) {
  if (source) return source.toUpperCase()
  const normalized = scenario.toLowerCase()
  if (normalized.includes('ifs')) return 'IFS'
  if (normalized.includes('gfs')) return 'GFS'
  if (normalized.includes('era5')) return 'ERA5'
  return undefined
}

function isAnalysisSegment(scenario: string, role?: string) {
  return role === 'past_7_days' || isAnalysisScenario(scenario)
}

function seriesColor(isAnalysis: boolean, scenario: string, source?: string) {
  if (isAnalysis) return '#2266cc'
  const normalized = `${scenario} ${source ?? ''}`.toLowerCase()
  if (normalized.includes('ifs')) return '#2ca02c'
  return '#ef7d22'
}

function buildSeriesLabel(isAnalysis: boolean, scenario: string, source?: string) {
  if (isAnalysis) return source ? `分析（${source}）` : '分析'
  if (source === 'GFS') return 'GFS 预报'
  if (source === 'IFS') return 'IFS 预报'
  return scenario || '预报'
}

function formatCycleTime(value?: string | null) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const month = String(date.getUTCMonth() + 1).padStart(2, '0')
  const day = String(date.getUTCDate()).padStart(2, '0')
  const hour = String(date.getUTCHours()).padStart(2, '0')
  return `${month}-${day} ${hour}Z`
}

function normalizeSplicedResponse(payload: SplicedForecastResponse): ForecastData {
  const series = (payload.segments ?? []).map((segment): ForecastSeries => {
    const scenario = segment.scenario ?? segment.scenario_id ?? ''
    const role = segment.segment_role ?? segment.role
    const source = inferSource(scenario, segment.source_id ?? segment.source)
    const isAnalysis = isAnalysisSegment(scenario, role)
    return {
      scenario,
      source,
      role,
      isAnalysis,
      label: buildSeriesLabel(isAnalysis, scenario, source),
      color: seriesColor(isAnalysis, scenario, source),
      cycleTime: segment.cycle_time,
      availableLeadHours:
        segment.available_lead_hours !== undefined && segment.available_lead_hours !== null
          ? Number(segment.available_lead_hours)
          : null,
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
    cycleAttribution: buildCycleAttribution(series, payload.issue_time ?? null),
  }
}

function normalizeRiverSeriesResponse(payload: RiverSeriesResponse): ForecastData {
  const series = ((payload.series ?? []) as RiverSeriesSegment[]).map((segment): ForecastSeries => {
    const scenario = segment.scenario_id ?? 'forecast_gfs_deterministic'
    const source = inferSource(scenario, segment.source_id ?? segment.source)
    const isAnalysis = isAnalysisSegment(scenario, segment.segment_role)
    return {
      scenario,
      source,
      role: segment.segment_role,
      isAnalysis,
      label: buildSeriesLabel(isAnalysis, scenario, source),
      color: seriesColor(isAnalysis, scenario, source),
      cycleTime: segment.cycle_time,
      availableLeadHours:
        segment.available_lead_hours !== undefined && segment.available_lead_hours !== null
          ? Number(segment.available_lead_hours)
          : null,
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
    cycleAttribution: buildCycleAttribution(series, payload.issue_time ?? null),
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
    .filter((segment): segment is ForecastSeries & { source: string } => Boolean(segment.source) && !segment.isAnalysis)
    .map((segment) => segment.source)
  return [...new Set(sources)].join(', ')
}

function buildCycleAttribution(series: ForecastSeries[], fallbackIssueTime: string | null) {
  const cycleEntries = series
    .filter((segment): segment is ForecastSeries & { source: string } => Boolean(segment.source) && !segment.isAnalysis)
    .map((segment) => {
      const cycleTime = formatCycleTime(segment.cycleTime ?? fallbackIssueTime)
      return cycleTime ? `${segment.source}: ${cycleTime}` : ''
    })
  return [...new Set(cycleEntries)].filter(Boolean).join(' | ')
}

async function fetchForecastSeries(
  segment: ForecastSegmentInfo,
  includeAnalysis: boolean,
  selectedScenarios: string[],
) {
  if (!segment.basinVersionId) {
    throw new Error('缺少 basin_version_id，无法请求河段预报')
  }

  const query = {
    issue_time: 'latest',
    variables: 'q_down',
    scenarios: selectedScenarios.join(','),
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
  selectedScenarios: ['GFS'],
  requestNonce: 0,
  selectSegment: (segment) =>
    set({
      selectedSegment: segment,
      forecastData: null,
      loading: false,
      error: null,
    }),
  toggleScenario: (scenario) =>
    set((state) => {
      const normalizedScenario = scenario.toUpperCase()
      const isSelected = state.selectedScenarios.includes(normalizedScenario)
      if (isSelected && state.selectedScenarios.length === 1) {
        return { selectedScenarios: state.selectedScenarios }
      }

      return {
        selectedScenarios: isSelected
          ? state.selectedScenarios.filter((selected) => selected !== normalizedScenario)
          : [...state.selectedScenarios, normalizedScenario],
      }
    }),
  fetchForecast: async (options) => {
    const segment = get().selectedSegment
    if (!segment) return

    const includeAnalysis = options?.includeAnalysis ?? get().includeAnalysis
    const selectedScenarios = get().selectedScenarios.length > 0 ? get().selectedScenarios : ['GFS']
    const requestedSegmentId = segment.segmentId
    const requestNonce = get().requestNonce + 1
    set({ loading: true, error: null, forecastData: null, includeAnalysis, requestNonce })

    try {
      const payload = await fetchForecastSeries(segment, includeAnalysis, selectedScenarios)
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

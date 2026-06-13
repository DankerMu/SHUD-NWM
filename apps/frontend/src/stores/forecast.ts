import { create } from 'zustand'

import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'
import {
  createForecastPointBudgetGuard,
  type ForecastPointBudgetStatus,
} from '@/lib/forecastRenderingBudget'
import type { M11Source } from '@/lib/m11/queryState'

export interface ForecastSegmentInfo {
  segmentId: string
  name?: string
  basinVersionId?: string
  riverNetworkVersionId: string
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
  color: '#2266cc' | '#ef7d22' | '#2ca02c' | '#34d399' | '#22d3ee'
  cycleTime?: string | null
  availableLeadHours?: number | null
  points: ForecastSeriesPoint[]
}

export interface ForecastData {
  segmentId: string
  basinVersionId?: string
  riverNetworkVersionId?: string
  source?: M11Source | null
  cycle?: string | null
  issueTime: string | null
  unit: string
  series: ForecastSeries[]
  sourceAttribution: string
  cycleAttribution: string
  frequencyThresholds?: components['schemas']['RiverSeriesResponse']['frequency_thresholds']
  pointBudgetStatus?: ForecastPointBudgetStatus
}

export interface FetchForecastOptions {
  includeAnalysis?: boolean
  issueTime?: string | null
  source?: M11Source | null
  useSelectedScenarios?: boolean
  ignoreActiveRequestContext?: boolean
}

export interface ForecastRequestContext {
  issueTime?: string | null
  source?: M11Source | null
}

interface ForecastState {
  selectedSegment: ForecastSegmentInfo | null
  forecastData: ForecastData | null
  loading: boolean
  error: string | null
  includeAnalysis: boolean
  selectedScenarios: string[]
  activeRequestContext: ForecastRequestContext | null
  activeForecastRequest: ForecastRequestIdentity | null
  requestNonce: number
  selectSegment: (segment: ForecastSegmentInfo) => void
  toggleScenario: (scenario: string) => void
  setRequestContext: (context: ForecastRequestContext | null) => void
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
  issue_time?: string | null
  unit?: string
  segments?: SplicedForecastSegment[]
  frequency_thresholds?: components['schemas']['SplicedForecastResponse']['frequency_thresholds']
}

type RiverSeriesResponse = components['schemas']['RiverSeriesResponse']
type RiverSeriesSegment = RiverSeriesResponse['series'][number] & {
  source?: string
  source_id?: string
  cycle_time?: string | null
  available_lead_hours?: number | null
}
type ForecastApiPayload = RiverSeriesResponse | SplicedForecastResponse

interface ForecastRequestIdentity {
  nonce: number
  segmentId: string
  basinVersionId?: string
  riverNetworkVersionId: string
  issueTime?: string | null
  scenarios: string
  contextIssueTimeBound: boolean
  contextScenariosBound: boolean
}

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
  const pointBudgetGuard = createForecastPointBudgetGuard()
  const sourceSegments = payload.segments ?? []
  pointBudgetGuard.setSourceSeriesCount(sourceSegments.length)
  pointBudgetGuard.setSourcePointCount(
    sourceSegments.reduce((total, segment) => total + (Array.isArray(segment.data) ? segment.data.length : 0), 0),
  )
  const series: ForecastSeries[] = []
  for (const segment of sourceSegments) {
    const scenario = segment.scenario ?? segment.scenario_id ?? ''
    const role = segment.segment_role ?? segment.role
    const source = inferSource(scenario, segment.source_id ?? segment.source)
    const isAnalysis = isAnalysisSegment(scenario, role)
    const retainedPoints = pointBudgetGuard.takeSeries(segment.data)
    if (retainedPoints === null) break
    series.push({
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
      points: retainedPoints
        .filter((point) => point.value !== undefined && (point.valid_time !== undefined || point.time !== undefined))
        .map((point) => ({
          time: point.valid_time ?? point.time ?? '',
          value: Number(point.value),
        })),
    })
  }

  return {
    segmentId: payload.river_segment_id ?? payload.segment_id ?? '',
    issueTime: payload.issue_time ?? null,
    unit: payload.unit ?? 'm3/s',
    series,
    sourceAttribution: buildSourceAttribution(series),
    cycleAttribution: buildCycleAttribution(series, payload.issue_time ?? null),
    frequencyThresholds: payload.frequency_thresholds,
    pointBudgetStatus: pointBudgetGuard.status(),
  }
}

function normalizeRiverSeriesResponse(payload: RiverSeriesResponse): ForecastData {
  const pointBudgetGuard = createForecastPointBudgetGuard()
  const sourceSegments = (payload.series ?? []) as RiverSeriesSegment[]
  pointBudgetGuard.setSourceSeriesCount(sourceSegments.length)
  pointBudgetGuard.setSourcePointCount(
    sourceSegments.reduce((total, segment) => total + (Array.isArray(segment.points) ? segment.points.length : 0), 0),
  )
  const series: ForecastSeries[] = []
  for (const segment of sourceSegments) {
    const scenario = segment.scenario_id ?? 'forecast_gfs_deterministic'
    const source = inferSource(scenario, segment.source_id ?? segment.source)
    const isAnalysis = isAnalysisSegment(scenario, segment.segment_role)
    const retainedPoints = pointBudgetGuard.takeSeries(segment.points)
    if (retainedPoints === null) break
    series.push({
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
      points: retainedPoints
        .filter((point) => point.length >= 2)
        .map((point) => ({
          time: point[0],
          value: Number(point[1]),
        })),
    })
  }

  return {
    segmentId: payload.segment_id,
    issueTime: payload.issue_time ?? null,
    unit: payload.unit ?? 'm3/s',
    series,
    sourceAttribution: buildSourceAttribution(series),
    cycleAttribution: buildCycleAttribution(series, payload.issue_time ?? null),
    frequencyThresholds: payload.frequency_thresholds,
    pointBudgetStatus: pointBudgetGuard.status(),
  }
}

function normalizeForecastPayload(payload: ForecastApiPayload): ForecastData {
  if ('segments' in payload && Array.isArray(payload.segments)) {
    return normalizeSplicedResponse(payload)
  }

  return normalizeRiverSeriesResponse(payload as RiverSeriesResponse)
}

function bindForecastIdentity(data: ForecastData, request: ForecastRequestIdentity, source: M11Source | null | undefined): ForecastData {
  if (data.segmentId && data.segmentId !== request.segmentId) {
    throw new Error(`预报曲线响应与请求河段不匹配：请求 ${request.segmentId}，返回 ${data.segmentId}。`)
  }

  return {
    ...data,
    segmentId: request.segmentId,
    basinVersionId: request.basinVersionId,
    riverNetworkVersionId: request.riverNetworkVersionId,
    source: source ?? null,
    cycle: request.issueTime ?? null,
  }
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

function isCurrentForecastRequest(state: ForecastState, request: ForecastRequestIdentity) {
  const activeForecastRequest = state.activeForecastRequest
  if (
    !activeForecastRequest ||
    activeForecastRequest.nonce !== request.nonce ||
    activeForecastRequest.segmentId !== request.segmentId ||
    activeForecastRequest.basinVersionId !== request.basinVersionId ||
    activeForecastRequest.riverNetworkVersionId !== request.riverNetworkVersionId ||
    activeForecastRequest.issueTime !== request.issueTime ||
    activeForecastRequest.scenarios !== request.scenarios ||
    activeForecastRequest.contextIssueTimeBound !== request.contextIssueTimeBound ||
    activeForecastRequest.contextScenariosBound !== request.contextScenariosBound
  ) {
    return false
  }

  if (
    state.requestNonce !== request.nonce ||
    state.selectedSegment?.segmentId !== request.segmentId ||
    state.selectedSegment.basinVersionId !== request.basinVersionId ||
    state.selectedSegment.riverNetworkVersionId !== request.riverNetworkVersionId
  ) {
    return false
  }

  if (request.contextIssueTimeBound || request.contextScenariosBound) {
    const activeRequestContext = state.activeRequestContext
    if (!activeRequestContext) return false
    if (request.contextIssueTimeBound && activeRequestContext.issueTime !== request.issueTime) return false
    if (
      request.contextScenariosBound &&
      selectedScenariosForSource(activeRequestContext.source, state.selectedScenarios).join(',') !== request.scenarios
    ) {
      return false
    }
  }

  return true
}

async function fetchForecastSeries(
  segment: ForecastSegmentInfo,
  includeAnalysis: boolean,
  selectedScenarios: string[],
  options: Pick<FetchForecastOptions, 'issueTime'> = {},
) {
  if (!segment.basinVersionId) {
    throw new Error('缺少 basin_version_id，无法请求河段预报')
  }
  if (!segment.riverNetworkVersionId) {
    throw new Error('缺少 river_network_version_id，无法请求河段预报')
  }

  const query: {
    river_network_version_id: string
    issue_time: string
    variables: string
    scenarios?: string
    include_analysis: boolean
  } = {
    river_network_version_id: segment.riverNetworkVersionId,
    issue_time: options.issueTime ?? 'latest',
    variables: 'q_down',
    include_analysis: includeAnalysis,
  }
  if (selectedScenarios.length > 0) {
    query.scenarios = selectedScenarios.join(',')
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
  activeRequestContext: null,
  activeForecastRequest: null,
  requestNonce: 0,
  selectSegment: (segment) =>
    set({
      selectedSegment: segment,
      forecastData: null,
      loading: false,
      error: null,
      activeForecastRequest: null,
    }),
  toggleScenario: (scenario) =>
    set((state) => {
      const normalizedScenario = scenario.toUpperCase()
      const isSelected = state.selectedScenarios.includes(normalizedScenario)
      if (isSelected && state.selectedScenarios.length === 1) {
        return { selectedScenarios: state.selectedScenarios }
      }

      const selectedScenarios = isSelected
        ? state.selectedScenarios.filter((selected) => selected !== normalizedScenario)
        : [...state.selectedScenarios, normalizedScenario]

      return {
        selectedScenarios,
        activeRequestContext: state.activeRequestContext ? { ...state.activeRequestContext, source: null } : null,
      }
    }),
  setRequestContext: (context) =>
    set((state) => {
      const selectedScenarios = selectedScenariosForSource(context?.source, state.selectedScenarios)
      return {
        activeRequestContext: context,
        selectedScenarios,
      }
    }),
  fetchForecast: async (options) => {
    const segment = get().selectedSegment
    if (!segment) return

    const activeRequestContext = options?.ignoreActiveRequestContext ? null : get().activeRequestContext
    const includeAnalysis = options?.includeAnalysis ?? get().includeAnalysis
    const source = options?.useSelectedScenarios ? null : (options?.source ?? activeRequestContext?.source)
    const selectedScenarios = selectedScenariosForSource(source, get().selectedScenarios)
    const issueTime = options?.issueTime ?? activeRequestContext?.issueTime
    const contextIssueTimeBound = Boolean(activeRequestContext) && options?.issueTime == null
    const contextScenariosBound =
      Boolean(activeRequestContext) && !options?.useSelectedScenarios && options?.source == null
    const requestNonce = get().requestNonce + 1
    const request: ForecastRequestIdentity = {
      nonce: requestNonce,
      segmentId: segment.segmentId,
      basinVersionId: segment.basinVersionId,
      riverNetworkVersionId: segment.riverNetworkVersionId,
      issueTime,
      scenarios: selectedScenarios.join(','),
      contextIssueTimeBound,
      contextScenariosBound,
    }
    set({ loading: true, error: null, forecastData: null, includeAnalysis, requestNonce, activeForecastRequest: request })

    try {
      const payload = await fetchForecastSeries(segment, includeAnalysis, selectedScenarios, { issueTime })
      const state = get()
      if (!isCurrentForecastRequest(state, request)) return

      const forecastData = bindForecastIdentity(normalizeForecastPayload(payload), request, source)
      set({
        forecastData,
        loading: false,
        error: null,
      })
    } catch (error) {
      const state = get()
      if (!isCurrentForecastRequest(state, request)) return
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
      activeForecastRequest: null,
    }),
  setLoading: (loading) => set({ loading }),
  setError: (error) => set({ error }),
}))

function selectedScenariosForSource(source: M11Source | null | undefined, selectedScenarios: string[]) {
  if (source === 'ifs') return ['IFS']
  if (source === 'compare') return ['GFS', 'IFS']
  if (source === 'gfs') return ['GFS']
  if (source === 'best') return []
  return selectedScenarios.length > 0 ? selectedScenarios : ['GFS']
}

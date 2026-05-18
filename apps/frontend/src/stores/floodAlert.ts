import { create } from 'zustand'

import { buildApiUrl } from '@/api/base'
import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'
import type { AlertLevel } from '@/components/flood/alertLevels'
import { isAlertLevel } from '@/components/flood/alertLevels'

export type FloodAlertSortBy = 'return_period_desc' | 'q_value_desc'
export type AlertThreshold = 'Q2' | 'Q5' | 'Q10' | 'Q20' | 'Q50' | 'Q100'
type ApiHydroRun = components['schemas']['HydroRun']
type ApiHydroRunPage = components['schemas']['HydroRunPage']
type ApiFloodAlertSummary = components['schemas']['FloodAlertSummary']
type ApiFloodAlertRanking = components['schemas']['FloodAlertRanking']
type ApiFloodAlertRankingItem = components['schemas']['FloodAlertRankingItem']
type ApiFloodAlertTimeline = components['schemas']['FloodAlertTimeline']
type ApiFloodAlertTimelinePoint = components['schemas']['FloodAlertTimelinePoint']
type ApiFloodFrequencyThresholds = components['schemas']['FloodFrequencyThresholds']

export interface FloodAlertLevelCount {
  level: AlertLevel
  count: number
  color: string
}

export interface FloodAlertSummary {
  runId: string
  levels: FloodAlertLevelCount[]
  totalSegments: number
  usableCurves: number
  unavailableCount: number
  qualityNote?: string | null
  updatedAt?: string | null
}

export interface FloodAlertRankingItem {
  rank: number
  riverSegmentId: string
  segmentId: string
  segmentName?: string | null
  basinVersionId?: string | null
  basinName?: string | null
  qValue?: number | null
  qUnit?: string | null
  returnPeriod?: number | null
  warningLevel?: AlertLevel | null
  duration?: string | null
  validTime?: string | null
  geomCentroid?: { type: 'Point'; coordinates: [number, number] } | null
}

export interface FloodAlertRanking {
  items: FloodAlertRankingItem[]
  total: number
  limit: number
  offset: number
}

export interface FloodAlertTimelinePoint {
  validTime: string
  returnPeriod?: number | null
  warningLevel?: AlertLevel | null
  qValue?: number | null
}

export type FloodFrequencyThresholds = ApiFloodFrequencyThresholds & {
  q2?: number | null
  q5?: number | null
  q10?: number | null
  q20?: number | null
  q50?: number | null
  q100?: number | null
}

export interface FloodAlertTimeline {
  runId: string
  segmentId: string
  riverSegmentId: string
  timesteps: FloodAlertTimelinePoint[]
  peak?: FloodAlertTimelinePoint | null
  frequencyThresholds?: FloodFrequencyThresholds | null
  qualityNote?: string | null
}

interface FloodAlertState {
  selectedRunId: string | null
  latestRun: ApiHydroRun | null
  alertThreshold: AlertThreshold | null
  selectedAlertLevel: AlertLevel | null
  selectedValidTime: string | null
  sortBy: FloodAlertSortBy
  topLimit: 10 | 20 | 50
  basinId: string
  timelineData: FloodAlertTimeline | null
  summaryData: FloodAlertSummary | null
  rankingData: FloodAlertRanking | null
  validTimes: string[]
  loading: boolean
  summaryLoading: boolean
  rankingLoading: boolean
  timelineLoading: boolean
  error: string | null
  empty: boolean
  setSelectedRunId: (runId: string | null) => void
  setAlertThreshold: (threshold: AlertThreshold | null) => void
  setSelectedAlertLevel: (level: AlertLevel | null) => void
  setSelectedValidTime: (validTime: string | null) => void
  setTopLimit: (limit: 10 | 20 | 50) => void
  setBasinId: (basinId: string) => void
  fetchLatestFrequencyDoneRun: (context?: { source?: string | null; cycleTime?: string | null; validTime?: string | null }) => Promise<void>
  fetchSummary: (options?: { validTime?: string | null }) => Promise<void>
  fetchRanking: (options?: { validTime?: string | null; limit?: 10 | 20 | 50 }) => Promise<void>
  fetchTimeline: (segmentId: string) => Promise<void>
}

async function fetchJson<T>(path: string, query: Record<string, string | number | boolean | null | undefined>) {
  const params = new URLSearchParams()
  Object.entries(query).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
  })

  const response = await fetch(buildApiUrl(`${path}?${params.toString()}`))
  const payload = await response.json().catch(() => null)
  if (!response.ok) throw new Error(getApiErrorMessage(payload, response.statusText || '请求失败'))
  return unwrapApiData<T>(payload, '请求失败')
}

function numberOrNull(value: number | string | null | undefined): number | null {
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric : null
}

function stringOrNull(value: string | null | undefined): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null
}

function normalizeLevel(value: string | null | undefined): AlertLevel | null {
  return isAlertLevel(value) ? value : null
}

function normalizeSummary(payload: ApiFloodAlertSummary): FloodAlertSummary {
  return {
    runId: payload.run_id,
    levels: payload.levels
      .map((row) => ({
        level: normalizeLevel(row.level),
        count: row.count,
        color: row.color,
      }))
      .filter((row): row is FloodAlertLevelCount => row.level !== null),
    totalSegments: payload.total_segments,
    usableCurves: payload.usable_curves,
    unavailableCount: payload.unavailable_count,
    qualityNote: stringOrNull(payload.quality_note),
    updatedAt: null,
  }
}

function normalizeRankingItem(item: ApiFloodAlertRankingItem, index: number): FloodAlertRankingItem {
  const segmentId = item.river_segment_id || item.segment_id
  return {
    rank: item.rank ?? index + 1,
    riverSegmentId: segmentId,
    segmentId: item.segment_id || segmentId,
    segmentName: stringOrNull(item.segment_name),
    basinVersionId: stringOrNull(item.basin_version_id),
    basinName: null,
    qValue: numberOrNull(item.q_value),
    qUnit: stringOrNull(item.q_unit) ?? 'm3/s',
    returnPeriod: numberOrNull(item.return_period),
    warningLevel: normalizeLevel(item.warning_level),
    duration: stringOrNull(item.duration),
    validTime: stringOrNull(item.valid_time),
    geomCentroid: null,
  }
}

function normalizeRanking(payload: ApiFloodAlertRanking, fallbackLimit: number): FloodAlertRanking {
  const items = payload.items.map(normalizeRankingItem)
  return {
    items,
    total: payload.total,
    limit: payload.limit ?? fallbackLimit,
    offset: payload.offset,
  }
}

function normalizeTimelinePoint(point: ApiFloodAlertTimelinePoint): FloodAlertTimelinePoint {
  return {
    validTime: point.valid_time,
    returnPeriod: numberOrNull(point.return_period),
    warningLevel: normalizeLevel(point.warning_level),
    qValue: numberOrNull(point.q_value),
  }
}

function normalizeFrequencyThresholds(
  thresholds: ApiFloodFrequencyThresholds | null | undefined,
): FloodFrequencyThresholds | null {
  if (!thresholds) return null
  return {
    ...thresholds,
    q2: numberOrNull(thresholds.Q2),
    q5: numberOrNull(thresholds.Q5),
    q10: numberOrNull(thresholds.Q10),
    q20: numberOrNull(thresholds.Q20),
    q50: numberOrNull(thresholds.Q50),
    q100: numberOrNull(thresholds.Q100),
  }
}

function normalizeTimeline(payload: ApiFloodAlertTimeline): FloodAlertTimeline {
  const timesteps = payload.timesteps.map(normalizeTimelinePoint)
  return {
    runId: payload.run_id,
    segmentId: payload.segment_id,
    riverSegmentId: payload.river_segment_id,
    timesteps,
    peak: payload.peak ? normalizeTimelinePoint(payload.peak) : null,
    frequencyThresholds: normalizeFrequencyThresholds(payload.frequency_thresholds),
    qualityNote: stringOrNull(payload.quality_note),
  }
}

function buildValidTimes(run: ApiHydroRun | null) {
  if (!run) return []
  const start = Date.parse(run.start_time)
  const end = Date.parse(run.end_time)
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return []

  const hours = Math.max(1, Math.round((end - start) / 3_600_000))
  const stepHours = hours > 96 ? 6 : hours > 48 ? 3 : 1
  const values: string[] = []
  for (let time = start; time <= end && values.length < 128; time += stepHours * 3_600_000) {
    values.push(new Date(time).toISOString())
  }
  if (values.at(-1) !== new Date(end).toISOString()) values.push(new Date(end).toISOString())
  return values
}

function mergeTimelineValidTimes(existing: string[], timeline: FloodAlertTimeline) {
  const merged = new Set(existing)
  timeline.timesteps.forEach((point) => {
    if (point.validTime) merged.add(point.validTime)
  })
  return [...merged].sort((a, b) => Date.parse(a) - Date.parse(b))
}

function sortLatestRuns(runs: ApiHydroRun[]) {
  return [...runs].sort((a, b) => {
    const bTime = Date.parse(b.cycle_time ?? b.created_at ?? b.updated_at)
    const aTime = Date.parse(a.cycle_time ?? a.created_at ?? a.updated_at)
    return (Number.isFinite(bTime) ? bTime : 0) - (Number.isFinite(aTime) ? aTime : 0)
  })
}

function normalizeIso(value: string | null | undefined) {
  if (!value) return null
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : null
}

function sourceMatches(run: ApiHydroRun, source: string | null | undefined) {
  if (!source) return true
  return run.source_id?.toLowerCase() === source.toLowerCase()
}

function cycleMatches(run: ApiHydroRun, cycleTime: string | null | undefined) {
  const normalized = normalizeIso(cycleTime)
  if (!normalized) return true
  return normalizeIso(run.cycle_time) === normalized
}

function sourceForRunsQuery(source: string | null | undefined) {
  if (!source) return undefined
  if (source.toLowerCase() === 'ifs') return 'IFS'
  if (source.toLowerCase() === 'gfs') return 'GFS'
  return source
}

function explicitContextMissReason(context: { source?: string | null; cycleTime?: string | null } | undefined) {
  const source = context?.source ? context.source.toUpperCase() : null
  const cycleTime = normalizeIso(context?.cycleTime)
  if (source && cycleTime) return `未找到 ${source} 周期 ${cycleTime} 的已完成洪水预警 Run。`
  if (source) return `未找到 ${source} 的已完成洪水预警 Run。`
  if (cycleTime) return `未找到周期 ${cycleTime} 的已完成洪水预警 Run。`
  return null
}

export const useFloodAlertStore = create<FloodAlertState>((set, get) => ({
  selectedRunId: null,
  latestRun: null,
  alertThreshold: null,
  selectedAlertLevel: null,
  selectedValidTime: null,
  sortBy: 'return_period_desc',
  topLimit: 20,
  basinId: '',
  timelineData: null,
  summaryData: null,
  rankingData: null,
  validTimes: [],
  loading: false,
  summaryLoading: false,
  rankingLoading: false,
  timelineLoading: false,
  error: null,
  empty: false,
  setSelectedRunId: (runId) => set({ selectedRunId: runId, timelineData: null }),
  setAlertThreshold: (threshold) => set({ alertThreshold: threshold }),
  setSelectedAlertLevel: (level) =>
    set((state) => ({ selectedAlertLevel: state.selectedAlertLevel === level ? null : level })),
  setSelectedValidTime: (validTime) => set({ selectedValidTime: validTime }),
  setTopLimit: (limit) => set({ topLimit: limit }),
  setBasinId: (basinId) => set({ basinId }),
  fetchLatestFrequencyDoneRun: async (context) => {
    set({ loading: true, error: null, empty: false })
    try {
      const explicitContext = Boolean(context?.source || context?.cycleTime)
      const runsQuery: {
        source?: string
        cycle_time?: string
        status: 'frequency_done'
        limit: number
      } = {
        status: 'frequency_done',
        limit: 50,
      }
      const source = sourceForRunsQuery(context?.source)
      const cycleTime = normalizeIso(context?.cycleTime)
      if (source) runsQuery.source = source
      if (cycleTime) runsQuery.cycle_time = cycleTime
      const { data, error } = await client.GET('/api/v1/runs', {
        params: { query: runsQuery },
      })
      if (error) throw new Error(getApiErrorMessage(error, '获取最新预警 Run 失败'))
      const payload = unwrapApiData<ApiHydroRunPage>(data, '获取最新预警 Run 失败')
      const runs = payload.items
      const matchingRuns = runs.filter((run) => sourceMatches(run, context?.source) && cycleMatches(run, context?.cycleTime))
      const candidates = explicitContext ? matchingRuns : runs
      const latestRun = sortLatestRuns(candidates).find((run) => run.run_type === 'forecast') ?? sortLatestRuns(candidates)[0] ?? null
      const validTimes = buildValidTimes(latestRun)
      const requestedValidTime = normalizeIso(context?.validTime)
      const contextMissReason = latestRun ? null : explicitContextMissReason(context)
      set({
        latestRun,
        selectedRunId: latestRun?.run_id ?? null,
        validTimes,
        selectedValidTime: requestedValidTime && validTimes.includes(requestedValidTime) ? requestedValidTime : null,
        loading: false,
        empty: latestRun === null,
        error: contextMissReason,
        summaryData: latestRun ? get().summaryData : null,
        rankingData: latestRun ? get().rankingData : null,
        timelineData: latestRun ? get().timelineData : null,
      })
    } catch (error) {
      const message = getApiErrorMessage(error, '获取最新预警 Run 失败')
      set({ loading: false, error: message, empty: false })
      throw error
    }
  },
  fetchSummary: async (options) => {
    const runId = get().selectedRunId
    if (!runId) return

    const validTime = options?.validTime ?? get().selectedValidTime
    set({ summaryLoading: true, error: null })
    try {
      const payload = await fetchJson<ApiFloodAlertSummary>('/api/v1/flood-alerts/summary', {
        run_id: runId,
        threshold: get().alertThreshold,
        valid_time: validTime,
      })
      set({ summaryData: normalizeSummary(payload), summaryLoading: false, error: null })
    } catch (error) {
      const message = getApiErrorMessage(error, '预警统计加载失败')
      set({ summaryLoading: false, error: message })
      throw error
    }
  },
  fetchRanking: async (options) => {
    const runId = get().selectedRunId
    if (!runId) return

    const validTime = options?.validTime ?? get().selectedValidTime
    const limit = options?.limit ?? get().topLimit
    set({ rankingLoading: true, error: null })
    try {
      const payload = await fetchJson<ApiFloodAlertRanking>('/api/v1/flood-alerts/ranking', {
        run_id: runId,
        limit,
        offset: 0,
        basin_id: get().basinId,
        valid_time: validTime,
      })
      set({ rankingData: normalizeRanking(payload, limit), rankingLoading: false, error: null })
    } catch (error) {
      const message = getApiErrorMessage(error, '预警排名加载失败')
      set({ rankingLoading: false, error: message })
      throw error
    }
  },
  fetchTimeline: async (segmentId) => {
    const runId = get().selectedRunId
    if (!runId) return

    set({ timelineLoading: true, error: null })
    try {
      const payload = await fetchJson<ApiFloodAlertTimeline>('/api/v1/flood-alerts/timeline', {
        run_id: runId,
        segment_id: segmentId,
      })
      const timeline = normalizeTimeline(payload)
      set((state) => ({
        timelineData: timeline,
        validTimes: mergeTimelineValidTimes(state.validTimes, timeline),
        timelineLoading: false,
        error: null,
      }))
    } catch (error) {
      const message = getApiErrorMessage(error, '河段预警详情加载失败')
      set({ timelineLoading: false, error: message })
      throw error
    }
  },
}))

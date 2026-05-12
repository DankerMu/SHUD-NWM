import { create } from 'zustand'

import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'
import type { AlertLevel } from '@/components/flood/alertLevels'
import { ALERT_LEVELS, isAlertLevel } from '@/components/flood/alertLevels'

export type FloodAlertSortBy = 'return_period_desc' | 'q_value_desc'
export type AlertThreshold = 'Q2' | 'Q5' | 'Q10' | 'Q20' | 'Q50' | 'Q100'

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

export interface FloodFrequencyThresholds {
  Q2?: number | null
  Q5?: number | null
  Q10?: number | null
  Q20?: number | null
  Q50?: number | null
  Q100?: number | null
  q2?: number | null
  q5?: number | null
  q10?: number | null
  q20?: number | null
  q50?: number | null
  q100?: number | null
  sample_quality?: Record<string, unknown> | null
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
  latestRun: components['schemas']['HydroRun'] | null
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
  fetchLatestFrequencyDoneRun: () => Promise<void>
  fetchSummary: (options?: { validTime?: string | null }) => Promise<void>
  fetchRanking: (options?: { validTime?: string | null; limit?: 10 | 20 | 50 }) => Promise<void>
  fetchTimeline: (segmentId: string) => Promise<void>
}

async function fetchJson<T>(path: string, query: Record<string, string | number | boolean | null | undefined>) {
  const params = new URLSearchParams()
  Object.entries(query).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
  })

  const response = await fetch(`${path}?${params.toString()}`)
  const payload = await response.json().catch(() => null)
  if (!response.ok) throw new Error(getApiErrorMessage(payload, response.statusText || '请求失败'))
  return unwrapApiData<T>(payload, '请求失败')
}

function numberOrNull(value: unknown): number | null {
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric : null
}

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null
}

function normalizeLevel(value: unknown): AlertLevel | null {
  return isAlertLevel(value) ? value : null
}

function normalizeSummary(payload: unknown): FloodAlertSummary {
  const record = (payload ?? {}) as Record<string, unknown>
  const counts = (record.alert_counts ?? {}) as Record<string, unknown>
  const levelRows = Array.isArray(record.levels)
    ? (record.levels as Array<Record<string, unknown>>)
    : ALERT_LEVELS.map((level) => ({ level, count: counts[level], color: undefined }))

  return {
    runId: String(record.run_id ?? ''),
    levels: levelRows
      .map((row) => ({
        level: normalizeLevel(row.level),
        count: Number(row.count ?? 0),
        color: String(row.color ?? ''),
      }))
      .filter((row): row is FloodAlertLevelCount => row.level !== null),
    totalSegments: Number(record.total_segments ?? 0),
    usableCurves: Number(record.usable_curves ?? record.total_segments ?? 0),
    unavailableCount: Number(record.unavailable_count ?? 0),
    qualityNote: stringOrNull(record.quality_note),
    updatedAt: stringOrNull(record.updated_at),
  }
}

function normalizeRankingItem(item: Record<string, unknown>, index: number): FloodAlertRankingItem {
  const segmentId = String(item.river_segment_id ?? item.segment_id ?? '')
  const centroid = item.geom_centroid as { type?: string; coordinates?: unknown } | null | undefined
  const coordinates = Array.isArray(centroid?.coordinates)
    ? centroid.coordinates.map(Number).filter(Number.isFinite)
    : []

  return {
    rank: Number(item.rank ?? index + 1),
    riverSegmentId: segmentId,
    segmentId: String(item.segment_id ?? segmentId),
    segmentName: stringOrNull(item.segment_name ?? item.name),
    basinVersionId: stringOrNull(item.basin_version_id),
    basinName: stringOrNull(item.basin_name ?? item.basin_id),
    qValue: numberOrNull(item.q_value),
    qUnit: stringOrNull(item.q_unit) ?? 'm3/s',
    returnPeriod: numberOrNull(item.return_period ?? item.max_return_period),
    warningLevel: normalizeLevel(item.warning_level ?? item.severity),
    duration: stringOrNull(item.duration),
    validTime: stringOrNull(item.valid_time),
    geomCentroid:
      centroid?.type === 'Point' && coordinates.length >= 2
        ? { type: 'Point', coordinates: [coordinates[0], coordinates[1]] }
        : null,
  }
}

function normalizeRanking(payload: unknown, fallbackLimit: number): FloodAlertRanking {
  const record = (payload ?? {}) as Record<string, unknown>
  const sourceItems = Array.isArray(record.items) ? record.items : Array.isArray(payload) ? payload : []
  const items = (sourceItems as Array<Record<string, unknown>>).map(normalizeRankingItem)
  return {
    items,
    total: Number(record.total ?? items.length),
    limit: Number(record.limit ?? fallbackLimit),
    offset: Number(record.offset ?? 0),
  }
}

function normalizeTimelinePoint(point: Record<string, unknown>): FloodAlertTimelinePoint {
  return {
    validTime: String(point.valid_time ?? point.validTime ?? ''),
    returnPeriod: numberOrNull(point.return_period),
    warningLevel: normalizeLevel(point.warning_level ?? point.severity),
    qValue: numberOrNull(point.q_value),
  }
}

function normalizeTimeline(payload: unknown): FloodAlertTimeline {
  const record = (payload ?? {}) as Record<string, unknown>
  const sourcePoints = Array.isArray(record.timesteps)
    ? record.timesteps
    : Array.isArray(record.timeline)
      ? record.timeline
      : Array.isArray(record.points)
        ? record.points
        : []
  const timesteps = (sourcePoints as Array<Record<string, unknown>>).map(normalizeTimelinePoint)
  return {
    runId: String(record.run_id ?? ''),
    segmentId: String(record.segment_id ?? record.river_segment_id ?? ''),
    riverSegmentId: String(record.river_segment_id ?? record.segment_id ?? ''),
    timesteps,
    peak: record.peak && typeof record.peak === 'object' ? normalizeTimelinePoint(record.peak as Record<string, unknown>) : null,
    frequencyThresholds: (record.frequency_thresholds as FloodFrequencyThresholds | undefined) ?? null,
    qualityNote: stringOrNull(record.quality_note),
  }
}

function buildValidTimes(run: components['schemas']['HydroRun'] | null) {
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

function sortLatestRuns(runs: components['schemas']['HydroRun'][]) {
  return [...runs].sort((a, b) => {
    const bTime = Date.parse(b.cycle_time ?? b.created_at ?? b.updated_at)
    const aTime = Date.parse(a.cycle_time ?? a.created_at ?? a.updated_at)
    return (Number.isFinite(bTime) ? bTime : 0) - (Number.isFinite(aTime) ? aTime : 0)
  })
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
  fetchLatestFrequencyDoneRun: async () => {
    set({ loading: true, error: null, empty: false })
    try {
      const { data, error } = await client.GET('/api/v1/runs', {
        params: { query: { status: 'frequency_done', limit: 50 } },
      })
      if (error) throw new Error(getApiErrorMessage(error, '获取最新预警 Run 失败'))
      const payload = unwrapApiData<unknown>(data, '获取最新预警 Run 失败')
      const runs = Array.isArray(payload)
        ? (payload as components['schemas']['HydroRun'][])
        : (((payload as { items?: unknown[] } | null)?.items ?? []) as components['schemas']['HydroRun'][])
      const latestRun = sortLatestRuns(runs).find((run) => run.run_type === 'forecast') ?? sortLatestRuns(runs)[0] ?? null
      set({
        latestRun,
        selectedRunId: latestRun?.run_id ?? null,
        validTimes: buildValidTimes(latestRun),
        selectedValidTime: null,
        loading: false,
        empty: latestRun === null,
        error: null,
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
      const payload = await fetchJson<unknown>('/api/v1/flood-alerts/summary', {
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
      const payload = await fetchJson<unknown>('/api/v1/flood-alerts/ranking', {
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
      const payload = await fetchJson<unknown>('/api/v1/flood-alerts/timeline', {
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

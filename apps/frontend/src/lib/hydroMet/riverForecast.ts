import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'
import { createForecastPointBudgetGuard } from '@/lib/forecastRenderingBudget'
import { normalizeHydroMetCycle, type HydroMetSource } from '@/lib/hydroMet/queryState'
import {
  HYDRO_MET_STATION_SERIES_MESSAGE_STRING_LIMIT,
  HYDRO_MET_STATION_SERIES_UI_STRING_LIMIT,
  formatHydroMetStationSeriesContractValue,
  formatHydroMetStationSeriesMessage,
  formatHydroMetStationSeriesUiString,
  isHydroMetStationSeriesUiStringCapped,
  type HydroMetStationSeriesUiStringOptions,
} from '@/lib/hydroMet/stationSeries'

export const HYDRO_MET_RIVER_FORECAST_VARIABLE = 'q_down'
export const HYDRO_MET_RIVER_FORECAST_LIMIT = 480
export const HYDRO_MET_RIVER_FORECAST_SERIES_INSPECTION_LIMIT = 8
export const HYDRO_MET_RIVER_FORECAST_MESSAGE_LIMIT = 6
export const HYDRO_MET_RIVER_FORECAST_UNIT_LIMIT = 32

export type HydroMetRiverForecastPayload =
  | components['schemas']['RiverSeriesResponse']
  | components['schemas']['SplicedForecastResponse']

export interface HydroMetRiverForecastProductIdentity {
  basin_version_id: string
  river_network_version_id: string
  source_id: HydroMetSource
  cycle_time: string
  river_valid_time_start: string | null
  river_valid_time_end: string | null
  valid_time_start: string | null
  valid_time_end: string | null
  available_horizon_hours: number | null
  expected_horizon_hours: number
  shorter_horizon: boolean
}

export interface HydroMetRiverForecastSegmentIdentity {
  river_segment_id: string
  segment_id: string
  river_network_version_id: string
  basin_version_id: string
  name: string
}

export interface HydroMetRiverForecastRequest {
  product: HydroMetRiverForecastProductIdentity
  segment: HydroMetRiverForecastSegmentIdentity
}

export interface HydroMetRiverForecastPoint {
  timestamp: number
  value: number
}

export interface HydroMetRiverForecastSeries {
  scenarioId: string
  sourceId: string | null
  cycleTime: string | null
  availableLeadHours: number | null
  role: string | null
  points: HydroMetRiverForecastPoint[]
  validTimeStart: string | null
  validTimeEnd: string | null
  pointCount: number
}

export type HydroMetRiverForecastValidation =
  | {
      ok: true
      segmentId: string
      variable: string
      unit: string
      issueTime: string | null
      sourceId: string
      scenarioId: string
      cycleTime: string | null
      validTimeStart: string
      validTimeEnd: string
      pointCount: number
      inspectedPointCount: number
      renderedPoints: HydroMetRiverForecastPoint[]
      capped: boolean
      horizonLabel: string
      horizonShorter: boolean
      series: HydroMetRiverForecastSeries
    }
  | { ok: false; messages: string[] }

const HYDRO_MET_RIVER_FORECAST_SCENARIOS: Record<HydroMetSource, string> = {
  GFS: 'forecast_gfs_deterministic',
  IFS: 'forecast_ifs_deterministic',
}

const HOUR_MS = 60 * 60 * 1000

export function hydroMetRiverScenarioForSource(source: HydroMetSource) {
  return HYDRO_MET_RIVER_FORECAST_SCENARIOS[source]
}

export function riverForecastRequestKey(product: HydroMetRiverForecastProductIdentity, segmentId: string) {
  return [
    product.basin_version_id,
    product.river_network_version_id,
    product.source_id,
    normalizeHydroMetCycle(product.cycle_time) ?? product.cycle_time,
    segmentId,
  ].join('|')
}

export async function loadHydroMetRiverForecast({
  product,
  segment,
}: HydroMetRiverForecastRequest): Promise<HydroMetRiverForecastPayload> {
  try {
    const segmentId = segment.river_segment_id || segment.segment_id
    const issueTime = normalizeHydroMetCycle(product.cycle_time) ?? product.cycle_time
    const { data, error } = await client.GET(
      '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series',
      {
        params: {
          path: {
            basin_version_id: product.basin_version_id,
            segment_id: segmentId,
          },
          query: {
            river_network_version_id: product.river_network_version_id,
            issue_time: issueTime || 'latest',
            variables: HYDRO_MET_RIVER_FORECAST_VARIABLE,
            scenarios: hydroMetRiverScenarioForSource(product.source_id),
            include_analysis: false,
          },
        },
      },
    )

    if (error) throw new Error(formatHydroMetRiverForecastMessage(error, 'river forecast-series 不可用'))
    const response = unwrapApiData<HydroMetRiverForecastPayload>(data, 'river forecast-series 不可用')
    if (!isRecord(response)) throw new Error('river forecast-series 响应不完整')
    if (!Array.isArray(response.series) && !Array.isArray(response.segments)) {
      throw new Error('river forecast-series series 缺失或格式无效')
    }
    return response
  } catch (error) {
    throw new Error(formatHydroMetRiverForecastMessage(error, 'river forecast-series 不可用'))
  }
}

export function validateHydroMetRiverForecastForChart(
  response: HydroMetRiverForecastPayload,
  product: HydroMetRiverForecastProductIdentity,
  segment: HydroMetRiverForecastSegmentIdentity,
): HydroMetRiverForecastValidation {
  if (!isRecord(response)) return { ok: false, messages: ['river forecast-series 响应格式无效'] }

  const messages: string[] = []
  const expectedSegmentId = segment.river_segment_id || segment.segment_id
  const responseSegmentId = responseSegmentIdentity(response)
  if (typeof responseSegmentId !== 'string') {
    messages.push('river_segment_id 元数据格式无效')
  } else if (responseSegmentId !== expectedSegmentId) {
    messages.push(`river_segment_id=${formatHydroMetRiverForecastContractValue(responseSegmentId)} 与当前选择 ${formatHydroMetRiverForecastContractValue(expectedSegmentId)} 不一致`)
  }

  const variableValue = response.variable
  if (variableValue !== undefined && variableValue !== null && variableValue !== HYDRO_MET_RIVER_FORECAST_VARIABLE) {
    messages.push(`variable=${formatHydroMetRiverForecastContractValue(variableValue)} 不是 q_down`)
  }

  const unit = parseRiverForecastUnit(response.unit, messages)
  const issueTime = parseOptionalRiverForecastTime(response.issue_time, 'issue_time', messages)
  const seriesItems = responseSeriesItems(response, messages)
  if (seriesItems.length > HYDRO_MET_RIVER_FORECAST_SERIES_INSPECTION_LIMIT) {
    messages.push(`river forecast-series series 数量 ${seriesItems.length} 超过前端检查上限 ${HYDRO_MET_RIVER_FORECAST_SERIES_INSPECTION_LIMIT}，已停止绘图。`)
  }

  const expectedScenario = hydroMetRiverScenarioForSource(product.source_id)
  const normalizedSeries = seriesItems
    .slice(0, HYDRO_MET_RIVER_FORECAST_SERIES_INSPECTION_LIMIT)
    .map((series, index) => normalizeRiverForecastSeries(series, index, product, expectedScenario, messages))
    .filter((series): series is HydroMetRiverForecastSeries => Boolean(series))

  if (messages.length > 0) return { ok: false, messages: capHydroMetRiverForecastMessages(messages) }

  const selectedSeries = selectRiverForecastSeries(normalizedSeries, product.source_id, expectedScenario)
  if (!selectedSeries) {
    return {
      ok: false,
      messages: [`响应中缺少 ${product.source_id} ${expectedScenario} 的 q_down river discharge series。`],
    }
  }

  if (!unit) {
    return { ok: false, messages: ['q_down river discharge 缺少 unit 元数据，停止绘图。'] }
  }

  if (selectedSeries.points.length === 0) {
    return { ok: false, messages: ['q_down river discharge 没有可绘制点。'] }
  }

  const validTimeStart = selectedSeries.validTimeStart
  const validTimeEnd = selectedSeries.validTimeEnd
  if (!validTimeStart || !validTimeEnd) {
    return { ok: false, messages: ['q_down river discharge valid-time range 缺失。'] }
  }

  const expectedHorizonHours = product.expected_horizon_hours
  const horizonHours = horizonHoursFromSeries(selectedSeries, issueTime)
  const productHorizonHours = typeof product.available_horizon_hours === 'number' && Number.isFinite(product.available_horizon_hours)
    ? product.available_horizon_hours
    : null
  const horizonShorter = Boolean(product.shorter_horizon) || (
    productHorizonHours !== null &&
    Number.isFinite(expectedHorizonHours) &&
    productHorizonHours < expectedHorizonHours
  ) || (
    horizonHours !== null &&
    Number.isFinite(expectedHorizonHours) &&
    horizonHours < expectedHorizonHours
  ) || (
    selectedSeries.availableLeadHours !== null &&
    Number.isFinite(expectedHorizonHours) &&
    selectedSeries.availableLeadHours < expectedHorizonHours
  )

  return {
    ok: true,
    segmentId: expectedSegmentId,
    variable: HYDRO_MET_RIVER_FORECAST_VARIABLE,
    unit,
    issueTime,
    sourceId: selectedSeries.sourceId ?? product.source_id,
    scenarioId: selectedSeries.scenarioId,
    cycleTime: selectedSeries.cycleTime,
    validTimeStart,
    validTimeEnd,
    pointCount: selectedSeries.pointCount,
    inspectedPointCount: selectedSeries.points.length,
    renderedPoints: selectedSeries.points,
    capped: selectedSeries.pointCount > selectedSeries.points.length,
    horizonLabel: buildRiverForecastHorizonLabel({
      validTimeEnd,
      horizonHours,
      productHorizonHours,
      expectedHorizonHours,
      availableLeadHours: selectedSeries.availableLeadHours,
    }),
    horizonShorter,
    series: selectedSeries,
  }
}

function responseSegmentIdentity(response: Record<string, unknown>) {
  return response.river_segment_id ?? response.segment_id
}

function responseSeriesItems(response: Record<string, unknown>, messages: string[]) {
  const seriesValue = response.series ?? response.segments
  if (!Array.isArray(seriesValue)) {
    messages.push('river forecast-series series 缺失或格式无效')
    return []
  }
  return seriesValue
}

function normalizeRiverForecastSeries(
  series: unknown,
  index: number,
  product: HydroMetRiverForecastProductIdentity,
  expectedScenario: string,
  messages: string[],
): HydroMetRiverForecastSeries | null {
  if (!isRecord(series)) {
    messages.push(`series[${index}] 不是对象，river forecast-series contract 无效`)
    return null
  }

  const scenarioId = parseRiverForecastString(series.scenario_id ?? series.scenario, `series[${index}].scenario_id`, messages)
  const sourceId = parseOptionalRiverForecastSource(series.source_id ?? series.source, `series[${index}].source_id`, messages)
  const cycleTime = parseOptionalRiverForecastTime(series.cycle_time, `series[${index}].cycle_time`, messages)
  const role = parseOptionalRiverForecastString(series.segment_role ?? series.role, `series[${index}].segment_role`, messages)
  const availableLeadHours = parseOptionalRiverForecastNumber(series.available_lead_hours, `series[${index}].available_lead_hours`, messages)
  const pointsValue = Array.isArray(series.points) ? series.points : series.data

  if (scenarioId && scenarioId !== expectedScenario) {
    messages.push(`series[${index}].scenario_id=${formatHydroMetRiverForecastContractValue(scenarioId)} 与 ${expectedScenario} 不一致`)
  }
  if (sourceId && sourceId !== product.source_id) {
    messages.push(`series[${index}].source_id=${formatHydroMetRiverForecastContractValue(sourceId)} 与 latest-product ${product.source_id} 不一致`)
  }
  if (cycleTime && normalizeHydroMetCycle(product.cycle_time) && cycleTime !== normalizeHydroMetCycle(product.cycle_time)) {
    messages.push(`series[${index}].cycle_time=${formatHydroMetRiverForecastContractValue(cycleTime)} 与 latest-product ${formatHydroMetRiverForecastContractValue(normalizeHydroMetCycle(product.cycle_time))} 不一致`)
  }
  if (!Array.isArray(pointsValue)) {
    messages.push(`series[${index}].points 缺失或格式无效`)
    return null
  }

  if (!scenarioId) return null

  const pointBudgetGuard = createForecastPointBudgetGuard(HYDRO_MET_RIVER_FORECAST_LIMIT)
  pointBudgetGuard.setSourceSeriesCount(1)
  pointBudgetGuard.setSourcePointCount(pointsValue.length)
  const retainedPoints = pointBudgetGuard.take(pointsValue)
  const pointMessages: string[] = []
  const points: HydroMetRiverForecastPoint[] = []
  let invalidPointCount = 0

  retainedPoints.forEach((point, pointIndex) => {
    const parsed = parseRiverForecastPoint(point)
    if (typeof parsed === 'string') {
      invalidPointCount += 1
      if (pointMessages.length < HYDRO_MET_RIVER_FORECAST_MESSAGE_LIMIT) {
        pointMessages.push(`series[${index}] 第 ${pointIndex + 1} 个点${parsed}`)
      }
      return
    }
    points.push(parsed)
  })

  if (invalidPointCount > 0) {
    messages.push(...capInvalidRiverForecastPointMessages(pointMessages, invalidPointCount, pointsValue.length, retainedPoints.length))
  }

  const sortedPoints = points.sort((left, right) => left.timestamp - right.timestamp)
  return {
    scenarioId,
    sourceId,
    cycleTime,
    availableLeadHours,
    role,
    points: sortedPoints,
    validTimeStart: sortedPoints[0] ? new Date(sortedPoints[0].timestamp).toISOString() : null,
    validTimeEnd: sortedPoints[sortedPoints.length - 1] ? new Date(sortedPoints[sortedPoints.length - 1].timestamp).toISOString() : null,
    pointCount: pointsValue.length,
  }
}

function parseRiverForecastPoint(point: unknown): HydroMetRiverForecastPoint | string {
  if (Array.isArray(point)) {
    if (point.length < 2) return '不是 [time,value] 二元组'
    return parseRiverForecastTimeValue(point[0], point[1])
  }
  if (!isRecord(point)) return '不是对象或二元组'
  return parseRiverForecastTimeValue(point.valid_time ?? point.time, point.value)
}

function parseRiverForecastTimeValue(timeValue: unknown, value: unknown): HydroMetRiverForecastPoint | string {
  const timestamp = timestampValue(timeValue)
  if (!Number.isFinite(timestamp)) {
    return `valid_time=${formatHydroMetRiverForecastContractValue(timeValue)} 不是有效 RFC3339 时间`
  }
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'value 不是有限数值'
  return { timestamp, value }
}

function timestampValue(value: unknown) {
  if (typeof value === 'number') return Number.isFinite(value) ? value : NaN
  if (typeof value !== 'string') return NaN
  const numeric = Number(value)
  if (Number.isFinite(numeric) && value.trim() !== '') return numeric
  const normalized = normalizeHydroMetCycle(value)
  return normalized ? Date.parse(normalized) : NaN
}

function selectRiverForecastSeries(
  series: HydroMetRiverForecastSeries[],
  source: HydroMetSource,
  scenario: string,
) {
  return series.find((item) => item.scenarioId === scenario && item.sourceId === source)
    ?? series.find((item) => item.scenarioId === scenario)
    ?? null
}

function parseRiverForecastUnit(unitValue: unknown, messages: string[]) {
  if (unitValue === undefined || unitValue === null) return null
  if (typeof unitValue !== 'string') {
    messages.push('q_down unit 格式无效')
    return null
  }
  if (isHydroMetStationSeriesUiStringCapped(unitValue, { limit: HYDRO_MET_RIVER_FORECAST_UNIT_LIMIT, fallback: '' })) {
    messages.push('q_down unit 过长，停止绘图')
    return null
  }
  return formatHydroMetStationSeriesUiString(unitValue, { limit: HYDRO_MET_RIVER_FORECAST_UNIT_LIMIT, fallback: '' }) || null
}

function parseRiverForecastString(value: unknown, field: string, messages: string[]) {
  if (typeof value === 'string' && value.trim()) return formatHydroMetStationSeriesUiString(value)
  messages.push(`${field} 缺失或格式无效`)
  return null
}

function parseOptionalRiverForecastString(value: unknown, field: string, messages: string[]) {
  if (value === undefined || value === null) return null
  if (typeof value !== 'string') {
    messages.push(`${field} 格式无效`)
    return null
  }
  return formatHydroMetStationSeriesUiString(value)
}

function parseOptionalRiverForecastSource(value: unknown, field: string, messages: string[]) {
  const parsed = parseOptionalRiverForecastString(value, field, messages)
  return parsed ? parsed.toUpperCase() : null
}

function parseOptionalRiverForecastTime(value: unknown, field: string, messages: string[]) {
  if (value === undefined || value === null) return null
  if (typeof value !== 'string') {
    messages.push(`${field} 格式无效`)
    return null
  }
  const normalized = normalizeHydroMetCycle(value)
  if (!normalized) {
    messages.push(`${field}=${formatHydroMetRiverForecastContractValue(value)} 不是有效 RFC3339 时间`)
    return null
  }
  return normalized
}

function parseOptionalRiverForecastNumber(value: unknown, field: string, messages: string[]) {
  if (value === undefined || value === null) return null
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    messages.push(`${field} 格式无效`)
    return null
  }
  return value
}

function horizonHoursFromSeries(series: HydroMetRiverForecastSeries, issueTime: string | null) {
  if (series.availableLeadHours !== null) return series.availableLeadHours
  const start = issueTime ?? series.cycleTime
  if (!start || !series.validTimeEnd) return null
  const startMs = Date.parse(start)
  const endMs = Date.parse(series.validTimeEnd)
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs < startMs) return null
  return Math.round((endMs - startMs) / HOUR_MS)
}

function buildRiverForecastHorizonLabel({
  validTimeEnd,
  horizonHours,
  productHorizonHours,
  expectedHorizonHours,
  availableLeadHours,
}: {
  validTimeEnd: string
  horizonHours: number | null
  productHorizonHours: number | null
  expectedHorizonHours: number
  availableLeadHours: number | null
}) {
  const actual = horizonHours ?? availableLeadHours ?? productHorizonHours
  const actualLabel = actual === null ? 'unknown horizon' : `${actual}h`
  return `actual available horizon ${actualLabel}; valid through ${validTimeEnd}; expected ${expectedHorizonHours}h`
}

function capHydroMetRiverForecastMessages(messages: string[]) {
  const safeMessages = messages.map((message) => (
    formatHydroMetRiverForecastMessage(message, 'river forecast-series contract 问题已截断')
  ))
  if (safeMessages.length <= HYDRO_MET_RIVER_FORECAST_MESSAGE_LIMIT) return safeMessages
  return [
    ...safeMessages.slice(0, HYDRO_MET_RIVER_FORECAST_MESSAGE_LIMIT),
    `另有 ${safeMessages.length - HYDRO_MET_RIVER_FORECAST_MESSAGE_LIMIT} 条 river forecast-series contract 问题已截断`,
  ]
}

function capInvalidRiverForecastPointMessages(
  messages: string[],
  invalidPointCount: number,
  reportedPointCount: number,
  inspectedPointCount: number,
) {
  const cappedMessages = messages.slice(0, Math.max(1, HYDRO_MET_RIVER_FORECAST_MESSAGE_LIMIT - 2))
  const hiddenCount = Math.max(0, invalidPointCount - cappedMessages.length)
  if (hiddenCount > 0) cappedMessages.push(`另有 ${hiddenCount} 个已检查 q_down 点无效，错误详情已截断`)
  if (reportedPointCount > inspectedPointCount) {
    cappedMessages.push(`q_down capped 仅检查前 ${inspectedPointCount}/${reportedPointCount} 个点，响应过大，已停止继续校验`)
  }
  return cappedMessages
}

export function formatHydroMetRiverForecastMessage(value: unknown, fallback = 'river forecast-series 不可用') {
  return formatHydroMetStationSeriesMessage(value, fallback)
}

export function formatHydroMetRiverForecastUiString(
  value: string,
  options: HydroMetStationSeriesUiStringOptions = {},
) {
  return formatHydroMetStationSeriesUiString(value, {
    limit: options.limit ?? HYDRO_MET_STATION_SERIES_UI_STRING_LIMIT,
    fallback: options.fallback,
    oversizeReplacement: options.oversizeReplacement,
  })
}

function formatHydroMetRiverForecastContractValue(value: unknown) {
  return formatHydroMetStationSeriesContractValue(value, {
    limit: HYDRO_MET_STATION_SERIES_MESSAGE_STRING_LIMIT,
    fallback: 'invalid',
  })
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

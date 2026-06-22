import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'
import { normalizeHydroMetCycle } from '@/lib/hydroMet/queryState'
import { sanitizeHydroMetMessage } from '@/lib/hydroMet/runtime'

export const HYDRO_MET_STATION_SERIES_DISPLAY_LIMIT = 240
export const HYDRO_MET_STATION_SERIES_API_TUPLE_LIMIT = 1600
export const HYDRO_MET_STATION_VARIABLES = ['PRCP', 'TEMP', 'RH', 'wind', 'Rn'] as const
export const HYDRO_MET_STATION_SERIES_UI_STRING_LIMIT = 96
export const HYDRO_MET_STATION_SERIES_MESSAGE_STRING_LIMIT = 180
export const HYDRO_MET_STATION_SERIES_RETAINED_DISK_MISS_CODE = 'STATION_FORCING_FILE_NOT_FOUND'

const HYDRO_MET_STATION_SERIES_TRUNCATION_MARKER = '...'
const HYDRO_MET_STATION_SERIES_MESSAGE_TOKEN_LIMIT = 72
const HYDRO_MET_STATION_SERIES_OVERSIZED_TOKEN = '过长内容已截断'

export type HydroMetStationSeriesVariable = (typeof HYDRO_MET_STATION_VARIABLES)[number]
export type HydroMetStationSeriesResponse = components['schemas']['StationSeriesResponse']
export type HydroMetStationSeries = components['schemas']['StationSeries']
export type HydroMetStationSeriesPoint = components['schemas']['StationSeriesPoint']

export interface HydroMetStationSeriesProductIdentity {
  forcing_version_id: string
  model_id: string
  source_id: string
  cycle_time: string
}

export interface HydroMetStationSeriesInventoryStation {
  station_id: string
}

export interface HydroMetStationSeriesRequest {
  product: HydroMetStationSeriesProductIdentity
  station: HydroMetStationSeriesInventoryStation
  limit?: number
}

export class HydroMetStationSeriesError extends Error {
  readonly code: string | null
  readonly details: unknown

  constructor(message: string, options: { code?: string | null; details?: unknown } = {}) {
    super(message)
    this.name = 'HydroMetStationSeriesError'
    this.code = options.code ?? null
    this.details = options.details
  }
}

export interface HydroMetStationSeriesUiStringOptions {
  limit?: number
  fallback?: string
  oversizeReplacement?: string
}

export function boundedHydroMetStationSeriesApiLimit(value: number | null | undefined) {
  if (!Number.isFinite(value)) return HYDRO_MET_STATION_SERIES_API_TUPLE_LIMIT
  return Math.max(1, Math.min(HYDRO_MET_STATION_SERIES_API_TUPLE_LIMIT, Math.trunc(Number(value))))
}

function normalizedHydroMetStationSeriesUiString(value: string, fallback: string) {
  const sanitized = sanitizeHydroMetMessage(value, fallback).replace(/\s{2,}/g, ' ').trim()
  return sanitized || fallback
}

export function isHydroMetStationSeriesUiStringCapped(
  value: string,
  options: HydroMetStationSeriesUiStringOptions = {},
) {
  const limit = Math.max(1, Math.trunc(options.limit ?? HYDRO_MET_STATION_SERIES_UI_STRING_LIMIT))
  return normalizedHydroMetStationSeriesUiString(value, options.fallback ?? '-').length > limit
}

export function formatHydroMetStationSeriesUiString(
  value: string,
  options: HydroMetStationSeriesUiStringOptions = {},
) {
  const limit = Math.max(1, Math.trunc(options.limit ?? HYDRO_MET_STATION_SERIES_UI_STRING_LIMIT))
  const fallback = options.fallback ?? '-'
  const normalized = normalizedHydroMetStationSeriesUiString(value, fallback)
  if (normalized.length <= limit) return normalized
  if (options.oversizeReplacement) return options.oversizeReplacement
  if (limit <= HYDRO_MET_STATION_SERIES_TRUNCATION_MARKER.length) return normalized.slice(0, limit)
  return `${normalized.slice(0, limit - HYDRO_MET_STATION_SERIES_TRUNCATION_MARKER.length)}${HYDRO_MET_STATION_SERIES_TRUNCATION_MARKER}`
}

function capHydroMetStationSeriesMessageTokens(value: string) {
  return value.replace(/\S+/g, (token) => {
    if (token.length <= HYDRO_MET_STATION_SERIES_MESSAGE_TOKEN_LIMIT) return token
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(token)) return token
    const assignment = /^([^\s=]{1,48}=)/.exec(token)
    return assignment ? `${assignment[1]}${HYDRO_MET_STATION_SERIES_OVERSIZED_TOKEN}` : HYDRO_MET_STATION_SERIES_OVERSIZED_TOKEN
  })
}

export function formatHydroMetStationSeriesMessage(value: unknown, fallback = 'station-series 不可用') {
  const message = sanitizeHydroMetMessage(getApiErrorMessage(value, fallback), fallback)
  return formatHydroMetStationSeriesUiString(capHydroMetStationSeriesMessageTokens(message), {
    limit: HYDRO_MET_STATION_SERIES_MESSAGE_STRING_LIMIT,
    fallback,
  })
}

function hydroMetStationSeriesApiErrorRecord(value: unknown) {
  if (!isRecord(value)) return null
  const error = value.error
  return isRecord(error) ? error : value
}

export function hydroMetStationSeriesErrorCode(value: unknown) {
  if (value instanceof HydroMetStationSeriesError) return value.code
  const record = hydroMetStationSeriesApiErrorRecord(value)
  return typeof record?.code === 'string' ? record.code : null
}

function hydroMetStationSeriesErrorDetails(value: unknown) {
  if (value instanceof HydroMetStationSeriesError) return value.details
  const record = hydroMetStationSeriesApiErrorRecord(value)
  return isRecord(record) && 'details' in record ? record.details : undefined
}

export function isHydroMetStationSeriesRetainedDiskMiss(value: unknown) {
  return hydroMetStationSeriesErrorCode(value) === HYDRO_MET_STATION_SERIES_RETAINED_DISK_MISS_CODE
}

export function formatHydroMetStationSeriesContractValue(
  value: unknown,
  options: HydroMetStationSeriesUiStringOptions = {},
) {
  if (value === null) return 'null'
  if (value === undefined) return 'undefined'
  if (typeof value === 'string') return formatHydroMetStationSeriesUiString(value, options)
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return Array.isArray(value) ? 'array' : typeof value
}

export function stationSeriesRequestKey(product: HydroMetStationSeriesProductIdentity, stationId: string) {
  return [
    product.forcing_version_id,
    product.model_id,
    product.source_id,
    normalizeHydroMetCycle(product.cycle_time) ?? product.cycle_time,
    stationId,
  ].join('|')
}

export async function loadHydroMetStationSeries({
  product,
  station,
  limit,
}: HydroMetStationSeriesRequest): Promise<HydroMetStationSeriesResponse> {
  try {
    const stationId = station.station_id
    const boundedLimit = boundedHydroMetStationSeriesApiLimit(limit)
    const { data, error } = await client.GET('/api/v1/met/stations/{station_id}/series', {
      params: {
        path: { station_id: stationId },
        query: {
          forcing_version_id: product.forcing_version_id,
          model_id: product.model_id,
          source_id: product.source_id,
          cycle_time: product.cycle_time,
          variables: [...HYDRO_MET_STATION_VARIABLES],
          limit: boundedLimit,
        },
      },
    })

    if (error) {
      throw new HydroMetStationSeriesError(formatHydroMetStationSeriesMessage(error, 'station-series 不可用'), {
        code: hydroMetStationSeriesErrorCode(error),
        details: hydroMetStationSeriesErrorDetails(error),
      })
    }
    const response = unwrapApiData<HydroMetStationSeriesResponse>(data, 'station-series 不可用')
    if (!isRecord(response) || !Array.isArray(response.series)) throw new Error('station-series 响应不完整')
    return response
  } catch (error) {
    if (error instanceof HydroMetStationSeriesError) throw error
    throw new HydroMetStationSeriesError(formatHydroMetStationSeriesMessage(error, 'station-series 不可用'), {
      code: hydroMetStationSeriesErrorCode(error),
      details: hydroMetStationSeriesErrorDetails(error),
    })
  }
}

export function validateHydroMetStationSeriesIdentity(
  response: HydroMetStationSeriesResponse,
  product: HydroMetStationSeriesProductIdentity,
  stationId: string,
) {
  const messages: string[] = []
  const responseRecord = isRecord(response) ? response : {}
  const responseStationId = responseRecord.station_id
  const responseModelId = responseRecord.model_id
  const responseSourceId = responseRecord.source_id
  const responseCycleTime = responseRecord.cycle_time

  if (typeof responseStationId !== 'string') {
    messages.push('station_id 元数据格式无效')
  } else if (responseStationId !== stationId) {
    messages.push(`station_id=${formatHydroMetStationSeriesContractValue(responseStationId)} 与当前选择 ${formatHydroMetStationSeriesContractValue(stationId)} 不一致`)
  }
  if (typeof responseModelId !== 'string') {
    messages.push('model_id 元数据格式无效')
  } else if (responseModelId !== product.model_id) {
    messages.push(`model_id=${formatHydroMetStationSeriesContractValue(responseModelId)} 与 latest-product ${formatHydroMetStationSeriesContractValue(product.model_id)} 不一致`)
  }
  if (typeof responseSourceId !== 'string') {
    messages.push('source_id 元数据格式无效')
  } else if (responseSourceId !== product.source_id) {
    messages.push(`source_id=${formatHydroMetStationSeriesContractValue(responseSourceId)} 与 latest-product ${formatHydroMetStationSeriesContractValue(product.source_id)} 不一致`)
  }
  if (typeof responseCycleTime !== 'string') {
    messages.push('cycle_time 元数据格式无效')
  }

  const responseCycle = typeof responseCycleTime === 'string' ? normalizeHydroMetCycle(responseCycleTime) : null
  const productCycle = normalizeHydroMetCycle(product.cycle_time)
  if (typeof responseCycleTime === 'string') {
    if (!responseCycle) {
      messages.push(`cycle_time=${formatHydroMetStationSeriesContractValue(responseCycleTime)} 不是有效 RFC3339 时间`)
    } else if (productCycle && responseCycle !== productCycle) {
      messages.push(`cycle_time=${formatHydroMetStationSeriesContractValue(responseCycle)} 与 latest-product ${formatHydroMetStationSeriesContractValue(productCycle)} 不一致`)
    }
  }

  return messages
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

// === station-series 逐变量图表校验金标准（从 HydroMetPage 原样抽出，同源消费）===
// 行为字节级等价：reject-on-any-invalid-point、缺 unit 门控、unit 长度/格式、
// metadata（limit/returned_points/truncated + 4 个 RFC3339 时间字段）、quality_flag、
// truncated、capped（LIMIT+sentinel 口径）、reportedPointCount=max(metadata.returned_points, points.length)。

export type ChartableStationSeriesPoint = {
  timestamp: number
  value: number
  qualityFlag: string | null
}

export type HydroMetStationSeriesRecord = Record<string, unknown> & {
  variable: HydroMetStationSeriesVariable
}

export type StationSeriesValidation =
  | {
      ok: true
      metadata: HydroMetStationSeries['metadata']
      unit: string | null
      sourceId: string | null
      cycleTime: string | null
      seriesTruncated: boolean
      reportedPointCount: number
      inspectedPointCount: number
      renderedPoints: ChartableStationSeriesPoint[]
      capped: boolean
      inspectionCapped: boolean
      nonOkFlags: string[]
      nonOkFlagsCapped: boolean
      qualitySummary: string
    }
  | { ok: false; messages: string[] }

const HYDRO_MET_STATION_SERIES_POINT_SENTINEL = 16
const HYDRO_MET_STATION_SERIES_POINT_INSPECTION_LIMIT = HYDRO_MET_STATION_SERIES_DISPLAY_LIMIT + HYDRO_MET_STATION_SERIES_POINT_SENTINEL
const HYDRO_MET_STATION_SERIES_MESSAGE_LIMIT = 6
const HYDRO_MET_STATION_SERIES_QC_FLAG_LIMIT = 6
const HYDRO_MET_STATION_SERIES_QC_LABEL_LIMIT = 32
const HYDRO_MET_STATION_SERIES_UNIT_LIMIT = 32
export const HYDRO_MET_STATION_SERIES_ITEM_INSPECTION_LIMIT = HYDRO_MET_STATION_VARIABLES.length * 2

export function isHydroMetStationSeriesVariable(value: unknown): value is HydroMetStationSeriesVariable {
  return typeof value === 'string' && (HYDRO_MET_STATION_VARIABLES as readonly string[]).includes(value)
}

export function isHydroMetStationSeriesRecord(value: unknown): value is HydroMetStationSeriesRecord {
  return isRecord(value) && isHydroMetStationSeriesVariable(value.variable)
}

export function hydroMetStationSeriesItems(response: unknown) {
  if (!isRecord(response)) return []
  const series = response.series
  return Array.isArray(series) ? series : []
}

export function mapUniqueHydroMetStationSeries(seriesList: unknown[]) {
  const seriesByVariable = new Map<HydroMetStationSeriesVariable, HydroMetStationSeriesRecord>()
  seriesList.slice(0, HYDRO_MET_STATION_SERIES_ITEM_INSPECTION_LIMIT).forEach((series) => {
    if (!isHydroMetStationSeriesRecord(series)) return
    if (!seriesByVariable.has(series.variable)) seriesByVariable.set(series.variable, series)
  })
  return seriesByVariable
}

export function validateHydroMetStationSeriesForChart(series: HydroMetStationSeriesRecord): StationSeriesValidation {
  const messages: string[] = []
  const metadataValue = series.metadata
  const pointsValue = series.points
  const unitValue = series.unit
  const truncatedValue = series.truncated

  let unit: string | null = null
  if (unitValue === undefined || unitValue === null) {
    unit = null
  } else if (typeof unitValue === 'string') {
    if (isHydroMetStationSeriesUiStringCapped(unitValue, { limit: HYDRO_MET_STATION_SERIES_UNIT_LIMIT, fallback: '' })) {
      messages.push(`变量 ${series.variable} unit 过长，停止绘图`)
    } else {
      unit = formatHydroMetStationSeriesUiString(unitValue, { limit: HYDRO_MET_STATION_SERIES_UNIT_LIMIT, fallback: '' }) || null
    }
  } else {
    messages.push(`变量 ${series.variable} unit 格式无效`)
  }

  let seriesTruncated = false
  if (truncatedValue === undefined || truncatedValue === null) {
    seriesTruncated = false
  } else if (typeof truncatedValue === 'boolean') {
    seriesTruncated = truncatedValue
  } else {
    messages.push(`变量 ${series.variable} truncated 格式无效`)
  }

  if (!isRecord(metadataValue)) {
    messages.push(`变量 ${series.variable} metadata 缺失或格式无效`)
  }
  if (!Array.isArray(pointsValue)) {
    messages.push(`变量 ${series.variable} points 缺失或格式无效`)
  }
  if (messages.length > 0) return { ok: false, messages: capHydroMetStationSeriesMessages(messages) }

  messages.push(...validateStationSeriesMetadata(metadataValue as Record<string, unknown>, series.variable))
  if (messages.length > 0) return { ok: false, messages: capHydroMetStationSeriesMessages(messages) }

  const metadata = metadataValue as unknown as HydroMetStationSeries['metadata']
  const points = pointsValue as unknown[]
  const reportedPointCount = Math.max(metadata.returned_points, points.length)
  const inspectedPointCount = Math.min(points.length, HYDRO_MET_STATION_SERIES_POINT_INSPECTION_LIMIT)
  const inspectionCapped = points.length > inspectedPointCount

  const renderedPoints: ChartableStationSeriesPoint[] = []
  const invalidPointMessages: string[] = []
  let invalidPointCount = 0
  const qualityFlagCounts = new Map<string, number>()

  for (let index = 0; index < inspectedPointCount; index += 1) {
    const point = points[index]
    const parsed = parseChartableStationSeriesPoint(point)
    if (typeof parsed === 'string') {
      invalidPointCount += 1
      if (invalidPointMessages.length < HYDRO_MET_STATION_SERIES_MESSAGE_LIMIT) {
        invalidPointMessages.push(`变量 ${series.variable} 第 ${index + 1} 个点${parsed}`)
      }
    } else {
      if (renderedPoints.length < HYDRO_MET_STATION_SERIES_DISPLAY_LIMIT) renderedPoints.push(parsed)
      const flag = parsed.qualityFlag ?? 'missing'
      qualityFlagCounts.set(flag, (qualityFlagCounts.get(flag) ?? 0) + 1)
    }
  }

  if (invalidPointCount > 0) {
    messages.push(...capInvalidPointMessages(series.variable, invalidPointMessages, invalidPointCount, inspectedPointCount, reportedPointCount, inspectionCapped))
  }
  if (messages.length > 0) return { ok: false, messages: capHydroMetStationSeriesMessages(messages) }

  const { nonOkFlags, nonOkFlagsCapped, qualitySummary } = qualityFlagSummary(qualityFlagCounts, inspectedPointCount, reportedPointCount, inspectionCapped)
  return {
    ok: true,
    metadata,
    unit,
    sourceId: typeof series.source_id === 'string' ? formatHydroMetStationSeriesUiString(series.source_id) : null,
    cycleTime: typeof series.cycle_time === 'string' ? normalizeHydroMetCycle(series.cycle_time) : null,
    seriesTruncated,
    reportedPointCount,
    inspectedPointCount,
    renderedPoints,
    capped: reportedPointCount > HYDRO_MET_STATION_SERIES_DISPLAY_LIMIT,
    inspectionCapped,
    nonOkFlags,
    nonOkFlagsCapped,
    qualitySummary,
  }
}

function validateStationSeriesMetadata(metadata: Record<string, unknown>, variable: HydroMetStationSeriesVariable) {
  const messages: string[] = []
  const limit = metadata.limit
  const returnedPoints = metadata.returned_points
  const truncated = metadata.truncated

  if (typeof limit !== 'number' || !Number.isInteger(limit) || limit <= 0) {
    messages.push(`变量 ${variable} metadata.limit 缺失或格式无效`)
  }
  if (typeof returnedPoints !== 'number' || !Number.isInteger(returnedPoints) || returnedPoints < 0) {
    messages.push(`变量 ${variable} metadata.returned_points 缺失或格式无效`)
  }
  if (typeof truncated !== 'boolean') {
    messages.push(`变量 ${variable} metadata.truncated 缺失或格式无效`)
  }

  const metadataTimeFields = ['requested_from', 'requested_to', 'returned_from', 'returned_to'] as const
  metadataTimeFields.forEach((field) => {
    if (!(field in metadata)) {
      messages.push(`变量 ${variable} metadata.${field} 缺失`)
      return
    }
    const value = metadata[field]
    if (value !== null && (typeof value !== 'string' || !normalizeHydroMetCycle(value))) {
      messages.push(`变量 ${variable} metadata.${field} 不是有效 RFC3339 时间`)
    }
  })

  return messages
}

export function capHydroMetStationSeriesMessages(messages: string[]) {
  const safeMessages = messages.map((message) => (
    formatHydroMetStationSeriesUiString(message, {
      limit: HYDRO_MET_STATION_SERIES_MESSAGE_STRING_LIMIT,
      fallback: 'station-series contract 问题已截断',
    })
  ))
  if (safeMessages.length <= HYDRO_MET_STATION_SERIES_MESSAGE_LIMIT) return safeMessages
  return [
    ...safeMessages.slice(0, HYDRO_MET_STATION_SERIES_MESSAGE_LIMIT),
    `另有 ${safeMessages.length - HYDRO_MET_STATION_SERIES_MESSAGE_LIMIT} 条 station-series contract 问题已截断`,
  ]
}

function capInvalidPointMessages(
  variable: HydroMetStationSeriesVariable,
  messages: string[],
  invalidPointCount: number,
  inspectedPointCount: number,
  reportedPointCount: number,
  inspectionCapped: boolean,
) {
  const reservedSummarySlots = (invalidPointCount > messages.length ? 1 : 0) + (inspectionCapped ? 1 : 0)
  const detailLimit = Math.max(1, HYDRO_MET_STATION_SERIES_MESSAGE_LIMIT - reservedSummarySlots)
  const cappedMessages = messages.slice(0, detailLimit)
  const hiddenByMessageCap = Math.max(0, invalidPointCount - cappedMessages.length)
  if (hiddenByMessageCap > 0) {
    cappedMessages.push(`变量 ${variable} 另有 ${hiddenByMessageCap} 个已检查点无效，错误详情已截断`)
  }
  if (inspectionCapped) {
    cappedMessages.push(`变量 ${variable} capped 仅检查前 ${inspectedPointCount}/${reportedPointCount} 个点，响应过大，已停止继续校验`)
  }
  return cappedMessages
}

export function parseChartableStationSeriesPoint(point: unknown): ChartableStationSeriesPoint | string {
  if (!isRecord(point)) return '不是对象'

  const validTimeValue = point.valid_time
  if (typeof validTimeValue !== 'string') return '缺少有效 valid_time'
  const validTime = normalizeHydroMetCycle(validTimeValue)
  if (!validTime) return `valid_time=${formatHydroMetStationSeriesContractValue(validTimeValue)} 不是有效 RFC3339 时间`

  const value = point.value
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'value 不是有限数值'

  const qualityFlagValue = point.quality_flag
  if (qualityFlagValue !== undefined && qualityFlagValue !== null && typeof qualityFlagValue !== 'string') {
    return 'quality_flag 格式无效'
  }

  return {
    timestamp: Date.parse(validTime),
    value,
    qualityFlag: qualityFlagValue
      ? formatHydroMetStationSeriesUiString(qualityFlagValue, {
          limit: HYDRO_MET_STATION_SERIES_QC_LABEL_LIMIT,
          fallback: 'missing',
          oversizeReplacement: 'flag capped',
        })
      : null,
  }
}

function qualityFlagLabel(value: string) {
  return formatHydroMetStationSeriesUiString(value, {
    limit: HYDRO_MET_STATION_SERIES_QC_LABEL_LIMIT,
    fallback: 'missing',
    oversizeReplacement: 'flag capped',
  })
}

function qualityFlagSummary(
  counts: Map<string, number>,
  inspectedPointCount: number,
  reportedPointCount: number,
  inspectionCapped: boolean,
) {
  const entries = Array.from(counts.entries()).sort(([left], [right]) => left.localeCompare(right))
  const nonOkFlagEntries = entries.filter(([flag]) => flag !== 'missing' && flag.trim() !== '' && flag.toLowerCase() !== 'ok')
  const nonOkFlags = nonOkFlagEntries.slice(0, HYDRO_MET_STATION_SERIES_QC_FLAG_LIMIT).map(([flag]) => qualityFlagLabel(flag))
  const nonOkFlagsCapped = nonOkFlagEntries.length > nonOkFlags.length

  if (counts.size === 0) {
    return {
      nonOkFlags,
      nonOkFlagsCapped,
      qualitySummary: inspectionCapped
        ? `none; inspected ${inspectedPointCount}/${reportedPointCount}, capped`
        : 'none',
    }
  }

  const visibleEntries = entries.slice(0, HYDRO_MET_STATION_SERIES_QC_FLAG_LIMIT)
  const summary = visibleEntries.map(([flag, count]) => `${qualityFlagLabel(flag || 'empty')}:${count}`).join(', ')
  const hidden = entries.length - visibleEntries.length
  const suffixes: string[] = []
  if (hidden > 0) suffixes.push(`+${hidden} flags capped`)
  if (inspectionCapped) suffixes.push(`inspected ${inspectedPointCount}/${reportedPointCount}, capped`)

  return {
    nonOkFlags,
    nonOkFlagsCapped,
    qualitySummary: [summary, ...suffixes].join('; '),
  }
}

import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'
import { normalizeHydroMetCycle } from '@/lib/hydroMet/queryState'
import { sanitizeHydroMetMessage } from '@/lib/hydroMet/runtime'

export const HYDRO_MET_STATION_SERIES_LIMIT = 240
export const HYDRO_MET_STATION_VARIABLES = ['PRCP', 'TEMP', 'RH', 'wind', 'Rn', 'Press'] as const
export const HYDRO_MET_STATION_SERIES_UI_STRING_LIMIT = 96
export const HYDRO_MET_STATION_SERIES_MESSAGE_STRING_LIMIT = 180

const HYDRO_MET_STATION_SERIES_TRUNCATION_MARKER = '...'
const HYDRO_MET_STATION_SERIES_MESSAGE_TOKEN_LIMIT = 72
const HYDRO_MET_STATION_SERIES_OVERSIZED_TOKEN = '过长内容已截断'

export type HydroMetStationSeriesVariable = (typeof HYDRO_MET_STATION_VARIABLES)[number]
export type HydroMetStationSeriesResponse = components['schemas']['StationSeriesResponse']
export type HydroMetStationSeries = components['schemas']['StationSeries']
export type HydroMetStationSeriesPoint = components['schemas']['StationSeriesPoint']

export interface HydroMetStationSeriesProductIdentity {
  forcing_version_id: string
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

export interface HydroMetStationSeriesUiStringOptions {
  limit?: number
  fallback?: string
  oversizeReplacement?: string
}

export function boundedHydroMetStationSeriesLimit(value: number | null | undefined) {
  if (!Number.isFinite(value)) return HYDRO_MET_STATION_SERIES_LIMIT
  return Math.max(1, Math.min(HYDRO_MET_STATION_SERIES_LIMIT, Math.trunc(Number(value))))
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
    const boundedLimit = boundedHydroMetStationSeriesLimit(limit)
    const { data, error } = await client.GET('/api/v1/met/stations/{station_id}/series', {
      params: {
        path: { station_id: stationId },
        query: {
          forcing_version_id: product.forcing_version_id,
          variables: [...HYDRO_MET_STATION_VARIABLES],
          limit: boundedLimit,
        },
      },
    })

    if (error) throw new Error(formatHydroMetStationSeriesMessage(error, 'station-series 不可用'))
    const response = unwrapApiData<HydroMetStationSeriesResponse>(data, 'station-series 不可用')
    if (!isRecord(response) || !Array.isArray(response.series)) throw new Error('station-series 响应不完整')
    return response
  } catch (error) {
    throw new Error(formatHydroMetStationSeriesMessage(error, 'station-series 不可用'))
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
  const responseForcingVersionId = responseRecord.forcing_version_id
  const responseSourceId = responseRecord.source_id
  const responseCycleTime = responseRecord.cycle_time

  if (typeof responseStationId !== 'string') {
    messages.push('station_id 元数据格式无效')
  } else if (responseStationId !== stationId) {
    messages.push(`station_id=${formatHydroMetStationSeriesContractValue(responseStationId)} 与当前选择 ${formatHydroMetStationSeriesContractValue(stationId)} 不一致`)
  }
  if (typeof responseForcingVersionId !== 'string') {
    messages.push('forcing_version_id 元数据格式无效')
  } else if (responseForcingVersionId !== product.forcing_version_id) {
    messages.push(`forcing_version_id=${formatHydroMetStationSeriesContractValue(responseForcingVersionId)} 与 latest-product ${formatHydroMetStationSeriesContractValue(product.forcing_version_id)} 不一致`)
  }
  if (typeof responseSourceId !== 'string') {
    messages.push('source_id 元数据格式无效')
  } else if (responseSourceId !== product.source_id) {
    messages.push(`source_id=${formatHydroMetStationSeriesContractValue(responseSourceId)} 与 latest-product ${formatHydroMetStationSeriesContractValue(product.source_id)} 不一致`)
  }
  if (responseCycleTime !== undefined && responseCycleTime !== null && typeof responseCycleTime !== 'string') {
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

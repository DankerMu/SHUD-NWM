import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'
import { normalizeHydroMetCycle } from '@/lib/hydroMet/queryState'
import { sanitizeHydroMetMessage } from '@/lib/hydroMet/runtime'

export const HYDRO_MET_STATION_SERIES_LIMIT = 240
export const HYDRO_MET_STATION_VARIABLES = ['PRCP', 'TEMP', 'RH', 'wind', 'Rn', 'Press'] as const

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

export function boundedHydroMetStationSeriesLimit(value: number | null | undefined) {
  if (!Number.isFinite(value)) return HYDRO_MET_STATION_SERIES_LIMIT
  return Math.max(1, Math.min(HYDRO_MET_STATION_SERIES_LIMIT, Math.trunc(Number(value))))
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

  if (error) throw new Error(sanitizeHydroMetMessage(getApiErrorMessage(error, 'station-series 不可用'), 'station-series 不可用'))
  const response = unwrapApiData<HydroMetStationSeriesResponse>(data, 'station-series 不可用')
  if (!response || !Array.isArray(response.series)) throw new Error('station-series 响应不完整')
  return response
}

export function validateHydroMetStationSeriesIdentity(
  response: HydroMetStationSeriesResponse,
  product: HydroMetStationSeriesProductIdentity,
  stationId: string,
) {
  const messages: string[] = []
  if (response.station_id !== stationId) {
    messages.push(`station_id=${response.station_id} 与当前选择 ${stationId} 不一致`)
  }
  if (response.forcing_version_id !== product.forcing_version_id) {
    messages.push(`forcing_version_id=${response.forcing_version_id} 与 latest-product ${product.forcing_version_id} 不一致`)
  }
  if (response.source_id !== product.source_id) {
    messages.push(`source_id=${response.source_id} 与 latest-product ${product.source_id} 不一致`)
  }

  const responseCycle = normalizeHydroMetCycle(response.cycle_time)
  const productCycle = normalizeHydroMetCycle(product.cycle_time)
  if (responseCycle && productCycle && responseCycle !== productCycle) {
    messages.push(`cycle_time=${responseCycle} 与 latest-product ${productCycle} 不一致`)
  }

  return messages
}

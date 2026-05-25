import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'
import { normalizeHydroMetCycle, type HydroMetSource } from '@/lib/hydroMet/queryState'
import { normalizeHydroMetStation, sanitizeHydroMetMessage } from '@/lib/hydroMet/runtime'

export const HYDRO_MET_STATION_LIMIT = 500
export const HYDRO_MET_RIVER_SEGMENT_LIMIT = 250

export type QhhLatestProduct = components['schemas']['QhhLatestProduct']
export type HydroMetStation = components['schemas']['MetStation']
export type HydroMetStationPage = components['schemas']['MetStationPage']
export type HydroMetRiverSegmentFeature = components['schemas']['RiverSegmentFeature']
export type HydroMetRiverSegmentCollection = components['schemas']['RiverSegmentFeatureCollection']

export type HydroMetBootstrapStatus =
  | 'ready'
  | 'latest-unavailable'
  | 'latest-incomplete'
  | 'cycle-unavailable'

export interface HydroMetBootstrapRequest {
  source: HydroMetSource
  cycle: string | null
  stationLimit?: number
  riverSegmentLimit?: number
}

export interface HydroMetBootstrapResult {
  status: HydroMetBootstrapStatus
  source: HydroMetSource
  cycle: string | null
  product: QhhLatestProduct | null
  stations: HydroMetStation[]
  riverSegments: HydroMetRiverSegmentFeature[]
  stationPage: HydroMetStationPage | null
  riverSegmentCollection: HydroMetRiverSegmentCollection | null
  latestReasons: string[]
  stationError: string | null
  riverError: string | null
}

async function getLatestProduct(source: HydroMetSource) {
  const { data, error } = await client.GET('/api/v1/mvp/qhh/latest-product', {
    params: { query: { source } },
  })
  if (error) throw new Error(getApiErrorMessage(error, 'latest-product 不可用'))
  const product = unwrapApiData<QhhLatestProduct>(data, 'latest-product 不可用')
  if (!product || typeof product !== 'object') throw new Error('latest-product 不可用')
  return product
}

async function getStationInventory(product: QhhLatestProduct, limit: number) {
  const { data, error } = await client.GET('/api/v1/met/stations', {
    params: {
      query: {
        model_id: product.model_id,
        limit,
        offset: 0,
      },
    },
  })
  if (error) throw new Error(getApiErrorMessage(error, '站点 inventory 加载失败'))
  const page = unwrapApiData<HydroMetStationPage>(data, '站点 inventory 加载失败')
  if (!page || !Array.isArray(page.items)) throw new Error('站点 inventory 响应不完整')
  return { ...page, items: page.items.map((station) => normalizeHydroMetStation(station)) }
}

async function getRiverSegments(product: QhhLatestProduct, limit: number) {
  const { data, error } = await client.GET('/api/v1/basin-versions/{basin_version_id}/river-segments', {
    params: {
      path: {
        basin_version_id: product.basin_version_id,
      },
      query: {
        river_network_version_id: product.river_network_version_id,
        limit,
        offset: 0,
      },
    },
  })
  if (error) throw new Error(getApiErrorMessage(error, '河段流量候选加载失败'))
  const collection = unwrapApiData<HydroMetRiverSegmentCollection>(data, '河段流量候选加载失败')
  if (!collection || !Array.isArray(collection.features)) throw new Error('河段流量候选响应不完整')
  return collection
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

function productAvailabilityReasons(product: QhhLatestProduct) {
  const reasons = product.availability?.unavailable_reasons ?? []
  return reasons.map((reason) => `${reason.code}: ${sanitizeHydroMetMessage(reason.message)}`)
}

function validateLatestProduct(product: QhhLatestProduct, request: HydroMetBootstrapRequest): {
  status: HydroMetBootstrapStatus
  reasons: string[]
} {
  const reasons: string[] = []
  const productCycle = normalizeHydroMetCycle(product.cycle_time)
  const requiredIdentity: Array<keyof Pick<
    QhhLatestProduct,
    'model_id' | 'basin_version_id' | 'river_network_version_id' | 'run_id' | 'forcing_version_id' | 'cycle_time'
  >> = ['model_id', 'basin_version_id', 'river_network_version_id', 'run_id', 'forcing_version_id', 'cycle_time']

  for (const field of requiredIdentity) {
    if (!isNonEmptyString(product[field])) reasons.push(`${field} 缺失`)
  }
  if (!productCycle) reasons.push('cycle_time 无法解析')
  if (product.source_id !== request.source) reasons.push(`source_id=${product.source_id} 与请求 source=${request.source} 不一致`)
  if (!Number.isFinite(product.station_count) || product.station_count <= 0) reasons.push('station_count 不可展示')
  if (!Number.isFinite(product.segment_count) || product.segment_count <= 0) reasons.push('segment_count 不可展示')

  if (product.status !== 'ready' || product.availability?.ready === false) {
    return {
      status: 'latest-unavailable',
      reasons: productAvailabilityReasons(product).concat(reasons),
    }
  }

  if (reasons.length > 0) {
    return { status: 'latest-incomplete', reasons }
  }

  if (request.cycle && productCycle && request.cycle !== productCycle) {
    return {
      status: 'cycle-unavailable',
      reasons: [
        `URL cycle=${request.cycle} 与 latest-product cycle=${productCycle} 不一致，已停止加载下游候选，避免混用产品。`,
      ],
    }
  }

  return { status: 'ready', reasons: [] }
}

function baseResult(
  request: HydroMetBootstrapRequest,
  product: QhhLatestProduct | null,
  status: HydroMetBootstrapStatus,
  latestReasons: string[],
): HydroMetBootstrapResult {
  return {
    status,
    source: request.source,
    cycle: request.cycle,
    product,
    stations: [],
    riverSegments: [],
    stationPage: null,
    riverSegmentCollection: null,
    latestReasons,
    stationError: null,
    riverError: null,
  }
}

function settledError(result: PromiseSettledResult<unknown>, fallback: string) {
  return result.status === 'rejected' ? sanitizeHydroMetMessage(getApiErrorMessage(result.reason, fallback), fallback) : null
}

export async function loadHydroMetBootstrap(request: HydroMetBootstrapRequest): Promise<HydroMetBootstrapResult> {
  const stationLimit = request.stationLimit ?? HYDRO_MET_STATION_LIMIT
  const riverSegmentLimit = request.riverSegmentLimit ?? HYDRO_MET_RIVER_SEGMENT_LIMIT
  let product: QhhLatestProduct

  try {
    product = await getLatestProduct(request.source)
  } catch (error) {
    return baseResult(request, null, 'latest-unavailable', [
      sanitizeHydroMetMessage(getApiErrorMessage(error, 'latest-product 不可用'), 'latest-product 不可用'),
    ])
  }

  const validation = validateLatestProduct(product, request)
  if (validation.status !== 'ready') {
    return baseResult(request, product, validation.status, validation.reasons)
  }

  const [stationResult, riverResult] = await Promise.allSettled([
    getStationInventory(product, stationLimit),
    getRiverSegments(product, riverSegmentLimit),
  ])

  const stationPage = stationResult.status === 'fulfilled' ? stationResult.value : null
  const riverSegmentCollection = riverResult.status === 'fulfilled' ? riverResult.value : null

  return {
    status: 'ready',
    source: request.source,
    cycle: request.cycle,
    product,
    stations: stationPage?.items ?? [],
    riverSegments: riverSegmentCollection?.features ?? [],
    stationPage,
    riverSegmentCollection,
    latestReasons: [],
    stationError: settledError(stationResult, '站点 inventory 加载失败'),
    riverError: settledError(riverResult, '河段流量候选加载失败'),
  }
}

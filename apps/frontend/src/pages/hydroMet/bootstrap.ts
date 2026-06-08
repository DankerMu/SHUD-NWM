import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'
import { normalizeHydroMetCycle, type HydroMetSource, type HydroMetStrictIdentity } from '@/lib/hydroMet/queryState'
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
  | 'strict-identity-mismatch'

export interface HydroMetBootstrapRequest {
  source: HydroMetSource
  cycle: string | null
  basinId?: string | null
  strictIdentity?: HydroMetStrictIdentity | null
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

async function getLatestProduct(request: HydroMetBootstrapRequest) {
  const basinId = request.basinId?.trim() ? { basin_id: request.basinId.trim() } : {}
  const query = request.strictIdentity
    ? {
      source: request.strictIdentity.source,
      cycle_time: request.strictIdentity.cycleTime,
      run_id: request.strictIdentity.runId,
      model_id: request.strictIdentity.modelId,
      ...basinId,
    }
    : { source: request.source, ...basinId }
  const { data, error } = await client.GET('/api/v1/mvp/qhh/latest-product', {
    params: { query },
  })
  if (error) throw new Error(getApiErrorMessage(error, 'latest-product 不可用'))
  const product = unwrapApiData<QhhLatestProduct>(data, 'latest-product 不可用')
  if (!product || typeof product !== 'object') throw new Error('latest-product 不可用')
  return product
}

/**
 * 弹窗专用轻量 latest-product 解析（identity_only=true）。
 * 后端只跑 run 身份 + cycle + horizon（实测 ~50ms），不算 station/segment 覆盖（实测 ~17s），
 * 也不附带 stations / river-segments 候选；并返回最近 N 个 cycle 作为可选起报时间。
 * request.cycle 可指定具体起报 cycle（起报时间选择器重取用）。失败抛错。
 */
export async function fetchHydroMetLatestProduct(request: HydroMetBootstrapRequest): Promise<QhhLatestProduct> {
  const basinId = request.basinId?.trim() ? { basin_id: request.basinId.trim() } : {}
  const cycle = request.cycle?.trim() ? { cycle_time: request.cycle.trim() } : {}
  const { data, error } = await client.GET('/api/v1/mvp/qhh/latest-product', {
    params: { query: { source: request.source, identity_only: true, ...cycle, ...basinId } },
  })
  if (error) throw new Error(getApiErrorMessage(error, 'latest-product 不可用'))
  const product = unwrapApiData<QhhLatestProduct>(data, 'latest-product 不可用')
  if (!product || typeof product !== 'object') throw new Error('latest-product 不可用')
  return product
}

export interface HydroMetStationQuery {
  search?: string
  variables?: string[]
  qcStatus?: string
  limit?: number
  offset?: number
}

export interface HydroMetRiverSegmentQuery {
  search?: string
  streamOrderMin?: number
  streamOrderMax?: number
  limit?: number
  offset?: number
}

function trimmedOrUndefined(value: string | undefined | null) {
  const trimmed = value?.trim()
  return trimmed ? trimmed : undefined
}

/**
 * Station inventory query. All identity (model_id/basin_version_id) is derived from the
 * latest-product so server-side search/variable filtering never breaks strict identity.
 */
export async function fetchHydroMetStations(product: QhhLatestProduct, query: HydroMetStationQuery = {}) {
  const search = trimmedOrUndefined(query.search)
  const variables = query.variables?.length ? query.variables : undefined
  const qcStatus = trimmedOrUndefined(query.qcStatus)
  const { data, error } = await client.GET('/api/v1/met/stations', {
    params: {
      query: {
        model_id: product.model_id,
        basin_version_id: product.basin_version_id,
        ...(search ? { search } : {}),
        ...(variables ? { variables } : {}),
        ...(qcStatus ? { qc_status: qcStatus } : {}),
        limit: query.limit ?? HYDRO_MET_STATION_LIMIT,
        offset: query.offset ?? 0,
      },
    },
  })
  if (error) throw new Error(getApiErrorMessage(error, '站点 inventory 加载失败'))
  const page = unwrapApiData<HydroMetStationPage>(data, '站点 inventory 加载失败')
  if (!page || !Array.isArray(page.items)) throw new Error('站点 inventory 响应不完整')
  return { ...page, items: page.items.map((station) => normalizeHydroMetStation(station)) }
}

/**
 * River segment candidates query. basin_version_id (path) and river_network_version_id come
 * from the latest-product, so search/stream_order filtering keeps the same product identity.
 */
export async function fetchHydroMetRiverSegments(product: QhhLatestProduct, query: HydroMetRiverSegmentQuery = {}) {
  const search = trimmedOrUndefined(query.search)
  const { data, error } = await client.GET('/api/v1/basin-versions/{basin_version_id}/river-segments', {
    params: {
      path: {
        basin_version_id: product.basin_version_id,
      },
      query: {
        river_network_version_id: product.river_network_version_id,
        ...(search ? { search } : {}),
        ...(Number.isFinite(query.streamOrderMin) ? { stream_order_min: query.streamOrderMin } : {}),
        ...(Number.isFinite(query.streamOrderMax) ? { stream_order_max: query.streamOrderMax } : {}),
        limit: query.limit ?? HYDRO_MET_RIVER_SEGMENT_LIMIT,
        offset: query.offset ?? 0,
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

function isRecordValue(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * Per-filter availability from the station page `filters.available` block. The backend reports
 * each advanced filter honestly so the UI can hide controls instead of erroring. Defaults to
 * unavailable for qc_status / variables when the block is missing, and search defaults available.
 */
export function hydroMetStationFilterAvailability(page: HydroMetStationPage | null) {
  const filters = isRecordValue(page?.filters) ? page.filters : null
  const available = filters && isRecordValue(filters.available) ? filters.available : null
  const flag = (key: string, fallback: boolean) =>
    available && typeof available[key] === 'boolean' ? (available[key] as boolean) : fallback
  return {
    search: flag('search', true),
    variables: flag('variables', false),
    qcStatus: flag('qc_status', false),
  }
}

/**
 * stream_order filtering is an optional enhancement: only offer it when the river segment data
 * actually carries a finite stream_order field. The river-segments collection has no filters
 * block, so availability is inferred from the returned features (honest, no schema fabrication).
 */
export function hydroMetStreamOrderAvailable(features: HydroMetRiverSegmentFeature[]) {
  return features.some((feature) => Number.isFinite(feature.properties?.stream_order))
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

  if (request.strictIdentity) {
    const identityReasons: string[] = []
    if (product.source_id !== request.strictIdentity.source) {
      identityReasons.push(`source_id=${product.source_id} 与 URL source=${request.strictIdentity.source} 不一致`)
    }
    if (productCycle !== request.strictIdentity.cycleTime) {
      identityReasons.push(`cycle_time=${productCycle ?? 'unavailable'} 与 URL cycle_time=${request.strictIdentity.cycleTime} 不一致`)
    }
    if (product.run_id !== request.strictIdentity.runId) {
      identityReasons.push(`run_id=${product.run_id} 与 URL run_id=${request.strictIdentity.runId} 不一致`)
    }
    if (product.model_id !== request.strictIdentity.modelId) {
      identityReasons.push(`model_id=${product.model_id} 与 URL model_id=${request.strictIdentity.modelId} 不一致`)
    }
    if (identityReasons.length > 0) {
      return { status: 'strict-identity-mismatch', reasons: identityReasons }
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
    product = await getLatestProduct(request)
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
    fetchHydroMetStations(product, { limit: stationLimit }),
    fetchHydroMetRiverSegments(product, { limit: riverSegmentLimit }),
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

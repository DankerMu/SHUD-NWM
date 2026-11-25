import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import { normalizeHydroMetStation } from '@/lib/hydroMet/runtime'

export const HYDRO_MET_STATION_LIMIT = 500

export type QhhLatestProduct = components['schemas']['QhhLatestProduct']
export type HydroMetStation = components['schemas']['MetStation']
export type HydroMetStationPage = components['schemas']['MetStationPage']

export interface HydroMetBootstrapRequest {
  source: HydroMetSource
  cycle: string | null
  basinId?: string | null
}

const HYDRO_MET_LATEST_PRODUCT_IDENTITY_CACHE_TTL_MS = 120_000

const latestProductIdentityCache = new Map<string, { expiresAt: number; promise: Promise<QhhLatestProduct> }>()

/**
 * 弹窗专用轻量 latest-product 解析（identity_only=true）。
 * 后端只跑 run 身份 + cycle + horizon（实测 ~50ms），不算 station/segment 覆盖（实测 ~17s），
 * 也不附带 stations 候选；并返回最近 N 个 cycle 作为可选起报时间。
 * request.cycle 可指定具体起报 cycle（起报时间选择器重取用）。失败抛错。
 */
async function fetchHydroMetLatestProductUncached(request: HydroMetBootstrapRequest): Promise<QhhLatestProduct> {
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

function latestProductIdentityCacheKey(request: HydroMetBootstrapRequest) {
  return [
    request.basinId?.trim() ?? '',
    request.source,
    request.cycle?.trim() || 'latest',
  ].join('|')
}

export async function fetchHydroMetLatestProduct(request: HydroMetBootstrapRequest): Promise<QhhLatestProduct> {
  const key = latestProductIdentityCacheKey(request)
  const now = Date.now()
  const cached = latestProductIdentityCache.get(key)
  if (cached && cached.expiresAt > now) return cached.promise

  const promise = fetchHydroMetLatestProductUncached(request)
    .then((product) => {
      const entry = latestProductIdentityCache.get(key)
      if (entry?.promise === promise) entry.expiresAt = Date.now() + HYDRO_MET_LATEST_PRODUCT_IDENTITY_CACHE_TTL_MS
      return product
    })
    .catch((error) => {
      if (latestProductIdentityCache.get(key)?.promise === promise) latestProductIdentityCache.delete(key)
      throw error
    })
  latestProductIdentityCache.set(key, { expiresAt: now + HYDRO_MET_LATEST_PRODUCT_IDENTITY_CACHE_TTL_MS, promise })
  return promise
}

export async function prefetchHydroMetLatestProducts({
  basinId,
  cycle,
}: {
  basinId?: string | null
  cycle?: string | null
}): Promise<void> {
  const normalizedBasinId = basinId?.trim()
  if (!normalizedBasinId) return
  await Promise.allSettled([
    fetchHydroMetLatestProduct({ source: 'GFS', cycle: cycle ?? null, basinId: normalizedBasinId }),
    fetchHydroMetLatestProduct({ source: 'IFS', cycle: cycle ?? null, basinId: normalizedBasinId }),
  ])
}

export function _clearHydroMetLatestProductIdentityCache() {
  latestProductIdentityCache.clear()
}

export interface HydroMetStationQuery {
  search?: string
  variables?: string[]
  qcStatus?: string
  limit?: number
  offset?: number
}

export interface HydroMetStationIdentity {
  basinVersionId?: string | null
  modelId?: string | null
}

function trimmedOrUndefined(value: string | undefined | null) {
  const trimmed = value?.trim()
  return trimmed ? trimmed : undefined
}

export async function fetchHydroMetStationsByIdentity(identity: HydroMetStationIdentity, query: HydroMetStationQuery = {}) {
  const basinVersionId = trimmedOrUndefined(identity.basinVersionId)
  const modelId = trimmedOrUndefined(identity.modelId)
  if (!basinVersionId && !modelId) throw new Error('站点 inventory 缺少 basin_version_id 或 model_id')
  const search = trimmedOrUndefined(query.search)
  const variables = query.variables?.length ? query.variables : undefined
  const qcStatus = trimmedOrUndefined(query.qcStatus)
  const { data, error } = await client.GET('/api/v1/met/stations', {
    params: {
      query: {
        model_id: modelId,
        basin_version_id: basinVersionId,
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

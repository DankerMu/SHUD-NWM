import { create } from 'zustand'

import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'
import {
  HYDRO_MET_STATION_LIMIT,
  fetchHydroMetStationsByIdentity,
  type HydroMetStation,
} from '@/pages/hydroMet/bootstrap'
import { sanitizeHydroMetMessage } from '@/lib/hydroMet/runtime'

/**
 * 国家级守卫：单流域代站客户端缓存上限。达到后停止翻页并标 truncated，
 * 不让"看似完整"的图层掩盖缺失（spec: 诚实标注 truncation）。
 */
export const STATION_CLIENT_CAP = 5000
export const STATION_PAGE_LIMIT = HYDRO_MET_STATION_LIMIT

type ApiBasinVersion = components['schemas']['BasinVersion']

export interface StationLayerData {
  stations: HydroMetStation[]
  stationBasinIds: Record<string, string>
  total: number
  loaded: number
  truncated: boolean
}

export interface StationLayerBasinContext {
  basinId: string
  basinVersionId: string | null
}

export interface StationLayerRequest {
  basinContexts: StationLayerBasinContext[]
}

interface StationLayerDataState {
  data: StationLayerData | null
  loading: boolean
  error: string | null
  /** 当前已解析快照的请求键（basinId+basinVersionId）；用于 UI 判定数据是否匹配当前请求。 */
  requestKey: string | null
  loadStationLayer: (request: StationLayerRequest) => Promise<StationLayerData>
  clear: () => void
}

function normalizeBasinContexts(contexts: StationLayerBasinContext[]) {
  const seen = new Set<string>()
  const normalized: StationLayerBasinContext[] = []
  for (const context of contexts) {
    const basinId = context.basinId.trim()
    const basinVersionId = context.basinVersionId?.trim() || null
    if (!basinId) continue
    const key = `${basinId}::${basinVersionId ?? ''}`
    if (seen.has(key)) continue
    seen.add(key)
    normalized.push({ basinId, basinVersionId })
  }
  return normalized
}

export function stationLayerRequestKey(request: StationLayerRequest) {
  return normalizeBasinContexts(request.basinContexts)
    .map((context) => `${context.basinId}:${context.basinVersionId ?? 'missing'}`)
    .join(',')
}

const inFlight = new Map<string, Promise<StationLayerData>>()
let requestNonce = 0
let activeRequestKey: string | null = null

/**
 * 地图点位分页：代站位置本身来自 basin_version_id 站点清单，不依赖 latest-product ready。
 * 曲线弹窗仍用 latest-product 做 GFS/IFS 严格身份校验；地图图层只负责把可见流域的点画出来。
 */
async function fetchAllStations(request: StationLayerRequest): Promise<StationLayerData> {
  const contexts = await resolveStationBasinContexts(normalizeBasinContexts(request.basinContexts))
  if (contexts.length === 0) throw new Error('代站图层缺少可用流域版本身份')

  const stations: HydroMetStation[] = []
  const stationBasinIds: Record<string, string> = {}
  let total = 0
  let truncated = false

  for (const context of contexts) {
    if (stations.length >= STATION_CLIENT_CAP) {
      truncated = true
      break
    }

    const firstPage = await fetchHydroMetStationsByIdentity(
      { basinVersionId: context.basinVersionId },
      { limit: Math.min(STATION_PAGE_LIMIT, STATION_CLIENT_CAP - stations.length), offset: 0 },
    )
    const basinTotal = Number.isFinite(firstPage.total_count) ? firstPage.total_count : firstPage.items.length
    total += basinTotal

    appendStations(stations, stationBasinIds, firstPage.items, context.basinId)

    let offset = firstPage.items.length
    while (offset < basinTotal && stations.length < STATION_CLIENT_CAP) {
      const remainingCap = STATION_CLIENT_CAP - stations.length
      const pageLimit = Math.min(STATION_PAGE_LIMIT, remainingCap)
      const page = await fetchHydroMetStationsByIdentity(
        { basinVersionId: context.basinVersionId },
        { limit: pageLimit, offset },
      )
      if (page.items.length === 0) break
      appendStations(stations, stationBasinIds, page.items, context.basinId)
      offset += page.items.length
    }

    if (offset < basinTotal) truncated = true
  }

  const loaded = stations.length
  return {
    stations,
    stationBasinIds,
    total,
    loaded,
    truncated: truncated || loaded < total,
  }
}

async function resolveStationBasinContexts(contexts: StationLayerBasinContext[]): Promise<StationLayerBasinContext[]> {
  const resolved = await Promise.all(
    contexts.map(async (context) => ({
      basinId: context.basinId,
      basinVersionId: context.basinVersionId ?? (await fetchDefaultBasinVersionId(context.basinId)),
    })),
  )
  return resolved.filter((context): context is StationLayerBasinContext & { basinVersionId: string } => Boolean(context.basinVersionId))
}

async function fetchDefaultBasinVersionId(basinId: string): Promise<string | null> {
  const { data, error } = await client.GET('/api/v1/basins/{basin_id}/versions', {
    params: { path: { basin_id: basinId }, query: { limit: 20, offset: 0 } },
  })
  if (error) throw new Error(getApiErrorMessage(error, '获取流域版本失败'))
  const versions = unwrapApiData<ApiBasinVersion[]>(data, '获取流域版本失败')
  const selected = versions.find((version) => version.active_flag) ?? versions[0] ?? null
  return selected?.basin_version_id ?? null
}

function appendStations(
  stations: HydroMetStation[],
  stationBasinIds: Record<string, string>,
  items: HydroMetStation[],
  basinId: string,
) {
  for (const station of items) {
    stations.push(station)
    if (station.station_id) stationBasinIds[station.station_id] = basinId
  }
}

export const useStationLayerDataStore = create<StationLayerDataState>((set) => ({
  data: null,
  loading: false,
  error: null,
  requestKey: null,
  clear: () => {
    requestNonce += 1
    activeRequestKey = null
    inFlight.clear()
    set({ data: null, loading: false, error: null, requestKey: null })
  },
  loadStationLayer: async (request) => {
    const key = stationLayerRequestKey(request)
    const existing = inFlight.get(key)
    if (existing && activeRequestKey === key) return existing

    const nonce = ++requestNonce
    activeRequestKey = key
    set({ loading: true, error: null })

    const load = (async () => {
      try {
        const data = await fetchAllStations(request)
        if (nonce === requestNonce && activeRequestKey === key) {
          set({ data, loading: false, error: null, requestKey: key })
        }
        return data
      } catch (error) {
        const message = sanitizeHydroMetMessage(getApiErrorMessage(error, '代站数据加载失败'), '代站数据加载失败')
        if (nonce === requestNonce && activeRequestKey === key) {
          set({ data: null, loading: false, error: message, requestKey: key })
        }
        throw error
      } finally {
        if (inFlight.get(key) === load) inFlight.delete(key)
      }
    })()

    inFlight.set(key, load)
    return load
  },
}))

import { create } from 'zustand'

import { getApiErrorMessage } from '@/api/response'
import {
  HYDRO_MET_STATION_LIMIT,
  fetchHydroMetLatestProduct,
  fetchHydroMetStationsByIdentity,
  type HydroMetStation,
} from '@/pages/hydroMet/bootstrap'
import { sanitizeHydroMetMessage } from '@/lib/hydroMet/runtime'

/**
 * 国家级守卫：单流域代站客户端缓存上限。达到后停止翻页并标 truncated，
 * 不让"看似完整"的图层掩盖缺失（spec: 诚实标注 truncation）。
 */
export const STATION_CLIENT_CAP = 5000
/** 国家级 overview 守卫：最多请求前 N 个已解析流域版本，避免空/稀疏流域扇出无界首页请求。 */
export const STATION_CONTEXT_CAP = 50
export const STATION_PAGE_LIMIT = HYDRO_MET_STATION_LIMIT

export interface StationLayerData {
  stations: HydroMetStation[]
  stationBasinIds: Record<string, string>
  total: number
  totalKnown: boolean
  loaded: number
  truncated: boolean
  /** 站点请求失败的流域 id（按请求顺序、去重）；全部成功时为 []。非空时 truncated=true、total 只是下界。 */
  failedBasinIds: string[]
}

export interface StationLayerBasinContext {
  basinId: string
  basinVersionId: string | null
  source?: 'GFS' | 'IFS' | null
  cycle?: string | null
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

function normalizeBasinContexts(contexts: StationLayerBasinContext[]): Array<StationLayerBasinContext & { basinVersionId: string }> {
  const seen = new Set<string>()
  const normalized: Array<StationLayerBasinContext & { basinVersionId: string }> = []
  for (const context of contexts) {
    const basinId = context.basinId.trim()
    const basinVersionId = context.basinVersionId?.trim() || null
    const source = context.source === 'GFS' || context.source === 'IFS' ? context.source : null
    const cycle = context.cycle?.trim() || null
    if (!basinId || !basinVersionId) continue
    const key = JSON.stringify([basinId, basinVersionId, source, cycle])
    if (seen.has(key)) continue
    seen.add(key)
    normalized.push({ basinId, basinVersionId, source, cycle })
  }
  return normalized
}

export function stationLayerRequestKey(request: StationLayerRequest) {
  return JSON.stringify(
    normalizeBasinContexts(request.basinContexts).map(({ basinId, basinVersionId, source, cycle }) => [
      basinId,
      basinVersionId,
      source,
      cycle,
    ]),
  )
}

const inFlight = new Map<string, Promise<StationLayerData>>()
let requestNonce = 0
let activeRequestKey: string | null = null

/**
 * 地图点位分页：代站位置本身来自 basin_version_id 站点清单，不依赖 latest-product ready。
 * 该清单（#2699）只列该流域版本最新可展示 forecast run 所用模型的代站，与 active_flag 无关；
 * 没有可展示 run 的流域版本返回空清单（total_count 0），图层上就没有点。
 * 曲线弹窗仍用 latest-product 做 GFS/IFS 严格身份校验；地图图层只负责把可见流域的点画出来。
 * 源已解析时先查 latest-product 取 model_id；该查询失败（含该流域无此源产品的 404）不算流域失败，
 * 回退为只按 basin_version_id 取清单。
 * 按流域隔离失败（#2694）：某流域的站点分页请求抛错时记入 failedBasinIds、保留它已取到的页、
 * 标 truncated 且 totalKnown=false，继续下一个流域；只有实际请求过的流域全部失败才抛出首个错误，
 * 不把全失败呈现为已加载的空图层。
 */
async function fetchAllStations(request: StationLayerRequest): Promise<StationLayerData> {
  const normalizedContexts = normalizeBasinContexts(request.basinContexts)
  if (normalizedContexts.length === 0) throw new Error('代站图层缺少可用流域版本身份')
  const contexts = normalizedContexts.slice(0, STATION_CONTEXT_CAP)

  const stations: HydroMetStation[] = []
  const stationBasinIds: Record<string, string> = {}
  let total = 0
  let totalKnown = normalizedContexts.length <= contexts.length
  let truncated = normalizedContexts.length > contexts.length
  const failedBasinIds: string[] = []
  let attemptedContexts = 0
  let failedContexts = 0
  let firstError: unknown

  for (const context of contexts) {
    if (stations.length >= STATION_CLIENT_CAP) {
      truncated = true
      totalKnown = false
      break
    }

    attemptedContexts += 1
    let stationIdentity: { basinVersionId: string; modelId?: string } = { basinVersionId: context.basinVersionId }
    if (context.source) {
      try {
        const product = await fetchHydroMetLatestProduct({
          basinId: context.basinId,
          source: context.source,
          cycle: context.cycle ?? null,
        })
        stationIdentity = { basinVersionId: product.basin_version_id, modelId: product.model_id }
      } catch {
        // latest-product 不可用不是流域失败：保持 basin-only 身份。
      }
    }

    try {
      const firstPage = await fetchHydroMetStationsByIdentity(
        stationIdentity,
        { limit: Math.min(STATION_PAGE_LIMIT, STATION_CLIENT_CAP - stations.length), offset: 0 },
      )
      const basinTotal = Number.isFinite(firstPage.total_count) ? firstPage.total_count : firstPage.items.length
      total += basinTotal

      if (appendStations(stations, stationBasinIds, firstPage.items, context.basinId)) truncated = true

      let offset = firstPage.items.length
      while (offset < basinTotal && stations.length < STATION_CLIENT_CAP) {
        const remainingCap = STATION_CLIENT_CAP - stations.length
        const pageLimit = Math.min(STATION_PAGE_LIMIT, remainingCap)
        const page = await fetchHydroMetStationsByIdentity(
          stationIdentity,
          { limit: pageLimit, offset },
        )
        if (page.items.length === 0) break
        if (appendStations(stations, stationBasinIds, page.items, context.basinId)) truncated = true
        offset += page.items.length
      }

      if (offset < basinTotal) truncated = true
    } catch (error) {
      if (failedContexts === 0) firstError = error
      failedContexts += 1
      if (!failedBasinIds.includes(context.basinId)) failedBasinIds.push(context.basinId)
      truncated = true
      totalKnown = false
    }
  }

  if (failedContexts > 0 && failedContexts === attemptedContexts) throw firstError

  const loaded = stations.length
  return {
    stations,
    stationBasinIds,
    total,
    totalKnown,
    loaded,
    truncated: truncated || loaded < total,
    failedBasinIds,
  }
}

function appendStations(
  stations: HydroMetStation[],
  stationBasinIds: Record<string, string>,
  items: HydroMetStation[],
  basinId: string,
) {
  const remainingCap = STATION_CLIENT_CAP - stations.length
  const appendedItems = items.slice(0, Math.max(0, remainingCap))
  for (const station of appendedItems) {
    stations.push(station)
    if (station.station_id) stationBasinIds[station.station_id] = basinId
  }
  return items.length > appendedItems.length
}

export const useStationLayerDataStore = create<StationLayerDataState>((set, get) => ({
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
    const current = get()
    if (!current.loading && current.requestKey === key && current.data) return current.data
    const existing = inFlight.get(key)
    if (existing && activeRequestKey === key) return existing

    const nonce = ++requestNonce
    activeRequestKey = key
    set({ loading: true, error: null })

    let load!: Promise<StationLayerData>
    load = (async () => {
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

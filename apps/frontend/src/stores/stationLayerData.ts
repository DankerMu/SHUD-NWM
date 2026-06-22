import { create } from 'zustand'

import { getApiErrorMessage } from '@/api/response'
import {
  HYDRO_MET_STATION_LIMIT,
  fetchHydroMetLatestProduct,
  fetchHydroMetStations,
  type HydroMetStation,
} from '@/pages/hydroMet/bootstrap'
import { normalizeHydroMetCycle, type HydroMetSource } from '@/lib/hydroMet/queryState'
import { sanitizeHydroMetMessage } from '@/lib/hydroMet/runtime'

/**
 * 国家级守卫：单流域代站客户端缓存上限。达到后停止翻页并标 truncated，
 * 不让"看似完整"的图层掩盖缺失（spec: 诚实标注 truncation）。
 */
export const STATION_CLIENT_CAP = 5000
export const STATION_PAGE_LIMIT = HYDRO_MET_STATION_LIMIT

export interface StationLayerData {
  stations: HydroMetStation[]
  total: number
  loaded: number
  truncated: boolean
}

export interface StationLayerRequest {
  basinId: string
  /** 已解析的具体源；store 不接受 best/compare（类型即 GFS/IFS）。 */
  resolvedSource: HydroMetSource
  cycle: string | null
}

interface StationLayerDataState {
  data: StationLayerData | null
  loading: boolean
  error: string | null
  /** 当前已解析快照的请求键（basinId+source+cycle）；用于 UI 判定数据是否匹配当前请求。 */
  requestKey: string | null
  loadStationLayer: (request: StationLayerRequest) => Promise<StationLayerData>
  clear: () => void
}

function requestKeyOf(request: StationLayerRequest) {
  return `${request.basinId}::${request.resolvedSource}::${request.cycle ?? 'latest'}`
}

const inFlight = new Map<string, Promise<StationLayerData>>()
let requestNonce = 0
let activeRequestKey: string | null = null

/**
 * 严格身份分页：先取轻量 latest-product identity，再以其 model_id/basin_version_id 取站点，
 * 首页拿 total_count，再 offset 翻页直到 loaded≥total 或 loaded≥STATION_CLIENT_CAP。
 */
async function fetchAllStations(request: StationLayerRequest): Promise<StationLayerData> {
  const product = await fetchHydroMetLatestProduct({
    source: request.resolvedSource,
    cycle: request.cycle,
    basinId: request.basinId,
  })
  const productCycle = normalizeHydroMetCycle(product.cycle_time)
  if (request.cycle && productCycle && request.cycle !== productCycle) {
    throw new Error(`代站起报 ${request.cycle} 已不可用`)
  }
  if (product.status !== 'ready' || product.availability?.ready === false) {
    const reason = product.availability?.unavailable_reasons?.[0]
    throw new Error(reason ? `${reason.code}: ${reason.message}` : '代站 latest-product 未就绪')
  }
  if (!product.model_id || !product.basin_version_id) throw new Error('代站 latest-product 身份不完整')

  const firstPage = await fetchHydroMetStations(product, { limit: STATION_PAGE_LIMIT, offset: 0 })
  const total = Number.isFinite(firstPage.total_count) ? firstPage.total_count : firstPage.items.length
  const stations: HydroMetStation[] = [...firstPage.items]

  // 后续页：从首页之后继续翻，直到取全或触顶 client cap。
  let offset = stations.length
  while (stations.length < total && stations.length < STATION_CLIENT_CAP) {
    const remainingCap = STATION_CLIENT_CAP - stations.length
    const pageLimit = Math.min(STATION_PAGE_LIMIT, remainingCap)
    const page = await fetchHydroMetStations(product, { limit: pageLimit, offset })
    if (page.items.length === 0) break
    stations.push(...page.items)
    offset += page.items.length
  }

  const loaded = stations.length
  return {
    stations,
    total,
    loaded,
    truncated: loaded < total,
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
    const key = requestKeyOf(request)
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

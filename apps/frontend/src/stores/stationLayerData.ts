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
 * 安全阀：整个代站图层（全部流域合并后）的客户端站点上限，远高于现量。达到后合并在此截断并标 truncated，
 * 不让"看似完整"的图层掩盖缺失（spec: 诚实标注 truncation）。
 */
export const STATION_CLIENT_CAP = 50_000
/** 安全阀：最多请求前 N 个已解析流域上下文，远高于现量；超出的计入 unloadedBasinCount。 */
export const STATION_CONTEXT_CAP = 500
/** 单次加载内同时在取数的流域数上限。 */
export const STATION_FETCH_CONCURRENCY = 6
export const STATION_PAGE_LIMIT = HYDRO_MET_STATION_LIMIT

export interface StationLayerData {
  stations: HydroMetStation[]
  stationBasinIds: Record<string, string>
  total: number
  totalKnown: boolean
  loaded: number
  truncated: boolean
  /** 站点请求失败的流域 id（按流域上下文顺序、去重）；全部成功时为 []。非空时 truncated=true、total 只是下界。 */
  failedBasinIds: string[]
  /** 安全阀使其没有代站的流域上下文数（截断流域之后的 + 超出 STATION_CONTEXT_CAP 的）；未触发时为 0。 */
  unloadedBasinCount: number
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
 * 标 truncated 且 totalKnown=false，其余流域照常加载；只有参与合并的流域全部失败才抛出按上下文顺序的首个错误，
 * 不把全失败呈现为已加载的空图层。
 * 全部流域上下文都取（#2867）：STATION_FETCH_CONCURRENCY 个 worker 按上下文顺序认领流域；
 * 合并按上下文顺序进行，与各请求完成的先后无关。
 * 安全阀：已取到的站点数达到 STATION_CLIENT_CAP 后 worker 不再认领新流域；合并在累计达到 cap 的流域
 * （截断流域）处截断，其后流域取到的内容全部丢弃（不计入 stations / total / failedBasinIds），
 * 连同超出 STATION_CONTEXT_CAP 的流域一起计入 unloadedBasinCount。阀限制的是合并结果而非网络量：
 * 截断点确定之前，在途的流域各自最多翻页到 cap。
 * 被取代的加载（isCurrent() 为 false）不再认领流域、不再翻页，并以错误结束。
 */
async function fetchAllStations(request: StationLayerRequest, isCurrent: () => boolean): Promise<StationLayerData> {
  const normalizedContexts = normalizeBasinContexts(request.basinContexts)
  if (normalizedContexts.length === 0) throw new Error('代站图层缺少可用流域版本身份')
  const contexts = normalizedContexts.slice(0, STATION_CONTEXT_CAP)

  const results: Array<BasinStations | undefined> = []
  let nextIndex = 0
  let fetchedCount = 0
  let superseded = false

  /** 取一个流域的全部分页；加载已被取代时返回 null。 */
  const loadBasin = async (context: (typeof contexts)[number]): Promise<BasinStations | null> => {
    const result: BasinStations = { rows: [], total: 0, incomplete: false, failed: false, error: undefined }
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
      if (!isCurrent()) return null
      const firstPage = await fetchHydroMetStationsByIdentity(stationIdentity, { limit: STATION_PAGE_LIMIT, offset: 0 })
      const basinTotal = Number.isFinite(firstPage.total_count) ? firstPage.total_count : firstPage.items.length
      result.total = basinTotal
      for (const station of firstPage.items) result.rows.push(station)
      fetchedCount += firstPage.items.length

      let offset = firstPage.items.length
      // 单流域内始终按 STATION_PAGE_LIMIT 翻页，本流域自己的行数达到 cap 即停。
      while (offset < basinTotal && result.rows.length < STATION_CLIENT_CAP) {
        if (!isCurrent()) return null
        const page = await fetchHydroMetStationsByIdentity(stationIdentity, { limit: STATION_PAGE_LIMIT, offset })
        if (page.items.length === 0) break
        for (const station of page.items) result.rows.push(station)
        fetchedCount += page.items.length
        offset += page.items.length
      }

      result.incomplete = offset < basinTotal
    } catch (error) {
      result.failed = true
      result.error = error
    }
    return result
  }

  const worker = async () => {
    while (nextIndex < contexts.length && fetchedCount < STATION_CLIENT_CAP) {
      if (!isCurrent()) {
        superseded = true
        return
      }
      const index = nextIndex
      nextIndex += 1
      const result = await loadBasin(contexts[index])
      if (!result) {
        superseded = true
        return
      }
      results[index] = result
    }
  }
  await Promise.all(Array.from({ length: Math.min(STATION_FETCH_CONCURRENCY, contexts.length) }, worker))
  if (superseded) throw new Error('代站图层请求已被取代')

  // 按上下文顺序合并；未被认领的流域只会出现在截断流域之后。
  const stations: HydroMetStation[] = []
  const stationBasinIds: Record<string, string> = {}
  let total = 0
  let totalKnown = true
  let truncated = false
  const failedBasinIds: string[] = []
  let mergedContexts = 0
  let failedContexts = 0
  let firstError: unknown

  for (let index = 0; index < contexts.length; index += 1) {
    const result = results[index]
    if (!result) break
    const basinId = contexts[index].basinId
    mergedContexts += 1
    total += result.total
    if (result.failed) {
      if (failedContexts === 0) firstError = result.error
      failedContexts += 1
      if (!failedBasinIds.includes(basinId)) failedBasinIds.push(basinId)
      truncated = true
      totalKnown = false
    }

    const appended = result.rows.slice(0, STATION_CLIENT_CAP - stations.length)
    for (const station of appended) {
      stations.push(station)
      if (station.station_id) stationBasinIds[station.station_id] = basinId
    }
    if (result.incomplete || result.rows.length > appended.length) truncated = true
    if (stations.length >= STATION_CLIENT_CAP) break
  }

  if (failedContexts > 0 && failedContexts === mergedContexts) throw firstError

  const unloadedBasinCount = normalizedContexts.length - mergedContexts
  if (unloadedBasinCount > 0) {
    truncated = true
    totalKnown = false
  }

  const loaded = stations.length
  return {
    stations,
    stationBasinIds,
    total,
    totalKnown,
    loaded,
    truncated: truncated || loaded < total,
    failedBasinIds,
    unloadedBasinCount,
  }
}

/** 单个流域上下文的取数结果；首页即失败时 total 为 0，中途失败时保留已取到的页与 total。 */
interface BasinStations {
  rows: HydroMetStation[]
  total: number
  /** 未抛错但没取全（本流域行数达到 cap，或后端提前返回空页）。 */
  incomplete: boolean
  failed: boolean
  error: unknown
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
        const data = await fetchAllStations(request, () => nonce === requestNonce && activeRequestKey === key)
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

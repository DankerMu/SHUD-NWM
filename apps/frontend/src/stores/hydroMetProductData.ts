import { create } from 'zustand'

import { getApiErrorMessage } from '@/api/response'
import { loadHydroMetBootstrap, type QhhLatestProduct } from '@/pages/hydroMet/bootstrap'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import { sanitizeHydroMetMessage } from '@/lib/hydroMet/runtime'

/**
 * 共享 QHH latest-product 解析 store（M26-4）。两类地图 popup（河段 / 代站）共用同一份
 * 选中流域 + 已解析具体源（GFS/IFS）的 latest-product 身份，避免各自重复 bootstrap。
 * store 内部带 in-flight 去重 + nonce 过期防护，与 stationLayerData 同风格；不接受 best/compare
 * （类型即 GFS/IFS），杜绝拿未解析源直接打曲线接口。
 */

export interface HydroMetProductRequest {
  basinId: string
  /** 已解析的具体源；best/compare 必须先经 resolvedSource 落为 GFS/IFS 再进来。 */
  resolvedSource: HydroMetSource
  cycle: string | null
}

interface HydroMetProductDataState {
  product: QhhLatestProduct | null
  loading: boolean
  error: string | null
  /** 当前已解析快照的请求键（basinId+source+cycle）；UI 据此判定 product 是否匹配当前请求。 */
  requestKey: string | null
  loadProduct: (request: HydroMetProductRequest) => Promise<QhhLatestProduct>
  clear: () => void
}

export function hydroMetProductRequestKey(request: HydroMetProductRequest) {
  return `${request.basinId}::${request.resolvedSource}::${request.cycle ?? 'latest'}`
}

const inFlight = new Map<string, Promise<QhhLatestProduct>>()
let requestNonce = 0
let activeRequestKey: string | null = null

async function fetchProduct(request: HydroMetProductRequest): Promise<QhhLatestProduct> {
  const bootstrap = await loadHydroMetBootstrap({
    source: request.resolvedSource,
    cycle: request.cycle,
    basinId: request.basinId,
  })
  if (bootstrap.status !== 'ready' || !bootstrap.product) {
    throw new Error(bootstrap.latestReasons[0] ?? `latest-product 未就绪（${bootstrap.status}）`)
  }
  return bootstrap.product
}

export const useHydroMetProductDataStore = create<HydroMetProductDataState>((set) => ({
  product: null,
  loading: false,
  error: null,
  requestKey: null,
  clear: () => {
    requestNonce += 1
    activeRequestKey = null
    inFlight.clear()
    set({ product: null, loading: false, error: null, requestKey: null })
  },
  loadProduct: async (request) => {
    const key = hydroMetProductRequestKey(request)
    const existing = inFlight.get(key)
    if (existing && activeRequestKey === key) return existing

    const nonce = ++requestNonce
    activeRequestKey = key
    set({ loading: true, error: null })

    const load = (async () => {
      try {
        const product = await fetchProduct(request)
        if (nonce === requestNonce && activeRequestKey === key) {
          set({ product, loading: false, error: null, requestKey: key })
        }
        return product
      } catch (error) {
        const message = sanitizeHydroMetMessage(getApiErrorMessage(error, 'latest-product 加载失败'), 'latest-product 加载失败')
        if (nonce === requestNonce && activeRequestKey === key) {
          set({ product: null, loading: false, error: message, requestKey: key })
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

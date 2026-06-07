import { useEffect } from 'react'

import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import type { QhhLatestProduct } from '@/pages/hydroMet/bootstrap'
import {
  hydroMetProductRequestKey,
  useHydroMetProductDataStore,
} from '@/stores/hydroMetProductData'

export interface HydroMetProductModel {
  /** 解析后的 QHH latest-product；basinId 为空 / 源未解析 / 加载失败时为 null。 */
  product: QhhLatestProduct | null
  loading: boolean
  error: string | null
  /** honest 空态原因；product 为 null 时给出"等待 Best Available 解析 / 请选择流域"等说明。 */
  reason: string | null
}

/**
 * 共享 product 解析 hook（M26-4）。两类 popup（河段 / 代站）共用：仅在拿到 basinId 且
 * resolvedSource 已落为 GFS/IFS 时取数；best/compare 未解析或无 basin 时返回 product=null +
 * honest 原因，杜绝拿未解析源直接打 station/river forecast 接口。
 */
export function useHydroMetProduct({
  basinId,
  resolvedSource,
  cycle,
}: {
  basinId: string | null
  /** basin detail / overview 的 resolvedSource；best/compare 未解析时为非 GFS/IFS。 */
  resolvedSource: string | null
  cycle: string | null
}): HydroMetProductModel {
  const product = useHydroMetProductDataStore((store) => store.product)
  const loading = useHydroMetProductDataStore((store) => store.loading)
  const error = useHydroMetProductDataStore((store) => store.error)
  const requestKey = useHydroMetProductDataStore((store) => store.requestKey)
  const loadProduct = useHydroMetProductDataStore((store) => store.loadProduct)
  const clear = useHydroMetProductDataStore((store) => store.clear)

  const concreteSource: HydroMetSource | null =
    resolvedSource === 'GFS' || resolvedSource === 'IFS' ? resolvedSource : null
  const shouldFetch = Boolean(basinId) && Boolean(concreteSource)
  const expectedKey =
    shouldFetch && basinId && concreteSource
      ? hydroMetProductRequestKey({ basinId, resolvedSource: concreteSource, cycle })
      : null

  useEffect(() => {
    if (!shouldFetch || !basinId || !concreteSource) {
      // 无 basinId / 源未解析：清掉过期 product，避免误用别的流域身份打曲线接口。
      clear()
      return
    }
    void loadProduct({ basinId, resolvedSource: concreteSource, cycle }).catch(() => undefined)
  }, [basinId, concreteSource, cycle, clear, loadProduct, shouldFetch])

  const matches = expectedKey !== null && requestKey === expectedKey
  const currentProduct = matches ? product : null

  const reason = (() => {
    if (currentProduct) return null
    if (!basinId) return '请选择流域'
    if (!concreteSource) return '等待 Best Available 解析'
    if (loading) return 'latest-product 加载中'
    if (matches && error) return error
    return 'latest-product 加载中'
  })()

  return {
    product: currentProduct,
    loading,
    error: matches ? error : null,
    reason,
  }
}

import { useCallback, useEffect, useState } from 'react'

import { getApiErrorMessage } from '@/api/response'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import { sanitizeHydroMetMessage } from '@/lib/hydroMet/runtime'
import { fetchHydroMetLatestProduct, type QhhLatestProduct } from '@/pages/hydroMet/bootstrap'

export const M11_POPUP_SOURCES: HydroMetSource[] = ['GFS', 'IFS']

export interface M11PopupProductModel {
  /** 当前 source 下解析到的 latest-product；加载中 / 失败 / 无 basin 时为 null。 */
  product: QhhLatestProduct | null
  loading: boolean
  error: string | null
  /** honest 空态原因（product=null 时给出说明）。 */
  reason: string | null
  /** 用户在弹窗内选择的 source（GFS/IFS）。 */
  source: HydroMetSource
  setSource: (source: HydroMetSource) => void
  /**
   * 真实可得的起报时间列表。后端仅 latest-product，故至多一项（当前 product 的 cycle_time）；
   * 不编造多个起报时间。无解析产品时为空数组。
   */
  issueTimes: string[]
  /** 当前所选起报时间（=已解析产品的 cycle_time），无产品时为 null。 */
  issueTime: string | null
}

/**
 * 弹窗内 source/起报时间受控的 product 解析（M26 单页全屏）。
 * 与地图/全局取数解耦：弹窗自持 local source（默认取传入 initialSource，回退 GFS），
 * 改 source → 以新源重取 latest-product，cycle 如实随之更新。
 * 后端仅 latest-product：起报时间列表 = 已解析产品 cycle_time 单项（不编造）。
 */
export function useHydroMetPopupProduct({
  basinId,
  initialSource,
}: {
  basinId: string | null
  /** 弹窗初始源；best/compare 未解析时回退 GFS（弹窗内仍可切换）。 */
  initialSource: HydroMetSource | null
}): M11PopupProductModel {
  const [source, setSource] = useState<HydroMetSource>(initialSource ?? 'GFS')
  const [product, setProduct] = useState<QhhLatestProduct | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // basinId 变化（换流域）时把 source 重置回初始解析源，避免跨流域沿用旧选择。
  useEffect(() => {
    setSource(initialSource ?? 'GFS')
    // 仅依赖 basinId：initialSource 在同一流域内变化不应覆盖用户弹窗内选择。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [basinId])

  useEffect(() => {
    if (!basinId) {
      setProduct(null)
      setError(null)
      setLoading(false)
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    void fetchHydroMetLatestProduct({ source, cycle: null, basinId })
      .then((resolved) => {
        if (cancelled) return
        setProduct(resolved)
        setError(null)
        setLoading(false)
      })
      .catch((caught) => {
        if (cancelled) return
        setProduct(null)
        setError(sanitizeHydroMetMessage(getApiErrorMessage(caught, 'latest-product 加载失败'), 'latest-product 加载失败'))
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [basinId, source])

  const reason = (() => {
    if (product) return null
    if (!basinId) return '请选择流域'
    if (loading) return 'latest-product 加载中'
    return error ?? 'latest-product 加载中'
  })()

  const handleSetSource = useCallback((next: HydroMetSource) => setSource(next), [])

  return {
    product,
    loading,
    error,
    reason,
    source,
    setSource: handleSetSource,
    issueTimes: product?.cycle_time ? [product.cycle_time] : [],
    issueTime: product?.cycle_time ?? null,
  }
}

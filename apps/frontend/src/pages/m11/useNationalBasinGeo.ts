import { useEffect, useState } from 'react'

import type { FeatureCollection } from 'geojson'

/** 全国静态边界几何。河道已迁移到按瓦片加载的 national river-network MVT。 */
export interface NationalBasinGeo {
  domain: FeatureCollection | null
  river: FeatureCollection | null
}

/** hook 返回：静态几何数据 + 加载态（供上层抑制"边界未就绪"瞬态空态，避免刷新闪烁）。 */
export interface NationalBasinGeoState extends NationalBasinGeo {
  loading: boolean
}

const EMPTY: NationalBasinGeo = { domain: null, river: null }

// 模块级缓存：轻量 domain GeoJSON 整个会话只取一次。
let cached: NationalBasinGeo | null = null
let inflight: Promise<NationalBasinGeo> | null = null

async function fetchOne(url: string): Promise<FeatureCollection | null> {
  try {
    const response = await fetch(url)
    if (!response.ok) return null
    const data = (await response.json()) as FeatureCollection
    return data && Array.isArray(data.features) ? data : null
  } catch {
    return null
  }
}

async function loadNationalBasinGeo(): Promise<NationalBasinGeo> {
  if (cached) return cached
  if (!inflight) {
    inflight = fetchOne('/geo/national-basin-domain.geojson').then((domain) => {
      // national-basin-river.geojson is intentionally not fetched: it is ~45 MB
      // decoded and used to block first paint. Base rivers now stream as MVT/PBF.
      cached = { domain, river: null }
      return cached
    })
  }
  return inflight
}

/**
 * 全国总览底图几何 hook。honest：取数失败/缺文件 → null，前端不画该底层（诚实降级）。
 * 仅在 active 时取数（其它图层/详情模式不需要）。
 */
export function useNationalBasinGeo(active: boolean): NationalBasinGeoState {
  const [geo, setGeo] = useState<NationalBasinGeo>(cached ?? EMPTY)
  // active 且无缓存 → 首帧即 loading=true，覆盖"取数返回前"窗口；缓存命中/未激活则不 loading。
  const [loading, setLoading] = useState<boolean>(() => active && !cached)
  useEffect(() => {
    if (!active || cached) {
      if (cached) setGeo(cached)
      setLoading(false)
      return
    }
    let cancelled = false
    setLoading(true)
    void loadNationalBasinGeo().then((next) => {
      if (!cancelled) {
        setGeo(next)
        setLoading(false)
      }
    })
    return () => {
      cancelled = true
    }
  }, [active])
  return active ? { ...geo, loading } : { ...EMPTY, loading: false }
}

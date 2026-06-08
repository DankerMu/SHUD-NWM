import { useEffect, useState } from 'react'

import type { FeatureCollection } from 'geojson'

/** 全国静态底图几何（来自各流域 SHUD shp：domain 溶解轮廓 + river 河道），来源 public/geo。 */
export interface NationalBasinGeo {
  domain: FeatureCollection | null
  river: FeatureCollection | null
}

const EMPTY: NationalBasinGeo = { domain: null, river: null }

// 模块级缓存：两份静态 GeoJSON 整个会话只取一次。
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
    inflight = Promise.all([
      fetchOne('/geo/national-basin-domain.geojson'),
      fetchOne('/geo/national-basin-river.geojson'),
    ]).then(([domain, river]) => {
      cached = { domain, river }
      return cached
    })
  }
  return inflight
}

/**
 * 全国总览底图几何 hook。honest：取数失败/缺文件 → null，前端不画该底层（诚实降级）。
 * 仅在 active 时取数（其它图层/详情模式不需要）。
 */
export function useNationalBasinGeo(active: boolean): NationalBasinGeo {
  const [geo, setGeo] = useState<NationalBasinGeo>(cached ?? EMPTY)
  useEffect(() => {
    if (!active || cached) {
      if (cached) setGeo(cached)
      return
    }
    let cancelled = false
    void loadNationalBasinGeo().then((next) => {
      if (!cancelled) setGeo(next)
    })
    return () => {
      cancelled = true
    }
  }, [active])
  return active ? geo : EMPTY
}

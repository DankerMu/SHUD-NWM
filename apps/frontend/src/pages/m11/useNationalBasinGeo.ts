import { useEffect, useState } from 'react'

import type { FeatureCollection } from 'geojson'

import { getApiErrorMessage } from '@/api/response'

/** 全国静态边界几何。河道已迁移到按瓦片加载的 national river-network MVT。 */
export interface NationalBasinGeo {
  domain: FeatureCollection | null
  river: FeatureCollection | null
}

/** hook 返回：静态几何数据 + 加载态（供上层抑制"边界未就绪"瞬态空态，避免刷新闪烁）+ 失败原因。 */
export interface NationalBasinGeoState extends NationalBasinGeo {
  loading: boolean
  error: string | null
}

/** 共享 promise 链的结算结果：永不 reject，失败以 error 携带原因。 */
interface NationalBasinGeoResult {
  geo: NationalBasinGeo | null
  error: string | null
}

const EMPTY: NationalBasinGeo = { domain: null, river: null }

// 模块级缓存：轻量 domain GeoJSON 整个会话只取一次；失败不缓存，下次挂载/再激活重试。
let cached: NationalBasinGeo | null = null
let inflight: Promise<NationalBasinGeoResult> | null = null

async function fetchOne(url: string): Promise<FeatureCollection> {
  const response = await fetch(url)
  if (!response.ok) throw new Error(`national basin domain HTTP ${response.status}`)
  let data: unknown
  try {
    data = await response.json()
  } catch {
    throw new Error('national basin domain payload malformed')
  }
  if (!data || typeof data !== 'object' || !Array.isArray((data as FeatureCollection).features)) {
    throw new Error('national basin domain payload malformed')
  }
  return data as FeatureCollection
}

async function loadNationalBasinGeo(): Promise<NationalBasinGeoResult> {
  if (cached) return { geo: cached, error: null }
  if (!inflight) {
    inflight = fetchOne('/geo/national-basin-domain.geojson')
      .then(
        (domain): NationalBasinGeoResult => {
          // national-basin-river.geojson is intentionally not fetched: it is ~45 MB
          // decoded and used to block first paint. Base rivers now stream as MVT/PBF.
          cached = { domain, river: null }
          return { geo: cached, error: null }
        },
        (err: unknown): NationalBasinGeoResult => ({
          geo: null,
          error: getApiErrorMessage(err, '全国流域边界加载失败'),
        }),
      )
      .finally(() => {
        // 共享链结算即清空：失败结果不被后续消费者复用（成功已进 cached）。
        inflight = null
      })
  }
  return inflight
}

/**
 * 全国总览底图几何 hook。honest：取数失败/缺文件 → domain/river 为 null，前端不画该底层（诚实降级），
 * 失败原因经 error 暴露。仅在 active 时取数（其它图层/详情模式不需要）。
 */
export function useNationalBasinGeo(active: boolean): NationalBasinGeoState {
  const [geo, setGeo] = useState<NationalBasinGeo>(cached ?? EMPTY)
  // active 且无缓存 → 首帧即 loading=true，覆盖"取数返回前"窗口；缓存命中/未激活则不 loading。
  const [loading, setLoading] = useState<boolean>(() => active && !cached)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (!active || cached) {
      if (cached) {
        setGeo(cached)
        setError(null)
      }
      setLoading(false)
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    void loadNationalBasinGeo().then((result) => {
      if (!cancelled) {
        setGeo(result.geo ?? EMPTY)
        setError(result.error)
        setLoading(false)
      }
    })
    return () => {
      cancelled = true
    }
  }, [active])
  return active ? { ...geo, loading, error } : { ...EMPTY, loading: false, error: null }
}

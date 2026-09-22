import { useCallback, useEffect, useRef, useState, type MutableRefObject, type ReactNode } from 'react'
import type { MapRef, MapStyle } from 'react-map-gl/maplibre'

import { buildApiTileUrlTemplate } from '@/api/base'

import type { M11Bbox } from '@/lib/m11/overviewDataContracts'
import type { M11Basemap } from '@/lib/m11/queryState'

export interface M11MapCameraFit {
  bounds: [[number, number], [number, number]]
  padding?: number
}

export interface M11MapCameraFlyTo {
  center: [number, number]
  zoom?: number
}

/** 流域 bbox → 相机 fit（留 36px 内边距）；bbox 缺失时不 fit。 */
export function bboxToMapFit(bbox: M11Bbox | null | undefined): M11MapCameraFit | null {
  if (!bbox) return null
  return {
    bounds: [
      [bbox.minLon, bbox.minLat],
      [bbox.maxLon, bbox.maxLat],
    ],
    padding: 36,
  }
}

type M11InitialViewState =
  | typeof CHINA_VIEW_STATE
  | {
      bounds: M11MapCameraFit['bounds']
      fitBoundsOptions: { padding: number }
    }

export const CHINA_VIEW_STATE = {
  longitude: 104,
  latitude: 35,
  zoom: 3.35,
}

export const m11MapStyleUrls: Record<M11Basemap, string> = {
  terrain: 'm11://basemaps/terrain',
  satellite: 'm11://basemaps/satellite',
  vector: 'm11://basemaps/vector',
}

// 天地图（Tianditu）栅格底图，经同源代理 `/api/v1/basemap/tianditu/*` 取瓦片：key 只在服务端
// （NHMS_TIANDITU_KEY），瓦片在服务端文件缓存，访问量不再逐人消耗 key 配额（天地图限流即 429）。
// 每种底图叠「底图 + 中文注记」两层；最底下垫一层纯色 background，瓦片失败时地图不会是空洞。
const TIANDITU_ATTRIBUTION = '© 天地图'
const M11_BASEMAP_BACKGROUND_COLOR = '#eef1f4'
export const M11_BASEMAP_UNAVAILABLE_NOTICE =
  '底图服务暂时不可用（天地图限流或网络异常），河网与预报图层不受影响，底图稍后自动恢复。'

export const m11MapStyles: Record<M11Basemap, MapStyle> = {
  terrain: tiandituStyle('ter', 'cta', 14),
  satellite: tiandituStyle('img', 'cia', 18),
  vector: tiandituStyle('vec', 'cva', 18),
}

const m11BasemapSourceIds = new Set(Object.values(m11MapStyles).flatMap((style) => Object.keys(style.sources)))

export function useM11MapCamera({
  fitTo,
  flyTo,
  mapRef,
}: {
  fitTo?: M11MapCameraFit | null
  flyTo?: M11MapCameraFlyTo | null
  mapRef: MutableRefObject<MapRef | null>
}): M11InitialViewState {
  const lastFitKeyRef = useRef<string | null>(null)
  const lastFlyKeyRef = useRef<string | null>(null)
  // 地图子树 remount 时相机会重置回 initialViewState。首挂载时若已知 fitTo（静态 bbox 已缓存
  // 同步可得），直接用该 bounds 初始化，避免「先闪回全国再飞入」的强制回初始视角（#1）；
  // mount 后到达的 fitTo 仍由下方 effect 兜底。
  const [initialViewState] = useState<M11InitialViewState>(() =>
    fitTo ? { bounds: fitTo.bounds, fitBoundsOptions: { padding: fitTo.padding ?? 32 } } : CHINA_VIEW_STATE,
  )

  useEffect(() => {
    const map = mapRef.current
    if (!fitTo || !map) return
    const fitKey = mapFitKey(fitTo)
    if (fitKey === lastFitKeyRef.current) return
    lastFitKeyRef.current = fitKey
    map.fitBounds(fitTo.bounds, { padding: fitTo.padding ?? 32, duration: 450 })
  }, [fitTo, mapRef])

  useEffect(() => {
    const map = mapRef.current
    if (!flyTo || !map) return
    const flyKey = mapFlyKey(flyTo)
    if (flyKey === lastFlyKeyRef.current) return
    lastFlyKeyRef.current = flyKey
    map.flyTo({ center: flyTo.center, zoom: flyTo.zoom, duration: 450 })
  }, [flyTo, mapRef])

  return initialViewState
}

export function m11MapSourceErrorResetKey({
  basinFeatureCount,
  overlaySourceId,
  basemap,
  layer,
  validTime,
}: {
  basinFeatureCount: number
  overlaySourceId?: string | null
  basemap: M11Basemap
  layer: string
  validTime: string | null
}) {
  return [basinFeatureCount, overlaySourceId ?? '', basemap, layer, validTime ?? ''].join('|')
}

export function useM11MapSourceError(resetKey: string) {
  const [mapSourceError, setMapSourceError] = useState<string | null>(null)

  useEffect(() => {
    setMapSourceError(null)
  }, [resetKey])

  const handleMapError = useCallback((event: { error?: { message?: string }; sourceId?: string }) => {
    // 底图瓦片失败每张一个 error 事件：收敛为一条固定提示（同值 setState 不触发重渲染），
    // 且不覆盖已显示的业务图层错误。
    if (event.sourceId && m11BasemapSourceIds.has(event.sourceId)) {
      setMapSourceError((current) => (current && current !== M11_BASEMAP_UNAVAILABLE_NOTICE ? current : M11_BASEMAP_UNAVAILABLE_NOTICE))
      return
    }
    const message = event.error?.message ?? ''
    // 天地图栅格 style 无 glyphs：symbol 文本层（如代站 cluster 计数）的 style 校验错误
    // 只影响该层文字渲染，不影响其它图层——降级为 console 警告，不弹错误横幅。
    if (message.includes('glyphs')) {
      console.warn('[m11-map] symbol text layer skipped (no glyphs in raster basemap style):', message)
      return
    }
    setMapSourceError(message || '地图源加载失败，受影响图层暂不可用。')
  }, [])

  return { mapSourceError, handleMapError }
}

export function M11MapStatusOverlays({
  loading,
  boundaryLoading,
  basinBoundaryOverlayEnabled,
  basinCount,
  basinFeatureCount,
  skippedBasinGeometryCount,
  unavailableReason,
  selectedSegmentMapState,
  selectedSegmentUnavailableReason,
  mapSourceError,
}: {
  loading: boolean
  boundaryLoading: boolean
  basinBoundaryOverlayEnabled: boolean
  basinCount: number
  basinFeatureCount: number
  skippedBasinGeometryCount: number
  unavailableReason: string | null
  selectedSegmentMapState: 'idle' | 'selected-layer' | 'unavailable'
  selectedSegmentUnavailableReason: string | null
  mapSourceError: string | null
}) {
  return (
    <>
      {basinBoundaryOverlayEnabled && !loading && !boundaryLoading && basinCount > 0 && basinFeatureCount === 0 ? (
        <M11MapStatusNotice testId="m11-basin-layer-unavailable" topClassName="top-20">
          {skippedBasinGeometryCount > 0
            ? '当前可见流域边界超过客户端渲染预算，地图不会注册过大的边界源。'
            : '当前没有可见流域边界。'}
        </M11MapStatusNotice>
      ) : null}

      {!loading && unavailableReason ? (
        <M11MapStatusNotice testId="m11-map-unavailable" topClassName="top-20">
          {unavailableReason}
        </M11MapStatusNotice>
      ) : null}

      {!loading && selectedSegmentMapState === 'unavailable' ? (
        <M11MapStatusNotice testId="m11-selected-segment-map-unavailable" topClassName="top-44">
          {selectedSegmentUnavailableReason}
        </M11MapStatusNotice>
      ) : null}

      {mapSourceError ? (
        <M11MapStatusNotice testId="m11-map-source-error" topClassName="top-32">
          {mapSourceError}
        </M11MapStatusNotice>
      ) : null}
    </>
  )
}

function M11MapStatusNotice({
  children,
  testId,
  topClassName,
}: {
  children: ReactNode
  testId: string
  topClassName: string
}) {
  return (
    <div
      className={`absolute left-1/2 -translate-x-1/2 ${topClassName} z-[90] max-w-[min(28rem,calc(100%-2.5rem))] rounded-md border border-warning/40 bg-white/95 px-3 py-2 text-sm text-neutral-800 shadow-md`}
      role="status"
      data-testid={testId}
    >
      {children}
    </div>
  )
}

// base = 底图图层码（vec/img/ter），annotation = 对应中文注记码（cva/cia/cta）；maxzoom 与天地图
// 各图层发布级别一致（ter/cta 到 14，其余到 18），更高级别由 MapLibre 放大上一级瓦片，不再请求。
function tiandituStyle(base: string, annotation: string, maxzoom: number): MapStyle {
  const source = (layer: string) => ({
    type: 'raster' as const,
    tiles: [buildApiTileUrlTemplate(`/api/v1/basemap/tianditu/${layer}/{z}/{x}/{y}`)],
    tileSize: 256,
    maxzoom,
    attribution: TIANDITU_ATTRIBUTION,
  })
  return {
    version: 8,
    sources: {
      [`${base}-base`]: source(base),
      [`${annotation}-anno`]: source(annotation),
    },
    layers: [
      { id: 'basemap-background', type: 'background', paint: { 'background-color': M11_BASEMAP_BACKGROUND_COLOR } },
      { id: `${base}-base`, type: 'raster', source: `${base}-base` },
      { id: `${annotation}-anno`, type: 'raster', source: `${annotation}-anno` },
    ],
  }
}

function mapFitKey(fitTo: M11MapCameraFit) {
  const [[minLon, minLat], [maxLon, maxLat]] = fitTo.bounds
  return `${minLon},${minLat},${maxLon},${maxLat},${fitTo.padding ?? 32}`
}

function mapFlyKey(flyTo: M11MapCameraFlyTo) {
  return `${flyTo.center[0]},${flyTo.center[1]},${flyTo.zoom ?? ''}`
}

import { useCallback, useEffect, useRef, useState, type MutableRefObject, type ReactNode } from 'react'
import type { MapRef, MapStyle } from 'react-map-gl/maplibre'

import type { M11Basemap } from '@/lib/m11/queryState'

export interface M11MapCameraFit {
  bounds: [[number, number], [number, number]]
  padding?: number
}

export interface M11MapCameraFlyTo {
  center: [number, number]
  zoom?: number
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

// 天地图（Tianditu）WMTS 栅格底图。key 由 VITE_TIANDITU_KEY 覆盖，缺省用部署 key。
// 每种底图叠「底图 + 中文注记」两层；t0..t7 子域由 MapLibre 在 tiles 数组间轮询负载均衡。
const TIANDITU_KEY = (import.meta.env.VITE_TIANDITU_KEY as string | undefined) ?? '25475cca5080dc60cb126b94fd6358d3'
const TIANDITU_ATTRIBUTION = '© 天地图'

export const m11MapStyles: Record<M11Basemap, MapStyle> = {
  terrain: tiandituStyle('ter', 'cta'),
  satellite: tiandituStyle('img', 'cia'),
  vector: tiandituStyle('vec', 'cva'),
}

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
  // 总览↔详情切换会 remount 整棵地图子树，相机重置回 initialViewState。挂载时若已知 fitTo
  // （从总览点入，静态 bbox 已缓存同步可得），直接用该 bounds 初始化，避免「先闪回全国再飞入」
  // 的强制回初始视角（#1）；mount 后到达的 fitTo 仍由下方 effect 兜底。
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

  const handleMapError = useCallback((event: { error?: { message?: string } }) => {
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
  basinCount,
  basinFeatureCount,
  skippedBasinGeometryCount,
  unavailableReason,
  basinRiverUnavailableReason,
  selectedSegmentMapState,
  selectedSegmentUnavailableReason,
  mapSourceError,
}: {
  loading: boolean
  boundaryLoading: boolean
  basinCount: number
  basinFeatureCount: number
  skippedBasinGeometryCount: number
  unavailableReason: string | null
  basinRiverUnavailableReason: string | null
  selectedSegmentMapState: 'idle' | 'selected-layer' | 'unavailable'
  selectedSegmentUnavailableReason: string | null
  mapSourceError: string | null
}) {
  return (
    <>
      {!loading && !boundaryLoading && basinCount > 0 && basinFeatureCount === 0 ? (
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

      {!loading && basinRiverUnavailableReason ? (
        <M11MapStatusNotice testId="m11-basin-river-unavailable" topClassName="top-32">
          {basinRiverUnavailableReason}
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

function tiandituTiles(layer: string): string[] {
  return ['t0', 't1', 't2', 't3', 't4', 't5', 't6', 't7'].map(
    (sub) => `https://${sub}.tianditu.gov.cn/DataServer?T=${layer}_w&x={x}&y={y}&l={z}&tk=${TIANDITU_KEY}`,
  )
}

// base = 底图图层码（vec/img/ter），annotation = 对应中文注记码（cva/cia/cta）。
function tiandituStyle(base: string, annotation: string): MapStyle {
  return {
    version: 8,
    sources: {
      [`${base}-base`]: { type: 'raster', tiles: tiandituTiles(base), tileSize: 256, attribution: TIANDITU_ATTRIBUTION },
      [`${annotation}-anno`]: { type: 'raster', tiles: tiandituTiles(annotation), tileSize: 256, attribution: TIANDITU_ATTRIBUTION },
    },
    layers: [
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

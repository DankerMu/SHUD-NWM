import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Map, {
  Layer,
  Marker,
  NavigationControl,
  Popup,
  ScaleControl,
  Source,
  type MapLayerMouseEvent,
  type MapRef,
  type MapStyle,
} from 'react-map-gl/maplibre'
import type { ReactNode } from 'react'
import type { FeatureCollection } from 'geojson'
import type { LayerProps } from 'react-map-gl/maplibre'
import type { FilterSpecification } from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'

import type { components } from '@/api/types'
import { cn } from '@/lib/cn'
import type { FloodReturnPeriodFeatureCollection } from '@/lib/floodReturnPeriodGeoJson'
import {
  buildBasinFeatureCollection,
  buildBasinRiverFeatureCollection,
  buildM11RegisteredOverlay,
  buildM11RenderedNationalRiverCollection,
  buildSelectedSegmentFeatureCollection,
  countSkippedBasinGeometries,
  m11BasinLabelAnchor,
  m11SelectedLayerUnavailableReason,
  segmentFilter,
  zoomScaledValueWidth,
  type BasinFeatureCollection,
  type BasinRiverFeature,
  type BasinRiverFeatureCollection,
  type M11RegisteredOverlay,
  type SelectedSegmentFeatureCollection,
} from '@/components/map/m11MapBuilders'
import {
  type BasinSegmentRow,
  type LayerState,
  type M11WarningLevel,
  type OverviewBasin,
} from '@/lib/m11/overviewDataContracts'
import type { M11Basemap, M11Layer, M11QueryState } from '@/lib/m11/queryState'

export interface M11MapOverlayInteraction {
  layerId: M11Layer | 'met-stations' | 'basin-boundaries' | 'basin-river-segments'
  event: MapLayerMouseEvent
  feature?: NonNullable<MapLayerMouseEvent['features']>[number]
}

// 代站 clustered-GeoJSON 图层的固定 source/layer id。以 layerId/source 抽象集中于此，
// 未来切换到后端 station-MVT 点图层瓦片端点时，只需替换 <Source> 实现而无需改交互/分发逻辑。
const MET_STATION_SOURCE_ID = 'm11-met-stations-source'
const MET_STATION_CLUSTER_LAYER_ID = 'clusters'
const MET_STATION_CLUSTER_COUNT_LAYER_ID = 'cluster-count'
const MET_STATION_POINT_LAYER_ID = 'met-stations-point'

export interface M11StationFeatureCollection {
  type: 'FeatureCollection'
  features: Array<{
    type: 'Feature'
    geometry: { type: 'Point'; coordinates: [number, number] }
    properties: { station_id: string; station_name: string | null; basin_id: string | null }
  }>
}

export interface M11MapCameraFit {
  bounds: [[number, number], [number, number]]
  padding?: number
}

export interface M11MapCameraFlyTo {
  center: [number, number]
  zoom?: number
}

/**
 * 地图 popup slot（M26-4）：popup 内容（河段 / 代站组件）与经纬度锚点由页面经 props 传入，
 * react-map-gl `<Popup>` 必须在 `<Map>` 内渲染，故由本组件挂载、页面只给数据。
 */
export interface M11MapPopupSlot {
  longitude: number
  latitude: number
  content: ReactNode
  onClose?: () => void
}

export {
  buildBasinFeatureCollection,
  buildBasinRiverFeatureCollection,
  buildM11RegisteredOverlay,
  buildM11RenderedNationalRiverCollection,
  buildSelectedSegmentFeatureCollection,
  countSkippedBasinGeometries,
  m11BasinLabelAnchor,
  m11SelectedLayerUnavailableReason,
  m11VectorSourceKey,
  segmentFilter,
  type BasinFeatureCollection,
  type BasinRiverFeatureCollection,
  type M11RegisteredOverlay,
  type SelectedSegmentFeatureCollection,
} from '@/components/map/m11MapBuilders'
export { m11BasinRiverCollectionBudget } from '@/lib/m11/overviewDataContracts'

interface M11MapLibreSurfaceProps {
  state: M11QueryState
  layers: LayerState[]
  basins?: OverviewBasin[]
  visibleBasinIds?: string[]
  basinSegments?: BasinSegmentRow[]
  /** 常态河网底图（来自 basin shp，WGS84，按 Type 分级）。null 则 honest 降级不画。 */
  nationalRiverGeo?: FeatureCollection | null
  /** 已被动态 mesh 河网层覆盖的流域 id：这些流域的静态河流从 national 底图剔除，规避双线。 */
  meshRiverBasinIds?: string[]
  selectedSegmentId?: string | null
  selectedSegmentGeometry?:
    | components['schemas']['GeoJsonLineString']
    | components['schemas']['GeoJsonMultiLineString']
    | null
  selectedStationId?: string | null
  metStations?: boolean
  stationFeatureCollection?: M11StationFeatureCollection | null
  popup?: M11MapPopupSlot | null
  /** 数据加载中（overview/basin 取数）：抑制叠加层/边界/河段"未就绪"类瞬态空态，避免刷新闪烁。 */
  loading?: boolean
  /** 静态底图几何加载中：额外抑制"流域边界未就绪"瞬态（静态边界回填晚于 overview 接口时）。 */
  boundaryLoading?: boolean
  className?: string
  fitTo?: M11MapCameraFit | null
  flyTo?: M11MapCameraFlyTo | null
  onOverlayHover?: (interaction: M11MapOverlayInteraction | null) => void
  onOverlayClick?: (interaction: M11MapOverlayInteraction) => void
}

const CHINA_VIEW_STATE = {
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

const m11MapStyles: Record<M11Basemap, MapStyle> = {
  terrain: tiandituStyle('ter', 'cta'),
  satellite: tiandituStyle('img', 'cia'),
  vector: tiandituStyle('vec', 'cva'),
}

export function M11MapLibreSurface({
  state,
  layers,
  basins = [],
  visibleBasinIds,
  basinSegments = [],
  nationalRiverGeo = null,
  meshRiverBasinIds = [],
  selectedSegmentId = null,
  selectedSegmentGeometry = null,
  selectedStationId = null,
  metStations,
  stationFeatureCollection = null,
  popup = null,
  loading = false,
  boundaryLoading = false,
  className,
  fitTo,
  flyTo,
  onOverlayHover,
  onOverlayClick,
}: M11MapLibreSurfaceProps) {
  const mapRef = useRef<MapRef | null>(null)
  const lastFitKeyRef = useRef<string | null>(null)
  const lastFlyKeyRef = useRef<string | null>(null)
  // 总览↔详情切换会 remount 整棵地图子树，相机重置回 initialViewState。挂载时若已知 fitTo
  // （从总览点入，静态 bbox 已缓存同步可得），直接用该 bounds 初始化，避免「先闪回全国再飞入」
  // 的强制回初始视角（#1）；mount 后到达的 fitTo 仍由下方 effect 兜底。
  const [initialViewState] = useState(() =>
    fitTo ? { bounds: fitTo.bounds, fitBoundsOptions: { padding: fitTo.padding ?? 32 } } : CHINA_VIEW_STATE,
  )
  const [mapSourceError, setMapSourceError] = useState<string | null>(null)
  const [overlayData, setOverlayData] = useState<FloodReturnPeriodFeatureCollection | null>(null)
  const [overlayUnavailableReason, setOverlayUnavailableReason] = useState<string | null>(null)
  const [hoveredRiverSegmentId, setHoveredRiverSegmentId] = useState<string | null>(null)
  const overlay = useMemo(() => buildM11RegisteredOverlay(state, layers), [layers, state])
  const basinFeatureCollection = useMemo(
    () => buildBasinFeatureCollection(basins, visibleBasinIds),
    [basins, visibleBasinIds],
  )
  const basinRiverFeatureCollection = useMemo(
    () => buildBasinRiverFeatureCollection(basinSegments, state.layer),
    [basinSegments, state.layer],
  )
  const skippedBasinGeometryCount = useMemo(
    () => countSkippedBasinGeometries(basins, visibleBasinIds),
    [basins, visibleBasinIds],
  )
  const renderableOverlay = overlay && (overlay.source.type === 'vector' || overlayData) ? overlay : null
  // 动态 mesh 河网层（详情 GeoJSON 河段 / 流量 MVT 线层）激活的流域，其静态河流要从 national 底图剔除，
  // 避免「平滑静态线 + 阶梯 mesh 线」双线叠画；非线叠加层不触发剔除，保留诚实降级。
  const overlayIsRiverLine = renderableOverlay?.layer.type === 'line'
  const dynamicRiverActive = basinRiverFeatureCollection.features.length > 0 || overlayIsRiverLine
  const renderedNationalRiver = useMemo<FeatureCollection | null>(
    () => buildM11RenderedNationalRiverCollection(nationalRiverGeo, meshRiverBasinIds, dynamicRiverActive),
    [dynamicRiverActive, meshRiverBasinIds, nationalRiverGeo],
  )
  const selectedSegmentFeatureCollection = useMemo(
    () => buildSelectedSegmentFeatureCollection(selectedSegmentId, selectedSegmentGeometry),
    [selectedSegmentGeometry, selectedSegmentId],
  )
  const selectedSegmentMapState = selectedSegmentId
    ? selectedSegmentFeatureCollection.features.length > 0 || renderableOverlay || basinRiverFeatureCollection.features.length > 0
      ? 'selected-layer'
      : 'unavailable'
    : 'idle'
  const unavailableReason = useMemo(
    () =>
      overlayUnavailableReason ??
      m11SelectedLayerUnavailableReason(state, layers, overlay, overlayData, basinRiverFeatureCollection.features.length > 0),
    [basinRiverFeatureCollection.features.length, layers, overlay, overlayData, overlayUnavailableReason, state],
  )
  // 代站图层由独立 overlay 状态控制，有非空 features 时渲染/注册（关闭 overlay 不注册 source/layer）。
  const showStationLayer = (metStations ?? state.metStations) && (stationFeatureCollection?.features.length ?? 0) > 0
  const interactiveLayerIds = [
    ...(showStationLayer ? [MET_STATION_POINT_LAYER_ID, MET_STATION_CLUSTER_LAYER_ID] : []),
    ...(basinRiverFeatureCollection.features.length > 0 ? ['m11-basin-river-line'] : []),
    ...(basinFeatureCollection.features.length > 0 ? ['m11-basin-fill'] : []),
    ...(renderableOverlay ? [`${renderableOverlay.layer.id}-hit`] : []),
  ]

  useEffect(() => {
    setMapSourceError(null)
  }, [basinFeatureCollection.features.length, overlay?.sourceId, state.basemap, state.layer, state.validTime])

  useEffect(() => {
    setOverlayData(null)
    setOverlayUnavailableReason(null)
  }, [overlay])

  useEffect(() => {
    const map = mapRef.current
    if (!fitTo || !map) return
    const fitKey = mapFitKey(fitTo)
    if (fitKey === lastFitKeyRef.current) return
    lastFitKeyRef.current = fitKey
    map.fitBounds(fitTo.bounds, { padding: fitTo.padding ?? 32, duration: 450 })
  }, [fitTo])

  useEffect(() => {
    const map = mapRef.current
    if (!flyTo || !map) return
    const flyKey = mapFlyKey(flyTo)
    if (flyKey === lastFlyKeyRef.current) return
    lastFlyKeyRef.current = flyKey
    map.flyTo({ center: flyTo.center, zoom: flyTo.zoom, duration: 450 })
  }, [flyTo])

  const handleMouseMove = useCallback(
    (event: MapLayerMouseEvent) => {
      // 代站点 / cluster hover 优先于河段命中；重叠像素上 station overlay 是最上层交互对象。
      if (showStationLayer) {
        const stationFeature =
          findRenderedFeature(event, mapRef.current, MET_STATION_POINT_LAYER_ID) ??
          findRenderedFeature(event, mapRef.current, MET_STATION_CLUSTER_LAYER_ID)
        if (stationFeature) {
          setHoveredRiverSegmentId(null)
          onOverlayHover?.(null)
          event.target.getCanvas().style.cursor = 'pointer'
          return
        }
      }
      const riverFeature = findEventFeature(event, 'm11-basin-river-line')
      if (riverFeature) {
        const riverSegmentId = featureStringProperty(riverFeature, 'river_segment_id') ?? featureStringProperty(riverFeature, 'segment_id')
        setHoveredRiverSegmentId(riverSegmentId)
        onOverlayHover?.({ layerId: 'basin-river-segments', event, feature: riverFeature })
        event.target.getCanvas().style.cursor = 'pointer'
        return
      }
      // 河段 hover 须先于 basin-fill（与点击优先级一致），否则河段高亮被 basin 抢走。
      const overlayFeature = renderableOverlay ? findEventFeature(event, `${renderableOverlay.layer.id}-hit`) : null
      if (renderableOverlay && overlayFeature) {
        onOverlayHover?.({ layerId: renderableOverlay.layerId, event, feature: overlayFeature })
        event.target.getCanvas().style.cursor = 'pointer'
        return
      }
      const basinFeature = findEventFeature(event, 'm11-basin-fill')
      if (basinFeature) {
        setHoveredRiverSegmentId(null)
        onOverlayHover?.({ layerId: 'basin-boundaries', event, feature: basinFeature })
        event.target.getCanvas().style.cursor = 'pointer'
        return
      }
      setHoveredRiverSegmentId(null)
      onOverlayHover?.(null)
      event.target.getCanvas().style.cursor = ''
    },
    [onOverlayHover, renderableOverlay, showStationLayer],
  )

  const handleMouseLeave = useCallback(
    (event: MapLayerMouseEvent) => {
      setHoveredRiverSegmentId(null)
      onOverlayHover?.(null)
      event.target.getCanvas().style.cursor = ''
    },
    [onOverlayHover],
  )

  const handleClick = useCallback(
    (event: MapLayerMouseEvent) => {
      if (showStationLayer) {
        // 点 cluster/代站优先于河段：重叠像素上 station overlay 位于 hydrology 上方。
        // 真实 MapLibre 可能不给 onClick event.features 填 cluster，因此用 queryRenderedFeatures 兜底命中。
        const clusterFeature = findRenderedFeature(event, mapRef.current, MET_STATION_CLUSTER_LAYER_ID)
        if (clusterFeature) {
          expandStationCluster(mapRef.current, clusterFeature)
          return
        }
        // 点单个代站：经 onOverlayClick 以 met-stations 分发，feature 带 station_id（为 #340 popup 预留）。
        const stationFeature = findRenderedFeature(event, mapRef.current, MET_STATION_POINT_LAYER_ID)
        if (stationFeature) {
          onOverlayClick?.({ layerId: 'met-stations', event, feature: stationFeature })
          return
        }
      }
      const riverFeature = findEventFeature(event, 'm11-basin-river-line')
      if (riverFeature) {
        onOverlayClick?.({ layerId: 'basin-river-segments', event, feature: riverFeature })
        return
      }
      // 河段（流量 MVT 线）比所在流域多边形更具体：须先于 basin-fill 命中。
      // 否则总览（无 basinSegments）点河段会被底下的 basin-fill 抢走、永远到不了河段分支。
      const overlayFeature = renderableOverlay ? findEventFeature(event, `${renderableOverlay.layer.id}-hit`) : null
      if (renderableOverlay && overlayFeature) {
        onOverlayClick?.({ layerId: renderableOverlay.layerId, event, feature: overlayFeature })
        return
      }
      // 点流域空白处（无河段命中）→ basin 分支（相机飞到流域）。
      const basinFeature = findEventFeature(event, 'm11-basin-fill')
      if (basinFeature) {
        onOverlayClick?.({ layerId: 'basin-boundaries', event, feature: basinFeature })
      }
    },
    [onOverlayClick, renderableOverlay, showStationLayer],
  )

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

  return (
    <div
      className={cn('absolute inset-0', className)}
      data-testid="m11-map-surface"
      data-basemap={state.basemap}
      data-basemap-style={m11MapStyleUrls[state.basemap]}
      {...(renderableOverlay ? { 'data-registered-overlays': renderableOverlay.layerId } : {})}
      data-basin-feature-count={basinFeatureCollection.features.length}
      data-visible-basin-ids={basinFeatureCollection.features.map((feature) => feature.properties.basin_id).join(',')}
      data-basin-river-feature-count={basinRiverFeatureCollection.features.length}
      data-basin-river-skipped-count={basinRiverFeatureCollection.skippedCount}
      data-basin-river-coordinate-count={basinRiverFeatureCollection.coordinateCount}
      data-basin-river-serialized-bytes={basinRiverFeatureCollection.serializedBytes}
      data-selected-segment-id={selectedSegmentId ?? ''}
      data-segment-highlight-hook={selectedSegmentMapState}
      data-selected-segment-map-state={selectedSegmentMapState}
      data-selected-station-id={selectedStationId ?? ''}
      data-hovered-segment-id={hoveredRiverSegmentId ?? ''}
      data-overlay-source-type={renderableOverlay?.source.type ?? ''}
      data-overlay-source-layer={renderableOverlay?.source.type === 'vector' ? renderableOverlay.source.sourceLayer : ''}
      data-met-station-feature-count={showStationLayer ? stationFeatureCollection?.features.length ?? 0 : 0}
      data-national-river-feature-count={renderedNationalRiver?.features.length ?? 0}
    >
      <Map
        ref={mapRef}
        initialViewState={initialViewState}
        mapStyle={m11MapStyles[state.basemap]}
        interactiveLayerIds={interactiveLayerIds}
        onMouseMove={handleMouseMove}
        onMouseLeave={handleMouseLeave}
        onClick={handleClick}
        onError={handleMapError}
        attributionControl
      >
        <NavigationControl position="top-right" visualizePitch />
        <ScaleControl position="bottom-left" unit="metric" />
        {renderedNationalRiver ? (
          <M11NationalRiverPrimitive
            collection={renderedNationalRiver}
            dimmed={Boolean(renderableOverlay) || basinRiverFeatureCollection.features.length > 0}
            satellite={state.basemap === 'satellite'}
          />
        ) : null}
        {basinFeatureCollection.features.length > 0 ? (
          <>
            <M11BasinPrimitive collection={basinFeatureCollection} />
            <M11BasinLabelMarkers collection={basinFeatureCollection} />
          </>
        ) : null}
        {basinRiverFeatureCollection.features.length > 0 ? (
          <M11BasinRiverPrimitive
            collection={basinRiverFeatureCollection}
            selectedSegmentId={selectedSegmentId}
            hoveredSegmentId={hoveredRiverSegmentId}
            subdued={Boolean(renderableOverlay)}
          />
        ) : null}
        {renderableOverlay ? <M11OverlayPrimitive overlay={renderableOverlay} data={overlayData} selectedSegmentId={selectedSegmentId} /> : null}
        {selectedSegmentFeatureCollection.features.length > 0 ? (
          <M11SelectedSegmentPrimitive collection={selectedSegmentFeatureCollection} />
        ) : null}
        {showStationLayer && stationFeatureCollection ? (
          <M11StationClusterPrimitive collection={stationFeatureCollection} selectedStationId={selectedStationId} />
        ) : null}
        {/* popup anchor 不指定 → maplibre 按可用空间自动选边，高弹窗在视口边缘不被裁切。 */}
        {popup ? (
          <Popup
            longitude={popup.longitude}
            latitude={popup.latitude}
            closeOnClick={false}
            onClose={popup.onClose}
            maxWidth="none"
          >
            {popup.content}
          </Popup>
        ) : null}
      </Map>

      {!loading && !boundaryLoading && basins.length > 0 && basinFeatureCollection.features.length === 0 ? (
        <div
          className="absolute left-1/2 -translate-x-1/2 top-20 z-[90] max-w-[min(28rem,calc(100%-2.5rem))] rounded-md border border-warning/40 bg-white/95 px-3 py-2 text-sm text-neutral-800 shadow-md"
          role="status"
          data-testid="m11-basin-layer-unavailable"
        >
          {skippedBasinGeometryCount > 0
            ? '当前可见流域边界超过客户端渲染预算，地图不会注册过大的边界源。'
            : '当前没有可见流域边界。'}
        </div>
      ) : null}

      {!loading && unavailableReason ? (
        <div
          className="absolute left-1/2 -translate-x-1/2 top-20 z-[90] max-w-[min(28rem,calc(100%-2.5rem))] rounded-md border border-warning/40 bg-white/95 px-3 py-2 text-sm text-neutral-800 shadow-md"
          role="status"
          data-testid="m11-map-unavailable"
        >
          {unavailableReason}
        </div>
      ) : null}

      {!loading && basinRiverFeatureCollection.unavailableReason ? (
        <div
          className="absolute left-1/2 -translate-x-1/2 top-32 z-[90] max-w-[min(28rem,calc(100%-2.5rem))] rounded-md border border-warning/40 bg-white/95 px-3 py-2 text-sm text-neutral-800 shadow-md"
          role="status"
          data-testid="m11-basin-river-unavailable"
        >
          {basinRiverFeatureCollection.unavailableReason}
        </div>
      ) : null}

      {hoveredRiverSegmentId ? (
        <M11RiverTooltip feature={basinRiverFeatureCollection.features.find((feature) => feature.properties.river_segment_id === hoveredRiverSegmentId || feature.properties.segment_id === hoveredRiverSegmentId) ?? null} />
      ) : null}

      {!loading && selectedSegmentMapState === 'unavailable' ? (
        <div
          className="absolute left-1/2 -translate-x-1/2 top-44 z-[90] max-w-[min(28rem,calc(100%-2.5rem))] rounded-md border border-warning/40 bg-white/95 px-3 py-2 text-sm text-neutral-800 shadow-md"
          role="status"
          data-testid="m11-selected-segment-map-unavailable"
        >
          {selectedSegmentFeatureCollection.unavailableReason}
        </div>
      ) : null}

      {mapSourceError ? (
        <div
          className="absolute left-1/2 -translate-x-1/2 top-32 z-[90] max-w-[min(28rem,calc(100%-2.5rem))] rounded-md border border-warning/40 bg-white/95 px-3 py-2 text-sm text-neutral-800 shadow-md"
          role="status"
          data-testid="m11-map-source-error"
        >
          {mapSourceError}
        </div>
      ) : null}
    </div>
  )
}

function M11OverlayPrimitive({
  overlay,
  data,
  selectedSegmentId,
}: {
  overlay: M11RegisteredOverlay
  data: FloodReturnPeriodFeatureCollection | null
  selectedSegmentId?: string | null
}) {
  // line 叠加层渲染三层（z 序自下而上）：白色光晕底衬 → 彩色主线 → 透明加宽点击热区。
  // 热区 id 带 -hit 后缀，是唯一进 interactiveLayerIds 的层，让细河段也好点中。
  const isLine = overlay.layer.type === 'line'
  const sourceLayerProp = overlay.layer['source-layer'] ? { 'source-layer': overlay.layer['source-layer'] } : {}
  const casingLayer = isLine
    ? {
        id: `${overlay.layer.id}-casing`,
        type: 'line' as const,
        source: overlay.sourceId,
        ...sourceLayerProp,
        layout: M11_ROUND_LINE_LAYOUT,
        paint: m11OverlayCasingPaint(overlay.layerId),
      }
    : null
  const hitLayer = isLine
    ? {
        id: `${overlay.layer.id}-hit`,
        type: 'line' as const,
        source: overlay.sourceId,
        ...sourceLayerProp,
        layout: M11_ROUND_LINE_LAYOUT,
        paint: M11_OVERLAY_HIT_PAINT,
      }
    : null
  const mainLayer = isLine ? { ...overlay.layer, layout: M11_ROUND_LINE_LAYOUT } : overlay.layer
  const selectedHaloLayer = isLine
    ? {
        id: `${overlay.layer.id}-selected-halo`,
        type: 'line' as const,
        source: overlay.sourceId,
        ...sourceLayerProp,
        layout: M11_ROUND_LINE_LAYOUT,
        filter: segmentFilter(selectedSegmentId),
        paint: {
          'line-color': '#FFFFFF',
          'line-width': 10,
          'line-opacity': 0.78,
        },
      }
    : null
  const selectedLineLayer = isLine
    ? {
        id: `${overlay.layer.id}-selected-line`,
        type: 'line' as const,
        source: overlay.sourceId,
        ...sourceLayerProp,
        layout: M11_ROUND_LINE_LAYOUT,
        filter: segmentFilter(selectedSegmentId),
        paint: {
          'line-color': '#F97316',
          'line-width': 6,
          'line-opacity': 1,
        },
      }
    : null
  if (overlay.source.type === 'vector') {
    return (
      <Source
        key={overlay.sourceKey}
        id={overlay.sourceId}
        type="vector"
        tiles={overlay.source.tiles}
        minzoom={overlay.source.minzoom}
        maxzoom={overlay.source.maxzoom}
        promoteId="feature_id"
      >
        {casingLayer ? <Layer {...casingLayer} /> : null}
        <Layer {...mainLayer} />
        {hitLayer ? <Layer {...hitLayer} /> : null}
        {selectedHaloLayer ? <Layer {...selectedHaloLayer} /> : null}
        {selectedLineLayer ? <Layer {...selectedLineLayer} /> : null}
      </Source>
    )
  }
  if (!data) return null
  return (
    <Source id={overlay.sourceId} type="geojson" data={data} promoteId="feature_id">
      {casingLayer ? <Layer {...casingLayer} /> : null}
      <Layer {...mainLayer} />
      {hitLayer ? <Layer {...hitLayer} /> : null}
      {selectedHaloLayer ? <Layer {...selectedHaloLayer} /> : null}
      {selectedLineLayer ? <Layer {...selectedLineLayer} /> : null}
    </Source>
  )
}

// 光晕底衬 paint：白色、比主线宽约 2px、半透明，给彩色河段一圈干净的描边光晕（类似标注 halo），
// 浅底图上清晰可辨且不像深色道路（弃用早期深色 casing）。
// discharge 主线走 log 域：casing 必须同域贴主线（约 +1.2px），线性域下白晕恒比
// 低流量主线宽一倍，会把彩色稀释成淡白。
function m11OverlayCasingPaint(_layerId: string): LayerProps['paint'] {
  const valueStops = [-2, 3, 0, 3.6, 2, 4.6, 4, 6.2, 4.7, 8.2]
  return {
    'line-color': '#FFFFFF',
    'line-opacity': 0.85,
    'line-width': zoomScaledValueWidth(valueStops, 0.4, true),
  }
}

// 透明点击热区 paint：不可见但加宽，便于鼠标点中细河段（可见线仍细）。
const M11_OVERLAY_HIT_PAINT: LayerProps['paint'] = {
  'line-color': '#000000',
  'line-opacity': 0,
  'line-width': 16,
}
const M11_ROUND_LINE_LAYOUT = { 'line-cap': 'round', 'line-join': 'round' } as const

// 常态河网底图 paint：按 Type(1..5,5=主干)深浅分级，线宽随 zoom×Type 增大，
// 透明度按「zoom 越大、Type 越低越晚出现」实现分级常显——低 zoom 只见主干，放大渐显支流。
// dimmed：彩色水文层（MVT 叠加 / 详情 GeoJSON 河段）激活时降透明衬底，消「双线」毛边；
// 但只在 zoom≥6 渐进生效——全国 zoom 下彩色层只是细线，静态河网必须保持全可见（缩略显示）。
// satellite：影像底图换浅青色系，深蓝在影像上不可读。
export function m11NationalRiverPaint({ dimmed, satellite }: { dimmed: boolean; satellite: boolean }): LayerProps['paint'] {
  // 各 zoom 档独立 fade：3/5 不降（全国缩略），7 起衬底化。
  const fadeAt = (zoomFade: number) => (dimmed ? zoomFade : 1)
  const fade5 = fadeAt(0.85)
  const fade7 = fadeAt(0.45)
  const fade9 = fadeAt(0.35)
  return {
    'line-color': [
      'interpolate',
      ['linear'],
      ['get', 'Type'],
      1,
      satellite ? '#9fe0ff' : '#9cc7e8',
      3,
      satellite ? '#5fc3f2' : '#3f88c5',
      5,
      satellite ? '#2196d8' : '#14487f',
    ],
    'line-width': [
      'interpolate',
      ['linear'],
      ['zoom'],
      3,
      ['interpolate', ['linear'], ['get', 'Type'], 1, 0.3, 5, 1.4],
      7,
      ['interpolate', ['linear'], ['get', 'Type'], 1, 0.8, 5, 2.6],
      12,
      ['interpolate', ['linear'], ['get', 'Type'], 1, 1.6, 5, 4.5],
    ],
    'line-opacity': [
      'interpolate',
      ['linear'],
      ['zoom'],
      3,
      ['match', ['get', 'Type'], 5, 0.9, 4, 0.55, 0],
      5,
      ['match', ['get', 'Type'], 5, 0.95 * fade5, 4, 0.85 * fade5, 3, 0.55 * fade5, 0],
      7,
      ['match', ['get', 'Type'], 5, 1 * fade7, 4, 0.95 * fade7, 3, 0.85 * fade7, 2, 0.6 * fade7, 0],
      9,
      0.9 * fade9,
    ],
  }
}

// 全国静态河网（basin shp 溶出）作为常态底图：秒显、不依赖 discharge run/接口；
// 流量 MVT 叠加在其上着色。honest：无文件 → 不渲染（OverviewPage 不传即可）。
function M11NationalRiverPrimitive({
  collection,
  dimmed,
  satellite,
}: {
  collection: FeatureCollection
  dimmed: boolean
  satellite: boolean
}) {
  return (
    <Source id="m11-national-river-source" type="geojson" data={collection}>
      <Layer
        id="m11-national-river-line"
        type="line"
        source="m11-national-river-source"
        layout={M11_ROUND_LINE_LAYOUT}
        paint={m11NationalRiverPaint({ dimmed, satellite })}
      />
    </Source>
  )
}

function M11BasinPrimitive({ collection }: { collection: BasinFeatureCollection }) {
  return (
    <Source id="m11-basin-boundaries-source" type="geojson" data={collection} promoteId="basin_id">
      <Layer
        id="m11-basin-fill"
        type="fill"
        source="m11-basin-boundaries-source"
        paint={{
          'fill-color': '#1E88E5',
          'fill-opacity': 0.14,
        }}
      />
      <Layer
        id="m11-basin-outline"
        type="line"
        source="m11-basin-boundaries-source"
        paint={{
          'line-color': '#0F3460',
          'line-width': 1.4,
          'line-opacity': 0.72,
        }}
      />
    </Source>
  )
}

/**
 * 流域名 DOM 标注（玻璃 chip）。不用 symbol+text-field：天地图栅格 style 无 glyphs
 * 字体源，symbol 文本层永远无法渲染且会上抛 style 错误；DOM Marker 零字体依赖、
 * 样式可控。pointer-events 关闭，避免挡住边界 fill 的点击钻取。
 */
function M11BasinLabelMarkers({ collection }: { collection: BasinFeatureCollection }) {
  return (
    <>
      {collection.features.map((feature) => {
        const anchor = m11BasinLabelAnchor(feature.geometry)
        if (!anchor) return null
        return (
          <Marker key={feature.properties.basin_id} longitude={anchor[0]} latitude={anchor[1]} anchor="center" style={{ pointerEvents: 'none' }}>
            <span
              className="pointer-events-none select-none rounded-full border border-white/60 bg-white/80 px-2.5 py-0.5 text-xs font-semibold text-primary-700 shadow-sm backdrop-blur-sm"
              data-testid="m11-basin-label"
              data-basin-id={feature.properties.basin_id}
            >
              {feature.properties.basin_name}
            </span>
          </Marker>
        )
      })}
    </>
  )
}

function M11BasinRiverPrimitive({
  collection,
  selectedSegmentId,
  hoveredSegmentId,
  subdued = false,
}: {
  collection: BasinRiverFeatureCollection
  selectedSegmentId?: string | null
  hoveredSegmentId?: string | null
  /** 彩色 MVT 叠加激活时退衬底：流量色由权威 MVT 层纯净呈现，本层只保留点击/hover 热区。 */
  subdued?: boolean
}) {
  return (
    <Source id="m11-basin-river-source" type="geojson" data={collection.sourceData} promoteId="river_segment_id">
      {/* 白色光晕衬底：彩色河网在任意底图上都有干净描边，弱化与底图水系的细微错位感。 */}
      <Layer
        id="m11-basin-river-casing"
        type="line"
        source="m11-basin-river-source"
        layout={M11_ROUND_LINE_LAYOUT}
        paint={{
          'line-color': '#FFFFFF',
          'line-width': ['interpolate', ['linear'], ['zoom'], 6, 2.6, 9, 3.8, 12, 5.2],
          'line-opacity': subdued ? 0.25 : 0.8,
        }}
      />
      <Layer
        id="m11-basin-river-line"
        type="line"
        source="m11-basin-river-source"
        layout={M11_ROUND_LINE_LAYOUT}
        paint={{
          'line-color': ['get', 'layer_color'],
          'line-width': ['interpolate', ['linear'], ['zoom'], 6, 1.6, 9, 2.6, 12, 3.6],
          'line-opacity': subdued ? 0.18 : 0.92,
        }}
      />
      <Layer
        id="m11-basin-river-hover-halo"
        type="line"
        source="m11-basin-river-source"
        layout={M11_ROUND_LINE_LAYOUT}
        filter={segmentFilter(hoveredSegmentId)}
        paint={{
          'line-color': '#FFFFFF',
          'line-width': 8.5,
          'line-opacity': 0.62,
        }}
      />
      <Layer
        id="m11-basin-river-selected-halo"
        type="line"
        source="m11-basin-river-source"
        layout={M11_ROUND_LINE_LAYOUT}
        filter={segmentFilter(selectedSegmentId)}
        paint={{
          'line-color': '#FFFFFF',
          'line-width': 9.5,
          'line-opacity': 0.68,
        }}
      />
      <Layer
        id="m11-basin-river-hover-line"
        type="line"
        source="m11-basin-river-source"
        layout={M11_ROUND_LINE_LAYOUT}
        filter={segmentFilter(hoveredSegmentId)}
        paint={{
          'line-color': ['get', 'layer_color'],
          'line-width': 4.8,
          'line-opacity': 0.98,
        }}
      />
      <Layer
        id="m11-basin-river-selected-line"
        type="line"
        source="m11-basin-river-source"
        layout={M11_ROUND_LINE_LAYOUT}
        filter={segmentFilter(selectedSegmentId)}
        paint={{
          'line-color': '#F97316',
          'line-width': 5.5,
          'line-opacity': 1,
        }}
      />
    </Source>
  )
}

function M11RiverTooltip({ feature }: { feature: BasinRiverFeature | null }) {
  if (!feature) return null
  const props = feature.properties
  return (
    <div
      className="pointer-events-none absolute right-5 top-24 z-[110] w-72 rounded-md border border-neutral-300 bg-white/95 p-3 text-xs text-neutral-700 shadow-lg"
      role="tooltip"
      data-testid="m11-river-tooltip"
    >
      <div className="truncate text-sm font-semibold text-neutral-900">{props.segment_name || props.river_segment_id}</div>
      <dl className="mt-2 grid grid-cols-[5rem_minmax(0,1fr)] gap-x-2 gap-y-1">
        <dt>河段 ID</dt>
        <dd className="min-w-0 truncate font-mono text-neutral-900">{props.river_segment_id}</dd>
        <dt>当前流量</dt>
        <dd>{props.q_value === null ? '无数据' : `${props.q_value.toLocaleString('en-US')} ${props.q_unit}`}</dd>
        <dt>重现期</dt>
        <dd>{props.return_period === null ? '无数据' : `${props.return_period} 年一遇`}</dd>
        <dt>预警</dt>
        <dd>{warningLabel(props.warning_level)}</dd>
      </dl>
    </div>
  )
}

function M11SelectedSegmentPrimitive({ collection }: { collection: SelectedSegmentFeatureCollection }) {
  return (
    <Source id="m11-selected-segment-source" type="geojson" data={collection} promoteId="segment_id">
      <Layer
        id="m11-selected-segment-halo"
        type="line"
        source="m11-selected-segment-source"
        paint={{
          'line-color': '#FFFFFF',
          'line-width': 8,
          'line-opacity': 0.7,
        }}
      />
      <Layer
        id="m11-selected-segment-line"
        type="line"
        source="m11-selected-segment-source"
        paint={{
          'line-color': '#F97316',
          'line-width': 5,
          'line-opacity': 0.95,
        }}
      />
    </Source>
  )
}

/**
 * 代站 clustered-GeoJSON primitive：以 layerId/source 抽象组织（id 集中常量），未来切换到后端
 * station-MVT 点图层瓦片端点时仅替换 <Source> 实现即可，无需重写交互/popup 分发。
 */
function M11StationClusterPrimitive({
  collection,
  selectedStationId,
}: {
  collection: M11StationFeatureCollection
  selectedStationId?: string | null
}) {
  return (
    <Source
      id={MET_STATION_SOURCE_ID}
      type="geojson"
      data={collection}
      cluster
      clusterRadius={50}
      clusterMaxZoom={14}
      promoteId="station_id"
    >
      <Layer
        id={MET_STATION_CLUSTER_LAYER_ID}
        type="circle"
        source={MET_STATION_SOURCE_ID}
        filter={['has', 'point_count']}
        paint={{
          'circle-color': ['step', ['get', 'point_count'], '#90CAF9', 25, '#42A5F5', 100, '#1E88E5'],
          'circle-radius': ['step', ['get', 'point_count'], 14, 25, 18, 100, 24],
          'circle-opacity': 0.85,
          'circle-stroke-color': '#FFFFFF',
          'circle-stroke-width': 1.5,
        }}
      />
      <Layer
        id={MET_STATION_CLUSTER_COUNT_LAYER_ID}
        type="symbol"
        source={MET_STATION_SOURCE_ID}
        filter={['has', 'point_count']}
        layout={{
          'text-field': ['get', 'point_count_abbreviated'],
          'text-size': 12,
          'text-allow-overlap': true,
        }}
        paint={{ 'text-color': '#0A1929' }}
      />
      <Layer
        id={MET_STATION_POINT_LAYER_ID}
        type="circle"
        source={MET_STATION_SOURCE_ID}
        filter={['!', ['has', 'point_count']]}
        paint={{
          'circle-color': '#F97316',
          'circle-radius': 6,
          'circle-opacity': 0.92,
          'circle-stroke-color': '#FFFFFF',
          'circle-stroke-width': 1.5,
        }}
      />
      <Layer
        id="met-stations-selected-halo"
        type="circle"
        source={MET_STATION_SOURCE_ID}
        filter={stationFilter(selectedStationId)}
        paint={{
          'circle-color': '#FFFFFF',
          'circle-radius': 12,
          'circle-opacity': 0.82,
          'circle-stroke-color': '#111827',
          'circle-stroke-width': 1,
        }}
      />
      <Layer
        id="met-stations-selected-point"
        type="circle"
        source={MET_STATION_SOURCE_ID}
        filter={stationFilter(selectedStationId)}
        paint={{
          'circle-color': '#FACC15',
          'circle-radius': 7.5,
          'circle-opacity': 1,
          'circle-stroke-color': '#111827',
          'circle-stroke-width': 2,
        }}
      />
    </Source>
  )
}

type StationClusterSource = {
  getClusterExpansionZoom?: (
    clusterId: number,
    callback?: (error: unknown, zoom: number) => void,
  ) => Promise<number> | void
}

function expandStationCluster(
  mapRef: MapRef | null,
  feature: NonNullable<MapLayerMouseEvent['features']>[number],
) {
  const map = mapRef?.getMap?.()
  if (!map) return
  const source = (map.getSource(MET_STATION_SOURCE_ID) as StationClusterSource | undefined) ?? undefined
  const clusterId = feature.properties?.cluster_id ?? feature.id
  const geometry = feature.geometry
  if (!source?.getClusterExpansionZoom || typeof clusterId !== 'number' || geometry?.type !== 'Point') return
  const [lon, lat] = geometry.coordinates as [number, number]
  const flyToZoom = (zoom: number) => {
    if (!Number.isFinite(zoom)) return
    map.flyTo({ center: [lon, lat], zoom, duration: 450 })
  }
  const expansion = source.getClusterExpansionZoom(clusterId, (error, zoom) => {
    if (!error) flyToZoom(zoom)
  })
  if (expansion && typeof expansion.then === 'function') {
    void expansion.then(flyToZoom).catch(() => undefined)
  }
}

function stationFilter(stationId?: string | null): FilterSpecification {
  return [
    'all',
    ['!', ['has', 'point_count']],
    ['==', ['get', 'station_id'], stationId ?? ''],
  ] as FilterSpecification
}

function warningLabel(level: M11WarningLevel) {
  const labels: Record<M11WarningLevel, string> = {
    normal: '正常',
    elevated: '偏高',
    watch: '关注',
    warning: '警戒',
    high_risk: '高风险',
    severe: '严重',
    extreme: '极端',
    unavailable: '无数据',
  }
  return labels[level]
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

function findEventFeature(event: MapLayerMouseEvent, layerId: string) {
  return event.features?.find((feature) => feature.layer?.id === layerId) ?? null
}

function findRenderedFeature(event: MapLayerMouseEvent, mapRef: MapRef | null, layerId: string) {
  const eventFeature = findEventFeature(event, layerId)
  if (eventFeature) return eventFeature
  const map = mapRef?.getMap?.()
  if (!map) return null
  try {
    return map.queryRenderedFeatures(event.point, { layers: [layerId] }).find((feature) => feature.layer?.id === layerId) ?? null
  } catch {
    return null
  }
}

function featureStringProperty(feature: NonNullable<MapLayerMouseEvent['features']>[number], key: string) {
  const value = feature.properties?.[key]
  return typeof value === 'string' && value.length > 0 ? value : null
}

function mapFitKey(fitTo: M11MapCameraFit) {
  const [[minLon, minLat], [maxLon, maxLat]] = fitTo.bounds
  return `${minLon},${minLat},${maxLon},${maxLat},${fitTo.padding ?? 32}`
}

function mapFlyKey(flyTo: M11MapCameraFlyTo) {
  return `${flyTo.center[0]},${flyTo.center[1]},${flyTo.zoom ?? ''}`
}

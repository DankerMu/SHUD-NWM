import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Map, {
  NavigationControl,
  Popup,
  ScaleControl,
  type MapLayerMouseEvent,
  type MapRef,
  type MapStyle,
} from 'react-map-gl/maplibre'
import type { ReactNode } from 'react'
import type { FeatureCollection } from 'geojson'
import 'maplibre-gl/dist/maplibre-gl.css'

import type { components } from '@/api/types'
import { cn } from '@/lib/cn'
import {
  buildBasinFeatureCollection,
  buildBasinRiverFeatureCollection,
  buildM11RegisteredOverlay,
  buildM11RenderedNationalRiverCollection,
  buildSelectedSegmentFeatureCollection,
  countSkippedBasinGeometries,
  m11SelectedLayerUnavailableReason,
  type BasinRiverFeature,
} from '@/components/map/m11MapBuilders'
import {
  M11_BASIN_FILL_LAYER_ID,
  M11_BASIN_RIVER_LINE_LAYER_ID,
  M11BasinLabelMarkers,
  M11BasinPrimitive,
  M11BasinRiverPrimitive,
  M11NationalRiverPrimitive,
  M11OverlayPrimitive,
  M11SelectedSegmentPrimitive,
  M11StationClusterPrimitive,
  MET_STATION_CLUSTER_LAYER_ID,
  MET_STATION_POINT_LAYER_ID,
  MET_STATION_SOURCE_ID,
  m11RegisteredOverlayHitLayerId,
  type M11StationFeatureCollection,
} from '@/components/map/m11MapPrimitives'
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
export { m11NationalRiverPaint, type M11StationFeatureCollection } from '@/components/map/m11MapPrimitives'
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
    ...(basinRiverFeatureCollection.features.length > 0 ? [M11_BASIN_RIVER_LINE_LAYER_ID] : []),
    ...(basinFeatureCollection.features.length > 0 ? [M11_BASIN_FILL_LAYER_ID] : []),
    ...(renderableOverlay ? [m11RegisteredOverlayHitLayerId(renderableOverlay)] : []),
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
      const riverFeature = findEventFeature(event, M11_BASIN_RIVER_LINE_LAYER_ID)
      if (riverFeature) {
        const riverSegmentId = featureStringProperty(riverFeature, 'river_segment_id') ?? featureStringProperty(riverFeature, 'segment_id')
        setHoveredRiverSegmentId(riverSegmentId)
        onOverlayHover?.({ layerId: 'basin-river-segments', event, feature: riverFeature })
        event.target.getCanvas().style.cursor = 'pointer'
        return
      }
      // 河段 hover 须先于 basin-fill（与点击优先级一致），否则河段高亮被 basin 抢走。
      const overlayFeature = renderableOverlay ? findEventFeature(event, m11RegisteredOverlayHitLayerId(renderableOverlay)) : null
      if (renderableOverlay && overlayFeature) {
        onOverlayHover?.({ layerId: renderableOverlay.layerId, event, feature: overlayFeature })
        event.target.getCanvas().style.cursor = 'pointer'
        return
      }
      const basinFeature = findEventFeature(event, M11_BASIN_FILL_LAYER_ID)
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
      const riverFeature = findEventFeature(event, M11_BASIN_RIVER_LINE_LAYER_ID)
      if (riverFeature) {
        onOverlayClick?.({ layerId: 'basin-river-segments', event, feature: riverFeature })
        return
      }
      // 河段（流量 MVT 线）比所在流域多边形更具体：须先于 basin-fill 命中。
      // 否则总览（无 basinSegments）点河段会被底下的 basin-fill 抢走、永远到不了河段分支。
      const overlayFeature = renderableOverlay ? findEventFeature(event, m11RegisteredOverlayHitLayerId(renderableOverlay)) : null
      if (renderableOverlay && overlayFeature) {
        onOverlayClick?.({ layerId: renderableOverlay.layerId, event, feature: overlayFeature })
        return
      }
      // 点流域空白处（无河段命中）→ basin 分支（相机飞到流域）。
      const basinFeature = findEventFeature(event, M11_BASIN_FILL_LAYER_ID)
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

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
import { floodTileLayerPaint } from '@/components/flood/alertLevels'
import { cn } from '@/lib/cn'
import { DEFAULT_FLOOD_RETURN_PERIOD_DURATION } from '@/lib/floodReturnPeriodDuration'
import type { FloodReturnPeriodFeatureCollection } from '@/lib/floodReturnPeriodGeoJson'
import {
  buildMvtTileUrlTemplate,
  isMvtLayerMetadata,
  isNationalOverlayMetadata,
  metadataHasValidTime,
  metadataMatchesRun,
  type MvtLayerMetadata,
} from '@/lib/mvtLayerMetadata'
import {
  getM11BasinGeometryBudgetStatus,
  getM11SelectedSegmentGeometryBudgetStatus,
  m11BasinRiverCollectionBudget,
  m11BasinRiverLayerColor,
  type BasinSegmentRow,
  type LayerState,
  type M11WarningLevel,
  type OverviewBasin,
} from '@/lib/m11/overviewDataContracts'
import type { M11Basemap, M11Layer, M11QueryState } from '@/lib/m11/queryState'

export interface M11MapOverlayInteraction {
  layerId: M11Layer | 'basin-boundaries' | 'basin-river-segments'
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
    properties: { station_id: string; station_name: string | null }
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

export { m11BasinRiverCollectionBudget } from '@/lib/m11/overviewDataContracts'

interface M11MapLibreSurfaceProps {
  state: M11QueryState
  layers: LayerState[]
  basins?: OverviewBasin[]
  visibleBasinIds?: string[]
  basinSegments?: BasinSegmentRow[]
  /** 常态河网底图（来自 basin shp，WGS84，按 Type 分级）。null 则 honest 降级不画。 */
  nationalRiverGeo?: FeatureCollection | null
  selectedSegmentId?: string | null
  selectedSegmentGeometry?: components['schemas']['GeoJsonLineString'] | null
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

interface M11RegisteredOverlay {
  layerId: M11Layer
  sourceId: string
  sourceKey: string
  layer: LayerProps
  source:
    | { type: 'vector'; tiles: string[]; sourceLayer: string; minzoom: number; maxzoom: number; metadata: MvtLayerMetadata }
}

interface BasinFeatureProperties {
  basin_id: string
  basin_name: string
  basin_group: string | null
  area_km2: number | null
  river_count: number | null
  active_model_count: number
  latest_forecast_time: string | null
  selected_basin_version_id: string | null
  unavailable_reason: string | null
}

interface BasinFeature {
  type: 'Feature'
  geometry: NonNullable<OverviewBasin['boundary']>
  properties: BasinFeatureProperties
}

interface BasinFeatureCollection {
  type: 'FeatureCollection'
  features: BasinFeature[]
}

interface BasinRiverFeatureProperties {
  segment_id: string
  river_segment_id: string
  basin_version_id: string
  river_network_version_id: string
  segment_name: string
  q_value: number | null
  q_unit: string
  return_period: number | null
  warning_level: M11WarningLevel
  layer_color: string
}

interface BasinRiverFeature {
  type: 'Feature'
  geometry: components['schemas']['GeoJsonLineString']
  properties: BasinRiverFeatureProperties
}

interface BasinRiverFeatureCollection {
  type: 'FeatureCollection'
  features: BasinRiverFeature[]
  sourceData: {
    type: 'FeatureCollection'
    features: BasinRiverFeature[]
  }
  skippedCount: number
  coordinateCount: number
  serializedBytes: number
  unavailableReason: string | null
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
  selectedSegmentId = null,
  selectedSegmentGeometry = null,
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
    () =>
      basins.filter((basin) => {
        if (!basin.boundary) return false
        const visible = visibleBasinIds ? visibleBasinIds.includes(basin.basinId) : true
        return visible && !getM11BasinGeometryBudgetStatus(basin.boundary).ok
      }).length,
    [basins, visibleBasinIds],
  )
  const renderableOverlay = overlay && (overlay.source.type === 'vector' || overlayData) ? overlay : null
  const selectedSegmentFeatureCollection = useMemo(
    () => buildSelectedSegmentFeatureCollection(selectedSegmentId, selectedSegmentGeometry),
    [selectedSegmentGeometry, selectedSegmentId],
  )
  const selectedSegmentMapState = selectedSegmentId
    ? selectedSegmentFeatureCollection.features.length > 0
      ? 'selected-layer'
      : 'unavailable'
    : 'idle'
  const unavailableReason = useMemo(
    () =>
      overlayUnavailableReason ??
      m11SelectedLayerUnavailableReason(state, layers, overlay, overlayData, basinRiverFeatureCollection.features.length > 0),
    [basinRiverFeatureCollection.features.length, layers, overlay, overlayData, overlayUnavailableReason, state],
  )
  // 代站图层仅在选中该图层模式且有非空 features 时渲染/注册（关闭图层不注册 source/layer）。
  const showStationLayer = state.layer === 'met-stations' && (stationFeatureCollection?.features.length ?? 0) > 0
  const interactiveLayerIds = [
    ...(basinRiverFeatureCollection.features.length > 0 ? ['m11-basin-river-line'] : []),
    ...(basinFeatureCollection.features.length > 0 ? ['m11-basin-fill'] : []),
    ...(renderableOverlay ? [`${renderableOverlay.layer.id}-hit`] : []),
    ...(showStationLayer ? [MET_STATION_POINT_LAYER_ID, MET_STATION_CLUSTER_LAYER_ID] : []),
  ]

  useEffect(() => {
    setMapSourceError(null)
  }, [basinFeatureCollection.features.length, overlay?.sourceId, state.basemap, state.layer, state.validTime])

  useEffect(() => {
    setOverlayData(null)
    setOverlayUnavailableReason(null)
  }, [overlay])

  useEffect(() => {
    if (!fitTo) return
    const fitKey = mapFitKey(fitTo)
    if (fitKey === lastFitKeyRef.current) return
    lastFitKeyRef.current = fitKey
    mapRef.current?.fitBounds(fitTo.bounds, { padding: fitTo.padding ?? 32, duration: 450 })
  }, [fitTo])

  useEffect(() => {
    if (!flyTo) return
    const flyKey = mapFlyKey(flyTo)
    if (flyKey === lastFlyKeyRef.current) return
    lastFlyKeyRef.current = flyKey
    mapRef.current?.flyTo({ center: flyTo.center, zoom: flyTo.zoom, duration: 450 })
  }, [flyTo])

  const handleMouseMove = useCallback(
    (event: MapLayerMouseEvent) => {
      const riverFeature = findEventFeature(event, 'm11-basin-river-line')
      if (riverFeature) {
        const riverSegmentId = featureStringProperty(riverFeature, 'river_segment_id') ?? featureStringProperty(riverFeature, 'segment_id')
        setHoveredRiverSegmentId(riverSegmentId)
        onOverlayHover?.({ layerId: 'basin-river-segments', event, feature: riverFeature })
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
      // 代站点 / cluster hover：cursor=pointer（#339 遗留 minor），但不触发 overlay hover 高亮。
      if (showStationLayer) {
        const stationFeature =
          findEventFeature(event, MET_STATION_POINT_LAYER_ID) ?? findEventFeature(event, MET_STATION_CLUSTER_LAYER_ID)
        if (stationFeature) {
          setHoveredRiverSegmentId(null)
          onOverlayHover?.(null)
          event.target.getCanvas().style.cursor = 'pointer'
          return
        }
      }
      const overlayFeature = renderableOverlay ? findEventFeature(event, `${renderableOverlay.layer.id}-hit`) : null
      if (!renderableOverlay || !overlayFeature) {
        setHoveredRiverSegmentId(null)
        onOverlayHover?.(null)
        event.target.getCanvas().style.cursor = ''
        return
      }
      onOverlayHover?.({ layerId: renderableOverlay.layerId, event, feature: overlayFeature })
      event.target.getCanvas().style.cursor = 'pointer'
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
      const riverFeature = findEventFeature(event, 'm11-basin-river-line')
      if (riverFeature) {
        onOverlayClick?.({ layerId: 'basin-river-segments', event, feature: riverFeature })
        return
      }
      const basinFeature = findEventFeature(event, 'm11-basin-fill')
      if (basinFeature) {
        onOverlayClick?.({ layerId: 'basin-boundaries', event, feature: basinFeature })
        return
      }
      if (showStationLayer) {
        // 点 cluster：用 source 运行时 API 取展开 zoom 后 flyTo（测试以 stub 验证调用）。
        const clusterFeature = findEventFeature(event, MET_STATION_CLUSTER_LAYER_ID)
        if (clusterFeature) {
          expandStationCluster(mapRef.current, clusterFeature)
          return
        }
        // 点单个代站：经 onOverlayClick 以 met-stations 分发，feature 带 station_id（为 #340 popup 预留）。
        const stationFeature = findEventFeature(event, MET_STATION_POINT_LAYER_ID)
        if (stationFeature) {
          onOverlayClick?.({ layerId: 'met-stations', event, feature: stationFeature })
          return
        }
      }
      const overlayFeature = renderableOverlay ? findEventFeature(event, `${renderableOverlay.layer.id}-hit`) : null
      if (renderableOverlay && overlayFeature) {
        onOverlayClick?.({ layerId: renderableOverlay.layerId, event, feature: overlayFeature })
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
      data-hovered-segment-id={hoveredRiverSegmentId ?? ''}
      data-overlay-source-type={renderableOverlay?.source.type ?? ''}
      data-overlay-source-layer={renderableOverlay?.source.type === 'vector' ? renderableOverlay.source.sourceLayer : ''}
      data-met-station-feature-count={showStationLayer ? stationFeatureCollection?.features.length ?? 0 : 0}
      data-national-river-feature-count={nationalRiverGeo?.features.length ?? 0}
    >
      <Map
        ref={mapRef}
        initialViewState={CHINA_VIEW_STATE}
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
        {nationalRiverGeo && nationalRiverGeo.features.length > 0 ? (
          <M11NationalRiverPrimitive
            collection={nationalRiverGeo}
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
        {renderableOverlay ? <M11OverlayPrimitive overlay={renderableOverlay} data={overlayData} /> : null}
        {selectedSegmentFeatureCollection.features.length > 0 ? (
          <M11SelectedSegmentPrimitive collection={selectedSegmentFeatureCollection} />
        ) : null}
        {showStationLayer && stationFeatureCollection ? (
          <M11StationClusterPrimitive collection={stationFeatureCollection} />
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

interface SelectedSegmentFeature {
  type: 'Feature'
  geometry: components['schemas']['GeoJsonLineString']
  properties: {
    segment_id: string
  }
}

interface SelectedSegmentFeatureCollection {
  type: 'FeatureCollection'
  features: SelectedSegmentFeature[]
  unavailableReason: string | null
}

export function buildM11RegisteredOverlay(state: M11QueryState, layers: LayerState[]): M11RegisteredOverlay | null {
  const selectedLayer = layers.find((layer) => layer.layerId === state.layer)
  if (!selectedLayer?.available) return null

  const selectedValidTime = normalizeIso(state.validTime)
  const validTime =
    selectedValidTime && selectedLayer.validTimes.includes(selectedValidTime) ? selectedValidTime : selectedLayer.currentValidTime
  if (!validTime) return null

  const metadata = selectedLayer.metadata
  if (!isMvtLayerMetadata(metadata) || metadata.release_blocking || !metadataHasValidTime(metadata, validTime)) {
    return null
  }

  // national 总览（无 {run_id} 占位）：不要求 runId、跳过 metadataMatchesRun；single-run 保持原契约。
  const national = isNationalOverlayMetadata(metadata)
  const runId = selectedLayer.freshness.runId
  if (!national) {
    if (!runId) return null
    if (
      !metadataMatchesRun(metadata, runId, {
        basin_version_id: selectedLayer.freshness.basinVersionId,
        river_network_version_id: selectedLayer.freshness.riverNetworkVersionId,
      })
    ) {
      return null
    }
  }

  const sourceId = `m11-${state.layer}-source`
  const layerId = `m11-${state.layer}-line`
  const variable = selectedLayer.layerId === 'water-level' ? 'water_level' : 'q_down'
  const replacements: Record<string, string> = national
    ? { valid_time: validTime, variable }
    : { run_id: runId as string, duration: DEFAULT_FLOOD_RETURN_PERIOD_DURATION, valid_time: validTime, variable }

  return {
    layerId: state.layer,
    sourceId,
    sourceKey: m11VectorSourceKey({
      layerId: selectedLayer.layerId,
      runId: national ? null : runId,
      validTime,
      variable,
      metadata,
    }),
    source: {
      type: 'vector',
      tiles: [buildMvtTileUrlTemplate(metadata, replacements)],
      sourceLayer: metadata.maplibre_source_layer,
      minzoom: metadata.min_zoom ?? 0,
      maxzoom: metadata.max_zoom ?? 14,
      metadata,
    },
    layer: {
      id: layerId,
      type: 'line',
      source: sourceId,
      'source-layer': metadata.maplibre_source_layer,
      paint: m11RegisteredOverlayPaint(selectedLayer.layerId),
    },
  }
}

function m11VectorSourceKey({
  layerId,
  runId,
  validTime,
  variable,
  metadata,
}: {
  layerId: string
  runId: string | null
  validTime: string
  variable: string
  metadata: NonNullable<LayerState['metadata']>
}): string {
  return JSON.stringify({
    basin_version_id: metadata.source_refs?.basin_version_id ?? null,
    cache_etag: metadata.cache_etag ?? null,
    cache_version: metadata.cache_version ?? null,
    canonical_route_layer_id: metadata.canonical_route_layer_id ?? metadata.layer_id,
    duration: layerId === 'flood-return-period' || layerId === 'warning-level' ? DEFAULT_FLOOD_RETURN_PERIOD_DURATION : null,
    encoder_version: metadata.encoder_version ?? null,
    layer_id: layerId,
    maplibre_source_layer: metadata.maplibre_source_layer,
    run_id: runId,
    schema_version: metadata.schema_version ?? metadata.property_schema_version ?? null,
    source_refs: metadata.source_refs ?? null,
    valid_time: validTime,
    variable,
  })
}

function m11RegisteredOverlayPaint(layerId: string): LayerProps['paint'] {
  if (layerId === 'flood-return-period' || layerId === 'warning-level') return floodTileLayerPaint()
  if (layerId === 'water-level') return waterLevelTileLayerPaint()
  return dischargeTileLayerPaint()
}

// 按 value 插值的线宽再套一层 zoom 收缩：全国 zoom 下整流域河网只占几十像素，
// 全宽会糊成实心色块；zoom4 收到 0.4×，zoom7 起恢复全宽。
// logDomain：流量值跨 6 个数量级（实测 p50≈0.0003、max≈300 m3/s），线性域无层次，
// 线宽与色带统一走 log10(max(value, 0.01)) 域。
function zoomScaledValueWidth(valueStops: number[], lowZoomFactor: number, logDomain = false) {
  const input = logDomain
    ? ['log10', ['max', ['coalesce', ['get', 'value'], 0], 0.01]]
    : ['coalesce', ['get', 'value'], 0]
  const widthAt = (scale: number) => [
    'interpolate',
    ['linear'],
    input,
    ...valueStops.map((stop, index) => (index % 2 === 1 ? Math.round(stop * scale * 100) / 100 : stop)),
  ]
  return ['interpolate', ['linear'], ['zoom'], 4, widthAt(lowZoomFactor), 7, widthAt(1)] as unknown as number
}

function dischargeTileLayerPaint(): LayerProps['paint'] {
  return {
    // log10 域插值的流量色带（与 m11DischargeColor/图例同源）。锚点按实测分布定
    // （近 2 日 q_down 分位：p50≈0.0003 / p75≈0.09 / p90≈1.6 / max≈307 m3/s）：
    // 线性域或高锚 log 域都会让山区小流域整体落进最低一桶、全网统一蓝；
    // 0.01→10000 跨 6 个数量级的 log 阶让支流→干流呈现浅蓝→深蓝→红的真实梯度。
    // 低端不走近白浅色（99% 河段值 <2，浅色在浅底图上整网隐形）：从可见中浅蓝
    // 起步，深浅只表达量级、不牺牲存在感。
    'line-color': [
      'interpolate',
      ['linear'],
      ['log10', ['max', ['coalesce', ['get', 'value'], 0], 0.01]],
      -2,
      '#7FB8DC',
      0,
      '#4292C6',
      1,
      '#2171B5',
      2,
      '#08519C',
      3,
      '#08306B',
      4,
      '#CB181D',
    ],
    // 线宽同走 log 域：小溪细、干流粗，视觉层级与流量量级一致。
    'line-width': zoomScaledValueWidth([-2, 1.8, 0, 2.4, 2, 3.4, 4, 5, 4.7, 7], 0.4, true),
    'line-opacity': ['case', ['has', 'value'], 0.95, 0.5],
  }
}

function waterLevelTileLayerPaint(): LayerProps['paint'] {
  return {
    // 颜色下限改可见浅青（原 #E0F7FA 近白）；与 m11WaterLevelColor/图例同步。
    'line-color': [
      'interpolate',
      ['linear'],
      ['coalesce', ['get', 'value'], 0],
      0,
      '#8FDCE8',
      0.5,
      '#80DEEA',
      1,
      '#26C6DA',
      2,
      '#00897B',
      4,
      '#FDD835',
      8,
      '#D81B60',
    ],
    'line-width': zoomScaledValueWidth([0, 2.2, 1, 3, 2, 3.8, 4, 5, 8, 6.2], 0.4),
    'line-opacity': ['case', ['has', 'value'], 0.95, 0.5],
  }
}

function M11OverlayPrimitive({
  overlay,
  data,
}: {
  overlay: M11RegisteredOverlay
  data: FloodReturnPeriodFeatureCollection | null
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
      </Source>
    )
  }
  if (!data) return null
  return (
    <Source id={overlay.sourceId} type="geojson" data={data} promoteId="feature_id">
      {casingLayer ? <Layer {...casingLayer} /> : null}
      <Layer {...mainLayer} />
      {hitLayer ? <Layer {...hitLayer} /> : null}
    </Source>
  )
}

// 光晕底衬 paint：白色、比主线宽约 2px、半透明，给彩色河段一圈干净的描边光晕（类似标注 halo），
// 浅底图上清晰可辨且不像深色道路（弃用早期深色 casing）。
function m11OverlayCasingPaint(layerId: string): LayerProps['paint'] {
  const isWaterLevel = layerId === 'water-level'
  // discharge 主线走 log 域：casing 必须同域贴主线（约 +1.2px），线性域下白晕恒比
  // 低流量主线宽一倍，会把彩色稀释成淡白。
  const valueStops = isWaterLevel ? [0, 3.4, 1, 4, 2, 5, 4, 6.2, 8, 7.4] : [-2, 3, 0, 3.6, 2, 4.6, 4, 6.2, 4.7, 8.2]
  return {
    'line-color': '#FFFFFF',
    'line-opacity': 0.85,
    'line-width': zoomScaledValueWidth(valueStops, 0.4, !isWaterLevel),
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
        const anchor = multiPolygonAnchor(feature.geometry)
        if (!anchor) return null
        return (
          <Marker key={feature.properties.basin_id} longitude={anchor[0]} latitude={anchor[1]} anchor="center">
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

// 最大外环的顶点平均作为标注锚点（凹形流域下比 bbox 中心更不容易飘出边界）。
function multiPolygonAnchor(geometry: BasinFeature['geometry']): [number, number] | null {
  let largestRing: number[][] | null = null
  for (const polygon of geometry?.coordinates ?? []) {
    const ring = (polygon as unknown as number[][][])[0]
    if (Array.isArray(ring) && (!largestRing || ring.length > largestRing.length)) largestRing = ring
  }
  if (!largestRing || largestRing.length === 0) return null
  let sumLon = 0
  let sumLat = 0
  let count = 0
  for (const position of largestRing) {
    const [lon, lat] = position
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue
    sumLon += lon
    sumLat += lat
    count += 1
  }
  return count > 0 ? [sumLon / count, sumLat / count] : null
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
function M11StationClusterPrimitive({ collection }: { collection: M11StationFeatureCollection }) {
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
    </Source>
  )
}

type StationClusterSource = {
  getClusterExpansionZoom?: (clusterId: number, callback: (error: unknown, zoom: number) => void) => void
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
  source.getClusterExpansionZoom(clusterId, (error, zoom) => {
    if (error) return
    map.flyTo({ center: [lon, lat], zoom, duration: 450 })
  })
}

export function buildSelectedSegmentFeatureCollection(
  selectedSegmentId: string | null | undefined,
  geometry: components['schemas']['GeoJsonLineString'] | null | undefined,
): SelectedSegmentFeatureCollection {
  const geometryStatus = selectedSegmentId ? getM11SelectedSegmentGeometryBudgetStatus(geometry) : null
  return {
    type: 'FeatureCollection',
    features:
      selectedSegmentId && geometryStatus?.sanitizedGeometry
        ? [
            {
              type: 'Feature',
              geometry: geometryStatus.sanitizedGeometry,
              properties: { segment_id: selectedSegmentId },
            },
          ]
        : [],
    unavailableReason:
      selectedSegmentId && !geometryStatus?.sanitizedGeometry
        ? selectedSegmentUnavailableReason(geometryStatus?.reason)
        : null,
  }
}

function selectedSegmentUnavailableReason(reason: string | null | undefined) {
  if (!reason) return '选中河段缺少可渲染几何，地图不会绘制河段高亮。'
  if (reason.includes('serialized-size')) return '选中河段几何超过客户端序列化预算，地图不会绘制河段高亮。'
  if (reason.includes('rendering budget') || reason.includes('coordinate dimensions')) {
    return '选中河段几何超过客户端渲染预算，地图不会绘制河段高亮。'
  }
  if (reason.includes('at least two')) return '选中河段几何少于两个坐标点，地图不会绘制河段高亮。'
  return '选中河段几何格式无效，地图不会绘制河段高亮。'
}

export function buildBasinFeatureCollection(basins: OverviewBasin[], visibleBasinIds: string[] | undefined): BasinFeatureCollection {
  const visible = visibleBasinIds ? new Set(visibleBasinIds) : null
  return {
    type: 'FeatureCollection',
    features: basins
      .filter((basin) => basin.boundary && getM11BasinGeometryBudgetStatus(basin.boundary).ok && (!visible || visible.has(basin.basinId)))
      .map((basin) => ({
        type: 'Feature',
        geometry: basin.boundary as NonNullable<OverviewBasin['boundary']>,
        properties: {
          basin_id: basin.basinId,
          basin_name: basin.displayName,
          basin_group: basin.basinGroup,
          area_km2: basin.areaKm2,
          river_count: basin.riverCount,
          active_model_count: basin.activeModelCount,
          latest_forecast_time: basin.latestForecastTime,
          selected_basin_version_id: basin.selectedBasinVersionId,
          unavailable_reason: basin.unavailableReason,
        },
      })),
  }
}

export function buildBasinRiverFeatureCollection(
  rows: BasinSegmentRow[],
  layer: M11Layer,
): BasinRiverFeatureCollection {
  let skippedCount = 0
  let coordinateCount = 0
  let featureSerializedBytes = 0
  let serializedBytes = serializedByteLength({ type: 'FeatureCollection', features: [] })
  const features: BasinRiverFeature[] = []

  for (const row of rows) {
    const geometryStatus = getM11SelectedSegmentGeometryBudgetStatus(row.geometry)
    if (!geometryStatus.sanitizedGeometry) {
      skippedCount += 1
      continue
    }

    const candidate: BasinRiverFeature = {
      type: 'Feature',
      geometry: geometryStatus.sanitizedGeometry,
      properties: {
        segment_id: row.segmentId,
        river_segment_id: row.riverSegmentId,
        basin_version_id: row.basinVersionId,
        river_network_version_id: row.riverNetworkVersionId,
        segment_name: row.displayName,
        q_value: row.currentQ,
        q_unit: row.qUnit,
        return_period: row.returnPeriod,
        warning_level: row.warningLevel,
        layer_color: m11BasinRiverLayerColor(row, layer),
      },
    }
    const candidateSerializedBytes = serializedByteLength(candidate)
    const nextFeatureCount = features.length + 1
    const nextCoordinateCount = coordinateCount + geometryStatus.coordinateCount
    const nextSerializedBytes =
      serializedByteLength({ type: 'FeatureCollection', features: [] }) +
      featureSerializedBytes +
      candidateSerializedBytes +
      Math.max(0, nextFeatureCount - 1)

    if (
      nextFeatureCount > m11BasinRiverCollectionBudget.maxFeatures ||
      nextCoordinateCount > m11BasinRiverCollectionBudget.maxCoordinates ||
      nextSerializedBytes > m11BasinRiverCollectionBudget.maxSerializedBytes
    ) {
      skippedCount += 1
      continue
    }

    features.push(candidate)
    coordinateCount = nextCoordinateCount
    featureSerializedBytes += candidateSerializedBytes
    serializedBytes = nextSerializedBytes
  }

  const sourceData = { type: 'FeatureCollection' as const, features }

  return {
    type: 'FeatureCollection',
    features,
    sourceData,
    skippedCount,
    coordinateCount,
    serializedBytes,
    unavailableReason:
      rows.length > 0 && features.length === 0
        ? '当前流域河段几何缺失或整体河网超过客户端渲染预算，地图不会注册过大的河网源。'
        : skippedCount > 0
          ? `${skippedCount} 条河段缺少可渲染几何或超出整体河网预算，已从地图河网中省略。`
          : null,
  }
}

function segmentFilter(segmentId?: string | null): FilterSpecification {
  return [
    'any',
    ['==', ['get', 'river_segment_id'], segmentId ?? ''],
    ['==', ['get', 'segment_id'], segmentId ?? ''],
  ] as FilterSpecification
}

function serializedByteLength(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value)).length
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

function m11SelectedLayerUnavailableReason(
  state: M11QueryState,
  layers: LayerState[],
  overlay: M11RegisteredOverlay | null,
  overlayData: FloodReturnPeriodFeatureCollection | null,
  hasBasinRiverNetwork = false,
) {
  if (overlay && (overlay.source.type === 'vector' || overlayData)) return null
  // 代站为独立 clustered-GeoJSON 图层，不走 MVT overlay 注册路径；其空态/truncation 由页面层诚实标注。
  if (state.layer === 'met-stations') return null
  if (hasBasinRiverNetwork && (state.layer === 'discharge' || state.layer === 'flood-return-period' || state.layer === 'warning-level')) {
    return null
  }
  if (overlay) return '洪水重现期地图数据正在加载或已被客户端预算拦截，地图暂不显示该叠加层。'
  const selectedLayer = layers.find((layer) => layer.layerId === state.layer)
  if (!selectedLayer) return '当前图层尚未由 /api/v1/layers 注册，地图不会渲染该叠加层。'
  if (!selectedLayer.available) return selectedLayer.disabledReason ?? '当前图层没有可渲染的有效时间。'
  // national 总览（无 run_id 占位）不要求 run_id，跳过该诚实空态分支。
  if (!isNationalOverlayMetadata(selectedLayer.metadata) && !selectedLayer.freshness.runId) {
    return '当前图层缺少可追溯 run_id，地图不会注册叠加层。'
  }
  if (!selectedLayer.currentValidTime) return '当前图层缺少有效时间，地图不会注册叠加层。'
  if (state.layer === 'discharge' || state.layer === 'water-level' || state.layer === 'flood-return-period' || state.layer === 'warning-level') {
    return '当前水文图层缺少可用 MVT 元数据或处于 release-blocked 状态，地图不会请求无边界 GeoJSON 兼容源。'
  }
  return '当前图层缺少可用地图源，地图不会注册叠加层。'
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

function normalizeIso(value: string | null | undefined) {
  if (!value) return null
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : null
}

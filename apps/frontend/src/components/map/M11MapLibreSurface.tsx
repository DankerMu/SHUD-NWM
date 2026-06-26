import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Map, {
  NavigationControl,
  Popup,
  ScaleControl,
  type MapLayerMouseEvent,
  type MapRef,
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
  buildM11InteractiveLayerIds,
  handleM11MapClick,
  handleM11MapMouseLeave,
  handleM11MapMouseMove,
  type M11MapOverlayInteraction,
} from '@/components/map/m11MapInteractions'
import {
  M11BasinLabelMarkers,
  M11BasinPrimitive,
  M11BasinRiverPrimitive,
  M11NationalRiverPrimitive,
  M11OverlayPrimitive,
  M11SelectedSegmentPrimitive,
  M11StationClusterPrimitive,
  type M11StationFeatureCollection,
} from '@/components/map/m11MapPrimitives'
import {
  M11MapStatusOverlays,
  m11MapSourceErrorResetKey,
  m11MapStyles,
  m11MapStyleUrls,
  useM11MapCamera,
  useM11MapSourceError,
  type M11MapCameraFit,
  type M11MapCameraFlyTo,
} from '@/components/map/m11MapRuntime'
import {
  type BasinSegmentRow,
  type LayerState,
  type M11WarningLevel,
  type OverviewBasin,
} from '@/lib/m11/overviewDataContracts'
import type { M11QueryState } from '@/lib/m11/queryState'

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
export type { M11MapOverlayInteraction } from '@/components/map/m11MapInteractions'
export { m11MapStyleUrls, type M11MapCameraFit, type M11MapCameraFlyTo } from '@/components/map/m11MapRuntime'
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
  const initialViewState = useM11MapCamera({ fitTo, flyTo, mapRef })
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
  const interactiveLayerIds = buildM11InteractiveLayerIds({
    showStationLayer,
    hasBasinRiverFeatures: basinRiverFeatureCollection.features.length > 0,
    hasBasinFeatures: basinFeatureCollection.features.length > 0,
    renderableOverlay,
  })
  const sourceErrorResetKey = m11MapSourceErrorResetKey({
    basinFeatureCount: basinFeatureCollection.features.length,
    overlaySourceId: overlay?.sourceId,
    basemap: state.basemap,
    layer: state.layer,
    validTime: state.validTime,
  })
  const { mapSourceError, handleMapError } = useM11MapSourceError(sourceErrorResetKey)

  useEffect(() => {
    setOverlayData(null)
    setOverlayUnavailableReason(null)
  }, [overlay])

  const handleMouseMove = useCallback(
    (event: MapLayerMouseEvent) => {
      handleM11MapMouseMove(event, {
        showStationLayer,
        renderableOverlay,
        mapRef: mapRef.current,
        onOverlayHover,
        setHoveredRiverSegmentId,
      })
    },
    [onOverlayHover, renderableOverlay, showStationLayer],
  )

  const handleMouseLeave = useCallback(
    (event: MapLayerMouseEvent) => {
      handleM11MapMouseLeave(event, { onOverlayHover, setHoveredRiverSegmentId })
    },
    [onOverlayHover],
  )

  const handleClick = useCallback(
    (event: MapLayerMouseEvent) => {
      handleM11MapClick(event, {
        showStationLayer,
        renderableOverlay,
        mapRef: mapRef.current,
        onOverlayClick,
      })
    },
    [onOverlayClick, renderableOverlay, showStationLayer],
  )

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

      {hoveredRiverSegmentId ? (
        <M11RiverTooltip feature={basinRiverFeatureCollection.features.find((feature) => feature.properties.river_segment_id === hoveredRiverSegmentId || feature.properties.segment_id === hoveredRiverSegmentId) ?? null} />
      ) : null}
      <M11MapStatusOverlays
        loading={loading}
        boundaryLoading={boundaryLoading}
        basinCount={basins.length}
        basinFeatureCount={basinFeatureCollection.features.length}
        skippedBasinGeometryCount={skippedBasinGeometryCount}
        unavailableReason={unavailableReason}
        basinRiverUnavailableReason={basinRiverFeatureCollection.unavailableReason}
        selectedSegmentMapState={selectedSegmentMapState}
        selectedSegmentUnavailableReason={selectedSegmentFeatureCollection.unavailableReason}
        mapSourceError={mapSourceError}
      />
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

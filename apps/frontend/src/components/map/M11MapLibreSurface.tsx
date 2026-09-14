import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Map, {
  NavigationControl,
  ScaleControl,
  type MapLayerMouseEvent,
  type MapRef,
} from 'react-map-gl/maplibre'
import type { FeatureCollection } from 'geojson'
import 'maplibre-gl/dist/maplibre-gl.css'

import { cn } from '@/lib/cn'
import {
  buildBasinFeatureCollection,
  buildM11RegisteredOverlay,
  buildSelectedSegmentFeatureCollection,
  countSkippedBasinGeometries,
  m11BasinBoundaryOverlayEnabled,
  m11SelectedLayerUnavailableReason,
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
  M11NationalRiverPrimitive,
  M11OverlayPrimitive,
  M11PrecipOverlayPrimitive,
  M11StationClusterPrimitive,
  M11_NATIONAL_RIVER_LINE_LAYER_ID,
  m11RegisteredOverlayHitLayerId,
  type M11StationFeatureCollection,
} from '@/components/map/m11MapPrimitives'
import type { M11PrecipOverlayModel } from '@/components/map/m11PrecipOverlay'
import { m11SelectionDataAttributes, resolveM11SelectedSegmentMapState } from '@/components/map/m11MapSelection'
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
  adaptRiverClickHookMap,
  createRiverClickEvidenceHook,
  createRiverClickHookController,
  deleteRiverClickHookIfOwned,
  selectRenderedRiverFeature,
  type RiverClickHookSelectionInput,
} from '@/lib/riverClickEvidence/hook'
import { type LayerState, type OverviewBasin } from '@/lib/m11/overviewDataContracts'
import type { M11Layer, M11QueryState } from '@/lib/m11/queryState'
import { buildMvtTileUrlTemplate, isMvtLayerMetadata } from '@/lib/mvtLayerMetadata'

export {
  buildBasinFeatureCollection,
  buildM11RegisteredOverlay,
  buildM11RenderedNationalRiverCollection,
  buildSelectedSegmentFeatureCollection,
  countSkippedBasinGeometries,
  m11BasinBoundaryOverlayEnabled,
  m11BasinLabelAnchor,
  m11SelectedLayerUnavailableReason,
  m11VectorSourceKey,
  segmentFilter,
  type BasinFeatureCollection,
  type M11RegisteredOverlay,
  type SelectedSegmentFeatureCollection,
} from '@/components/map/m11MapBuilders'
export type { M11MapOverlayInteraction } from '@/components/map/m11MapInteractions'
export { m11MapStyleUrls, type M11MapCameraFit, type M11MapCameraFlyTo } from '@/components/map/m11MapRuntime'
export { m11NationalRiverPaint, type M11StationFeatureCollection } from '@/components/map/m11MapPrimitives'

// Monotonic token source and the token of the currently installed hook.
// Cleanup deletes only when both object identity and the installed token match.
let riverClickHookGeneration = 0
let riverClickInstalledGeneration: number | null = null

interface M11MapLibreSurfaceProps {
  state: M11QueryState
  layers: LayerState[]
  basins?: OverviewBasin[]
  visibleBasinIds?: string[]
  /** 常态河网底图（来自 basin shp，WGS84，按 Type 分级）。null 则 honest 降级不画。 */
  nationalRiverGeo?: FeatureCollection | null
  /** 已被动态 mesh 河网层覆盖的流域 id：这些流域的静态河流从 national 底图剔除，规避双线。 */
  meshRiverBasinIds?: string[]
  selectedSegmentId?: string | null
  selectedStationId?: string | null
  metStations?: boolean
  stationFeatureCollection?: M11StationFeatureCollection | null
  /**
   * 已解析的降水叠加模型（`resolveM11PrecipOverlay` 的输出）。
   * 不传 / null = 无叠加：绝不在本组件里造第二条解析路径。
   */
  precipOverlay?: M11PrecipOverlayModel | null
  /** 数据加载中（overview 取数）：抑制叠加层/边界/河段"未就绪"类瞬态空态，避免刷新闪烁。 */
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
  nationalRiverGeo = null,
  selectedSegmentId = null,
  selectedStationId = null,
  metStations,
  stationFeatureCollection = null,
  precipOverlay = null,
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
  const [overlayData, setOverlayData] = useState<FeatureCollection | null>(null)
  const [overlayUnavailableReason, setOverlayUnavailableReason] = useState<string | null>(null)
  const overlay = useMemo(() => buildM11RegisteredOverlay(state, layers), [layers, state])
  const basinFeatureCollection = useMemo(
    () => buildBasinFeatureCollection(basins, visibleBasinIds),
    [basins, visibleBasinIds],
  )
  const skippedBasinGeometryCount = useMemo(
    () => countSkippedBasinGeometries(basins, visibleBasinIds),
    [basins, visibleBasinIds],
  )
  const renderableOverlay = overlay && (overlay.source.type === 'vector' || overlayData) ? overlay : null
  const nationalRiverVectorSource = useMemo(() => {
    const metadata = layers.find((layer) => layer.layerId === 'river-network')?.metadata
    if (
      !isMvtLayerMetadata(metadata) ||
      !metadata.url_template.includes('/river-network-national/') ||
      metadata.release_blocking
    ) {
      return null
    }
    return {
      tiles: [buildMvtTileUrlTemplate(metadata, {})],
      minzoom: metadata.min_zoom ?? 0,
      maxzoom: metadata.max_zoom ?? 14,
    }
  }, [layers])
  // 全国总览不携带选中河段几何：选中态只靠已注册的叠加层高亮，缺叠加层时诚实提示几何不可用。
  const selectedSegmentUnavailableReason = useMemo(
    () => buildSelectedSegmentFeatureCollection(selectedSegmentId, null).unavailableReason,
    [selectedSegmentId],
  )
  const selectedSegmentMapState = resolveM11SelectedSegmentMapState({
    selectedSegmentId,
    hasRenderableOverlay: Boolean(renderableOverlay),
  })
  const unavailableReason = useMemo(
    () =>
      overlayUnavailableReason ??
      m11SelectedLayerUnavailableReason(state, layers, overlay, overlayData),
    [layers, overlay, overlayData, overlayUnavailableReason, state],
  )
  // 代站图层由独立 overlay 状态控制，有非空 features 时渲染/注册（关闭 overlay 不注册 source/layer）。
  const showStationLayer = (metStations ?? state.metStations) && (stationFeatureCollection?.features.length ?? 0) > 0
  const interactiveLayerIds = buildM11InteractiveLayerIds({
    showStationLayer,
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

  // 仅测试门（exact pre-start boolean）下的只读 river-click 钩子；无该 flag 时绝不暴露全局。
  // 该钩子 fit/query 当前地图并把匹配的已渲染要素交给既有 onOverlayClick；只暴露
  // selectRenderedRiver 一个方法，不暴露 map ref / 通用 query / 任何 mutation 面。
  const overlayRef = useRef(renderableOverlay)
  overlayRef.current = renderableOverlay
  // onOverlayClick 回调身份可能随父组件渲染变化；用 ref 承载最新回调，使钩子对象
  // 在本挂载期内保持同一 identity（payload 回调用最新真实回调，cleanup 仍走代次 token）。
  const onOverlayClickRef = useRef(onOverlayClick)
  onOverlayClickRef.current = onOverlayClick
  useEffect(() => {
    if ((window as { __NHMS_E2E_HOOKS__?: unknown }).__NHMS_E2E_HOOKS__ !== true) return
    // 单调代次 token：每个挂载 owner 独有一份；riverClickInstalledGeneration 记录
    // 当前安装者的 token。过期 cleanup 只有在对象与已安装 token 都仍匹配时才允许
    // 删除全局并清空 token，绝不能删掉更新的实例。
    const generation = riverClickHookGeneration
    riverClickHookGeneration += 1
    const controller = createRiverClickHookController({
      // Adapter: the native maplibre-gl Map is narrowed to the hook's
      // RiverClickHookMap (bound only to the read/fit/query/idle methods). A
      // null/absent map stays null so the controller waits, never fails fast.
      getMap: () => {
        const native = mapRef.current?.getMap?.()
        return native === undefined || native === null ? null : adaptRiverClickHookMap(native)
      },
      getOverlayHitLayerId: () => {
        const overlay = overlayRef.current
        return overlay?.layerId === 'discharge' ? m11RegisteredOverlayHitLayerId(overlay) : null
      },
      now: () => performance.now(),
      select: selectRenderedRiverFeature,
    })
    const hook = createRiverClickEvidenceHook({
      // Fail closed: without a real onOverlayClick the hook must reject, never
      // dispatch into a no-op success. The LATEST real callback is dispatched
      // through the ref, so a rerender never replaces the hook object.
      onOverlayClick: (dispatch) => {
        const callback = onOverlayClickRef.current
        if (typeof callback !== 'function') {
          throw new Error('river-click hook callback dispatch failed')
        }
        callback({
          layerId: dispatch.layerId as M11Layer | 'met-stations' | 'basin-boundaries',
          event: dispatch.event as MapLayerMouseEvent,
          feature: dispatch.feature as NonNullable<MapLayerMouseEvent['features']>[number],
        })
      },
      controller,
      now: () => performance.now(),
    })
    ;(window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence = hook
    riverClickInstalledGeneration = generation
    return () => {
      const current = (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence
      // 只允许「同一对象 + 已安装 token」删除；过期 cleanup 不能删掉更新的实例。
      if (deleteRiverClickHookIfOwned(current, hook, generation, riverClickInstalledGeneration)) {
        delete (window as unknown as Record<string, unknown>).__nhmsRiverClickEvidence
        riverClickInstalledGeneration = null
      }
    }
  }, [])

  const handleMouseMove = useCallback(
    (event: MapLayerMouseEvent) => {
      handleM11MapMouseMove(event, {
        showStationLayer,
        renderableOverlay,
        mapRef: mapRef.current,
        onOverlayHover,
      })
    },
    [onOverlayHover, renderableOverlay, showStationLayer],
  )

  const handleMouseLeave = useCallback(
    (event: MapLayerMouseEvent) => {
      handleM11MapMouseLeave(event, { onOverlayHover })
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
      {...m11SelectionDataAttributes({ selectedSegmentId, selectedSegmentMapState, selectedStationId })}
      data-overlay-source-type={renderableOverlay?.source.type ?? ''}
      data-overlay-source-layer={renderableOverlay?.source.type === 'vector' ? renderableOverlay.source.sourceLayer : ''}
      data-met-station-feature-count={showStationLayer ? stationFeatureCollection?.features.length ?? 0 : 0}
      data-national-river-feature-count="0"
      data-national-river-source-type={nationalRiverVectorSource ? 'vector' : nationalRiverGeo ? 'legacy-geojson' : ''}
      data-national-river-generation={
        layers.find((layer) => layer.layerId === 'river-network')?.metadata?.source_generation ?? ''
      }
      {...(precipOverlay?.url ? { 'data-precip-url': precipOverlay.url } : {})}
      {...(precipOverlay?.hiddenReason ? { 'data-precip-hidden-reason': precipOverlay.hiddenReason } : {})}
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
        {nationalRiverVectorSource ? (
          <M11NationalRiverPrimitive
            tiles={nationalRiverVectorSource.tiles}
            minzoom={nationalRiverVectorSource.minzoom}
            maxzoom={nationalRiverVectorSource.maxzoom}
            dimmed={Boolean(renderableOverlay)}
            satellite={state.basemap === 'satellite'}
          />
        ) : null}
        {/*
          紧跟河网原语挂载，`beforeId` **仅当**河网 source 真的存在时才传（fixture 决策 4）：
          `maplibre-gl` 的 `addLayer(layer, beforeId)` 在 `beforeId` 指向的图层不存在时会触发
          ErrorEvent 且**不添加**该图层，本组件的 `handleMapError` 会把它显示成「地图源加载失败」
          横幅——降水叠加静默消失，还顺带诬告整张地图。
        */}
        {precipOverlay ? (
          <M11PrecipOverlayPrimitive
            model={precipOverlay}
            beforeId={nationalRiverVectorSource ? M11_NATIONAL_RIVER_LINE_LAYER_ID : undefined}
          />
        ) : null}
        {basinFeatureCollection.features.length > 0 ? (
          <>
            <M11BasinPrimitive collection={basinFeatureCollection} />
            <M11BasinLabelMarkers collection={basinFeatureCollection} />
          </>
        ) : null}
        {renderableOverlay ? <M11OverlayPrimitive overlay={renderableOverlay} data={overlayData} selectedSegmentId={selectedSegmentId} /> : null}
        {showStationLayer && stationFeatureCollection ? (
          <M11StationClusterPrimitive collection={stationFeatureCollection} selectedStationId={selectedStationId} />
        ) : null}
      </Map>

      <M11MapStatusOverlays
        loading={loading}
        boundaryLoading={boundaryLoading}
        basinBoundaryOverlayEnabled={m11BasinBoundaryOverlayEnabled}
        basinCount={basins.length}
        basinFeatureCount={basinFeatureCollection.features.length}
        skippedBasinGeometryCount={skippedBasinGeometryCount}
        unavailableReason={unavailableReason}
        selectedSegmentMapState={selectedSegmentMapState}
        selectedSegmentUnavailableReason={selectedSegmentUnavailableReason}
        mapSourceError={mapSourceError}
      />
    </div>
  )
}

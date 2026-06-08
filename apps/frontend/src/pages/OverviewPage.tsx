import { useCallback, useEffect, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import {
  M11MapLibreSurface,
  type M11MapCameraFit,
  type M11MapOverlayInteraction,
  type M11MapPopupSlot,
  type M11StationFeatureCollection,
} from '@/components/map/M11MapLibreSurface'
import {
  M11BackToOverviewButton,
  M11FloatingLayerSwitcher,
  M11FloatingLegend,
  M11FloatingNotice,
  M11MapInfoCard,
  M11MetRasterNotice,
  M11OpsLink,
} from '@/components/map/M11FloatingControls'
import { useBasinDetailMode } from '@/components/m11/BasinDetailPanels'
import type { LayerState, M11Bbox, OverviewBasin } from '@/lib/m11/overviewDataContracts'
import {
  defaultM11QueryState,
  type M11QueryPatch,
  type M11QueryState,
  needsM11QueryReplacement,
  parseM11QueryState,
  serializeM11QueryState,
} from '@/lib/m11/queryState'
import { resolveM11ValidTimeCorrection } from '@/pages/m11/M11Controls'
import { useMetStationLayer } from '@/pages/m11/useStationLayer'
import { useAuthStore } from '@/stores/auth'
import { overviewSnapshotMatchesQuery, overviewSnapshotMetadataMatchesQuery, useOverviewDataStore } from '@/stores/overviewData'

const OPERATOR_ROLES = ['operator', 'model_admin', 'sys_admin']

/**
 * 单页全屏地图展示端（M26）：整个展示端 = 一张铺满视口的地图 + 玻璃质感浮层。
 * 删去左/右/底所有边栏；按 query 内 basinId 双模式：null=全国总览 / 非null=流域详情。
 * 图层切换（含气象栅格 honest 占位）走左上浮层；图例走右下浮层；河段/代站详情走玻璃弹窗。
 */
export function OverviewPage() {
  const location = useLocation()
  const navigate = useNavigate()
  const state = useMemo(() => parseM11QueryState(location.search), [location.search])
  const normalizedSearch = useMemo(() => serializeM11QueryState(state), [state])
  const needsQueryReplacement = needsM11QueryReplacement(location.search)

  const handleQueryChange = useCallback(
    (patch: M11QueryPatch) => {
      const nextSearch = serializeM11QueryState({ ...state, ...patch })
      navigate({ pathname: location.pathname, search: nextSearch ? `?${nextSearch}` : '' })
    },
    [location.pathname, navigate, state],
  )

  useEffect(() => {
    if (!needsQueryReplacement) return
    navigate({ pathname: location.pathname, search: normalizedSearch ? `?${normalizedSearch}` : '' }, { replace: true })
  }, [location.pathname, navigate, needsQueryReplacement, normalizedSearch])

  if (needsQueryReplacement) return null

  return state.basinId ? (
    <BasinDetailMode basinId={state.basinId} state={state} onQueryChange={handleQueryChange} />
  ) : (
    <OverviewMode state={state} onQueryChange={handleQueryChange} />
  )
}

/**
 * 全屏地图外壳：地图铺满视口，浮层切换器/图例/运维链接/气象栅格 honest 占位 + 自定义浮层。
 */
function M11FullscreenMap({
  state,
  layers,
  basins,
  visibleBasinIds,
  basinSegments,
  selectedSegmentId,
  selectedSegmentGeometry,
  stationFeatureCollection,
  popup,
  fitTo,
  mapLabel,
  infoTitle,
  infoMeta,
  onQueryChange,
  onOverlayHover,
  onOverlayClick,
  children,
}: {
  state: M11QueryState
  layers: LayerState[]
  basins?: OverviewBasin[]
  visibleBasinIds?: string[]
  basinSegments?: import('@/lib/m11/overviewDataContracts').BasinSegmentRow[]
  selectedSegmentId?: string | null
  selectedSegmentGeometry?: import('@/api/types').components['schemas']['GeoJsonLineString'] | null
  stationFeatureCollection?: M11StationFeatureCollection | null
  popup?: M11MapPopupSlot | null
  fitTo?: M11MapCameraFit | null
  mapLabel: string
  infoTitle: string
  infoMeta: string
  onQueryChange: (patch: M11QueryPatch) => void
  onOverlayHover?: (interaction: M11MapOverlayInteraction | null) => void
  onOverlayClick?: (interaction: M11MapOverlayInteraction) => void
  children?: React.ReactNode
}) {
  const role = useAuthStore((store) => store.role)
  const opsVisible = OPERATOR_ROLES.includes(role)

  return (
    <div
      className="flex h-[calc(100vh-var(--m11-nav-height))] min-h-[40rem] w-full flex-col overflow-hidden bg-[#d7e7ef]"
      style={{ '--m11-nav-height': '0px' } as React.CSSProperties}
    >
      <header className="flex h-12 shrink-0 items-center gap-2 border-b border-neutral-200 bg-white px-4 shadow-sm">
        <span className="text-base font-semibold tracking-wide text-primary-700">全国水文预报系统</span>
      </header>
      <section
        className="relative w-full flex-1 overflow-hidden"
        aria-label={mapLabel}
        data-testid="m11-fullscreen-map"
      >
      <M11MapLibreSurface
        state={state}
        layers={layers}
        basins={basins}
        visibleBasinIds={visibleBasinIds}
        basinSegments={basinSegments}
        selectedSegmentId={selectedSegmentId}
        selectedSegmentGeometry={selectedSegmentGeometry}
        stationFeatureCollection={stationFeatureCollection}
        popup={popup}
        fitTo={fitTo}
        onOverlayHover={onOverlayHover}
        onOverlayClick={onOverlayClick}
      />
      <M11FloatingLayerSwitcher layer={state.layer} onQueryChange={onQueryChange} />
      <M11MapInfoCard title={infoTitle} meta={infoMeta} />
      <M11OpsLink visible={opsVisible} />
      {state.layer === 'met-raster' ? <M11MetRasterNotice /> : null}
      {children}
      <M11FloatingLegend layer={state.layer} layers={layers} />
      </section>
    </div>
  )
}

function BasinDetailMode({
  basinId,
  state,
  onQueryChange,
}: {
  basinId: string
  state: M11QueryState
  onQueryChange: (patch: M11QueryPatch) => void
}) {
  const detail = useBasinDetailMode({ basinId, state, onQueryChange })

  return (
    <M11FullscreenMap
      state={state}
      layers={detail.layers}
      basins={detail.basins}
      visibleBasinIds={detail.visibleBasinIds}
      basinSegments={detail.basinSegments}
      selectedSegmentId={detail.selectedSegmentId}
      selectedSegmentGeometry={detail.selectedSegmentGeometry}
      stationFeatureCollection={detail.stationFeatureCollection}
      popup={detail.popup}
      fitTo={detail.fitTo}
      mapLabel={detail.mapLabel}
      infoTitle={detail.mapTitle}
      infoMeta={detail.mapMeta}
      onQueryChange={onQueryChange}
      onOverlayHover={detail.onMapOverlayHover}
      onOverlayClick={detail.onMapOverlayClick}
    >
      <M11BackToOverviewButton onClick={detail.backToOverview} />
      {detail.basinNotFoundReason ? (
        <M11FloatingNotice testId="m11-basin-not-found">
          未找到流域 {basinId}：{detail.basinNotFoundReason}
        </M11FloatingNotice>
      ) : detail.error ? (
        <M11FloatingNotice testId="m11-basin-error">{detail.error}</M11FloatingNotice>
      ) : detail.stationStatusNote ? (
        <M11FloatingNotice testId="m11-met-station-status">{detail.stationStatusNote}</M11FloatingNotice>
      ) : null}
    </M11FullscreenMap>
  )
}

const NONE_VISIBLE_SENTINEL = '__none__'

function OverviewMode({ state, onQueryChange }: { state: M11QueryState; onQueryChange: (patch: M11QueryPatch) => void }) {
  const dataLoadState = useMemo(
    () => ({
      source: state.source,
      cycle: state.cycle,
      validTime: state.validTime,
      layer: state.layer,
      basemap: defaultM11QueryState.basemap,
      basinVersionId: state.basinVersionId,
      riverNetworkVersionId: state.riverNetworkVersionId,
      basinId: null,
      segmentId: state.segmentId,
      warningLevel: state.warningLevel,
      q: state.q,
    }),
    [
      state.basinVersionId,
      state.cycle,
      state.layer,
      state.q,
      state.riverNetworkVersionId,
      state.segmentId,
      state.source,
      state.validTime,
      state.warningLevel,
    ],
  )
  const overview = useOverviewDataStore((store) => store.overview)
  const loading = useOverviewDataStore((store) => store.loading)
  const error = useOverviewDataStore((store) => store.error)
  const loadOverview = useOverviewDataStore((store) => store.loadOverview)
  const overviewMatchesQuery = overviewSnapshotMatchesQuery(overview, state)
  const overviewMetadataMatchesQuery = overviewSnapshotMetadataMatchesQuery(overview, state)
  const currentOverview = overviewMatchesQuery ? overview : null
  const metadataLayers = overviewMetadataMatchesQuery ? (overview?.layers ?? []) : []
  const layers = currentOverview?.layers ?? metadataLayers

  useEffect(() => {
    void loadOverview(dataLoadState).catch(() => undefined)
  }, [dataLoadState, loadOverview])

  useEffect(() => {
    if (loading || !overviewMetadataMatchesQuery || metadataLayers.length === 0) return
    const correctedValidTime = resolveM11ValidTimeCorrection(state, metadataLayers)
    if (correctedValidTime === undefined) return
    onQueryChange({ validTime: correctedValidTime })
  }, [onQueryChange, loading, metadataLayers, overviewMetadataMatchesQuery, state])

  const basins = currentOverview?.basins ?? []
  const summary = currentOverview?.summary
  const sourceSelection = summary?.sourceSelection ?? null
  const visibleBasinIdList = useMemo(() => basins.map((basin) => basin.basinId), [basins])
  const visibleBasinSet = useMemo(() => new Set(visibleBasinIdList), [visibleBasinIdList])
  const mapFitTo = useMemo(() => bboxToMapFit(unionBasinBbox(basins)), [basins])

  // 进入流域：点 basin → 写 basinId（就地切详情模式）。
  const handleMapOverlayClick = useCallback(
    (interaction: M11MapOverlayInteraction) => {
      const feature = interaction.feature ?? interaction.event.features?.find((item) => item.layer?.id === 'm11-basin-fill')
      const basinId = feature?.properties?.basin_id
      if (typeof basinId !== 'string' || !visibleBasinSet.has(basinId)) return
      const basin = basins.find((item) => item.basinId === basinId)
      if (!basin) return
      onQueryChange(basinAnalysisPatch(basin, state))
    },
    [basins, onQueryChange, state, visibleBasinSet],
  )
  const handleMapOverlayHover = useCallback((_interaction: M11MapOverlayInteraction | null) => undefined, [])

  // 全国总览开代站图层：无 basinId 不取数，honest 空态。
  const stationLayer = useMetStationLayer({
    active: state.layer === 'met-stations',
    basinId: null,
    resolvedSource: sourceSelection?.resolvedSource ?? null,
    cycle: state.cycle,
  })

  const boundaryCount = basins.filter((basin) => basin.boundary).length
  const emptyBasinReason =
    !loading && basins.length === 0
      ? error ??
        (summary?.totalBasins === 0
          ? '暂无可用流域数据'
          : currentOverview?.aggregationDecision.needsAggregationEndpoint
            ? currentOverview.aggregationDecision.evidence
            : '流域清单暂不可用')
      : null

  return (
    <M11FullscreenMap
      state={state}
      layers={layers}
      basins={basins}
      visibleBasinIds={visibleBasinIdList}
      stationFeatureCollection={stationLayer.featureCollection}
      fitTo={mapFitTo}
      mapLabel="全国总览地图"
      infoTitle="全国水文总览"
      infoMeta={`全国范围 73E-135E / 18N-53N；已接入 ${boundaryCount}/${basins.length} 个可用流域边界。点击流域边界进入流域详情。`}
      onQueryChange={onQueryChange}
      onOverlayHover={handleMapOverlayHover}
      onOverlayClick={handleMapOverlayClick}
    >
      {state.layer === 'met-stations' && stationLayer.statusNote ? (
        // 代站图层的 honest 状态优先（全国总览未选流域时诚实提示「请选择流域」）。
        <M11FloatingNotice testId="m11-met-station-status">{stationLayer.statusNote}</M11FloatingNotice>
      ) : loading ? (
        <M11FloatingNotice testId="m11-overview-loading">总览数据加载中</M11FloatingNotice>
      ) : emptyBasinReason ? (
        <M11FloatingNotice testId="m11-overview-empty">{emptyBasinReason}</M11FloatingNotice>
      ) : null}
    </M11FullscreenMap>
  )
}

function bboxToMapFit(bbox: M11Bbox | null | undefined): M11MapCameraFit | null {
  if (!bbox) return null
  return {
    bounds: [
      [bbox.minLon, bbox.minLat],
      [bbox.maxLon, bbox.maxLat],
    ],
    padding: 36,
  }
}

function unionBasinBbox(basins: OverviewBasin[]) {
  return basins.reduce<M11Bbox | null>((bbox, basin) => {
    if (!basin.bbox) return bbox
    return bbox
      ? {
          minLon: Math.min(bbox.minLon, basin.bbox.minLon),
          minLat: Math.min(bbox.minLat, basin.bbox.minLat),
          maxLon: Math.max(bbox.maxLon, basin.bbox.maxLon),
          maxLat: Math.max(bbox.maxLat, basin.bbox.maxLat),
        }
      : basin.bbox
  }, null)
}

// 进入流域分析的 query patch：写 basinId（就地切详情），携带 basinVersionId/segmentId 上下文。
function basinAnalysisPatch(basin: OverviewBasin, state: M11QueryState): M11QueryPatch {
  const selectedVersionIds = new Set(basin.basinVersions.map((version) => version.basinVersionId))
  const basinVersionId =
    state.basinVersionId && selectedVersionIds.has(state.basinVersionId) ? state.basinVersionId : basin.selectedBasinVersionId
  const carriesContext = Boolean(basinVersionId && basinVersionId === state.basinVersionId)
  return {
    basinId: basin.basinId,
    basinVersionId,
    riverNetworkVersionId: carriesContext ? state.riverNetworkVersionId : null,
    segmentId: carriesContext ? state.segmentId : null,
  }
}

export { NONE_VISIBLE_SENTINEL }

// 跨页上下文交接 helper（保留为纯函数：把当前 source/cycle/validTime 解析为具体源后拼到目标页）。
export function contextHandoff(
  pathname: string,
  state: M11QueryState,
  sourceSelection: import('@/lib/m11/overviewDataContracts').SourceScenarioSelectionState | null,
) {
  const sourceContext = resolvedDestinationSourceContext(state, sourceSelection)
  const search = serializeM11QueryState({
    ...defaultM11QueryState,
    source: sourceContext.source,
    cycle: sourceContext.cycle,
    validTime: sourceContext.validTime,
    warningLevel: state.warningLevel,
  })
  return {
    href: `${pathname}${search ? `?${search}` : ''}`,
    description: sourceContext.description,
  }
}

function resolvedDestinationSourceContext(
  state: M11QueryState,
  sourceSelection: import('@/lib/m11/overviewDataContracts').SourceScenarioSelectionState | null,
): { source: 'gfs' | 'ifs' | 'best'; cycle: string | null; validTime: string | null; description: string } {
  if (state.source === 'compare') {
    return {
      source: 'best' as const,
      cycle: null,
      validTime: null,
      description: 'GFS+IFS 对比暂不支持跨页保真，已省略具体源上下文',
    }
  }

  if (state.source === 'gfs' || state.source === 'ifs') {
    return {
      source: state.source,
      cycle: state.cycle,
      validTime: state.validTime,
      description: `带入 ${state.source.toUpperCase()} source/cycle/validTime 上下文`,
    }
  }

  const concrete = concreteSourceFromSelection(sourceSelection)
  if (!concrete) {
    return {
      source: 'best' as const,
      cycle: null,
      validTime: null,
      description: '等待 Best Available 解析到具体源后带入上下文',
    }
  }

  return {
    source: concrete,
    cycle: sourceSelection?.cycleTime ?? state.cycle,
    validTime: sourceSelection?.validTime ?? state.validTime,
    description: `带入 ${concrete.toUpperCase()} source/cycle/validTime 上下文`,
  }
}

function concreteSourceFromSelection(
  sourceSelection: import('@/lib/m11/overviewDataContracts').SourceScenarioSelectionState | null,
): 'gfs' | 'ifs' | null {
  if (sourceSelection?.resolvedSource === 'GFS') return 'gfs'
  if (sourceSelection?.resolvedSource === 'IFS') return 'ifs'
  return null
}

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
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
  M11FloatingBasemapSwitcher,
  M11FloatingLayerSwitcher,
  M11FloatingLegend,
  M11FloatingNotice,
  M11OpsLink,
} from '@/components/map/M11FloatingControls'
import { resolveNationalOverlayCycle } from '@/components/map/m11MapBuilders'
import { resolveM11PrecipOverlay, type M11PrecipOverlayModel } from '@/components/map/m11PrecipOverlay'
import { bboxToMapFit, mapFeatureStringProperty, popupAnchorFromInteraction, useBasinDetailMode } from '@/components/m11/BasinDetailPanels'
import { M11RiverForecastPanel, type M11RiverPopupSegment } from '@/components/map/M11RiverForecastPanel'
import { M11StationForcingPopup, type M11StationPopupStation } from '@/components/map/M11StationForcingPopup'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import { mergeLayerStates, type LayerState, type OverviewBasin } from '@/lib/m11/overviewDataContracts'
import {
  defaultM11QueryState,
  type M11QueryPatch,
  type M11QueryState,
  needsM11QueryReplacement,
  parseM11QueryState,
  serializeM11QueryState,
} from '@/lib/m11/queryState'
import { withStaticBasinBboxes } from '@/lib/m11/staticBasinFallback'
import { prefetchHydroMetLatestProducts } from '@/pages/hydroMet/bootstrap'
import {
  M11BottomControlBar,
  deriveM11ControlBarModel,
  type M11BottomControlBarProps,
} from '@/pages/m11/M11BottomControlBar'
import { resolveM11NationalValidTimeCorrection } from '@/pages/m11/M11Controls'
import { useNationalBasinGeo } from '@/pages/m11/useNationalBasinGeo'
import { useMetStationLayer } from '@/pages/m11/useStationLayer'
import { useAuthStore } from '@/stores/auth'
import {
  nationalConcreteSource,
  overviewSnapshotMatchesQuery,
  overviewSnapshotMetadataMatchesQuery,
  useOverviewDataStore,
} from '@/stores/overviewData'

const OPERATOR_ROLES = ['operator', 'model_admin', 'sys_admin']

/**
 * 单页全屏地图展示端（M26）：整个展示端 = 一张铺满视口的地图 + 玻璃质感浮层。
 * 删去左/右/底所有边栏；按 query 内 basinId 双模式：null=全国总览 / 非null=流域详情。
 * 图层切换走左上浮层；图例走右下浮层；河段/代站详情走玻璃弹窗。
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

  // 刷新/直达带 basinId 的 URL：仅首挂载剥离 basinId（`replace`），落到全国总览主页。剥离期间同步
  // 按总览渲染（绝不挂 BasinDetailMode），否则详情副作用会把 basinId 回写 URL、盖掉剥离形成竞态。
  // 闸门只认「首挂载是否带 basinId」，basinId 真正消失后即关闭，此后挂载期内写入的 basinId 不受影响。
  // 现状：本 build 里没有任何「挂载后写非空 basinId」的路径 —— 两处写 basinId 的调用点写的都是 null
  // （BasinDetailPanels.tsx `backToOverview`、本文件的剥离 effect），全国视图点流域只做相机 fit
  // （见 handleMapOverlayClick）。因此 BasinDetailMode 目前只对未来新增的写入方可达。
  const initialBasinStripRef = useRef(Boolean(state.basinId))
  const strippingInitialBasin = initialBasinStripRef.current && Boolean(state.basinId)
  useEffect(() => {
    if (!initialBasinStripRef.current) return
    if (!state.basinId) {
      initialBasinStripRef.current = false
      return
    }
    const next = serializeM11QueryState({ ...state, basinId: null })
    navigate({ pathname: location.pathname, search: next ? `?${next}` : '' }, { replace: true })
  }, [state, location.pathname, navigate])

  if (needsQueryReplacement) return null

  const effectiveBasinId = strippingInitialBasin ? null : state.basinId
  return effectiveBasinId ? (
    <BasinDetailMode basinId={effectiveBasinId} state={state} onQueryChange={handleQueryChange} />
  ) : (
    <OverviewMode state={state} onQueryChange={handleQueryChange} />
  )
}

/**
 * 全屏地图外壳：地图铺满视口，浮层切换器/图例/运维链接 + 自定义浮层。
 */
function M11FullscreenMap({
  state,
  layers,
  basins,
  visibleBasinIds,
  basinSegments,
  nationalRiverGeo,
  meshRiverBasinIds,
  selectedSegmentId,
  selectedSegmentGeometry,
  selectedStationId,
  stationFeatureCollection,
  popup,
  precipOverlay,
  precipAvailable,
  loading,
  boundaryLoading,
  fitTo,
  mapLabel,
  controlBar,
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
  nationalRiverGeo?: import('geojson').FeatureCollection | null
  meshRiverBasinIds?: string[]
  selectedSegmentId?: string | null
  selectedSegmentGeometry?:
    | import('@/api/types').components['schemas']['GeoJsonLineString']
    | import('@/api/types').components['schemas']['GeoJsonMultiLineString']
    | null
  selectedStationId?: string | null
  stationFeatureCollection?: M11StationFeatureCollection | null
  popup?: M11MapPopupSlot | null
  /** 已解析的降水叠加模型；不传 = 无叠加（流域详情模式，blocked by #2109）。 */
  precipOverlay?: M11PrecipOverlayModel | null
  /** 目录里是否有 `precip` 条目；不传 = 降水开关禁用并标「未实现」。 */
  precipAvailable?: boolean
  loading?: boolean
  boundaryLoading?: boolean
  fitTo?: M11MapCameraFit | null
  mapLabel: string
  /** 底部控制条模型（全国模式）。null / 不传 = 不渲染任何控制条 DOM（流域详情模式）。 */
  controlBar?: M11BottomControlBarProps | null
  onQueryChange: (patch: M11QueryPatch) => void
  onOverlayHover?: (interaction: M11MapOverlayInteraction | null) => void
  onOverlayClick?: (interaction: M11MapOverlayInteraction) => void
  children?: React.ReactNode
}) {
  const role = useAuthStore((store) => store.role)
  const opsVisible = OPERATOR_ROLES.includes(role)

  return (
    <div className="flex h-full min-h-[40rem] w-full flex-col overflow-hidden bg-[#d7e7ef]">
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
        nationalRiverGeo={nationalRiverGeo}
        meshRiverBasinIds={meshRiverBasinIds}
        selectedSegmentId={selectedSegmentId}
        selectedSegmentGeometry={selectedSegmentGeometry}
        selectedStationId={selectedStationId}
        stationFeatureCollection={stationFeatureCollection}
        popup={popup}
        precipOverlay={precipOverlay}
        loading={loading}
        boundaryLoading={boundaryLoading}
        fitTo={fitTo}
        onOverlayHover={onOverlayHover}
        onOverlayClick={onOverlayClick}
      />
      <M11FloatingLayerSwitcher
        layer={state.layer}
        metStations={state.metStations}
        precip={state.precip}
        precipAvailable={precipAvailable}
        onQueryChange={onQueryChange}
      />
      <M11FloatingBasemapSwitcher basemap={state.basemap} onQueryChange={onQueryChange} />
      <M11OpsLink visible={opsVisible} />
      {children}
      {controlBar ? <M11BottomControlBar {...controlBar} onQueryChange={onQueryChange} /> : null}
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
      nationalRiverGeo={detail.nationalRiverGeo}
      meshRiverBasinIds={detail.meshRiverBasinIds}
      selectedSegmentId={detail.selectedSegmentId}
      selectedSegmentGeometry={detail.selectedSegmentGeometry}
      selectedStationId={detail.selectedStationId}
      stationFeatureCollection={detail.stationFeatureCollection}
      popup={detail.popup}
      loading={detail.surfaceSettling}
      boundaryLoading={detail.boundaryLoading}
      fitTo={detail.fitTo}
      mapLabel={detail.mapLabel}
      onQueryChange={onQueryChange}
      onOverlayHover={detail.onMapOverlayHover}
      onOverlayClick={detail.onMapOverlayClick}
    >
      <M11BackToOverviewButton onClick={detail.backToOverview} />
      {detail.riverPanel}
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
type ActiveCurveWindow = 'river' | 'station'

function stationSeriesSourceAvailability(resolvedSource: string | null | undefined): HydroMetSource | 'GFS+IFS' | null {
  if (resolvedSource === 'GFS' || resolvedSource === 'IFS' || resolvedSource === 'GFS+IFS') return resolvedSource
  return null
}

function OverviewMode({ state, onQueryChange }: { state: M11QueryState; onQueryChange: (patch: M11QueryPatch) => void }) {
  const dataLoadState = useMemo(
    () => ({
      source: state.source,
      cycle: state.cycle,
      validTime: state.validTime,
      layer: state.layer,
      metStations: false,
      // precip 是纯渲染开关，不参与取数身份：带进 store 的 query 会让降水开关整轮重载
      // overview（并作废在途的 cycles / valid-times / precip index enrichment）。
      precip: defaultM11QueryState.precip,
      basemap: defaultM11QueryState.basemap,
      basinVersionId: state.basinVersionId,
      riverNetworkVersionId: state.riverNetworkVersionId,
      basinId: null,
      segmentId: state.segmentId,
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
    ],
  )
  const overview = useOverviewDataStore((store) => store.overview)
  const mapBootstrapLoading = useOverviewDataStore((store) => store.mapBootstrapLoading)
  // enrichment 错误透出对应面板，但不阻塞 map（spec scenario "Enrichment failure does not block map"）。
  const error = useOverviewDataStore((store) => store.error)
  const bootstrapError = useOverviewDataStore((store) => store.bootstrapError)
  const loadOverview = useOverviewDataStore((store) => store.loadOverview)
  // 起报时次列表是 enrichment（bootstrap 之后才到）：控制条只读它，尚未到达时回落 default_cycle。
  const cyclesBySource = useOverviewDataStore((store) => store.cyclesBySource)
  // 降水 index 同属 enrichment（bootstrap 之后才到）：键缺席 = 在途，不是错误态。
  const precipIndexByCycle = useOverviewDataStore((store) => store.precipIndexByCycle)
  const overviewMatchesQuery = overviewSnapshotMatchesQuery(overview, state)
  const overviewMetadataMatchesQuery = overviewSnapshotMetadataMatchesQuery(overview, state)
  const currentOverview = overviewMatchesQuery ? overview : null
  // validTime auto-correction can briefly make the precise data snapshot miss while the
  // source/layer/cycle metadata is still current. Keep map bootstrap data alive in that
  // window so basin boundaries and MVT overlays do not disappear.
  const metadataOverview = overviewMetadataMatchesQuery ? overview : null
  const mapOverview = currentOverview ?? metadataOverview
  const metadataLayers = overviewMetadataMatchesQuery ? (overview?.layers ?? []) : []
  const snapshotLayers = currentOverview?.layers ?? metadataLayers
  const layers = useMemo(
    () => mergeLayerStates(mapOverview?.bootstrap?.layerStates ?? [], snapshotLayers),
    [mapOverview?.bootstrap?.layerStates, snapshotLayers],
  )

  useEffect(() => {
    void loadOverview(dataLoadState).catch(() => undefined)
  }, [dataLoadState, loadOverview])

  useEffect(() => {
    // validTime 校正只需 bootstrap 落定即可（与 enrichment 解耦）。
    if (mapBootstrapLoading || !overviewMetadataMatchesQuery || metadataLayers.length === 0) return
    // 活动周期的时次列表未定（pending / error）时不校正：见 resolveM11NationalValidTimeCorrection。
    const correctedValidTime = resolveM11NationalValidTimeCorrection(state, metadataLayers)
    if (correctedValidTime === undefined) return
    onQueryChange({ validTime: correctedValidTime })
  }, [onQueryChange, mapBootstrapLoading, metadataLayers, overviewMetadataMatchesQuery, state])

  // 静态文件只保留轻量 basin domain；常态河网由 layer catalog 注册的 national MVT 分片加载。
  const nationalGeo = useNationalBasinGeo(true)

  // DB 内 basin geom 是 mesh 碎片、被客户端预算拒绝时，静态 domain 仅回填 bbox 给相机
  // 定位；流域边界和边界关联的地图名称标记不再渲染。
  const basins = useMemo(
    () => withStaticBasinBboxes(mapOverview?.basins ?? [], nationalGeo.domain),
    [mapOverview?.basins, nationalGeo.domain],
  )
  const summary = currentOverview?.summary ?? mapOverview?.summary
  const sourceSelection = summary?.sourceSelection ?? null
  // 底部控制条：全部派生自上面这组只读快照，零新增请求、零 store 写入。
  const controlBar = useMemo(
    () =>
      deriveM11ControlBarModel({
        state,
        layers,
        metadata: layers.find((layer) => layer.layerId === state.layer)?.metadata ?? null,
        cyclesBySource,
        sourceSelection,
      }),
    [cyclesBySource, layers, sourceSelection, state],
  )
  // 降水叠加：三元组与流量层**逐字同源**——具体源走 store 的 `nationalConcreteSource`，
  // 周期走 `resolveNationalOverlayCycle`（读 store 盖的 `activeNationalCycle` 章），时次走同一个
  // discharge `LayerState.currentValidTime`（`pickCurrentValidTime` 已把 URL 的 T 与活动列表调和过，
  // 与 `buildM11RegisteredOverlay` 取到的时次逐字相同）。这里不新造任何第二份解析规则。
  const dischargeLayer = useMemo(() => layers.find((entry) => entry.layerId === 'discharge') ?? null, [layers])
  const precipOverlay = useMemo(
    () =>
      resolveM11PrecipOverlay({
        precip: state.precip,
        concreteSource: nationalConcreteSource(state.source),
        cycle: dischargeLayer ? resolveNationalOverlayCycle(dischargeLayer) : null,
        validTime: dischargeLayer?.currentValidTime ?? null,
        precipIndexByCycle,
      }),
    [dischargeLayer, precipIndexByCycle, state.precip, state.source],
  )
  // 目录里有没有 `precip` 条目：**一处**推导，传给浮层开关（组件内不重复 find）。
  const precipAvailable = useMemo(() => layers.some((entry) => entry.layerId === 'precip'), [layers])
  // basin_version_id → basin_id：全国点河段开流量弹窗时反查所属流域去取该流域 latest-product。
  const basinVersionToBasinId = currentOverview?.basinVersionToBasinId ?? mapOverview?.basinVersionToBasinId ?? {}
  const visibleBasinIdList = useMemo(() => basins.map((basin) => basin.basinId), [basins])
  const visibleBasinSet = useMemo(() => new Set(visibleBasinIdList), [visibleBasinIdList])
  const basinIdToActiveVersionId = useMemo(() => {
    const result: Record<string, string> = {}
    for (const [basinVersionId, basinId] of Object.entries(basinVersionToBasinId)) {
      if (!result[basinId]) result[basinId] = basinVersionId
    }
    return result
  }, [basinVersionToBasinId])
  const stationLayerBasinContexts = useMemo(
    () =>
      basins.map((basin) => {
        const queryBasinVersionId =
          state.basinVersionId && basinVersionToBasinId[state.basinVersionId] === basin.basinId ? state.basinVersionId : null
        return {
          basinId: basin.basinId,
          basinVersionId: basin.selectedBasinVersionId ?? queryBasinVersionId ?? basinIdToActiveVersionId[basin.basinId] ?? null,
          source:
            sourceSelection?.resolvedSource === 'GFS' || sourceSelection?.resolvedSource === 'IFS'
              ? sourceSelection.resolvedSource
              : null,
          cycle: state.cycle,
        }
      }),
    [basinIdToActiveVersionId, basinVersionToBasinId, basins, sourceSelection?.resolvedSource, state.basinVersionId, state.cycle],
  )
  // 全国总览不做相机 fit：这是全国系统，保持中国全景（CHINA_VIEW_STATE）；
  // fit 到流域并集会把视野错误地收窄到测试流域（qhh/heihe）区域。

  // 全国点河段的就地流量弹窗（segment 身份 + 经纬度锚点 + 反查到的 basinId）。
  const [riverPopup, setRiverPopup] = useState<{ segment: M11RiverPopupSegment; lngLat: [number, number]; basinId: string | null } | null>(null)
  const [stationPopup, setStationPopup] = useState<{ station: M11StationPopupStation; basinId: string | null } | null>(null)
  const [activeCurveWindow, setActiveCurveWindow] = useState<ActiveCurveWindow>('river')
  // 点流域 → 相机飞到其 bbox（留在全国总览、不钻取/不锁定）。
  const [basinFit, setBasinFit] = useState<M11MapCameraFit | null>(null)
  // 切图层时清掉残留弹窗（弹窗只属于当前水文图层）。
  useEffect(() => {
    setRiverPopup(null)
    setStationPopup(null)
  }, [state.layer])
  useEffect(() => {
    if (!state.metStations) setStationPopup(null)
  }, [state.metStations])

  const handleMapOverlayClick = useCallback(
    (interaction: M11MapOverlayInteraction) => {
      if (interaction.layerId === 'met-stations') {
        const stationId = mapFeatureStringProperty(interaction.feature, 'station_id')
        if (!stationId) return
        setActiveCurveWindow('station')
        setStationPopup({
          station: { station_id: stationId, station_name: mapFeatureStringProperty(interaction.feature, 'station_name') },
          basinId: mapFeatureStringProperty(interaction.feature, 'basin_id') ?? stationLayerBasinContexts[0]?.basinId ?? null,
        })
        return
      }
      // 点 discharge 河段 → 就地开流量预报弹窗（national：feature 自带 segment 身份）。
      if (interaction.layerId === state.layer && state.layer === 'discharge') {
        const segmentId =
          mapFeatureStringProperty(interaction.feature, 'river_segment_id') ?? mapFeatureStringProperty(interaction.feature, 'segment_id')
        const basinVersionId = mapFeatureStringProperty(interaction.feature, 'basin_version_id')
        const riverNetworkVersionId = mapFeatureStringProperty(interaction.feature, 'river_network_version_id')
        const lngLat = popupAnchorFromInteraction(interaction)
        if (!segmentId || !basinVersionId || !riverNetworkVersionId || !lngLat) return
        setActiveCurveWindow('river')
        setRiverPopup({
          segment: {
            river_segment_id: segmentId,
            segment_id: mapFeatureStringProperty(interaction.feature, 'segment_id') ?? segmentId,
            river_network_version_id: riverNetworkVersionId,
            basin_version_id: basinVersionId,
            name: mapFeatureStringProperty(interaction.feature, 'segment_name'),
          },
          lngLat,
          // national 瓦片自带 basin_id（feature 自描述）→ 直接取，不依赖 N+1 versions 映射；
          // 单 run 瓦片无此属性时回退 version→basin 映射，保证两条路都能取到曲线。
          basinId: mapFeatureStringProperty(interaction.feature, 'basin_id') ?? basinVersionToBasinId[basinVersionId] ?? null,
        })
        return
      }
      // 点流域边界 → 相机飞到该流域（留在全国总览，不钻取/不锁定；缩放后直接点河段、
      // 随时点别的流域切换，无需「返回总览」）。
      const feature = interaction.feature ?? interaction.event.features?.find((item) => item.layer?.id === 'm11-basin-fill')
      const basinId = feature?.properties?.basin_id
      if (typeof basinId !== 'string' || !visibleBasinSet.has(basinId)) return
      const basin = basins.find((item) => item.basinId === basinId)
      if (!basin) return
      setBasinFit(bboxToMapFit(basin.bbox))
    },
    [basins, basinVersionToBasinId, state, stationLayerBasinContexts, visibleBasinSet],
  )
  const handleMapOverlayHover = useCallback(
    (interaction: M11MapOverlayInteraction | null) => {
      if (!interaction || interaction.layerId !== state.layer || state.layer !== 'discharge') return
      const basinVersionId = mapFeatureStringProperty(interaction.feature, 'basin_version_id')
      const basinId = mapFeatureStringProperty(interaction.feature, 'basin_id') ?? (basinVersionId ? basinVersionToBasinId[basinVersionId] : null)
      if (!basinId) return
      void prefetchHydroMetLatestProducts({ basinId, cycle: state.cycle })
    },
    [basinVersionToBasinId, state.cycle, state.layer],
  )

  const riverForecastPanel = riverPopup ? (
    <M11RiverForecastPanel
      basinId={riverPopup.basinId}
      segment={riverPopup.segment}
      active={(activeCurveWindow === 'river' && Boolean(riverPopup)) || !stationPopup}
      onActivate={() => setActiveCurveWindow('river')}
      onClose={() => setRiverPopup(null)}
    />
  ) : null
  const stationForecastPanel = stationPopup ? (
    <M11StationForcingPopup
      basinId={stationPopup.basinId}
      initialSource={stationSeriesSourceAvailability(sourceSelection?.resolvedSource)}
      station={stationPopup.station}
      active={(activeCurveWindow === 'station' && Boolean(stationPopup)) || !riverPopup}
      onActivate={() => setActiveCurveWindow('station')}
      onClose={() => setStationPopup(null)}
    />
  ) : null

  // 全国总览开代站图层：按当前总览中的所有可见流域版本取代站点位；
  // 点位展示不依赖 latest-product ready，避免某个流域 forcing 曲线未就绪时站点也消失。
  const stationLayer = useMetStationLayer({
    active: state.metStations,
    basinContexts: stationLayerBasinContexts,
  })

  // 「mapBootstrap 尚未首次落定」单一信号：阶段 1 完成且 overview.bootstrap 已写入即解锁。
  // 与 enrichment 解耦（spec scenario "Map bootstrap completes before enrichment"）。
  //   surfaceSettling = mapBootstrapLoading || (!overview?.bootstrap && !bootstrapError)
  // bootstrap reject 时 mapBootstrapLoading=false / bootstrapError !=null / overview.bootstrap=null →
  //   surfaceSettling=false → emptyBasinReason 走 bootstrapError 分支诚实告知失败（spec scenario
  //   "Map bootstrap rejection"：renders bootstrap failed state rather than indefinite spinner）。
  const surfaceSettling = mapBootstrapLoading || (!overview?.bootstrap && !bootstrapError)
  // 有已发布 run 的流域（latestForecastTime != null ⟺ 河段进了流量 MVT）的静态河流须剔除，规避双线；
  // 无 run 的流域（如 heihe）不在 MVT 中，保留其静态河流。
  const meshRiverBasinIds = useMemo(
    () => basins.filter((basin) => basin.latestForecastTime != null).map((basin) => basin.basinId),
    [basins],
  )
  const emptyBasinReason =
    !surfaceSettling && basins.length === 0
      ? bootstrapError ??
        error ??
        (summary?.totalBasins === 0
          ? '暂无可用流域数据'
          : mapOverview?.aggregationDecision.needsAggregationEndpoint
            ? mapOverview.aggregationDecision.evidence
            : '流域清单暂不可用')
      : null

  return (
    <M11FullscreenMap
      state={state}
      layers={layers}
      basins={basins}
      visibleBasinIds={visibleBasinIdList}
      nationalRiverGeo={nationalGeo.river}
      meshRiverBasinIds={meshRiverBasinIds}
      selectedSegmentId={riverPopup?.segment.river_segment_id ?? null}
      selectedStationId={stationPopup?.station.station_id ?? null}
      stationFeatureCollection={stationLayer.featureCollection}
      precipOverlay={precipOverlay}
      precipAvailable={precipAvailable}
      loading={surfaceSettling}
      boundaryLoading={nationalGeo.loading}
      fitTo={basinFit}
      mapLabel="全国总览地图"
      controlBar={controlBar}
      onQueryChange={onQueryChange}
      onOverlayHover={handleMapOverlayHover}
      onOverlayClick={handleMapOverlayClick}
    >
      {riverForecastPanel}
      {stationForecastPanel}
      {state.metStations && stationLayer.statusNote ? (
        // 代站图层的 honest 状态优先（全国总览未选流域时诚实提示「请选择流域」）。
        <M11FloatingNotice testId="m11-met-station-status">{stationLayer.statusNote}</M11FloatingNotice>
      ) : surfaceSettling ? (
        <M11FloatingNotice testId="m11-overview-loading">总览数据加载中</M11FloatingNotice>
      ) : precipOverlay.notice ? (
        // 链位钉死在 `surfaceSettling` 之后、`emptyBasinReason` 之前（fixture 决策 9）：
        // 加载中不让降水提示盖过「加载中」；加载完成后降水提示优先于空流域提示。
        // 所有 M11FloatingNotice 同坐标绝对定位，并列挂两条会像素重叠，故必须留在这条互斥链上。
        <M11FloatingNotice testId="m11-precip-notice">{precipOverlay.notice}</M11FloatingNotice>
      ) : emptyBasinReason ? (
        <M11FloatingNotice testId="m11-overview-empty">{emptyBasinReason}</M11FloatingNotice>
      ) : null}
    </M11FullscreenMap>
  )
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
    ...state,
    source: sourceContext.source,
    cycle: sourceContext.cycle,
    validTime: sourceContext.validTime,
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

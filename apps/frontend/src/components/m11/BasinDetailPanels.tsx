import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import type { FeatureCollection } from 'geojson'

import type { M11MapOverlayInteraction, M11MapPopupSlot } from '@/components/map/M11MapLibreSurface'
import { M11RiverForecastPopup, type M11RiverPopupSegment } from '@/components/map/M11RiverForecastPopup'
import { M11StationForcingPopup, type M11StationPopupStation } from '@/components/map/M11StationForcingPopup'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import {
  type BasinDetail,
  type M11Bbox,
  type OverviewBasin,
} from '@/lib/m11/overviewDataContracts'
import {
  defaultM11QueryState,
  type M11QueryPatch,
  type M11QueryState,
} from '@/lib/m11/queryState'
import { staticBasinBoundaryIndex, withStaticBasinBoundaries } from '@/lib/m11/staticBasinFallback'
import { resolveM11ValidTimeCorrection } from '@/pages/m11/M11Controls'
import { useNationalBasinGeo } from '@/pages/m11/useNationalBasinGeo'
import { useMetStationLayer } from '@/pages/m11/useStationLayer'
import { basinSnapshotMatchesQuery, basinSnapshotMetadataMatchesQuery, useOverviewDataStore } from '@/stores/overviewData'

const BASIN_NOT_FOUND_REASON = 'Basin was not found.'
const BASIN_FALLBACK_EXTENT: M11Bbox = { minLon: 73, minLat: 18, maxLon: 135, maxLat: 54 }

function concreteSource(resolvedSource: string | null | undefined): HydroMetSource | null {
  if (resolvedSource === 'GFS' || resolvedSource === 'IFS') return resolvedSource
  return null
}

/**
 * 流域详情就地化（M26 全屏单页）：取数 + 地图点选 → 弹窗的接线抽到此 hook。
 * 不再返回左右侧栏 ReactNode；只返回全屏地图所需 props + 弹窗 slot + honest 状态。
 * 河段/代站详情全部走玻璃质感弹窗（弹窗内自选 source/起报/变量），无侧栏段表/筛选。
 */
export function useBasinDetailMode({
  basinId,
  state,
  onQueryChange,
}: {
  basinId: string
  state: M11QueryState
  onQueryChange: (patch: M11QueryPatch) => void
}) {
  const dataLoadState = useMemo(
    () => ({
      source: state.source,
      cycle: state.cycle,
      validTime: state.validTime,
      layer: state.layer,
      basemap: defaultM11QueryState.basemap,
      basinVersionId: state.basinVersionId,
      riverNetworkVersionId: state.riverNetworkVersionId,
      basinId: state.basinId,
      segmentId: state.segmentId,
      warningLevel: null,
      q: null,
    }),
    [state.basinId, state.basinVersionId, state.cycle, state.layer, state.riverNetworkVersionId, state.segmentId, state.source, state.validTime],
  )
  const basinData = useOverviewDataStore((store) => store.basinDetail)
  const loading = useOverviewDataStore((store) => store.basinLoading)
  const error = useOverviewDataStore((store) => store.basinError)
  const loadBasinDetail = useOverviewDataStore((store) => store.loadBasinDetail)
  const basinMatchesQuery = basinSnapshotMatchesQuery(basinData, basinId, state)
  const basinMetadataMatchesQuery = basinSnapshotMetadataMatchesQuery(basinData, basinId, state)
  const currentBasinData = basinMatchesQuery ? basinData : null
  const metadataLayers = basinMetadataMatchesQuery ? (basinData?.layers ?? []) : []
  const layers = currentBasinData?.layers ?? metadataLayers
  const sourceSelection = currentBasinData?.selectedSegment?.sourceSelection ?? currentBasinData?.detail.sourceSelection ?? null
  const derivedTimeline = useMemo(() => {
    const points = currentBasinData?.selectedSegment?.trendPoints ?? []
    return points.length > 0
      ? { validTimes: points.map((point) => point.validTime), label: 'selected segment forecast payload' }
      : null
  }, [currentBasinData?.selectedSegment?.trendPoints])

  useEffect(() => {
    void loadBasinDetail(basinId, dataLoadState).catch(() => undefined)
  }, [basinId, dataLoadState, loadBasinDetail])

  useEffect(() => {
    if (loading || !basinMetadataMatchesQuery) return
    const correctedValidTime = resolveM11ValidTimeCorrection(state, metadataLayers, derivedTimeline)
    if (correctedValidTime === undefined) return
    onQueryChange({ validTime: correctedValidTime })
  }, [basinMetadataMatchesQuery, derivedTimeline, onQueryChange, loading, metadataLayers, state])

  useEffect(() => {
    if (loading || !currentBasinData?.selectedSegment?.riverNetworkVersionId) return
    const resolvedRiverNetworkVersionId = currentBasinData.selectedSegment.riverNetworkVersionId
    if (state.riverNetworkVersionId === resolvedRiverNetworkVersionId) return
    onQueryChange({ riverNetworkVersionId: resolvedRiverNetworkVersionId })
  }, [currentBasinData?.selectedSegment?.riverNetworkVersionId, onQueryChange, loading, state.riverNetworkVersionId])

  const detail = currentBasinData?.detail
  const basinNotFoundReason = !loading && detail?.unavailableReason === BASIN_NOT_FOUND_REASON ? detail.unavailableReason : null
  const basinDisplayName = detail?.displayName || basinId
  const selectedSegment = currentBasinData?.selectedSegment
  const selectedSegmentId = selectedSegment?.riverSegmentId ?? null
  // 服务端 bbox/boundary 缺失（mesh 碎片被预算拒绝）时回落静态 domain 轮廓，恢复相机 fit + 边界。
  const nationalGeo = useNationalBasinGeo(true)
  const staticFallbackBbox = useMemo(
    () => staticBasinBoundaryIndex(nationalGeo.domain).get(basinId)?.bbox ?? null,
    [basinId, nationalGeo.domain],
  )
  // 本流域静态河网（shp 真实河道）：详情页秒显垫底；可点击 mesh 河段层加载后，该流域静态河流整段从底图
  // 剔除（见下方 meshRiverBasinIds），规避平滑静态线 + 阶梯 mesh 线双线叠画，而非降透明并存。
  const basinRiverGeo = useMemo(() => {
    const features = nationalGeo.river?.features.filter((feature) => feature.properties?.basin_id === basinId) ?? []
    return features.length > 0 ? ({ type: 'FeatureCollection', features } as FeatureCollection) : null
  }, [basinId, nationalGeo.river])
  // 稳定引用：basinId 不变则同一数组，避免每次渲染都让 surface 的 renderedNationalRiver memo 失效重算。
  const meshRiverBasinIds = useMemo(() => (basinId ? [basinId] : []), [basinId])
  const mapFitTo = useMemo(
    () => bboxToMapFit(detail?.bbox ?? staticFallbackBbox ?? (detail && !basinNotFoundReason ? BASIN_FALLBACK_EXTENT : null)),
    [basinNotFoundReason, detail, staticFallbackBbox],
  )
  const basinMapContext = useMemo(
    () =>
      detail && !basinNotFoundReason
        ? withStaticBasinBoundaries([basinDetailToOverviewBasin(detail)], nationalGeo.domain)
        : [],
    [basinNotFoundReason, detail, nationalGeo.domain],
  )

  const resolvedSource = concreteSource(sourceSelection?.resolvedSource)

  // 两类 popup 互斥状态：river 与 station 各持选中要素 + 经纬度锚点。
  const [riverPopup, setRiverPopup] = useState<{ segment: M11RiverPopupSegment; lngLat: [number, number] } | null>(null)
  const [stationPopup, setStationPopup] = useState<{ station: M11StationPopupStation; lngLat: [number, number] } | null>(null)

  const handleMapOverlayHover = useCallback((_interaction: M11MapOverlayInteraction | null) => undefined, [])
  const handleMapOverlayClick = useCallback(
    (interaction: M11MapOverlayInteraction) => {
      if (interaction.layerId === 'met-stations') {
        const stationId = mapFeatureStringProperty(interaction.feature, 'station_id')
        if (!stationId) return
        const lngLat = popupAnchorFromInteraction(interaction)
        if (!lngLat) return
        setRiverPopup(null)
        setStationPopup({
          station: { station_id: stationId, station_name: mapFeatureStringProperty(interaction.feature, 'station_name') },
          lngLat,
        })
        return
      }
      if (interaction.layerId !== 'basin-river-segments') return
      const nextSegmentId =
        mapFeatureStringProperty(interaction.feature, 'river_segment_id') ?? mapFeatureStringProperty(interaction.feature, 'segment_id')
      if (!nextSegmentId) return
      const nextRiverNetworkVersionId = mapFeatureStringProperty(interaction.feature, 'river_network_version_id')
      const nextBasinVersionId = mapFeatureStringProperty(interaction.feature, 'basin_version_id')
      const lngLat = popupAnchorFromInteraction(interaction)
      if (lngLat) {
        setStationPopup(null)
        setRiverPopup({
          segment: {
            river_segment_id: nextSegmentId,
            segment_id: mapFeatureStringProperty(interaction.feature, 'segment_id') ?? nextSegmentId,
            river_network_version_id: nextRiverNetworkVersionId ?? state.riverNetworkVersionId ?? '',
            basin_version_id: nextBasinVersionId ?? state.basinVersionId ?? '',
            name: mapFeatureStringProperty(interaction.feature, 'segment_name'),
          },
          lngLat,
        })
      }
      if (
        nextSegmentId === state.segmentId &&
        (nextRiverNetworkVersionId ?? state.riverNetworkVersionId) === state.riverNetworkVersionId &&
        (nextBasinVersionId ?? state.basinVersionId) === state.basinVersionId
      ) {
        return
      }
      onQueryChange({
        basinVersionId: nextBasinVersionId ?? state.basinVersionId,
        riverNetworkVersionId: nextRiverNetworkVersionId ?? state.riverNetworkVersionId,
        segmentId: nextSegmentId,
      })
    },
    [onQueryChange, state.basinVersionId, state.riverNetworkVersionId, state.segmentId],
  )

  const backToOverview = useCallback(() => onQueryChange({ basinId: null, segmentId: null }), [onQueryChange])

  const stationLayer = useMetStationLayer({
    active: state.layer === 'met-stations',
    basinId,
    resolvedSource: sourceSelection?.resolvedSource ?? null,
    cycle: state.cycle,
  })

  // 切流域时清掉残留 popup。
  useEffect(() => {
    setRiverPopup(null)
    setStationPopup(null)
  }, [basinId])

  // 已解析具体源在 GFS↔IFS 间真正切换时清 popup；transient null 不清。
  const lastConcretePopupSourceRef = useRef<string | null>(null)
  useEffect(() => {
    if (resolvedSource === null) return
    if (lastConcretePopupSourceRef.current && lastConcretePopupSourceRef.current !== resolvedSource) {
      setRiverPopup(null)
      setStationPopup(null)
    }
    lastConcretePopupSourceRef.current = resolvedSource
  }, [resolvedSource])

  const popup: M11MapPopupSlot | null = riverPopup
    ? {
        longitude: riverPopup.lngLat[0],
        latitude: riverPopup.lngLat[1],
        onClose: () => setRiverPopup(null),
        content: (
          <M11RiverForecastPopup
            basinId={basinId}
            initialSource={resolvedSource}
            segment={riverPopup.segment}
            onClose={() => setRiverPopup(null)}
          />
        ),
      }
    : stationPopup
      ? {
          longitude: stationPopup.lngLat[0],
          latitude: stationPopup.lngLat[1],
          onClose: () => setStationPopup(null),
          content: (
            <M11StationForcingPopup
              basinId={basinId}
              initialSource={resolvedSource}
              station={stationPopup.station}
              onClose={() => setStationPopup(null)}
            />
          ),
        }
      : null

  return {
    mapLabel: '流域钻取地图',
    mapTitle: `${basinDisplayName} 流域钻取`,
    mapMeta:
      detail?.bbox || staticFallbackBbox
        ? '地图已按流域 bbox 定位；点击河段查看 q_down 流量预报，点击代站查看六要素 forcing。'
        : '当前流域缺少 bbox，地图使用中国范围兜底视域；点击河段/代站查看详情弹窗。',
    layers,
    sourceSelection,
    fitTo: mapFitTo,
    basins: basinMapContext,
    visibleBasinIds: [basinId],
    basinSegments: currentBasinData?.segments ?? [],
    nationalRiverGeo: basinRiverGeo,
    meshRiverBasinIds,
    selectedSegmentId,
    selectedSegmentGeometry: selectedSegment?.geometry ?? null,
    stationFeatureCollection: stationLayer.featureCollection,
    popup,
    onMapOverlayHover: handleMapOverlayHover,
    onMapOverlayClick: handleMapOverlayClick,
    backToOverview,
    basinNotFoundReason,
    basinDisplayName,
    loading,
    // 「流域数据尚未首次落定」单一信号：用 raw basinData（首个流域加载后恒非 null），仅深链/刷新直达
    // 某流域 URL 的 frame-1 为真，驱动 surface 占位、避免闪 m11-map-unavailable；不用 currentBasinData
    // ——那会在流域间切换的 settle 窗口误抑制诚实状态。
    surfaceSettling: loading || !basinData,
    boundaryLoading: nationalGeo.loading,
    error,
    stationStatusNote: stationLayer.statusNote,
  }
}

function bboxToMapFit(bbox: M11Bbox | null | undefined) {
  if (!bbox) return null
  return {
    bounds: [
      [bbox.minLon, bbox.minLat],
      [bbox.maxLon, bbox.maxLat],
    ] as [[number, number], [number, number]],
    padding: 36,
  }
}

function basinDetailToOverviewBasin(detail: BasinDetail): OverviewBasin {
  return {
    basinId: detail.basinId,
    displayName: detail.displayName,
    basinGroup: detail.basinGroup,
    parentBasinId: null,
    level: 0,
    boundary: detail.boundary,
    bbox: detail.bbox,
    areaKm2: null,
    riverCount: detail.segmentCount,
    activeModelCount: detail.activeModelCount,
    latestForecastTime: detail.latestRun.validTime,
    warningCounts: detail.warningDistribution,
    basinVersions: detail.basinVersions,
    selectedBasinVersionId: detail.selectedBasinVersionId,
    unavailableReason: detail.unavailableReason,
    qualityNote: detail.partialErrors[0] ?? null,
  }
}

export function mapFeatureStringProperty(feature: M11MapOverlayInteraction['feature'], key: string) {
  const value = feature?.properties?.[key]
  return typeof value === 'string' && value.length > 0 ? value : null
}

export function popupAnchorFromInteraction(interaction: M11MapOverlayInteraction): [number, number] | null {
  const geometry = interaction.feature?.geometry
  if (geometry && geometry.type === 'Point' && Array.isArray(geometry.coordinates)) {
    const [lon, lat] = geometry.coordinates as number[]
    if (Number.isFinite(lon) && Number.isFinite(lat)) return [lon, lat]
  }
  const lngLat = (interaction.event as { lngLat?: { lng?: number; lat?: number } }).lngLat
  if (lngLat && Number.isFinite(lngLat.lng) && Number.isFinite(lngLat.lat)) {
    return [lngLat.lng as number, lngLat.lat as number]
  }
  return null
}

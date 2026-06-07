import { forwardRef, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Activity, GitBranch, ListFilter, Search, Split, TrendingUp } from 'lucide-react'

import type { M11MapOverlayInteraction, M11MapPopupSlot } from '@/components/map/M11MapLibreSurface'
import { M11RiverForecastPopup, type M11RiverPopupSegment } from '@/components/map/M11RiverForecastPopup'
import { M11StationForcingPopup, type M11StationPopupStation } from '@/components/map/M11StationForcingPopup'
import { useHydroMetProduct } from '@/pages/m11/useHydroMetProduct'
import {
  filterBasinSegmentRows,
  type BasinDetail,
  type BasinSegmentRow,
  type M11Bbox,
  type M11WarningLevel,
  type OverviewBasin,
  type SelectedSegmentDetail,
  type TrendPoint,
} from '@/lib/m11/overviewDataContracts'
import { StateReadout } from '@/pages/m11/M11Shell'
import {
  defaultM11QueryState,
  m11QueryHref,
  type M11QueryPatch,
  type M11QueryState,
  type M11QueryWarningLevel,
} from '@/lib/m11/queryState'
import { cn } from '@/lib/cn'
import { LayerGroupControls, LayerLegendPanel, SourceScenarioControls, resolveM11ValidTimeCorrection } from '@/pages/m11/M11Controls'
import { useMetStationLayer } from '@/pages/m11/useStationLayer'
import { basinSnapshotMatchesQuery, basinSnapshotMetadataMatchesQuery, useOverviewDataStore } from '@/stores/overviewData'

const BASIN_NOT_FOUND_REASON = 'Basin was not found.'
const BASIN_FALLBACK_EXTENT: M11Bbox = { minLon: 73, minLat: 18, maxLon: 135, maxLat: 54 }
const NO_SEGMENT_EMPTY_TEXT = '该流域暂无已发布的预报数据'
const warningFilterOptions: Array<{ value: M11QueryWarningLevel | ''; label: string }> = [
  { value: '', label: '全部预警' },
  { value: 'normal', label: '正常' },
  { value: 'elevated', label: '偏高' },
  { value: 'watch', label: '关注' },
  { value: 'warning', label: '警戒' },
  { value: 'major', label: '高风险' },
  { value: 'severe', label: '严重' },
  { value: 'extreme', label: '极端' },
  { value: 'orange', label: '橙色' },
  { value: 'red', label: '红色' },
]

/**
 * 流域详情就地化（M26-2）：把原 BasinDetailPage 的取数 + 详情面板抽到此 hook，
 * 由单页（OverviewPage 详情模式）按 query 内 basinId 调用，返回组装好的 M11Layout props。
 * basinId 入参来自 query（非路由 param），其余取数/honest 契约与原 BasinDetailPage 一致。
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
      ? {
          validTimes: points.map((point) => point.validTime),
          label: 'selected segment forecast payload',
        }
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
  const filteredSegments = useMemo(
    () => filterBasinSegmentRows(currentBasinData?.segments ?? [], state),
    [currentBasinData?.segments, state],
  )
  const basinNotFoundReason = !loading && detail?.unavailableReason === BASIN_NOT_FOUND_REASON ? detail.unavailableReason : null
  const basinDisplayName = detail?.displayName || basinId
  const selectedSegment = currentBasinData?.selectedSegment
  const invalidSegmentRequested = Boolean(state.segmentId && currentBasinData && !loading && !basinNotFoundReason && !selectedSegment)
  const selectedSegmentId = selectedSegment?.riverSegmentId ?? null
  const mapFitTo = useMemo(
    () => bboxToMapFit(detail?.bbox ?? (detail && !basinNotFoundReason ? BASIN_FALLBACK_EXTENT : null)),
    [basinNotFoundReason, detail],
  )
  const basinMapContext = useMemo(() => (detail && !basinNotFoundReason ? [basinDetailToOverviewBasin(detail)] : []), [
    basinNotFoundReason,
    detail,
  ])
  // 两类 popup（M26-4）互斥状态：river 与 station 各持选中要素 + 经纬度锚点，开一个关另一个。
  const [riverPopup, setRiverPopup] = useState<{ segment: M11RiverPopupSegment; lngLat: [number, number] } | null>(null)
  const [stationPopup, setStationPopup] = useState<{ station: M11StationPopupStation; lngLat: [number, number] } | null>(null)
  const handleMapOverlayHover = useCallback((_interaction: M11MapOverlayInteraction | null) => undefined, [])
  const handleMapOverlayClick = useCallback(
    (interaction: M11MapOverlayInteraction) => {
      if (interaction.layerId === 'met-stations') {
        // 点代站：开代站 popup（关河段 popup）；锚点用 station 坐标。
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
      // 点河段：开河段 popup（关代站 popup）；锚点用点击 event lngLat。
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

  // 代站图层（M26-3）：详情模式以该 basin detail 的 resolvedSource 取数；best 未解析时 honest 空态，不取数。
  const stationLayer = useMetStationLayer({
    active: state.layer === 'met-stations',
    basinId,
    resolvedSource: sourceSelection?.resolvedSource ?? null,
    cycle: state.cycle,
  })

  // 共享 product 解析（M26-4）：两类 popup 共用同一份选中流域 + 已解析具体源的 latest-product；
  // best/compare 未解析时 product=null + honest 原因，杜绝拿未解析源直接打曲线接口。
  const popupProduct = useHydroMetProduct({
    basinId,
    resolvedSource: sourceSelection?.resolvedSource ?? null,
    cycle: state.cycle,
  })

  // 切流域时清掉残留 popup（避免跨流域误展示）。
  useEffect(() => {
    setRiverPopup(null)
    setStationPopup(null)
  }, [basinId])

  // 已解析具体源在 GFS↔IFS 间真正切换时清 popup；transient null（数据重载中）不清，
  // 否则一次选段导致 snapshot 暂不匹配会误关刚开的 popup。
  const concretePopupSource =
    sourceSelection?.resolvedSource === 'GFS' || sourceSelection?.resolvedSource === 'IFS'
      ? sourceSelection.resolvedSource
      : null
  const lastConcretePopupSourceRef = useRef<string | null>(null)
  useEffect(() => {
    if (concretePopupSource === null) return
    if (lastConcretePopupSourceRef.current && lastConcretePopupSourceRef.current !== concretePopupSource) {
      setRiverPopup(null)
      setStationPopup(null)
    }
    lastConcretePopupSourceRef.current = concretePopupSource
  }, [concretePopupSource])

  const popup: M11MapPopupSlot | null = riverPopup
    ? {
        longitude: riverPopup.lngLat[0],
        latitude: riverPopup.lngLat[1],
        onClose: () => setRiverPopup(null),
        content: (
          <M11RiverForecastPopup
            product={popupProduct.product}
            segment={riverPopup.segment}
            productReason={popupProduct.reason}
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
              product={popupProduct.product}
              station={stationPopup.station}
              productReason={popupProduct.reason}
              onClose={() => setStationPopup(null)}
            />
          ),
        }
      : null

  return {
    title: '流域分析',
    subtitle: `当前流域 ${basinDisplayName}`,
    mapLabel: '流域钻取地图',
    mapTitle: `${basinDisplayName} 流域钻取`,
    mapMeta: detail?.bbox
      ? '地图已按流域 bbox 定位，河段选择会同步到地图高亮状态。'
      : '当前流域缺少 bbox，地图使用中国范围兜底视域，河段列表不受影响。',
    layers,
    sourceSelection,
    derivedTimeline,
    fitTo: mapFitTo,
    basins: basinMapContext,
    visibleBasinIds: [basinId],
    basinSegments: currentBasinData?.segments ?? [],
    selectedSegmentId,
    selectedSegmentGeometry: selectedSegment?.geometry ?? null,
    stationFeatureCollection: stationLayer.featureCollection,
    popup,
    onMapOverlayHover: handleMapOverlayHover,
    onMapOverlayClick: handleMapOverlayClick,
    left: (
      <>
        <SourceScenarioControls state={state} sourceSelection={sourceSelection} onQueryChange={onQueryChange} />
        <StateReadout state={state} basinId={basinId} />
        {detail && !basinNotFoundReason ? <BasinIdentityCard detail={detail} /> : null}
        {detail && !basinNotFoundReason && !detail.bbox ? <MissingBboxNotice /> : null}
        {detail && !basinNotFoundReason ? <MapContextNotice detail={detail} /> : null}
        <SegmentDiscoveryPanel
          basinName={basinDisplayName}
          basinVersionId={detail?.selectedBasinVersionId ?? state.basinVersionId}
          rows={filteredSegments}
          selectedSegmentId={selectedSegmentId}
          query={state.q}
          warningLevel={state.warningLevel}
          segmentCount={detail?.segmentCount ?? null}
          invalidSegmentId={invalidSegmentRequested ? state.segmentId : null}
          disabled={Boolean(basinNotFoundReason)}
          onQueryChange={onQueryChange}
        />
        <LayerGroupControls state={state} layers={layers} onQueryChange={onQueryChange} />
      </>
    ),
    right: (
      <>
        {basinNotFoundReason ? (
          <BasinUnavailableNotice basinId={basinId} reason={basinNotFoundReason} onBackToOverview={backToOverview} />
        ) : (
          <>
            <SelectedSegmentPanel
              segment={selectedSegment}
              requestedSegmentId={state.segmentId}
              invalidSegmentRequested={invalidSegmentRequested}
              routeState={state}
            />
            <SelectedSegmentTrendPanel segment={selectedSegment} />
            <div className="rounded-md border border-neutral-300 p-3">
              <div className="text-base font-semibold text-neutral-900">预警状态</div>
              <p className="mt-2 font-mono text-sm text-neutral-700">{state.warningLevel ?? 'all'}</p>
            </div>
            {detail ? <BasinRunMetadata detail={detail} /> : null}
          </>
        )}
        <LayerLegendPanel state={state} layers={layers} />
        {stationLayer.statusNote ? (
          <div
            className="rounded-md border border-warning/40 bg-primary-50 p-3 text-xs text-neutral-700"
            role="status"
            data-testid="m11-met-station-status"
          >
            {stationLayer.statusNote}
          </div>
        ) : null}
        {loading || error ? (
          <div className="rounded-md border border-neutral-300 bg-neutral-50 p-3 text-xs text-neutral-700">
            {loading ? '流域数据加载中' : error}
          </div>
        ) : null}
        {!basinNotFoundReason ? (
          <button
            type="button"
            className="block w-full rounded border border-primary-600 px-3 py-2 text-left text-sm font-medium text-primary-600 hover:bg-primary-50"
            onClick={backToOverview}
          >
            返回全国总览
          </button>
        ) : null}
      </>
    ),
  }
}

function BasinUnavailableNotice({
  basinId,
  reason,
  onBackToOverview,
}: {
  basinId: string
  reason: string
  onBackToOverview: () => void
}) {
  const title = reason === 'Basin was not found.' ? '未找到流域' : '流域暂不可用'

  return (
    <section className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950" aria-label="流域不可用">
      <div className="text-base font-semibold">{title}</div>
      <p className="mt-2">{reason}</p>
      <p className="mt-1 text-xs">
        请求的流域 ID：<code className="font-mono">{basinId}</code>
      </p>
      <button
        type="button"
        className="mt-3 block w-full rounded border border-primary-600 bg-white px-3 py-2 text-left text-sm font-medium text-primary-600"
        onClick={onBackToOverview}
      >
        返回全国总览
      </button>
    </section>
  )
}

function BasinIdentityCard({ detail }: { detail: BasinDetail }) {
  return (
    <section className="rounded-md border border-neutral-300 bg-neutral-50 p-3 text-xs text-neutral-700" aria-label="流域版本摘要">
      <div className="text-sm font-semibold text-neutral-900">{detail.displayName}</div>
      <dl className="mt-2 grid grid-cols-[7rem_minmax(0,1fr)] gap-x-3 gap-y-1">
        <dt>basin_version_id</dt>
        <dd className="min-w-0 truncate font-mono text-neutral-900">{detail.selectedBasinVersionId ?? '-'}</dd>
        <dt>河段</dt>
        <dd>{detail.segmentCount ?? '-'}</dd>
        <dt>活跃模型</dt>
        <dd>{detail.activeModelCount}</dd>
        <dt>latest run</dt>
        <dd className="min-w-0 truncate font-mono text-neutral-900">{detail.latestRun.runId ?? '-'}</dd>
      </dl>
    </section>
  )
}

function MissingBboxNotice() {
  return (
    <section
      className="rounded-md border border-amber-300 bg-amber-50 p-3 text-xs leading-5 text-amber-950"
      role="status"
      aria-label="缺少流域 bbox"
    >
      当前流域版本缺少 bbox，地图使用中国范围兜底视域（73,18,135,54），河段列表和预报数据继续显示。
    </section>
  )
}

function MapContextNotice({ detail }: { detail: BasinDetail }) {
  const missingBoundary = !detail.boundary
  return (
    <section
      className="rounded-md border border-neutral-300 bg-neutral-50 p-3 text-xs leading-5 text-neutral-700"
      role="status"
      aria-label="地图上下文状态"
    >
      {missingBoundary ? '当前流域版本缺少边界几何，地图仅按 bbox 定位，不伪造边界。' : '地图已加载当前流域边界上下文。'}
      <br />
      城市与站点标签暂不可用：M11 当前没有城市/站点标签合同或数据源。
    </section>
  )
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

function BasinRunMetadata({ detail }: { detail: BasinDetail }) {
  const warningEntries = Object.entries(detail.warningDistribution).filter(([, count]) => count > 0)
  return (
    <section className="rounded-md border border-neutral-300 p-3" aria-label="流域运行元数据">
      <div className="text-base font-semibold text-neutral-900">流域运行元数据</div>
      <dl className="mt-2 grid grid-cols-[5.5rem_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs text-neutral-700">
        <dt>source</dt>
        <dd className="font-mono text-neutral-900">{detail.sourceSelection.resolvedSource}</dd>
        <dt>cycle</dt>
        <dd className="min-w-0 truncate font-mono text-neutral-900">{formatDateTime(detail.latestRun.cycleTime) ?? '-'}</dd>
        <dt>valid</dt>
        <dd className="min-w-0 truncate font-mono text-neutral-900">{formatDateTime(detail.latestRun.validTime) ?? '-'}</dd>
        <dt>run_id</dt>
        <dd className="min-w-0 truncate font-mono text-neutral-900">{detail.latestRun.runId ?? '-'}</dd>
      </dl>
      <div className="mt-3 flex flex-wrap gap-2">
        {(warningEntries.length > 0 ? warningEntries : [['unavailable', 0]]).map(([level, count]) => (
          <span key={level} className="rounded border border-neutral-300 px-2 py-1 text-xs text-neutral-700">
            {warningLabel(level as M11WarningLevel)} {count}
          </span>
        ))}
      </div>
      {detail.sourceSelection.unavailableReason ? (
        <p className="mt-2 text-xs text-neutral-700">{detail.sourceSelection.unavailableReason}</p>
      ) : null}
      {detail.partialErrors.length > 0 ? (
        <p className="mt-2 text-xs text-neutral-700">部分数据暂不可用：{detail.partialErrors[0]}</p>
      ) : null}
    </section>
  )
}

function SegmentDiscoveryPanel({
  basinName,
  basinVersionId,
  rows,
  selectedSegmentId,
  query,
  warningLevel,
  segmentCount,
  invalidSegmentId,
  disabled,
  onQueryChange,
}: {
  basinName: string
  basinVersionId: string | null | undefined
  rows: BasinSegmentRow[]
  selectedSegmentId: string | null | undefined
  query: string | null
  warningLevel: M11QueryWarningLevel | null
  segmentCount: number | null
  invalidSegmentId: string | null
  disabled?: boolean
  onQueryChange: (patch: M11QueryPatch) => void
}) {
  const hasPublishedSegments = segmentCount === null ? rows.length > 0 : segmentCount > 0
  const rowRefs = useRef<Record<string, HTMLButtonElement | null>>({})

  useEffect(() => {
    if (!selectedSegmentId) return
    const row = rows.find((item) => item.riverSegmentId === selectedSegmentId || item.segmentId === selectedSegmentId)
    const element = row ? rowRefs.current[row.riverSegmentId] ?? rowRefs.current[row.segmentId] : null
    element?.scrollIntoView({ block: 'nearest' })
  }, [rows, selectedSegmentId])

  return (
    <section className="space-y-3 rounded-md border border-neutral-300 p-3" aria-label="河段发现">
      <div>
        <div className="text-sm font-semibold text-neutral-900">{basinName}</div>
        <div className="mt-0.5 font-mono text-xs text-neutral-700">{basinVersionId ?? '-'}</div>
      </div>

      <label className="flex h-9 items-center gap-2 rounded border border-neutral-300 bg-white px-3 text-sm">
        <Search className="h-4 w-4 text-neutral-500" aria-hidden="true" />
        <span className="sr-only">搜索河段</span>
        <input
          value={query ?? ''}
          disabled={disabled || !hasPublishedSegments}
          placeholder="搜索河段名称或 ID"
          className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-neutral-500 disabled:cursor-not-allowed"
          onChange={(event) => onQueryChange({ q: event.target.value || null })}
        />
      </label>

      <label className="flex items-center gap-2 text-xs text-neutral-700">
        <ListFilter className="h-4 w-4" aria-hidden="true" />
        <span className="sr-only">预警筛选</span>
        <select
          value={warningLevel ?? ''}
          disabled={disabled || !hasPublishedSegments}
          className="h-9 min-w-0 flex-1 rounded border border-neutral-300 bg-white px-2 text-sm text-neutral-900 disabled:cursor-not-allowed disabled:text-neutral-500"
          onChange={(event) => onQueryChange({ warningLevel: event.target.value || null })}
        >
          {warningFilterOptions.map((option) => (
            <option key={option.value || 'all'} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>

      <div className="flex items-center justify-between text-xs text-neutral-700">
        <span>结果 {rows.length}</span>
        <span>总河段 {segmentCount ?? rows.length}</span>
      </div>

      {invalidSegmentId ? (
        <div className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-950" role="status">
          未找到河段 {invalidSegmentId}
        </div>
      ) : null}

      {!hasPublishedSegments ? (
        <div className="rounded border border-neutral-300 bg-neutral-50 px-3 py-6 text-center text-sm text-neutral-700" role="status">
          {NO_SEGMENT_EMPTY_TEXT}
        </div>
      ) : rows.length === 0 ? (
        <div className="rounded border border-neutral-300 bg-neutral-50 px-3 py-6 text-center text-sm text-neutral-700" role="status">
          没有匹配的河段
        </div>
      ) : (
        <div className="max-h-80 space-y-2 overflow-y-auto pr-1" role="list" aria-label="河段列表">
          {rows.map((row) => (
            <SegmentRowButton
              key={`${row.basinVersionId}:${row.riverSegmentId}:${row.segmentId}`}
              ref={(element) => {
                rowRefs.current[row.riverSegmentId] = element
                rowRefs.current[row.segmentId] = element
              }}
              row={row}
              selected={row.riverSegmentId === selectedSegmentId || row.segmentId === selectedSegmentId}
              onSelect={() => onQueryChange({ riverNetworkVersionId: row.riverNetworkVersionId, segmentId: row.riverSegmentId })}
            />
          ))}
        </div>
      )}
    </section>
  )
}

const SegmentRowButton = forwardRef<
  HTMLButtonElement,
  { row: BasinSegmentRow; selected: boolean; onSelect: () => void }
>(function SegmentRowButton({ row, selected, onSelect }, ref) {
  return (
    <button
      ref={ref}
      type="button"
      role="listitem"
      aria-current={selected ? 'true' : undefined}
      data-testid={selected ? 'm11-selected-segment-row' : undefined}
      className={cn(
        'w-full rounded-md border p-3 text-left transition-colors',
        selected
          ? 'border-primary-600 bg-primary-50 text-primary-900'
          : 'border-neutral-300 bg-white text-neutral-800 hover:border-primary-300 hover:bg-primary-50/50',
      )}
      onClick={onSelect}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold">{row.displayName}</div>
          <div className="mt-0.5 truncate font-mono text-xs text-neutral-600">{row.riverSegmentId}</div>
        </div>
        <span className={cn('shrink-0 rounded px-2 py-0.5 text-xs', warningPillClass(row.warningLevel))}>
          {warningLabel(row.warningLevel)}
        </span>
      </div>
      <div className="mt-2 flex items-center justify-between gap-2 text-xs text-neutral-700">
        <span>
          Q {formatMetric(row.currentQ)} {row.currentQ === null ? '' : row.qUnit}
        </span>
        <span>{row.returnPeriod === null ? row.qualityNote ?? row.unavailableReason ?? '-' : `RP ${row.returnPeriod}`}</span>
      </div>
    </button>
  )
})

function SelectedSegmentPanel({
  segment,
  requestedSegmentId,
  invalidSegmentRequested,
  routeState,
}: {
  segment: SelectedSegmentDetail | null | undefined
  requestedSegmentId: string | null
  invalidSegmentRequested: boolean
  routeState: M11QueryState
}) {
  const [comparisonVisible, setComparisonVisible] = useState(false)

  useEffect(() => {
    setComparisonVisible(false)
  }, [segment?.riverSegmentId, segment?.sourceSelection.requestedSource, segment?.freshness.validTime])

  if (!segment) {
    return (
      <section className="rounded-md border border-neutral-300 p-3" aria-label="选中河段详情">
        <div className="flex items-center gap-2 text-base font-semibold text-neutral-900">
          <Activity className="h-4 w-4 text-primary-600" aria-hidden="true" />
          选中河段
        </div>
        <p className="mt-2 text-sm text-neutral-700">
          {invalidSegmentRequested ? `未找到河段 ${requestedSegmentId}` : '尚未选择河段'}
        </p>
        {invalidSegmentRequested ? <p className="mt-1 text-xs text-neutral-700">当前流域版本中没有匹配的河段数据。</p> : null}
      </section>
    )
  }

  return (
    <section className="rounded-md border border-neutral-300 p-3" aria-label="选中河段详情" data-testid="m11-selected-segment-panel">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-base font-semibold text-neutral-900">{segment.displayName}</div>
          <div className="mt-0.5 truncate font-mono text-xs text-neutral-600">{segment.riverSegmentId}</div>
        </div>
        <span className={cn('shrink-0 rounded px-2 py-0.5 text-xs', warningPillClass(segment.warningLevel))}>
          {warningLabel(segment.warningLevel)}
        </span>
      </div>

      <dl className="mt-3 grid grid-cols-[6rem_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-xs text-neutral-700">
        <dt>river_segment_id</dt>
        <dd className="min-w-0 truncate font-mono text-neutral-900">{segment.riverSegmentId}</dd>
        <dt>segment_id</dt>
        <dd className="min-w-0 truncate font-mono text-neutral-900">{segment.segmentId}</dd>
        <dt>流域</dt>
        <dd className="min-w-0 truncate">{segment.basinName ?? segment.basinId ?? '-'}</dd>
        <dt>basin_version</dt>
        <dd className="min-w-0 truncate font-mono text-neutral-900">{segment.basinVersionId}</dd>
        <dt>model_id</dt>
        <dd className="min-w-0 truncate font-mono text-neutral-900">{segment.modelId ?? '-'}</dd>
        <dt>river_network</dt>
        <dd className="min-w-0 truncate font-mono text-neutral-900">{segment.riverNetworkVersionId ?? '-'}</dd>
        <dt>当前 Q</dt>
        <dd>
          {formatMetric(segment.currentQ)} {segment.currentQ === null ? '' : segment.qUnit}
        </dd>
        <dt>水位变化</dt>
        <dd className="text-neutral-500">暂无水位差合同</dd>
        <dt>重现期</dt>
        <dd>{segment.returnPeriod === null ? '-' : `${segment.returnPeriod} 年一遇`}</dd>
        <dt>valid</dt>
        <dd className="min-w-0 truncate font-mono text-neutral-900">{formatDateTime(segment.freshness.validTime) ?? '-'}</dd>
        <dt>source</dt>
        <dd className="min-w-0 truncate">{segment.sourceSelection.resolvedSource}</dd>
        <dt>cycle</dt>
        <dd className="min-w-0 truncate font-mono text-neutral-900">{formatDateTime(segment.freshness.cycleTime) ?? '-'}</dd>
        <dt>quality</dt>
        <dd>{qualityLabel(segment.qualityFlag)}{segment.qualityNote ? ` / ${segment.qualityNote}` : ''}</dd>
        <dt>lineage</dt>
        <dd>{lineageLabel(segment.lineageStatus, segment.lineageUnavailableReason)}</dd>
      </dl>

      {segment.sourceSelection.unavailableReason ? (
        <p className="mt-3 rounded border border-amber-300 bg-amber-50 px-2 py-1.5 text-xs text-amber-950">
          {segment.sourceSelection.unavailableReason}
        </p>
      ) : null}
      {segment.unavailableReason ? (
        <p className="mt-3 rounded border border-amber-300 bg-amber-50 px-2 py-1.5 text-xs text-amber-950">
          {segment.unavailableReason}
        </p>
      ) : null}

      <div className="mt-3 flex flex-wrap gap-2">
        <Link
          className="rounded border border-primary-600 px-3 py-1.5 text-xs font-medium text-primary-600"
          to={segmentDetailHref(segment, routeState)}
        >
          查看河段详情
        </Link>
        <Link className="rounded border border-neutral-300 px-3 py-1.5 text-xs font-medium text-neutral-700" to={segment.handoffUrl}>
          查看预报地图
        </Link>
        {segment.comparisonAvailable ? (
          <button
            type="button"
            className={cn(
              'rounded border px-3 py-1.5 text-xs font-medium',
              comparisonVisible ? 'border-primary-600 bg-primary-50 text-primary-700' : 'border-primary-600 text-primary-600',
            )}
            aria-pressed={comparisonVisible}
            onClick={() => setComparisonVisible((value) => !value)}
          >
            对比预报
          </button>
        ) : (
          <button
            type="button"
            className="rounded border border-neutral-300 px-3 py-1.5 text-xs font-medium text-neutral-400"
            disabled
            aria-disabled="true"
            title={segment.sourceSelection.unavailableReason ?? '对比数据不可用'}
          >
            对比预报
          </button>
        )}
      </div>

      {comparisonVisible ? (
        <SelectedSegmentComparisonTable segment={segment} />
      ) : !segment.comparisonAvailable ? (
        <div className="mt-3 rounded border border-neutral-300 bg-neutral-50 px-3 py-2 text-xs text-neutral-700" role="status">
          对比预报不可用：当前河段缺少可比 GFS/IFS 序列或需要聚合端点。
        </div>
      ) : null}
    </section>
  )
}

function segmentDetailHref(segment: SelectedSegmentDetail, routeState: M11QueryState) {
  const resolvedSource =
    routeState.source === 'best' && (segment.sourceSelection.resolvedSource === 'GFS' || segment.sourceSelection.resolvedSource === 'IFS')
      ? (segment.sourceSelection.resolvedSource.toLowerCase() as 'gfs' | 'ifs')
      : routeState.source
  return m11QueryHref(`/segments/${encodeURIComponent(segment.riverSegmentId)}`, {
    source: resolvedSource,
    cycle: routeState.cycle ?? segment.sourceSelection.cycleTime,
    validTime: routeState.validTime ?? segment.freshness.validTime,
    layer: defaultM11QueryState.layer,
    basemap: defaultM11QueryState.basemap,
    basinVersionId: segment.basinVersionId,
    riverNetworkVersionId: segment.riverNetworkVersionId,
    basinId: null,
    segmentId: segment.riverSegmentId,
    warningLevel: null,
    q: null,
  })
}

function SelectedSegmentComparisonTable({ segment }: { segment: SelectedSegmentDetail }) {
  const rows = buildComparisonRows(segment.trendPoints, segment.freshness.validTime)

  if (rows.length < 2) {
    return (
      <div className="mt-3 rounded border border-neutral-300 bg-neutral-50 px-3 py-2 text-xs text-neutral-700" role="status">
        对比预报不可用：当前河段缺少可比 GFS/IFS 序列或需要聚合端点。
      </div>
    )
  }

  return (
    <div className="mt-3 overflow-hidden rounded border border-primary-200 bg-white text-xs" role="region" aria-label="GFS IFS 对比数据">
      <table className="w-full border-collapse">
        <thead className="bg-primary-50 text-primary-800">
          <tr>
            <th className="px-2 py-1.5 text-left font-semibold">source</th>
            <th className="px-2 py-1.5 text-left font-semibold">valid</th>
            <th className="px-2 py-1.5 text-right font-semibold">Q</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.source} className="border-t border-primary-100">
              <td className="px-2 py-1.5 font-semibold text-neutral-900">{row.source}</td>
              <td className="px-2 py-1.5 font-mono text-neutral-700">{formatDateTime(row.validTime) ?? '-'}</td>
              <td className="px-2 py-1.5 text-right font-mono text-neutral-900">
                {formatMetric(row.value)} {row.value === null ? '' : segment.qUnit}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function SelectedSegmentTrendPanel({ segment }: { segment: SelectedSegmentDetail | null | undefined }) {
  const trend = useMemo(() => buildTrendModel(segment?.trendPoints ?? [], segment?.freshness.validTime ?? null), [segment])

  return (
    <section className="rounded-md border border-neutral-300 p-3" aria-label="河段趋势">
      <div className="flex items-center gap-2 text-base font-semibold text-neutral-900">
        <TrendingUp className="h-4 w-4 text-primary-600" aria-hidden="true" />
        趋势预览
      </div>
      {trend.points.length > 0 ? (
        <>
          <div className="mt-2 flex items-baseline justify-between gap-3">
            <div>
              <div className="text-xs text-neutral-700">当前值</div>
              <div className="font-mono text-lg font-semibold text-neutral-900">{formatMetric(trend.currentValue)}</div>
            </div>
            <div
              className={cn(
                'text-xs font-medium',
                trend.direction === '上升' ? 'text-danger' : trend.direction === '下降' ? 'text-primary-700' : 'text-neutral-700',
              )}
            >
              {trend.direction}
            </div>
          </div>
          <svg className="mt-3 h-20 w-full overflow-visible" viewBox="0 0 240 72" role="img" aria-label="选中河段趋势 sparkline">
            <polyline points={trend.polyline} fill="none" stroke="#1E88E5" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
            {trend.currentPoint ? <circle cx={trend.currentPoint.x} cy={trend.currentPoint.y} r="4" fill="#F97316" /> : null}
          </svg>
          <div className="mt-1 flex items-center justify-between text-[11px] text-neutral-500">
            <span>{formatDateTime(trend.points[0]?.validTime) ?? '-'}</span>
            <span>{formatDateTime(trend.points.at(-1)?.validTime) ?? '-'}</span>
          </div>
        </>
      ) : (
        <div className="mt-3 rounded border border-neutral-300 bg-neutral-50 px-3 py-6 text-center text-sm text-neutral-700" role="status">
          当前河段暂无可用趋势点。
        </div>
      )}
      {segment?.lineageStatus === 'available' ? (
        <div className="mt-3 flex items-center gap-2 rounded border border-success/30 bg-success/10 px-3 py-2 text-xs text-success">
          <GitBranch className="h-3.5 w-3.5" aria-hidden="true" />
          追溯数据可用
        </div>
      ) : segment ? (
        <div className="mt-3 flex items-center gap-2 rounded border border-neutral-300 bg-neutral-50 px-3 py-2 text-xs text-neutral-700">
          <Split className="h-3.5 w-3.5" aria-hidden="true" />
          {segment.lineageUnavailableReason ?? '追溯数据暂不可用'}
        </div>
      ) : null}
    </section>
  )
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

function formatMetric(value: number | null | undefined) {
  return value === null || value === undefined ? '-' : value.toLocaleString('en-US')
}

function formatDateTime(value: string | null | undefined) {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return `${date.toISOString().slice(0, 16).replace('T', ' ')} UTC`
}

function buildTrendModel(points: TrendPoint[], selectedValidTime: string | null) {
  const usable = points
    .filter((point) => point.value !== null)
    .sort((a, b) => Date.parse(a.validTime) - Date.parse(b.validTime))
  const values = usable.map((point) => point.value as number)
  const min = values.length > 0 ? Math.min(...values) : 0
  const max = values.length > 0 ? Math.max(...values) : 0
  const span = max - min || 1
  const currentPoint =
    (selectedValidTime ? usable.find((point) => point.validTime === selectedValidTime) : null) ?? usable[usable.length - 1] ?? null
  const currentIndex = currentPoint ? usable.indexOf(currentPoint) : -1
  const previousPoint = currentIndex > 0 ? usable[currentIndex - 1] : null
  const direction =
    currentPoint && previousPoint
      ? currentPoint.value === previousPoint.value
        ? '持平'
        : (currentPoint.value as number) > (previousPoint.value as number)
          ? '上升'
          : '下降'
      : '趋势不足'
  const coordinates = usable.map((point, index) => {
    const x = usable.length === 1 ? 120 : (index / (usable.length - 1)) * 240
    const y = 60 - ((((point.value as number) - min) / span) * 48)
    return { point, x, y }
  })
  const currentCoordinate = currentPoint ? coordinates.find((entry) => entry.point === currentPoint) ?? null : null

  return {
    points: usable,
    currentValue: currentPoint?.value ?? null,
    direction,
    polyline: coordinates.map((entry) => `${entry.x},${entry.y}`).join(' '),
    currentPoint: currentCoordinate,
  }
}

function buildComparisonRows(points: TrendPoint[], selectedValidTime: string | null) {
  const comparableSources = ['GFS', 'IFS'] as const
  return comparableSources.flatMap((source) => {
    const sourcePoints = points
      .filter((point) => point.source === source && point.value !== null)
      .sort((a, b) => Date.parse(a.validTime) - Date.parse(b.validTime))
    if (sourcePoints.length === 0) return []
    const selectedPoint =
      (selectedValidTime ? sourcePoints.find((point) => point.validTime === selectedValidTime) : null) ??
      sourcePoints[sourcePoints.length - 1]
    return selectedPoint ? [{ source, validTime: selectedPoint.validTime, value: selectedPoint.value }] : []
  })
}

function qualityLabel(value: string) {
  const labels: Record<string, string> = {
    ok: '通过',
    degraded: '降级',
    unavailable: '不可用',
    failed: '失败',
    unknown: '未知',
  }
  return labels[value] ?? value
}

function lineageLabel(status: SelectedSegmentDetail['lineageStatus'], reason: string | null) {
  if (status === 'available') return '可用'
  if (status === 'failed') return `失败${reason ? ` / ${reason}` : ''}`
  return reason ?? '不可用'
}

function mapFeatureStringProperty(feature: M11MapOverlayInteraction['feature'], key: string) {
  const value = feature?.properties?.[key]
  return typeof value === 'string' && value.length > 0 ? value : null
}

// popup 经纬度锚点：station 优先用 feature 点几何坐标，否则回退点击 event lngLat（river 用之）。
function popupAnchorFromInteraction(interaction: M11MapOverlayInteraction): [number, number] | null {
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

function warningLabel(level: M11WarningLevel | string) {
  const labels: Record<string, string> = {
    normal: '正常',
    elevated: '偏高',
    watch: '关注',
    warning: '橙色',
    high_risk: '高风险',
    severe: '红色',
    extreme: '极端',
    unavailable: '无数据',
  }
  return labels[level] ?? level
}

function warningPillClass(level: M11WarningLevel) {
  if (level === 'warning' || level === 'high_risk') return 'bg-amber-100 text-amber-900'
  if (level === 'severe' || level === 'extreme') return 'bg-danger/10 text-danger'
  if (level === 'watch' || level === 'elevated') return 'bg-primary-50 text-primary-700'
  if (level === 'unavailable') return 'bg-neutral-100 text-neutral-600'
  return 'bg-success/10 text-success'
}

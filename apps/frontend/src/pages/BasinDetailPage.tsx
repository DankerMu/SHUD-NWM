import { useCallback, useEffect, useMemo } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'

import type { M11MapOverlayInteraction } from '@/components/map/M11MapLibreSurface'
import type { M11Bbox } from '@/lib/m11/overviewDataContracts'
import { M11Layout, SegmentSearchStub, StateReadout } from '@/pages/m11/M11Shell'
import {
  defaultM11QueryState,
  type M11QueryPatch,
  needsM11QueryReplacement,
  parseM11QueryState,
  serializeM11QueryState,
} from '@/lib/m11/queryState'
import {
  LayerGroupControls,
  LayerLegendPanel,
  SourceScenarioControls,
  resolveM11ValidTimeCorrection,
} from '@/pages/m11/M11Controls'
import { basinSnapshotMatchesQuery, basinSnapshotMetadataMatchesQuery, useOverviewDataStore } from '@/stores/overviewData'

const BASIN_NOT_FOUND_REASON = 'Basin was not found.'

export function BasinDetailPage() {
  const { basinId = 'unknown' } = useParams()
  const location = useLocation()
  const navigate = useNavigate()
  const state = useMemo(() => parseM11QueryState(location.search), [location.search])
  const dataLoadState = useMemo(
    () => ({
      source: state.source,
      cycle: state.cycle,
      validTime: state.validTime,
      layer: state.layer,
      basemap: defaultM11QueryState.basemap,
      basinVersionId: state.basinVersionId,
      segmentId: state.segmentId,
      warningLevel: state.warningLevel,
      q: state.q,
    }),
    [state.basinVersionId, state.cycle, state.layer, state.q, state.segmentId, state.source, state.validTime, state.warningLevel],
  )
  const normalizedSearch = useMemo(() => serializeM11QueryState(state), [state])
  const basinData = useOverviewDataStore((store) => store.basinDetail)
  const loading = useOverviewDataStore((store) => store.basinLoading)
  const error = useOverviewDataStore((store) => store.basinError)
  const loadBasinDetail = useOverviewDataStore((store) => store.loadBasinDetail)
  const needsQueryReplacement = needsM11QueryReplacement(location.search)
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

  useEffect(() => {
    if (needsQueryReplacement) return
    void loadBasinDetail(basinId, dataLoadState).catch(() => undefined)
  }, [basinId, dataLoadState, loadBasinDetail, needsQueryReplacement])

  useEffect(() => {
    if (needsQueryReplacement || loading || !basinMetadataMatchesQuery) return
    const correctedValidTime = resolveM11ValidTimeCorrection(state, metadataLayers, derivedTimeline)
    if (correctedValidTime === undefined) return
    handleQueryChange({ validTime: correctedValidTime })
  }, [basinMetadataMatchesQuery, derivedTimeline, handleQueryChange, loading, metadataLayers, needsQueryReplacement, state])

  const detail = currentBasinData?.detail
  const basinNotFoundReason = !loading && detail?.unavailableReason === BASIN_NOT_FOUND_REASON ? detail.unavailableReason : null
  const basinDisplayName = detail?.displayName || basinId
  const selectedSegment = currentBasinData?.selectedSegment
  const invalidSegmentRequested = Boolean(state.segmentId && currentBasinData && !loading && !basinNotFoundReason && !selectedSegment)
  const mapFitTo = useMemo(() => bboxToMapFit(detail?.bbox), [detail?.bbox])
  const handleMapOverlayHover = useCallback((_interaction: M11MapOverlayInteraction | null) => undefined, [])
  const handleMapOverlayClick = useCallback((_interaction: M11MapOverlayInteraction) => undefined, [])

  return (
    <M11Layout
      title="流域分析"
      subtitle={`当前流域 ${basinDisplayName}`}
      state={state}
      layers={layers}
      sourceSelection={sourceSelection}
      derivedTimeline={derivedTimeline}
      fitTo={mapFitTo}
      onMapOverlayHover={handleMapOverlayHover}
      onMapOverlayClick={handleMapOverlayClick}
      onQueryChange={handleQueryChange}
      mapLabel="流域钻取地图"
      mapTitle={`${basinDisplayName} 流域钻取`}
      mapMeta="初始钻取壳恢复 basinVersionId、segmentId、source、cycle、validTime、warningLevel 与搜索条件，后续接入真实河段数据。"
      left={
        <>
          <SourceScenarioControls state={state} sourceSelection={sourceSelection} onQueryChange={handleQueryChange} />
          <StateReadout state={state} basinId={basinId} />
          {detail && !basinNotFoundReason ? (
            <div className="rounded-md border border-neutral-300 bg-neutral-50 p-3 text-xs text-neutral-700">
              <div className="font-mono text-neutral-900">{detail.selectedBasinVersionId ?? '-'}</div>
              <div className="mt-1">河段 {detail.segmentCount ?? '-'} / 活跃模型 {detail.activeModelCount}</div>
            </div>
          ) : null}
          <SegmentSearchStub query={state.q} />
          <LayerGroupControls state={state} layers={layers} onQueryChange={handleQueryChange} />
        </>
      }
      right={
        <>
          {basinNotFoundReason ? (
            <BasinUnavailableNotice basinId={basinId} reason={basinNotFoundReason} />
          ) : (
            <>
              <div className="rounded-md border border-neutral-300 p-3">
                <div className="text-base font-semibold text-neutral-900">选中河段</div>
                <p className="mt-2 text-sm text-neutral-700">
                  {selectedSegment
                    ? `已恢复 ${selectedSegment.riverSegmentId}`
                    : invalidSegmentRequested
                      ? `未找到河段 ${state.segmentId}`
                      : '尚未选择河段'}
                </p>
                {invalidSegmentRequested ? (
                  <p className="mt-1 text-xs text-neutral-700">当前流域版本中没有匹配的河段数据。</p>
                ) : null}
                {selectedSegment ? (
                  <p className="mt-1 text-xs text-neutral-700">
                    {selectedSegment.currentQ ?? '-'} {selectedSegment.qUnit} / {selectedSegment.warningLevel}
                  </p>
                ) : null}
              </div>
              <div className="rounded-md border border-neutral-300 p-3">
                <div className="text-base font-semibold text-neutral-900">预警状态</div>
                <p className="mt-2 font-mono text-sm text-neutral-700">{state.warningLevel ?? 'all'}</p>
              </div>
            </>
          )}
          <LayerLegendPanel state={state} layers={layers} />
          {loading || error ? (
            <div className="rounded-md border border-neutral-300 bg-neutral-50 p-3 text-xs text-neutral-700">
              {loading ? '流域数据加载中' : error}
            </div>
          ) : null}
          {!basinNotFoundReason ? (
            <Link className="block rounded border border-primary-600 px-3 py-2 text-sm font-medium text-primary-600" to="/overview">
              返回全国总览
            </Link>
          ) : null}
          <Link className="block rounded border border-primary-600 px-3 py-2 text-sm font-medium text-primary-600" to="/forecast">
            返回水文预报
          </Link>
        </>
      }
    />
  )
}

function BasinUnavailableNotice({ basinId, reason }: { basinId: string; reason: string }) {
  const title = reason === 'Basin was not found.' ? '未找到流域' : '流域暂不可用'

  return (
    <section className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950" aria-label="流域不可用">
      <div className="text-base font-semibold">{title}</div>
      <p className="mt-2">{reason}</p>
      <p className="mt-1 text-xs">
        请求的流域 ID：<code className="font-mono">{basinId}</code>
      </p>
      <Link className="mt-3 block rounded border border-primary-600 bg-white px-3 py-2 text-sm font-medium text-primary-600" to="/overview">
        返回全国总览
      </Link>
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

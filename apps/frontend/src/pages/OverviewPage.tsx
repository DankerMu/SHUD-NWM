import { useCallback, useEffect, useMemo } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'

import type { M11MapOverlayInteraction } from '@/components/map/M11MapLibreSurface'
import type { M11Bbox } from '@/lib/m11/overviewDataContracts'
import { BasinLink, M11Layout, StateReadout } from '@/pages/m11/M11Shell'
import {
  type M11QueryPatch,
  needsM11QueryReplacement,
  parseM11QueryState,
  serializeM11QueryState,
} from '@/lib/m11/queryState'
import { LayerGroupControls, LayerLegendPanel, SourceScenarioControls, resolveM11ValidTimeCorrection } from '@/pages/m11/M11Controls'
import { overviewSnapshotMatchesQuery, overviewSnapshotMetadataMatchesQuery, useOverviewDataStore } from '@/stores/overviewData'

export function OverviewPage() {
  const location = useLocation()
  const navigate = useNavigate()
  const state = useMemo(() => parseM11QueryState(location.search), [location.search])
  const normalizedSearch = useMemo(() => serializeM11QueryState(state), [state])
  const overview = useOverviewDataStore((store) => store.overview)
  const loading = useOverviewDataStore((store) => store.loading)
  const error = useOverviewDataStore((store) => store.error)
  const loadOverview = useOverviewDataStore((store) => store.loadOverview)
  const needsQueryReplacement = needsM11QueryReplacement(location.search)
  const overviewMatchesQuery = overviewSnapshotMatchesQuery(overview, state)
  const overviewMetadataMatchesQuery = overviewSnapshotMetadataMatchesQuery(overview, state)
  const currentOverview = overviewMatchesQuery ? overview : null
  const metadataLayers = overviewMetadataMatchesQuery ? (overview?.layers ?? []) : []
  const layers = currentOverview?.layers ?? metadataLayers

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
    void loadOverview(state).catch(() => undefined)
  }, [loadOverview, needsQueryReplacement, state])

  useEffect(() => {
    if (needsQueryReplacement || loading || !overviewMetadataMatchesQuery || metadataLayers.length === 0) return
    const correctedValidTime = resolveM11ValidTimeCorrection(state, metadataLayers)
    if (correctedValidTime === undefined) return
    handleQueryChange({ validTime: correctedValidTime })
  }, [handleQueryChange, loading, metadataLayers, needsQueryReplacement, overviewMetadataMatchesQuery, state])

  const basins = currentOverview?.basins ?? []
  const summary = currentOverview?.summary
  const sourceSelection = summary?.sourceSelection ?? null
  const firstBasin = basins[0]
  const mapFitTo = useMemo(() => bboxToMapFit(firstBasin?.bbox), [firstBasin?.bbox])
  const handleMapOverlayHover = useCallback((_interaction: M11MapOverlayInteraction | null) => undefined, [])
  const handleMapOverlayClick = useCallback((_interaction: M11MapOverlayInteraction) => undefined, [])
  const emptyBasinReason =
    !loading && basins.length === 0
      ? error ??
        (summary?.totalBasins === 0
          ? '暂无可用流域数据'
          : currentOverview?.aggregationDecision.needsAggregationEndpoint
            ? currentOverview.aggregationDecision.evidence
            : '流域清单暂不可用')
      : null
  const basinSearch = firstBasin
    ? serializeM11QueryState({
        ...state,
        basinVersionId: state.basinVersionId ?? firstBasin.selectedBasinVersionId,
        segmentId: state.segmentId,
      })
    : serializeM11QueryState(state)
  const basinLinkTarget = firstBasin ? `/basins/${firstBasin.basinId}${basinSearch ? `?${basinSearch}` : ''}` : '/overview'

  return (
    <M11Layout
      title="全国总览"
      subtitle="全国流域、图层和运行态势"
      state={state}
      layers={layers}
      sourceSelection={sourceSelection}
      fitTo={mapFitTo}
      onMapOverlayHover={handleMapOverlayHover}
      onMapOverlayClick={handleMapOverlayClick}
      onQueryChange={handleQueryChange}
      mapLabel="全国总览地图"
      mapTitle="全国水文总览"
      mapMeta="初始地图壳保留全国范围、流域边界、河网和图层占位，不加载未实现的真实适配器。"
      left={
        <>
          <SourceScenarioControls state={state} sourceSelection={sourceSelection} onQueryChange={handleQueryChange} />
          <div className="space-y-2">
            <div className="text-sm font-semibold text-neutral-900">流域管理</div>
            {basins.length > 0 ? (
              basins.map((basin) => (
                <label key={basin.basinId} className="flex items-center gap-2 rounded px-2 py-1.5 text-sm text-neutral-700">
                  <input type="checkbox" defaultChecked className="h-4 w-4" />
                  {basin.displayName}
                </label>
              ))
            ) : (
              <div className="rounded-md border border-neutral-300 bg-neutral-50 p-3 text-xs text-neutral-700">
                {loading ? '流域清单加载中' : emptyBasinReason}
              </div>
            )}
          </div>
          <LayerGroupControls state={state} layers={layers} onQueryChange={handleQueryChange} />
          <BasinLink to={basinLinkTarget}>{firstBasin ? '进入流域分析' : '等待可用流域'}</BasinLink>
        </>
      }
      right={
        <>
          <div className="grid grid-cols-2 gap-3">
            <SummaryMetric value={formatMetric(summary?.completedCyclesToday)} label="今日完成周期" />
            <SummaryMetric value={formatMetric(summary?.runningJobs)} label="当前运行中" />
            <SummaryMetric value={formatMetric(summary?.warningSegmentCount)} label="超警河段" tone="warning" />
            <SummaryMetric value={formatTime(summary?.latestUpdate) ?? '-'} label="最新更新时间" />
          </div>
          {loading || error ? (
            <div className="rounded-md border border-neutral-300 bg-neutral-50 p-3 text-xs text-neutral-700">
              {loading ? '总览数据加载中' : error}
            </div>
          ) : null}
          <LayerLegendPanel state={state} layers={layers} />
          <div className="space-y-2">
            <Link className="block rounded border border-neutral-300 p-3 hover:bg-primary-50" to="/monitoring">
              产品监控摘要
            </Link>
            <Link className="block rounded border border-neutral-300 p-3 hover:bg-primary-50" to="/flood-alerts">
              洪水预警摘要
            </Link>
          </div>
          <StateReadout state={state} />
        </>
      }
    />
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

function SummaryMetric({ value, label, tone = 'default' }: { value: string; label: string; tone?: 'default' | 'warning' }) {
  return (
    <div className="rounded-md border border-neutral-300 bg-white p-3 text-center shadow-sm">
      <div className={tone === 'warning' ? 'text-2xl font-bold text-warning' : 'text-2xl font-bold text-primary-600'}>
        {value}
      </div>
      <div className="mt-1 text-xs text-neutral-700">{label}</div>
    </div>
  )
}

function formatMetric(value: number | null | undefined) {
  return value === null || value === undefined ? '-' : String(value)
}

function formatTime(value: string | null | undefined) {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return `${String(date.getUTCHours()).padStart(2, '0')}:${String(date.getUTCMinutes()).padStart(2, '0')}`
}

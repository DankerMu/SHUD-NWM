import { useCallback, useEffect, useMemo } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { ListFilter, Search } from 'lucide-react'

import type { M11MapOverlayInteraction } from '@/components/map/M11MapLibreSurface'
import {
  filterBasinSegmentRows,
  type BasinDetail,
  type BasinSegmentRow,
  type M11Bbox,
  type M11WarningLevel,
} from '@/lib/m11/overviewDataContracts'
import { M11Layout, StateReadout } from '@/pages/m11/M11Shell'
import {
  defaultM11QueryState,
  type M11QueryPatch,
  type M11QueryWarningLevel,
  needsM11QueryReplacement,
  parseM11QueryState,
  serializeM11QueryState,
} from '@/lib/m11/queryState'
import { cn } from '@/lib/cn'
import {
  LayerGroupControls,
  LayerLegendPanel,
  SourceScenarioControls,
  resolveM11ValidTimeCorrection,
} from '@/pages/m11/M11Controls'
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
      warningLevel: null,
      q: null,
    }),
    [state.basinVersionId, state.cycle, state.layer, state.segmentId, state.source, state.validTime],
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
      selectedSegmentId={selectedSegmentId}
      selectedSegmentGeometry={selectedSegment?.geometry ?? null}
      onMapOverlayHover={handleMapOverlayHover}
      onMapOverlayClick={handleMapOverlayClick}
      onQueryChange={handleQueryChange}
      mapLabel="流域钻取地图"
      mapTitle={`${basinDisplayName} 流域钻取`}
      mapMeta={detail?.bbox ? '地图已按流域 bbox 定位，河段选择会同步到地图高亮状态。' : '当前流域缺少 bbox，地图使用中国范围兜底视域，河段列表不受影响。'}
      left={
        <>
          <SourceScenarioControls state={state} sourceSelection={sourceSelection} onQueryChange={handleQueryChange} />
          <StateReadout state={state} basinId={basinId} />
          {detail && !basinNotFoundReason ? <BasinIdentityCard detail={detail} /> : null}
          {detail && !basinNotFoundReason && !detail.bbox ? <MissingBboxNotice /> : null}
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
            onQueryChange={handleQueryChange}
          />
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
                  <>
                    <p className="mt-1 text-xs text-neutral-700">
                      {selectedSegment.currentQ ?? '-'} {selectedSegment.qUnit} / {selectedSegment.warningLevel}
                    </p>
                    <div className="mt-3 flex flex-wrap gap-2">
                      <Link
                        className="rounded border border-primary-600 px-3 py-1.5 text-xs font-medium text-primary-600"
                        to={selectedSegment.handoffUrl}
                      >
                        查看详情
                      </Link>
                      {selectedSegment.comparisonAvailable ? (
                        <Link
                          className="rounded border border-primary-600 px-3 py-1.5 text-xs font-medium text-primary-600"
                          to={selectedSegment.handoffUrl}
                        >
                          对比预报
                        </Link>
                      ) : (
                        <button
                          type="button"
                          className="rounded border border-neutral-300 px-3 py-1.5 text-xs font-medium text-neutral-400"
                          disabled
                          aria-disabled="true"
                          title="对比数据不可用"
                        >
                          对比预报
                        </button>
                      )}
                    </div>
                  </>
                ) : null}
              </div>
              <div className="rounded-md border border-neutral-300 p-3">
                <div className="text-base font-semibold text-neutral-900">预警状态</div>
                <p className="mt-2 font-mono text-sm text-neutral-700">{state.warningLevel ?? 'all'}</p>
              </div>
              {detail ? <BasinRunMetadata detail={detail} /> : null}
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
              row={row}
              selected={row.riverSegmentId === selectedSegmentId || row.segmentId === selectedSegmentId}
              onSelect={() => onQueryChange({ segmentId: row.riverSegmentId })}
            />
          ))}
        </div>
      )}
    </section>
  )
}

function SegmentRowButton({ row, selected, onSelect }: { row: BasinSegmentRow; selected: boolean; onSelect: () => void }) {
  return (
    <button
      type="button"
      role="listitem"
      aria-current={selected ? 'true' : undefined}
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

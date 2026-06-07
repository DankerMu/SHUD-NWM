import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { AlertTriangle, ArrowRight, ExternalLink, Layers, Rows3 } from 'lucide-react'

import type { M11MapOverlayInteraction } from '@/components/map/M11MapLibreSurface'
import { cn } from '@/lib/cn'
import type { M11Bbox, OverviewBasin, SourceScenarioSelectionState } from '@/lib/m11/overviewDataContracts'
import { M11Layout, StateReadout } from '@/pages/m11/M11Shell'
import { useBasinDetailMode } from '@/components/m11/BasinDetailPanels'
import {
  defaultM11QueryState,
  type M11QueryPatch,
  type M11QueryState,
  needsM11QueryReplacement,
  parseM11QueryState,
  serializeM11QueryState,
} from '@/lib/m11/queryState'
import { LayerGroupControls, LayerLegendPanel, SourceScenarioControls, resolveM11ValidTimeCorrection } from '@/pages/m11/M11Controls'
import { overviewSnapshotMatchesQuery, overviewSnapshotMetadataMatchesQuery, useOverviewDataStore } from '@/stores/overviewData'

const NONE_VISIBLE_SENTINEL = '__none__'

/**
 * 单页地图（M26-2）：按 query 内 basinId 双模式渲染——
 * basinId 为空 → 全国总览模式；非空 → 流域详情模式（就地 zoom-in）。
 * pathname 恒为 `/`，模式切换不触发整页路由跳转。
 * 子模式拆为独立组件，借组件 mount/unmount 隔离两套 hooks/effects。
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

function BasinDetailMode({
  basinId,
  state,
  onQueryChange,
}: {
  basinId: string
  state: M11QueryState
  onQueryChange: (patch: M11QueryPatch) => void
}) {
  const layoutProps = useBasinDetailMode({ basinId, state, onQueryChange })
  return <M11Layout state={state} onQueryChange={onQueryChange} {...layoutProps} />
}

function OverviewMode({ state, onQueryChange }: { state: M11QueryState; onQueryChange: (patch: M11QueryPatch) => void }) {
  const handleQueryChange = onQueryChange
  const dataLoadState = useMemo(
    () => ({
      source: state.source,
      cycle: state.cycle,
      validTime: state.validTime,
      layer: state.layer,
      basemap: defaultM11QueryState.basemap,
      basinVersionId: state.basinVersionId,
      riverNetworkVersionId: state.riverNetworkVersionId,
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
  const [visibleBasinIds, setVisibleBasinIds] = useState<Set<string>>(() => new Set())
  const [selectedBasinId, setSelectedBasinId] = useState<string | null>(null)
  const [popupBasinId, setPopupBasinId] = useState<string | null>(null)

  useEffect(() => {
    void loadOverview(dataLoadState).catch(() => undefined)
  }, [dataLoadState, loadOverview])

  useEffect(() => {
    if (loading || !overviewMetadataMatchesQuery || metadataLayers.length === 0) return
    const correctedValidTime = resolveM11ValidTimeCorrection(state, metadataLayers)
    if (correctedValidTime === undefined) return
    handleQueryChange({ validTime: correctedValidTime })
  }, [handleQueryChange, loading, metadataLayers, overviewMetadataMatchesQuery, state])

  const basins = currentOverview?.basins ?? []
  const summary = currentOverview?.summary
  const sourceSelection = summary?.sourceSelection ?? null
  const visibleBasinIdList = useMemo(() => {
    if (basins.length === 0) return []
    if (visibleBasinIds.size === 0) return basins.map((basin) => basin.basinId)
    if (visibleBasinIds.has(NONE_VISIBLE_SENTINEL)) return []
    return basins.filter((basin) => visibleBasinIds.has(basin.basinId)).map((basin) => basin.basinId)
  }, [basins, visibleBasinIds])
  const visibleBasins = useMemo(
    () =>
      visibleBasinIds.size === 0
        ? basins
        : visibleBasinIds.has(NONE_VISIBLE_SENTINEL)
          ? []
          : basins.filter((basin) => visibleBasinIds.has(basin.basinId)),
    [basins, visibleBasinIds],
  )
  const visibleBasinSet = useMemo(() => new Set(visibleBasinIdList), [visibleBasinIdList])
  const selectedBasin =
    selectedBasinId && visibleBasinSet.has(selectedBasinId) ? (basins.find((basin) => basin.basinId === selectedBasinId) ?? null) : null
  const popupBasin = popupBasinId && popupBasinId === selectedBasin?.basinId ? selectedBasin : null
  const mapFitTo = useMemo(() => bboxToMapFit(unionBasinBbox(visibleBasins) ?? selectedBasin?.bbox), [selectedBasin?.bbox, visibleBasins])
  const handleMapOverlayHover = useCallback((_interaction: M11MapOverlayInteraction | null) => undefined, [])
  const handleMapOverlayClick = useCallback(
    (interaction: M11MapOverlayInteraction) => {
      const feature = interaction.feature ?? interaction.event.features?.find((item) => item.layer?.id === 'm11-basin-fill')
      const basinId = feature?.properties?.basin_id
      if (typeof basinId === 'string' && visibleBasinSet.has(basinId)) {
        setSelectedBasinId(basinId)
        setPopupBasinId(basinId)
      }
    },
    [visibleBasinSet],
  )
  const handleSelectBasin = useCallback(
    (basinId: string) => {
      setSelectedBasinId(basinId)
      setPopupBasinId(visibleBasinSet.has(basinId) ? basinId : null)
    },
    [visibleBasinSet],
  )
  // 进入流域分析：写 basinId（就地切详情模式），并由详情模式 fitTo 该 basin bbox。
  const handleEnterAnalysis = useCallback(
    (basin: OverviewBasin) => handleQueryChange(basinAnalysisPatch(basin, state)),
    [handleQueryChange, state],
  )
  useEffect(() => {
    if (basins.length === 0) {
      setVisibleBasinIds((current) => (current.size === 0 ? current : new Set()))
      setSelectedBasinId((current) => (current === null ? current : null))
      setPopupBasinId((current) => (current === null ? current : null))
      return
    }
    const basinIds = new Set([NONE_VISIBLE_SENTINEL, ...basins.map((basin) => basin.basinId)])
    setVisibleBasinIds((current) => {
      if (current.size === 0) return current
      const next = new Set([...current].filter((basinId) => basinIds.has(basinId)))
      return next.size === current.size ? current : next
    })
    setSelectedBasinId((current) => (current && basinIds.has(current) && current !== NONE_VISIBLE_SENTINEL ? current : null))
    setPopupBasinId((current) => (current && basinIds.has(current) && current !== NONE_VISIBLE_SENTINEL ? current : null))
  }, [basins])
  useEffect(() => {
    setPopupBasinId((current) => (current && visibleBasinSet.has(current) ? current : null))
    setSelectedBasinId((current) => (current && visibleBasinSet.has(current) ? current : null))
  }, [visibleBasinSet])
  const emptyBasinReason =
    !loading && basins.length === 0
      ? error ??
        (summary?.totalBasins === 0
          ? '暂无可用流域数据'
          : currentOverview?.aggregationDecision.needsAggregationEndpoint
            ? currentOverview.aggregationDecision.evidence
            : '流域清单暂不可用')
      : null
  const monitoringHandoff = contextHandoff('/monitoring', state, sourceSelection)
  const floodAlertsHandoff = contextHandoff('/flood-alerts', state, sourceSelection)
  const partialErrors = summary?.partialErrors ?? []
  const unavailableBasinCount = basins.filter((basin) => basin.unavailableReason).length
  const boundaryCount = basins.filter((basin) => basin.boundary).length
  const selectedLayer = layers.find((layer) => layer.layerId === state.layer)

  return (
    <M11Layout
      title="全国总览"
      subtitle="全国流域、图层和运行态势"
      state={state}
      layers={layers}
      basins={basins}
      visibleBasinIds={visibleBasinIdList}
      sourceSelection={sourceSelection}
      fitTo={mapFitTo}
      onMapOverlayHover={handleMapOverlayHover}
      onMapOverlayClick={handleMapOverlayClick}
      onQueryChange={handleQueryChange}
      mapLabel="全国总览地图"
      mapTitle="全国水文总览"
      mapMeta={`全国范围 73E-135E / 18N-53N；已接入 ${boundaryCount}/${basins.length} 个可用流域边界，河网与气象图层按合同可用性呈现。`}
      left={
        <>
          <SourceScenarioControls state={state} sourceSelection={sourceSelection} onQueryChange={handleQueryChange} />
          <BasinVisibilityTree
            basins={basins}
            visibleBasinIds={visibleBasinIds}
            loading={loading}
            emptyReason={emptyBasinReason}
            selectedBasinId={selectedBasin?.basinId ?? null}
            onSelectBasin={handleSelectBasin}
            onSetVisibleBasinIds={setVisibleBasinIds}
          />
          <LayerGroupControls state={state} layers={layers} onQueryChange={handleQueryChange} />
          {unavailableBasinCount > 0 ? (
            <ScopedNotice tone="warning">
              {unavailableBasinCount} 个流域缺少已发布版本或边界，地图不会绘制对应边界。
            </ScopedNotice>
          ) : null}
          <EnterAnalysisButton
            disabled={!selectedBasin}
            onClick={() => selectedBasin && handleEnterAnalysis(selectedBasin)}
          >
            {selectedBasin ? '进入流域分析' : '等待可见流域选择'}
          </EnterAnalysisButton>
        </>
      }
      right={
        <>
          <section className="space-y-2" aria-label="M11 底图与图层摘要">
            <div className="flex items-center gap-2 text-sm font-semibold text-neutral-900">
              <Layers className="h-4 w-4 text-primary-600" aria-hidden="true" />
              当前图层
            </div>
            <div className="rounded-md border border-neutral-300 bg-neutral-50 p-3 text-xs text-neutral-700">
              <div className="font-medium text-neutral-900">{selectedLayer?.displayName ?? state.layer}</div>
              <div className="mt-1">
                {selectedLayer?.available
                  ? `${selectedLayer.validTimes.length} 个有效时刻 / ${selectedLayer.validTimeSource}`
                  : selectedLayer?.disabledReason ?? '当前图层暂不可渲染'}
              </div>
            </div>
          </section>
          <div className="grid grid-cols-2 gap-3">
            <SummaryMetric value={formatMetric(summary?.completedCyclesToday)} label="今日完成周期" />
            <SummaryMetric value={formatMetric(summary?.runningJobs)} label="当前运行中" />
            <SummaryMetric value={formatMetric(summary?.warningSegmentCount)} label="超警河段" tone="warning" />
            <SummaryMetric value={formatTime(summary?.latestUpdate) ?? '-'} label="最新更新时间" />
          </div>
          {loading || error ? <ScopedNotice>{loading ? '总览数据加载中' : error}</ScopedNotice> : null}
          {partialErrors.length > 0 ? (
            <ScopedNotice tone="warning">{partialErrors[0]}</ScopedNotice>
          ) : null}
          <LayerLegendPanel state={state} layers={layers} />
          <div className="space-y-2">
            <SummaryLink to={monitoringHandoff.href} title="产品监控摘要" description={monitoringHandoff.description} />
            <SummaryLink to={floodAlertsHandoff.href} title="洪水预警摘要" description={floodAlertsHandoff.description} />
          </div>
          <StateReadout state={state} />
        </>
      }
    >
      {popupBasin ? <BasinPopup basin={popupBasin} onEnterAnalysis={() => handleEnterAnalysis(popupBasin)} /> : null}
    </M11Layout>
  )
}

function EnterAnalysisButton({
  disabled,
  onClick,
  children,
}: {
  disabled: boolean
  onClick: () => void
  children: ReactNode
}) {
  if (disabled) {
    return (
      <span
        aria-disabled="true"
        className="flex h-9 cursor-not-allowed items-center justify-between rounded border border-neutral-300 px-3 text-sm font-medium text-neutral-500"
      >
        {children}
        <ArrowRight className="h-4 w-4" aria-hidden="true" />
      </span>
    )
  }
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex h-9 w-full items-center justify-between rounded border border-primary-600 px-3 text-sm font-medium text-primary-600 transition-colors hover:bg-primary-50"
    >
      {children}
      <ArrowRight className="h-4 w-4" aria-hidden="true" />
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

function BasinVisibilityTree({
  basins,
  visibleBasinIds,
  loading,
  emptyReason,
  selectedBasinId,
  onSelectBasin,
  onSetVisibleBasinIds,
}: {
  basins: OverviewBasin[]
  visibleBasinIds: Set<string>
  loading: boolean
  emptyReason: string | null
  selectedBasinId: string | null
  onSelectBasin: (basinId: string) => void
  onSetVisibleBasinIds: (ids: Set<string>) => void
}) {
  const groups = useMemo(() => groupBasins(basins), [basins])
  const allVisible = basins.length > 0 && (visibleBasinIds.size === 0 || visibleBasinIds.size === basins.length)
  const noneVisible = visibleBasinIds.has(NONE_VISIBLE_SENTINEL)
  const isVisible = useCallback(
    (basinId: string) => !noneVisible && (visibleBasinIds.size === 0 || visibleBasinIds.has(basinId)),
    [noneVisible, visibleBasinIds],
  )

  return (
    <section className="space-y-3" aria-label="全国流域树">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-semibold text-neutral-900">
          <Rows3 className="h-4 w-4 text-primary-600" aria-hidden="true" />
          流域管理
        </div>
        <div className="flex items-center gap-1 text-xs">
          <button
            type="button"
            className="cursor-pointer rounded border border-neutral-300 px-2 py-1 text-neutral-700 hover:bg-neutral-50"
            onClick={() => onSetVisibleBasinIds(new Set())}
          >
            全选
          </button>
          <button
            type="button"
            className="cursor-pointer rounded border border-neutral-300 px-2 py-1 text-neutral-700 hover:bg-neutral-50"
            onClick={() => onSetVisibleBasinIds(new Set())}
            disabled={basins.length === 0}
          >
            重置
          </button>
          <button
            type="button"
            className="cursor-pointer rounded border border-neutral-300 px-2 py-1 text-neutral-700 hover:bg-neutral-50 disabled:cursor-not-allowed disabled:text-neutral-500"
            onClick={() => onSetVisibleBasinIds(new Set([NONE_VISIBLE_SENTINEL]))}
            disabled={basins.length === 0}
          >
            全不选
          </button>
        </div>
      </div>
      {basins.length > 0 ? (
        <div className="max-h-[18rem] space-y-3 overflow-auto pr-1">
          {groups.map((group) => (
            <div key={group.name} className="space-y-1">
              <div className="text-xs font-semibold text-primary-700">{group.name}</div>
              {group.basins.map((basin) => {
                const checked = !noneVisible && (allVisible || isVisible(basin.basinId))
                return (
                  <label
                    key={basin.basinId}
                    className={cn(
                      'flex cursor-pointer items-start gap-2 rounded px-2 py-1.5 text-sm transition-colors',
                      selectedBasinId === basin.basinId ? 'bg-primary-50 text-primary-700' : 'text-neutral-700 hover:bg-neutral-50',
                    )}
                    data-testid={`m11-basin-row-${basin.basinId}`}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      className="mt-0.5 h-4 w-4"
                      aria-label={`${basin.displayName} 可见`}
                      onChange={(event) => {
                        const next = noneVisible
                          ? new Set<string>()
                          : allVisible
                            ? new Set(basins.map((item) => item.basinId))
                            : new Set(visibleBasinIds)
                        next.delete(NONE_VISIBLE_SENTINEL)
                        if (event.target.checked) next.add(basin.basinId)
                        else next.delete(basin.basinId)
                        onSetVisibleBasinIds(next.size === 0 ? new Set([NONE_VISIBLE_SENTINEL]) : next)
                      }}
                    />
                    <span
                      className="min-w-0 flex-1"
                      onClick={(event) => {
                        event.preventDefault()
                        onSelectBasin(basin.basinId)
                      }}
                    >
                      <span className="block truncate font-medium">{basin.displayName}</span>
                      <span className="block truncate text-xs text-neutral-500">
                        {basin.selectedBasinVersionId ?? basin.unavailableReason ?? '版本不可用'}
                      </span>
                    </span>
                  </label>
                )
              })}
            </div>
          ))}
        </div>
      ) : (
        <div className="rounded-md border border-neutral-300 bg-neutral-50 p-3 text-xs text-neutral-700">
          {loading ? '流域清单加载中' : emptyReason}
        </div>
      )}
    </section>
  )
}

function BasinPopup({ basin, onEnterAnalysis }: { basin: OverviewBasin; onEnterAnalysis: () => void }) {
  const disabled = Boolean(basin.unavailableReason || !basin.selectedBasinVersionId)
  return (
    <aside
      className="absolute right-[calc(var(--m11-right-panel-width)+1.5rem)] top-24 z-[200] w-[min(22.5rem,calc(100%-3rem))] rounded-md border border-neutral-300 bg-white shadow-lg max-xl:right-6"
      aria-label="流域信息弹窗"
      data-testid="m11-basin-popup"
    >
      <div className="border-b border-neutral-300 px-4 py-3">
        <div className="text-base font-semibold text-neutral-900">{basin.displayName}</div>
        <div className="mt-0.5 text-xs text-neutral-700">{basin.basinGroup ?? '未分组流域'}</div>
      </div>
      <dl className="grid grid-cols-[7rem_minmax(0,1fr)] gap-x-3 gap-y-2 px-4 py-3 text-sm">
        <dt className="text-neutral-700">流域面积</dt>
        <dd className="font-mono text-neutral-900">{formatArea(basin.areaKm2)}</dd>
        <dt className="text-neutral-700">模型河段数</dt>
        <dd className="font-mono text-neutral-900">{formatMetric(basin.riverCount)}</dd>
        <dt className="text-neutral-700">活跃模型版本</dt>
        <dd className="font-mono text-neutral-900">{basin.activeModelCount}</dd>
        <dt className="text-neutral-700">最新预报时间</dt>
        <dd className="min-w-0 truncate font-mono text-neutral-900">{formatDateTime(basin.latestForecastTime) ?? '-'}</dd>
      </dl>
      {basin.unavailableReason ? (
        <div className="mx-4 mb-3 flex items-start gap-2 rounded border border-warning/40 bg-neutral-50 p-2 text-xs text-neutral-700">
          <AlertTriangle className="mt-0.5 h-4 w-4 text-warning" aria-hidden="true" />
          {basin.unavailableReason}
        </div>
      ) : null}
      <div className="flex justify-end gap-2 border-t border-neutral-300 px-4 py-3">
        <Link
          to={modelAssetPlaceholderHref(basin)}
          className="inline-flex h-9 items-center gap-1 rounded border border-primary-600 px-3 text-sm font-medium text-primary-600 hover:bg-primary-50"
        >
          查看详情
          <ExternalLink className="h-4 w-4" aria-hidden="true" />
        </Link>
        {disabled ? (
          <button
            type="button"
            disabled
            className="inline-flex h-9 cursor-not-allowed items-center gap-1 rounded bg-neutral-100 px-3 text-sm font-medium text-neutral-500"
          >
            进入分析
          </button>
        ) : (
          <button
            type="button"
            onClick={onEnterAnalysis}
            className="inline-flex h-9 items-center gap-1 rounded bg-primary-600 px-3 text-sm font-medium text-white hover:bg-primary-700"
          >
            进入分析
            <ArrowRight className="h-4 w-4" aria-hidden="true" />
          </button>
        )}
      </div>
    </aside>
  )
}

function SummaryLink({ to, title, description }: { to: string; title: string; description: string }) {
  return (
    <Link className="block rounded border border-neutral-300 p-3 transition-colors hover:bg-primary-50" to={to}>
      <span className="block text-sm font-medium text-neutral-900">{title}</span>
      <span className="mt-1 block text-xs text-neutral-700">{description}</span>
    </Link>
  )
}

function ScopedNotice({ children, tone = 'default' }: { children: string | null | undefined; tone?: 'default' | 'warning' }) {
  if (!children) return null
  return (
    <div
      className={cn(
        'rounded-md border p-3 text-xs text-neutral-700',
        tone === 'warning' ? 'border-warning/40 bg-primary-50' : 'border-neutral-300 bg-neutral-50',
      )}
    >
      {children}
    </div>
  )
}

function groupBasins(basins: OverviewBasin[]) {
  const groups = new Map<string, OverviewBasin[]>()
  basins.forEach((basin) => {
    const groupName = basin.basinGroup ?? '未分组流域'
    groups.set(groupName, [...(groups.get(groupName) ?? []), basin])
  })
  return [...groups.entries()].map(([name, groupBasins]) => ({ name, basins: groupBasins }))
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

// 进入流域分析的 query patch：写 basinId（就地切详情模式），并携带 basinVersionId/segmentId
// 上下文（与改造前 /basins/:id 深链的 carry-over 语义一致，仅落点从路由 param 变为 query basinId）。
function basinAnalysisPatch(basin: OverviewBasin, state: M11QueryState): M11QueryPatch {
  const selectedVersionIds = new Set(basin.basinVersions.map((version) => version.basinVersionId))
  const basinVersionId =
    state.basinVersionId && selectedVersionIds.has(state.basinVersionId)
      ? state.basinVersionId
      : basin.selectedBasinVersionId
  const carriesContext = Boolean(basinVersionId && basinVersionId === state.basinVersionId)
  return {
    basinId: basin.basinId,
    basinVersionId,
    riverNetworkVersionId: carriesContext ? state.riverNetworkVersionId : null,
    segmentId: carriesContext ? state.segmentId : null,
  }
}

export function contextHandoff(
  pathname: string,
  state: ReturnType<typeof parseM11QueryState>,
  sourceSelection: SourceScenarioSelectionState | null,
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
  state: ReturnType<typeof parseM11QueryState>,
  sourceSelection: SourceScenarioSelectionState | null,
): { source: 'gfs' | 'ifs' | 'best'; cycle: string | null; validTime: string | null; description: string } {
  if (state.source === 'compare') {
    return {
      source: defaultM11QueryState.source,
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

  const concreteSource = concreteSourceFromSelection(sourceSelection)
  if (!concreteSource) {
    return {
      source: defaultM11QueryState.source,
      cycle: null,
      validTime: null,
      description: '等待 Best Available 解析到具体源后带入上下文',
    }
  }

  return {
    source: concreteSource,
    cycle: sourceSelection?.cycleTime ?? state.cycle,
    validTime: sourceSelection?.validTime ?? state.validTime,
    description: `带入 ${concreteSource.toUpperCase()} source/cycle/validTime 上下文`,
  }
}

function concreteSourceFromSelection(sourceSelection: SourceScenarioSelectionState | null): 'gfs' | 'ifs' | null {
  if (sourceSelection?.resolvedSource === 'GFS') return 'gfs'
  if (sourceSelection?.resolvedSource === 'IFS') return 'ifs'
  return null
}

function modelAssetPlaceholderHref(basin: OverviewBasin) {
  const params = new URLSearchParams({ basinId: basin.basinId })
  if (basin.selectedBasinVersionId) params.set('basinVersionId', basin.selectedBasinVersionId)
  return `/monitoring?${params.toString()}`
}

function formatArea(value: number | null | undefined) {
  return value === null || value === undefined ? '-' : `${value.toLocaleString('en-US')} km2`
}

function formatDateTime(value: string | null | undefined) {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return `${date.toISOString().slice(0, 16).replace('T', ' ')} UTC`
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

import { useEffect, useMemo, useState } from 'react'
import { Waves, X } from 'lucide-react'

import { ForecastChart } from '@/components/charts/ForecastChart'
import { formatIssueTime, M11_POPUP_GLASS } from '@/components/map/M11PopupChrome'
import { cn } from '@/lib/cn'
import {
  formatHydroMetRiverForecastMessage,
  formatHydroMetRiverForecastUiString,
  loadHydroMetRiverForecast,
  validateHydroMetRiverForecastForChart,
  type HydroMetRiverForecastProductIdentity,
  type HydroMetRiverForecastSegmentIdentity,
} from '@/lib/hydroMet/riverForecast'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import type { ForecastData } from '@/stores/forecast'
import { fetchHydroMetLatestProduct, type QhhLatestProduct } from '@/pages/hydroMet/bootstrap'

type ForecastSeries = ForecastData['series'][number]

/** 选中河段（来自地图 feature.properties / basinSegments）的必要身份字段。 */
export interface M11RiverPopupSegment {
  river_segment_id: string
  segment_id: string
  river_network_version_id: string
  basin_version_id: string
  name?: string | null
}

// 双源同轴：GFS/IFS 各占一条 series，固定配色（GFS 青、IFS 绿），不切换。
const DUAL_SOURCES: HydroMetSource[] = ['GFS', 'IFS']
// 字面量配色（GFS 青 / IFS 绿）：ForecastSeries.color 是 hex 字面量联合，须用 as const 收窄。
const SOURCE_COLOR = { GFS: '#22d3ee', IFS: '#34d399' } as const
const LOADING_COPY_DELAY_MS = 600

interface SourceResult {
  source: HydroMetSource
  series: ForecastSeries | null
  unit: string | null
  cycleTime: string | null
  issueTime: string | null
  availableIssueTimes: string[]
  reason: string | null
}

interface DualForecast {
  data: ForecastData | null
  results: SourceResult[]
}

function productIdentity(product: QhhLatestProduct): HydroMetRiverForecastProductIdentity {
  return {
    basin_version_id: product.basin_version_id,
    river_network_version_id: product.river_network_version_id,
    source_id: product.source_id as HydroMetSource,
    cycle_time: product.cycle_time,
    river_valid_time_start: product.river_valid_time_start,
    river_valid_time_end: product.river_valid_time_end,
    valid_time_start: product.valid_time_start,
    valid_time_end: product.valid_time_end,
    available_horizon_hours: product.available_horizon_hours,
    expected_horizon_hours: product.expected_horizon_hours,
    shorter_horizon: product.shorter_horizon,
  }
}

function segmentIdentity(segment: M11RiverPopupSegment): HydroMetRiverForecastSegmentIdentity {
  return {
    river_segment_id: segment.river_segment_id || segment.segment_id,
    segment_id: segment.segment_id || segment.river_segment_id,
    river_network_version_id: segment.river_network_version_id,
    basin_version_id: segment.basin_version_id,
    name: formatHydroMetRiverForecastUiString(segment.name || segment.river_segment_id || segment.segment_id, {
      fallback: segment.river_segment_id || segment.segment_id,
    }),
  }
}

/** 单源解析 latest-product → 取 forecast-series → 契约校验 → 导出一条 ForecastSeries（失败给 honest reason）。 */
async function loadSource(
  basinId: string,
  source: HydroMetSource,
  segment: HydroMetRiverForecastSegmentIdentity,
  cycle: string | null,
): Promise<SourceResult> {
  const empty: SourceResult = { source, series: null, unit: null, cycleTime: null, issueTime: null, availableIssueTimes: [], reason: null }
  try {
    const product = await fetchHydroMetLatestProduct({ source, cycle, basinId })
    if (!product) return { ...empty, reason: `${source}：暂无 latest-product` }
    const availableIssueTimes = product.available_issue_times ?? []
    // honest 红线：所选起报时次滑出后端候选窗口时，latest-product 会静默回退到最新一轮（rows[0]）；
    // 显式拒绝串档——返回的 cycle 与请求不一致即诚实标注不可用，绝不画错时次的数据。
    if (cycle && new Date(product.cycle_time).getTime() !== new Date(cycle).getTime()) {
      return { ...empty, availableIssueTimes, reason: `${source}：起报 ${formatIssueTime(cycle)} 已不可用` }
    }
    const identity = productIdentity(product)
    const response = await loadHydroMetRiverForecast({ product: identity, segment })
    const validation = validateHydroMetRiverForecastForChart(response, identity, segment)
    if (!validation.ok) return { ...empty, availableIssueTimes, reason: `${source}：${validation.messages[0] ?? '契约校验失败'}` }
    const points = validation.renderedPoints.map((point) => ({ time: point.timestamp, value: point.value }))
    return {
      source,
      unit: validation.unit,
      cycleTime: validation.cycleTime ?? product.cycle_time,
      issueTime: validation.issueTime ?? validation.cycleTime ?? product.cycle_time,
      availableIssueTimes,
      reason: null,
      series: {
        scenario: validation.scenarioId,
        source: validation.sourceId,
        isAnalysis: false,
        label: source,
        color: SOURCE_COLOR[source],
        cycleTime: validation.cycleTime,
        availableLeadHours: validation.series.availableLeadHours,
        points,
      },
    }
  } catch (error) {
    return { ...empty, reason: `${source}：${formatHydroMetRiverForecastMessage(error, 'forecast-series 不可用')}` }
  }
}

function buildDualForecast(segment: HydroMetRiverForecastSegmentIdentity, results: SourceResult[]): DualForecast {
  const series = results.map((result) => result.series).filter((value): value is ForecastSeries => value !== null)
  if (series.length === 0) return { data: null, results }
  const primary = results.find((result) => result.series) ?? results[0]
  return {
    results,
    data: {
      segmentId: segment.river_segment_id,
      basinVersionId: segment.basin_version_id,
      riverNetworkVersionId: segment.river_network_version_id,
      cycle: primary.cycleTime ?? '',
      issueTime: primary.issueTime ?? primary.cycleTime ?? '',
      unit: primary.unit ?? 'm3/s',
      sourceAttribution: series.map((item) => item.source ?? item.scenario).join(' + '),
      cycleAttribution: primary.issueTime ?? primary.cycleTime ?? '',
      series,
    },
  }
}

/**
 * 河段 q_down 预报面板（M26 单页全屏）：屏幕居中 16:9 玻璃窗（点河段后于屏幕中央展开）。
 * GFS + IFS 同一坐标轴同时渲染、不做切换；滚轮缩放时间轴（以光标所在时刻为中心）。
 * honest 红线：每个源契约校验失败/无产品 → 列出原因，绝不绘制；两源皆无 → honest 空态。
 */
export function M11RiverForecastPanel({
  basinId,
  segment,
  onClose,
}: {
  basinId: string | null
  segment: M11RiverPopupSegment
  onClose?: () => void
}) {
  const identity = useMemo(
    () => segmentIdentity(segment),
    [segment.river_segment_id, segment.segment_id, segment.river_network_version_id, segment.basin_version_id, segment.name],
  )
  const [loading, setLoading] = useState(true)
  const [showLoadingCopy, setShowLoadingCopy] = useState(false)
  const [forecast, setForecast] = useState<DualForecast>({ data: null, results: [] })
  // 起报时间按河段身份绑定：换河段（identity 变）时所选 cycle 自动失效回最新，
  // 派生而非 reset effect —— 规避 setState-in-effect 触发的双重加载。双源时次一致 → 单一选择器同步切换。
  const [selection, setSelection] = useState<{ key: string; cycle: string } | null>(null)
  const identityKey = identity.river_segment_id
  const selectedCycle = selection && selection.key === identityKey ? selection.cycle : null
  // 可选起报列表从已加载结果派生（双源一致取首个非空）；forecast 重载期间不清空 → 下拉不闪烁。
  const issueTimes = useMemo(
    () => forecast.results.find((result) => result.availableIssueTimes.length > 0)?.availableIssueTimes ?? [],
    [forecast.results],
  )

  useEffect(() => {
    if (!loading) {
      setShowLoadingCopy(false)
      return
    }
    const timer = window.setTimeout(() => setShowLoadingCopy(true), LOADING_COPY_DELAY_MS)
    return () => window.clearTimeout(timer)
  }, [loading])

  useEffect(() => {
    if (!basinId) {
      setForecast({ data: null, results: [] })
      setLoading(false)
      return
    }
    let cancelled = false
    setLoading(true)
    void Promise.all(DUAL_SOURCES.map((source) => loadSource(basinId, source, identity, selectedCycle))).then((results) => {
      if (cancelled) return
      setForecast(buildDualForecast(identity, results))
      setLoading(false)
    })
    return () => {
      cancelled = true
    }
  }, [basinId, identity, selectedCycle])

  const failedReasons = forecast.results.filter((result) => result.reason).map((result) => result.reason as string)
  const showInitialLoading = loading && !forecast.data && showLoadingCopy

  return (
    <aside
      className={cn(
        'absolute left-1/2 top-1/2 z-[130] flex aspect-video w-[min(44rem,46vw)] max-h-[82vh] -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden',
        M11_POPUP_GLASS,
      )}
      data-testid="m11-river-forecast-panel"
    >
      <div className="h-px shrink-0 bg-gradient-to-r from-transparent via-cyan-400/60 to-transparent" aria-hidden="true" />
      <header className="flex shrink-0 items-start justify-between gap-2.5 border-b border-white/10 px-4 py-3">
        <div className="flex min-w-0 items-start gap-2.5">
          <span className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-cyan-400/10 text-cyan-300 ring-1 ring-inset ring-cyan-400/30">
            <Waves className="h-4 w-4" aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold leading-tight text-slate-50" title={identity.name}>
              {identity.river_segment_id} · {identity.name}
            </div>
            <div className="mt-0.5 text-[11px] uppercase tracking-[0.14em] text-cyan-300/80">河段 q_down 流量预报 · GFS+IFS</div>
          </div>
        </div>
        {onClose ? (
          <button
            type="button"
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-slate-400 transition-colors hover:bg-white/10 hover:text-slate-100"
            aria-label="关闭面板"
            onClick={onClose}
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        ) : null}
      </header>

      {issueTimes.length > 0 ? (
        <div className="flex shrink-0 items-center gap-2 border-b border-white/10 px-4 py-2 text-[11px] text-slate-400" data-testid="m11-river-panel-cycle-bar">
          <span className="shrink-0 uppercase tracking-wide">起报</span>
          <select
            aria-label="起报时间选择"
            data-testid="m11-river-panel-cycle"
            className="h-7 min-w-0 max-w-[12rem] cursor-pointer appearance-none rounded-md border border-white/15 bg-white/10 px-2 font-mono text-[11px] text-slate-100 transition-colors [color-scheme:dark] hover:border-cyan-400/50 focus:border-cyan-400 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
            value={selectedCycle && issueTimes.includes(selectedCycle) ? selectedCycle : issueTimes[0]}
            onChange={(event) => setSelection({ key: identityKey, cycle: event.target.value })}
            disabled={loading}
          >
            {issueTimes.map((time) => (
              <option key={time} value={time}>
                {formatIssueTime(time)}
              </option>
            ))}
          </select>
          <span className="ml-auto text-[10px] text-slate-500">GFS + IFS 同步切换</span>
        </div>
      ) : null}

      {showInitialLoading ? (
        <div className="flex flex-1 items-center justify-center text-sm text-slate-300" role="status" data-testid="m11-river-panel-loading">
          正在加载 GFS / IFS q_down forecast-series...
        </div>
      ) : forecast.data ? (
        <div className="flex min-h-0 flex-1 flex-col px-3 pb-2 pt-2.5">
          <div className="flex shrink-0 items-center gap-3 px-1 pb-1.5">
            {DUAL_SOURCES.map((source) => {
              const ok = forecast.results.some((result) => result.source === source && result.series)
              return (
                <span key={source} className={cn('inline-flex items-center gap-1.5 text-[11px]', ok ? 'text-slate-200' : 'text-slate-500 line-through')}>
                  <span className="h-2 w-3.5 rounded-sm" style={{ backgroundColor: SOURCE_COLOR[source] }} aria-hidden="true" />
                  {source}
                </span>
              )
            })}
            <span className="ml-auto text-[10px] text-slate-500">滚轮缩放时间轴</span>
          </div>
          <div className="min-h-0 flex-1" data-testid="m11-river-panel-chart">
            <ForecastChart data={forecast.data} segmentName={identity.name} variant="compact" appearance="dark" zoomable fill />
          </div>
          {failedReasons.length > 0 ? (
            <p className="shrink-0 px-1 pt-1 text-[10px] text-amber-300/80" data-testid="m11-river-panel-partial">
              {failedReasons.join('；')}
            </p>
          ) : null}
        </div>
      ) : loading ? (
        <div className="flex flex-1" aria-busy="true" data-testid="m11-river-panel-pending" />
      ) : (
        <div className="m-4 flex-1 rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-sm text-amber-100" role="status" data-testid="m11-river-panel-empty">
          {failedReasons.length > 0 ? (
            <ul className="space-y-1">
              {failedReasons.map((reason, index) => (
                <li key={`${index}-${reason}`}>{reason}</li>
              ))}
            </ul>
          ) : (
            basinId ? '暂无 q_down 预报数据' : '请选择流域'
          )}
        </div>
      )}
    </aside>
  )
}

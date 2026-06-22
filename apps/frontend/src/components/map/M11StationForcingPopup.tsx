import { useEffect, useMemo, useState } from 'react'
import ReactEChartsCore from 'echarts-for-react/lib/core'
import { CloudRain } from 'lucide-react'

import { echarts } from '@/components/charts/echartsCore'
import {
  formatIssueTime,
  M11_POPUP_GLASS,
  M11PopupEmpty,
  M11PopupHeader,
  M11PopupLoading,
} from '@/components/map/M11PopupChrome'
import { cn } from '@/lib/cn'
import {
  HYDRO_MET_STATION_SERIES_API_TUPLE_LIMIT,
  HYDRO_MET_STATION_VARIABLES,
  formatHydroMetStationSeriesMessage,
  isHydroMetStationSeriesRetainedDiskMiss,
  loadHydroMetStationSeries,
  mapUniqueHydroMetStationSeries,
  stationSeriesRequestKey,
  validateHydroMetStationSeriesForChart,
  validateHydroMetStationSeriesIdentity,
  type ChartableStationSeriesPoint,
  type HydroMetStationSeriesProductIdentity,
  type HydroMetStationSeriesRecord,
  type HydroMetStationSeriesResponse,
  type HydroMetStationSeriesVariable,
  type StationSeriesValidation,
} from '@/lib/hydroMet/stationSeries'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import { fetchHydroMetLatestProduct, type QhhLatestProduct } from '@/pages/hydroMet/bootstrap'

/** 选中代站（来自地图 feature.properties + 坐标）的必要字段。 */
export interface M11StationPopupStation {
  station_id: string
  station_name?: string | null
}

type StationSourceResult = {
  source: HydroMetSource
  product: QhhLatestProduct | null
  response: HydroMetStationSeriesResponse | null
  availableIssueTimes: string[]
  reason: string | null
}

type StationPanelState = {
  loading: boolean
  results: StationSourceResult[]
}

type ValidVariableSeries = {
  source: HydroMetSource
  unit: string
  validation: Extract<StationSeriesValidation, { ok: true }>
}

const DUAL_SOURCES: HydroMetSource[] = ['GFS', 'IFS']
const SOURCE_COLOR = { GFS: '#22d3ee', IFS: '#34d399' } as const

function stationSeriesProductIdentity(product: QhhLatestProduct): HydroMetStationSeriesProductIdentity {
  return {
    forcing_version_id: product.forcing_version_id,
    model_id: product.model_id,
    source_id: product.source_id,
    cycle_time: product.cycle_time,
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function seriesByVariable(response: HydroMetStationSeriesResponse) {
  const list = isRecord(response) && Array.isArray(response.series) ? response.series : []
  return mapUniqueHydroMetStationSeries(list)
}

function retainedDiskMissMessage(source: HydroMetSource, cycleLabel: string) {
  return `${source}：该起报 ${cycleLabel} 的 station-series 已不在当前磁盘保留窗口内`
}

async function loadSource(
  basinId: string,
  source: HydroMetSource,
  station: M11StationPopupStation,
  cycle: string | null,
): Promise<StationSourceResult> {
  const empty: StationSourceResult = { source, product: null, response: null, availableIssueTimes: [], reason: null }
  try {
    const product = await fetchHydroMetLatestProduct({ source, cycle, basinId })
    const availableIssueTimes = product.available_issue_times ?? []
    if (cycle && new Date(product.cycle_time).getTime() !== new Date(cycle).getTime()) {
      return { ...empty, product, availableIssueTimes, reason: `${source}：起报 ${formatIssueTime(cycle)} 已不可用` }
    }

    const identity = stationSeriesProductIdentity(product)
    const requestKey = stationSeriesRequestKey(identity, station.station_id)
    const response = await loadHydroMetStationSeries({
      product: identity,
      station: { station_id: station.station_id },
      limit: HYDRO_MET_STATION_SERIES_API_TUPLE_LIMIT,
    })
    const identityMessages = validateHydroMetStationSeriesIdentity(response, identity, station.station_id)
    if (identityMessages.length > 0) {
      return { ...empty, product, availableIssueTimes, reason: `${source}：${identityMessages[0]}` }
    }
    return { source, product, response, availableIssueTimes, reason: null }
  } catch (error) {
    const cycleLabel = cycle ? formatIssueTime(cycle) : 'latest'
    if (isHydroMetStationSeriesRetainedDiskMiss(error)) {
      return { ...empty, reason: retainedDiskMissMessage(source, cycleLabel) }
    }
    return { ...empty, reason: `${source}：${formatHydroMetStationSeriesMessage(error, 'station-series 不可用')}` }
  }
}

/**
 * 代站五要素 forcing 曲线面板（M26 全屏单页）。与河段流量曲线同样使用居中 16:9 玻璃窗。
 * 每次只展示一个要素；该要素内 GFS + IFS 同轴双绘，不做 source 切换。
 * honest 红线：单源失败只列 partial reason；两源都不可绘制才进入空态，绝不绘制错身份数据。
 */
export function M11StationForcingPopup({
  basinId,
  station,
  onClose,
}: {
  basinId: string | null
  initialSource: HydroMetSource | null
  station: M11StationPopupStation
  onClose?: () => void
}) {
  const [state, setState] = useState<StationPanelState>({ loading: true, results: [] })
  const [selectedVariable, setSelectedVariable] = useState<HydroMetStationSeriesVariable>('PRCP')
  // 起报时间按代站绑定：换站后选中的 cycle 自动失效回最新。
  const [selection, setSelection] = useState<{ key: string; cycle: string } | null>(null)
  const selectedCycle = selection && selection.key === station.station_id ? selection.cycle : null

  useEffect(() => {
    if (!basinId) {
      setState({ loading: false, results: [] })
      return
    }
    let cancelled = false
    setState((current) => ({ loading: true, results: current.results }))
    void Promise.all(DUAL_SOURCES.map((source) => loadSource(basinId, source, station, selectedCycle))).then((results) => {
      if (cancelled) return
      setState({ loading: false, results })
    })
    return () => {
      cancelled = true
    }
  }, [basinId, selectedCycle, station])

  const issueTimes = useMemo(
    () => state.results.find((result) => result.availableIssueTimes.length > 0)?.availableIssueTimes ?? [],
    [state.results],
  )
  const failedReasons = state.results.filter((result) => result.reason).map((result) => result.reason as string)
  const showInitialLoading = state.loading && state.results.length === 0

  return (
    <aside
      className={cn(
        'absolute left-1/2 top-1/2 z-[130] flex aspect-video w-[min(44rem,46vw)] max-h-[82vh] -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden',
        M11_POPUP_GLASS,
      )}
      data-testid="m11-station-popup"
    >
      <div className="h-px shrink-0 bg-gradient-to-r from-transparent via-cyan-400/60 to-transparent" aria-hidden="true" />
      <M11PopupHeader
        icon={CloudRain}
        title={`${station.station_id}${station.station_name ? ` · ${station.station_name}` : ''}`}
        subtitle="气象代站五要素 forcing · GFS+IFS"
        onClose={onClose}
      />
      <StationVariableSelector
        issueTimes={issueTimes}
        selectedCycle={selectedCycle}
        onCycleChange={(cycle) => setSelection({ key: station.station_id, cycle })}
        disabled={state.loading}
        selected={selectedVariable}
        onChange={setSelectedVariable}
      />

      {!basinId ? (
        <M11PopupEmpty testId="m11-station-popup-no-product">请选择流域</M11PopupEmpty>
      ) : showInitialLoading ? (
        <M11PopupLoading testId="m11-station-popup-loading">正在加载 GFS / IFS {station.station_id} station-series...</M11PopupLoading>
      ) : (
        <StationForcingBody
          results={state.results}
          selectedVariable={selectedVariable}
          failedReasons={failedReasons}
          loading={state.loading}
        />
      )}
    </aside>
  )
}

function StationVariableSelector({
  issueTimes,
  selectedCycle,
  onCycleChange,
  disabled,
  selected,
  onChange,
}: {
  issueTimes: string[]
  selectedCycle: string | null
  onCycleChange: (cycle: string) => void
  disabled: boolean
  selected: HydroMetStationSeriesVariable
  onChange: (variable: HydroMetStationSeriesVariable) => void
}) {
  return (
    <div
      className="flex shrink-0 flex-wrap items-center gap-3 border-b border-white/10 px-4 py-2 text-[11px] text-slate-400"
      data-testid="m11-station-toolbar"
    >
      {issueTimes.length > 0 ? (
        <label className="flex shrink-0 items-center gap-2" data-testid="m11-station-cycle-bar">
          <span className="shrink-0 uppercase tracking-wide">起报</span>
          <select
            aria-label="起报时间选择"
            data-testid="m11-popup-issue-time"
            className="h-7 min-w-0 max-w-[12rem] cursor-pointer appearance-none rounded-md border border-white/15 bg-white/10 px-2 font-mono text-[11px] text-slate-100 transition-colors [color-scheme:dark] hover:border-cyan-400/50 focus:border-cyan-400 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
            value={selectedCycle && issueTimes.includes(selectedCycle) ? selectedCycle : issueTimes[0]}
            onChange={(event) => onCycleChange(event.target.value)}
            disabled={disabled}
          >
            {issueTimes.map((time) => (
              <option key={time} value={time}>
                {formatIssueTime(time)}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      <div
        className="flex min-w-0 flex-1 flex-wrap items-center justify-end gap-1.5"
        role="tablist"
        aria-label="代站变量选择"
        data-testid="m11-station-variable-selector"
      >
        {HYDRO_MET_STATION_VARIABLES.map((variable) => {
          const active = selected === variable
          return (
            <button
              key={variable}
              type="button"
              className={cn(
                'h-7 cursor-pointer rounded-md border px-2.5 text-xs font-medium leading-none transition-colors',
                active
                  ? 'border-cyan-400/50 bg-cyan-400/15 text-cyan-200'
                  : 'border-white/15 bg-white/5 text-slate-300 hover:bg-white/10',
              )}
              aria-selected={active}
              aria-pressed={active}
              role="tab"
              data-testid={`m11-station-variable-toggle-${variable}`}
              onClick={() => onChange(variable)}
            >
              {variable}
            </button>
          )
        })}
      </div>
    </div>
  )
}

function StationForcingBody({
  results,
  selectedVariable,
  failedReasons,
  loading,
}: {
  results: StationSourceResult[]
  selectedVariable: HydroMetStationSeriesVariable
  failedReasons: string[]
  loading: boolean
}) {
  const chartResult = useMemo(
    () => buildVariableChartResult(results, selectedVariable),
    [results, selectedVariable],
  )

  return (
    <div className="flex min-h-0 flex-1 flex-col px-3 pb-2 pt-2.5" data-testid="m11-station-popup-loaded">
      <div className="flex shrink-0 items-center gap-3 px-1 pb-1.5">
        {DUAL_SOURCES.map((source) => {
          const ok = chartResult.series.some((item) => item.source === source)
          return (
            <span key={source} className={cn('inline-flex items-center gap-1.5 text-[11px]', ok ? 'text-slate-200' : 'text-slate-500 line-through')}>
              <span className="h-2 w-3.5 rounded-sm" style={{ backgroundColor: SOURCE_COLOR[source] }} aria-hidden="true" />
              {source}
            </span>
          )
        })}
        {loading ? <span className="text-[10px] text-slate-500" data-testid="m11-station-panel-refreshing">刷新中</span> : null}
        <span className="ml-auto text-[10px] text-slate-500">单要素双源同轴</span>
      </div>
      {chartResult.series.length > 0 ? (
        <>
          <div className="flex min-h-0 flex-1 flex-col rounded-lg bg-white/[0.04] p-2 ring-1 ring-inset ring-white/10" data-testid={`m11-station-variable-${selectedVariable}-chart`}>
            <div className="flex shrink-0 flex-wrap items-start justify-between gap-1">
              <div className="text-xs font-semibold text-slate-100">
                {selectedVariable} · {chartResult.unitLabel}
              </div>
              <div className="flex flex-wrap gap-1 text-[11px]">
                {chartResult.badges.map((badge) => (
                  <span key={badge.key} className={badge.className} data-testid={badge.testId}>
                    {badge.label}
                  </span>
                ))}
              </div>
            </div>
            <div className="min-h-0 flex-1">
              <StationVariableEcharts variable={selectedVariable} unit={chartResult.unitLabel} series={chartResult.series} />
            </div>
          </div>
          {failedReasons.concat(chartResult.reasons).length > 0 ? (
            <p className="shrink-0 px-1 pt-1 text-[10px] text-amber-300/80" data-testid="m11-station-popup-partial">
              {failedReasons.concat(chartResult.reasons).join('；')}
            </p>
          ) : null}
        </>
      ) : (
        <M11PopupEmpty testId="m11-station-popup-empty">
          <ul className="space-y-1">
            {failedReasons.concat(chartResult.reasons).length > 0
              ? failedReasons.concat(chartResult.reasons).map((reason, index) => <li key={`${index}-${reason}`}>{reason}</li>)
              : <li>{selectedVariable} 暂无可绘制 station-series</li>}
          </ul>
        </M11PopupEmpty>
      )}
    </div>
  )
}

function buildVariableChartResult(results: StationSourceResult[], variable: HydroMetStationSeriesVariable) {
  const series: ValidVariableSeries[] = []
  const reasons: string[] = []
  for (const result of results) {
    if (!result.response) continue
    const record = seriesByVariable(result.response).get(variable)
    if (!record) {
      reasons.push(`${result.source}：变量 ${variable} 在 station-series 响应中缺失`)
      continue
    }
    const validation = validateHydroMetStationSeriesForChart(record)
    if (!validation.ok) {
      reasons.push(`${result.source}：${validation.messages.join('；')}`)
      continue
    }
    if (!validation.unit) {
      reasons.push(`${result.source}：变量 ${variable} 缺少 unit 元数据，停止绘图`)
      continue
    }
    if (validation.renderedPoints.length === 0) {
      reasons.push(`${result.source}：变量 ${variable} 没有可绘制点`)
      continue
    }
    series.push({ source: result.source, unit: validation.unit, validation })
  }
  const units = Array.from(new Set(series.map((item) => item.unit)))
  return {
    series,
    reasons,
    unitLabel: units.length <= 1 ? (units[0] ?? '-') : units.join(' / '),
    badges: buildVariableBadges(variable, series),
  }
}

function buildVariableBadges(variable: HydroMetStationSeriesVariable, series: ValidVariableSeries[]) {
  return series.flatMap((item) => {
    const validation = item.validation
    const truncated = validation.seriesTruncated || validation.metadata.truncated
    const badges: Array<{ key: string; label: string; testId: string; className: string }> = []
    if (validation.nonOkFlags.length > 0) {
      badges.push({
        key: `${item.source}-qc`,
        label: `${item.source} QC ${validation.nonOkFlags.join(', ')}${validation.nonOkFlagsCapped ? ', ...' : ''}`,
        testId: `m11-station-variable-${variable}-${item.source}-qc`,
        className: 'rounded border border-amber-400/40 bg-amber-400/10 px-1.5 py-0.5 text-amber-200',
      })
    }
    if (truncated) {
      badges.push({
        key: `${item.source}-truncated`,
        label: `${item.source} truncated`,
        testId: `m11-station-variable-${variable}-${item.source}-truncated`,
        className: 'rounded border border-red-400/40 bg-red-400/10 px-1.5 py-0.5 text-red-300',
      })
    }
    if (validation.capped) {
      badges.push({
        key: `${item.source}-capped`,
        label: `${item.source} capped ${validation.renderedPoints.length}/${validation.reportedPointCount}`,
        testId: `m11-station-variable-${variable}-${item.source}-capped`,
        className: 'rounded border border-amber-400/40 bg-amber-400/10 px-1.5 py-0.5 text-amber-200',
      })
    }
    return badges
  })
}

function StationVariableEcharts({
  variable,
  unit,
  series,
}: {
  variable: HydroMetStationSeriesVariable
  unit: string
  series: ValidVariableSeries[]
}) {
  const option = useMemo(
    () => ({
      color: series.map((item) => SOURCE_COLOR[item.source]),
      grid: { left: 48, right: 16, top: 18, bottom: 28 },
      tooltip: {
        trigger: 'axis',
        renderMode: 'richText',
        backgroundColor: 'rgba(8, 14, 32, 0.92)',
        borderColor: 'rgba(34, 211, 238, 0.35)',
        textStyle: { color: '#e2e8f0' },
        valueFormatter: (value: number) => `${Number(value).toFixed(3)} ${unit}`,
      },
      legend: {
        right: 8,
        top: 0,
        textStyle: { color: '#cbd5e1', fontSize: 11 },
      },
      xAxis: {
        type: 'time',
        axisLabel: { color: '#94a3b8' },
        axisLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.25)' } },
      },
      yAxis: {
        type: 'value',
        name: unit,
        axisLabel: { color: '#94a3b8' },
        nameTextStyle: { color: '#94a3b8' },
        splitLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.14)' } },
      },
      dataZoom: [{ type: 'inside', xAxisIndex: 0, filterMode: 'none' }],
      series: series.map((item) => ({
        type: 'line',
        name: item.source,
        showSymbol: item.validation.renderedPoints.length <= 48,
        symbolSize: 4,
        smooth: true,
        lineStyle: { width: 2, shadowBlur: 8, shadowColor: `${SOURCE_COLOR[item.source]}55`, shadowOffsetY: 2 },
        areaStyle: {
          opacity: 0.08,
        },
        data: item.validation.renderedPoints.map((point: ChartableStationSeriesPoint) => [point.timestamp, point.value]),
      })),
    }),
    [series, unit],
  )

  return <ReactEChartsCore echarts={echarts} option={option} notMerge lazyUpdate style={{ height: '100%', minHeight: 0, width: '100%' }} />
}

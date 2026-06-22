import { useEffect, useMemo, useState } from 'react'
import ReactEChartsCore from 'echarts-for-react/lib/core'
import { CloudRain } from 'lucide-react'

import { echarts } from '@/components/charts/echartsCore'
import {
  M11PopupEmpty,
  M11PopupHeader,
  M11PopupLoading,
  M11PopupShell,
  M11PopupSourceControls,
} from '@/components/map/M11PopupChrome'
import { useHydroMetPopupProduct } from '@/components/map/useHydroMetPopupProduct'
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
} from '@/lib/hydroMet/stationSeries'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import type { QhhLatestProduct } from '@/pages/hydroMet/bootstrap'

/** 选中代站（来自地图 feature.properties + 坐标）的必要字段。 */
export interface M11StationPopupStation {
  station_id: string
  station_name?: string | null
}

type StationSeriesLoadState =
  | { kind: 'idle' }
  | { kind: 'loading'; requestKey: string }
  | { kind: 'loaded'; requestKey: string; response: HydroMetStationSeriesResponse }
  | { kind: 'error'; requestKey: string; message: string }
  | { kind: 'retention-missing'; requestKey: string; message: string; cycleTime: string }

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

function unavailableIssueTimeKey(source: string, stationId: string, issueTime: string) {
  return [source, stationId, issueTime].join('|')
}

function retainedDiskMissMessage(cycleTime: string) {
  return `该起报 ${cycleTime} 的 station-series 已不在当前磁盘保留窗口内；请选择其他起报时间或回到最新起报。`
}

/**
 * 代站五要素 forcing 曲线 popup（M26 全屏单页）。玻璃质感 + 弹窗内 source/起报/变量选择。
 * 变量选择：PRCP/TEMP/RH/wind/Rn 多选切换（默认全显），改选只显所选变量曲线。
 * source 切换 → 以新源重取 latest-product + station-series。
 * honest 红线：身份不符 / 任一无效点 / 缺 unit / 坏 metadata → 空态，绝不渲染 echarts；product=null → honest 空态。
 */
export function M11StationForcingPopup({
  basinId,
  initialSource,
  station,
  onClose,
}: {
  basinId: string | null
  initialSource: HydroMetSource | null
  station: M11StationPopupStation
  onClose?: () => void
}) {
  const popupProduct = useHydroMetPopupProduct({ basinId, initialSource })
  const { product } = popupProduct
  const [state, setState] = useState<StationSeriesLoadState>({ kind: 'idle' })
  const [unavailableIssueTimeKeys, setUnavailableIssueTimeKeys] = useState<Set<string>>(() => new Set())
  const [selectedVariables, setSelectedVariables] = useState<Set<HydroMetStationSeriesVariable>>(
    () => new Set(HYDRO_MET_STATION_VARIABLES),
  )

  useEffect(() => {
    setUnavailableIssueTimeKeys(new Set())
  }, [basinId, station.station_id])

  const toggleVariable = (variable: HydroMetStationSeriesVariable) => {
    setSelectedVariables((current) => {
      const next = new Set(current)
      if (next.has(variable)) {
        // 至少保留一个变量，避免全空态歧义。
        if (next.size > 1) next.delete(variable)
      } else {
        next.add(variable)
      }
      return next
    })
  }

  useEffect(() => {
    if (!product) {
      setState({ kind: 'idle' })
      return
    }
    const identity = stationSeriesProductIdentity(product)
    const requestKey = stationSeriesRequestKey(identity, station.station_id)
    let cancelled = false
    setState({ kind: 'loading', requestKey })
    void loadHydroMetStationSeries({
      product: identity,
      station: { station_id: station.station_id },
      limit: HYDRO_MET_STATION_SERIES_API_TUPLE_LIMIT,
    }).then(
      (response) => {
        if (!cancelled) setState({ kind: 'loaded', requestKey, response })
      },
      (error) => {
        if (!cancelled) {
          if (isHydroMetStationSeriesRetainedDiskMiss(error)) {
            setUnavailableIssueTimeKeys((current) => {
              const next = new Set(current)
              next.add(unavailableIssueTimeKey(identity.source_id, station.station_id, identity.cycle_time))
              return next
            })
            setState({
              kind: 'retention-missing',
              requestKey,
              cycleTime: identity.cycle_time,
              message: retainedDiskMissMessage(identity.cycle_time),
            })
          } else {
            setState({ kind: 'error', requestKey, message: formatHydroMetStationSeriesMessage(error, 'station-series 不可用') })
          }
        }
      },
    )
    return () => {
      cancelled = true
    }
  }, [product, station.station_id])

  const unavailableIssueTimes = useMemo(
    () =>
      popupProduct.issueTimes.filter((time) =>
        unavailableIssueTimeKeys.has(unavailableIssueTimeKey(popupProduct.source, station.station_id, time)),
      ),
    [popupProduct.issueTimes, popupProduct.source, station.station_id, unavailableIssueTimeKeys],
  )

  return (
    <M11PopupShell testId="m11-station-popup">
      <M11PopupHeader
        icon={CloudRain}
        title={`${station.station_id}${station.station_name ? ` · ${station.station_name}` : ''}`}
        subtitle="气象代站五要素 forcing"
        onClose={onClose}
      />
      <M11PopupSourceControls
        source={popupProduct.source}
        onSourceChange={popupProduct.setSource}
        issueTimes={popupProduct.issueTimes}
        issueTime={popupProduct.issueTime}
        unavailableIssueTimes={unavailableIssueTimes}
        onIssueTimeChange={popupProduct.setIssueTime}
      />
      <StationVariableSelector selected={selectedVariables} onToggle={toggleVariable} />

      {!product ? (
        <M11PopupEmpty testId="m11-station-popup-no-product">{popupProduct.reason ?? '等待 Best Available 解析'}</M11PopupEmpty>
      ) : state.kind === 'loading' ? (
        <M11PopupLoading testId="m11-station-popup-loading">正在加载 {station.station_id} 的 station-series...</M11PopupLoading>
      ) : state.kind === 'error' ? (
        <M11PopupEmpty testId="m11-station-popup-error">{state.message}</M11PopupEmpty>
      ) : state.kind === 'retention-missing' ? (
        <M11PopupEmpty testId="m11-station-popup-retention-missing">{state.message}</M11PopupEmpty>
      ) : state.kind === 'loaded' ? (
        <StationForcingBody
          product={product}
          stationId={station.station_id}
          response={state.response}
          selectedVariables={selectedVariables}
        />
      ) : null}
    </M11PopupShell>
  )
}

function StationVariableSelector({
  selected,
  onToggle,
}: {
  selected: Set<HydroMetStationSeriesVariable>
  onToggle: (variable: HydroMetStationSeriesVariable) => void
}) {
  return (
    <div
      className="flex flex-wrap items-center gap-1.5 border-b border-white/10 px-4 py-2"
      role="group"
      aria-label="代站变量选择"
      data-testid="m11-station-variable-selector"
    >
      {HYDRO_MET_STATION_VARIABLES.map((variable) => {
        const active = selected.has(variable)
        return (
          <button
            key={variable}
            type="button"
            className={cn(
              'cursor-pointer rounded-md border px-2 py-0.5 text-xs font-medium transition-colors',
              active
                ? 'border-cyan-400/50 bg-cyan-400/15 text-cyan-200'
                : 'border-white/15 bg-white/5 text-slate-300 hover:bg-white/10',
            )}
            aria-pressed={active}
            data-testid={`m11-station-variable-toggle-${variable}`}
            onClick={() => onToggle(variable)}
          >
            {variable}
          </button>
        )
      })}
    </div>
  )
}

function StationForcingBody({
  product,
  stationId,
  response,
  selectedVariables,
}: {
  product: QhhLatestProduct
  stationId: string
  response: HydroMetStationSeriesResponse
  selectedVariables: Set<HydroMetStationSeriesVariable>
}) {
  const identity = stationSeriesProductIdentity(product)
  const identityMessages = validateHydroMetStationSeriesIdentity(response, identity, stationId)
  const byVariable = useMemo(() => seriesByVariable(response), [response])

  if (identityMessages.length > 0) {
    return (
      <div className="px-4 py-3" data-testid="m11-station-popup-identity-mismatch">
        <M11PopupEmpty testId="m11-station-popup-identity-reasons">
          <div className="font-semibold">station-series identity 不一致</div>
          <ul className="mt-1 space-y-1">
            {identityMessages.map((message, index) => (
              <li key={`${index}-${message}`}>{message}</li>
            ))}
          </ul>
        </M11PopupEmpty>
      </div>
    )
  }

  return (
    <div className="max-h-[60vh] space-y-3 overflow-auto px-4 py-3" data-testid="m11-station-popup-loaded">
      {HYDRO_MET_STATION_VARIABLES.filter((variable) => selectedVariables.has(variable)).map((variable) => (
        <StationVariableChart key={variable} variable={variable} series={byVariable.get(variable)} />
      ))}
    </div>
  )
}

function StationVariableChart({
  variable,
  series,
}: {
  variable: HydroMetStationSeriesVariable
  series: HydroMetStationSeriesRecord | undefined
}) {
  if (!series) {
    return (
      <VariableEmpty variable={variable} testId={`m11-station-variable-${variable}-missing`}>
        变量 {variable} 在 station-series 响应中缺失。
      </VariableEmpty>
    )
  }

  const validation = validateHydroMetStationSeriesForChart(series)
  if (!validation.ok) {
    return (
      <VariableEmpty variable={variable} testId={`m11-station-variable-${variable}-invalid`}>
        {validation.messages.join('；')}
      </VariableEmpty>
    )
  }

  if (!validation.unit) {
    return (
      <VariableEmpty variable={variable} testId={`m11-station-variable-${variable}-missing-unit`}>
        变量 {variable} 缺少 unit 元数据，停止绘图。
      </VariableEmpty>
    )
  }

  if (validation.renderedPoints.length === 0) {
    return (
      <VariableEmpty variable={variable} testId={`m11-station-variable-${variable}-empty`}>
        变量 {variable} 没有可绘制点。
      </VariableEmpty>
    )
  }

  const truncated = validation.seriesTruncated || validation.metadata.truncated

  return (
    <div className="rounded-lg bg-white/[0.04] p-2 ring-1 ring-inset ring-white/10" data-testid={`m11-station-variable-${variable}-chart`}>
      <div className="flex flex-wrap items-start justify-between gap-1">
        <div className="text-xs font-semibold text-slate-100">
          {variable} · {validation.unit}
        </div>
        <div className="flex flex-wrap gap-1 text-[11px]">
          {validation.nonOkFlags.length > 0 ? (
            <span className="rounded border border-amber-400/40 bg-amber-400/10 px-1.5 py-0.5 text-amber-200" data-testid={`m11-station-variable-${variable}-qc`}>
              QC {validation.nonOkFlags.join(', ')}{validation.nonOkFlagsCapped ? ', ...' : ''}
            </span>
          ) : null}
          {truncated ? (
            <span className="rounded border border-red-400/40 bg-red-400/10 px-1.5 py-0.5 text-red-300" data-testid={`m11-station-variable-${variable}-truncated`}>
              truncated
            </span>
          ) : null}
          {validation.capped ? (
            <span className="rounded border border-amber-400/40 bg-amber-400/10 px-1.5 py-0.5 text-amber-200" data-testid={`m11-station-variable-${variable}-capped`}>
              capped {validation.renderedPoints.length}/{validation.reportedPointCount}
            </span>
          ) : null}
        </div>
      </div>
      <StationVariableEcharts variable={variable} unit={validation.unit} points={validation.renderedPoints} />
      <div className="mt-1 text-[11px] text-slate-400" data-testid={`m11-station-variable-${variable}-metadata`}>
        returned {validation.metadata.returned_points} / limit {validation.metadata.limit}; rendered {validation.renderedPoints.length}; quality_flag {validation.qualitySummary}
      </div>
    </div>
  )
}

function StationVariableEcharts({
  variable,
  unit,
  points,
}: {
  variable: HydroMetStationSeriesVariable
  unit: string
  points: ChartableStationSeriesPoint[]
}) {
  const option = useMemo(
    () => ({
      color: ['#22d3ee'],
      grid: { left: 44, right: 12, top: 10, bottom: 28 },
      tooltip: {
        trigger: 'axis',
        renderMode: 'richText',
        backgroundColor: 'rgba(8, 14, 32, 0.92)',
        borderColor: 'rgba(34, 211, 238, 0.35)',
        textStyle: { color: '#e2e8f0' },
        valueFormatter: (value: number) => `${Number(value).toFixed(3)} ${unit}`,
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
      series: [
        {
          type: 'line',
          name: variable,
          showSymbol: points.length <= 48,
          symbolSize: 4,
          smooth: true,
          lineStyle: { width: 2, shadowBlur: 8, shadowColor: 'rgba(34, 211, 238, 0.35)', shadowOffsetY: 2 },
          areaStyle: {
            color: {
              type: 'linear',
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: 'rgba(34, 211, 238, 0.22)' },
                { offset: 1, color: 'rgba(34, 211, 238, 0.02)' },
              ],
            },
          },
          data: points.map((point) => [point.timestamp, point.value]),
        },
      ],
    }),
    [points, unit, variable],
  )

  return <ReactEChartsCore echarts={echarts} option={option} notMerge lazyUpdate style={{ height: 160, width: '100%' }} />
}

function VariableEmpty({
  variable,
  testId,
  children,
}: {
  variable: HydroMetStationSeriesVariable
  testId: string
  children: React.ReactNode
}) {
  return (
    <div className="rounded-lg border border-dashed border-white/15 bg-white/5 p-2 text-xs text-slate-300" data-testid={testId}>
      <div className="font-semibold text-slate-100">{variable}</div>
      <div className="mt-1">{children}</div>
    </div>
  )
}

import { useEffect, useMemo, useState } from 'react'
import ReactEChartsCore from 'echarts-for-react/lib/core'
import { CloudRain, Loader2, X } from 'lucide-react'

import { echarts } from '@/components/charts/echartsCore'
import {
  HYDRO_MET_STATION_SERIES_LIMIT,
  HYDRO_MET_STATION_VARIABLES,
  formatHydroMetStationSeriesMessage,
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

// 从 QhhLatestProduct 构造 station-series product identity（照搬 HydroMetPage 的派生口径）。
function stationSeriesProductIdentity(product: QhhLatestProduct): HydroMetStationSeriesProductIdentity {
  return {
    forcing_version_id: product.forcing_version_id,
    source_id: product.source_id,
    cycle_time: product.cycle_time,
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

// 复用金标准 mapUniqueHydroMetStationSeries：仅保留 MVP 变量、去重、保 record 类型。
function seriesByVariable(response: HydroMetStationSeriesResponse) {
  const list = isRecord(response) && Array.isArray(response.series) ? response.series : []
  return mapUniqueHydroMetStationSeries(list)
}

/**
 * 代站六要素 forcing 曲线 popup（M26-4）。逻辑照搬 HydroMetPage station 段：
 * loadHydroMetStationSeries → validateHydroMetStationSeriesIdentity → 逐变量
 * validateHydroMetStationSeriesForChart（同源金标准）→ 渲染六要素 echarts。
 * honest 红线：身份不符 / 任一无效点 / 缺 unit / 坏 metadata → 空态，绝不渲染 echarts。
 * product=null（best 未解析 / 无 basin）→ honest 空态。
 */
export function M11StationForcingPopup({
  product,
  station,
  productReason,
  onClose,
}: {
  product: QhhLatestProduct | null
  station: M11StationPopupStation
  productReason?: string | null
  onClose?: () => void
}) {
  const [state, setState] = useState<StationSeriesLoadState>({ kind: 'idle' })

  useEffect(() => {
    if (!product) {
      setState({ kind: 'idle' })
      return
    }
    const identity = stationSeriesProductIdentity(product)
    const requestKey = stationSeriesRequestKey(identity, station.station_id)
    let cancelled = false
    setState({ kind: 'loading', requestKey })
    void loadHydroMetStationSeries({ product: identity, station: { station_id: station.station_id }, limit: HYDRO_MET_STATION_SERIES_LIMIT }).then(
      (response) => {
        if (!cancelled) setState({ kind: 'loaded', requestKey, response })
      },
      (error) => {
        if (!cancelled) {
          setState({
            kind: 'error',
            requestKey,
            message: formatHydroMetStationSeriesMessage(error, 'station-series 不可用'),
          })
        }
      },
    )
    return () => {
      cancelled = true
    }
  }, [product, station.station_id])

  return (
    <div className="w-[min(26rem,80vw)]" data-testid="m11-station-popup">
      <PopupHeader
        title={`${station.station_id}${station.station_name ? ` · ${station.station_name}` : ''}`}
        subtitle="气象代站六要素 forcing"
        onClose={onClose}
      />

      {!product ? (
        <EmptyState testId="m11-station-popup-no-product">{productReason ?? '等待 Best Available 解析'}</EmptyState>
      ) : state.kind === 'loading' ? (
        <LoadingState testId="m11-station-popup-loading">正在加载 {station.station_id} 的 station-series...</LoadingState>
      ) : state.kind === 'error' ? (
        <EmptyState testId="m11-station-popup-error">{state.message}</EmptyState>
      ) : state.kind === 'loaded' ? (
        <StationForcingBody product={product} stationId={station.station_id} response={state.response} />
      ) : null}
    </div>
  )
}

function StationForcingBody({
  product,
  stationId,
  response,
}: {
  product: QhhLatestProduct
  stationId: string
  response: HydroMetStationSeriesResponse
}) {
  const identity = stationSeriesProductIdentity(product)
  const identityMessages = validateHydroMetStationSeriesIdentity(response, identity, stationId)
  const byVariable = useMemo(() => seriesByVariable(response), [response])

  if (identityMessages.length > 0) {
    // honest 红线：身份不符 → 空态，绝不绘制曲线。
    return (
      <div className="px-4 py-3" data-testid="m11-station-popup-identity-mismatch">
        <EmptyState testId="m11-station-popup-identity-reasons">
          <div className="font-semibold">station-series identity 不一致</div>
          <ul className="mt-1 space-y-1">
            {identityMessages.map((message, index) => (
              <li key={`${index}-${message}`}>{message}</li>
            ))}
          </ul>
        </EmptyState>
      </div>
    )
  }

  return (
    <div className="max-h-[60vh] space-y-3 overflow-auto px-4 py-3" data-testid="m11-station-popup-loaded">
      {HYDRO_MET_STATION_VARIABLES.map((variable) => (
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

  // honest 红线：调金标准校验器；任一无效点 / 坏 metadata / 坏 unit → ok:false → 空态，绝不画曲线。
  const validation = validateHydroMetStationSeriesForChart(series)
  if (!validation.ok) {
    return (
      <VariableEmpty variable={variable} testId={`m11-station-variable-${variable}-invalid`}>
        {validation.messages.join('；')}
      </VariableEmpty>
    )
  }

  // 缺 unit 门控（与金标准一致）：缺少 unit 元数据 → 停止绘图。
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
    <div className="rounded-md border border-neutral-300 p-2" data-testid={`m11-station-variable-${variable}-chart`}>
      <div className="flex flex-wrap items-start justify-between gap-1">
        <div className="text-xs font-semibold text-neutral-900">
          {variable} · {validation.unit}
        </div>
        <div className="flex flex-wrap gap-1 text-[11px]">
          {validation.nonOkFlags.length > 0 ? (
            <span className="rounded border border-warning/50 bg-warning/10 px-1.5 py-0.5 text-neutral-900" data-testid={`m11-station-variable-${variable}-qc`}>
              QC {validation.nonOkFlags.join(', ')}{validation.nonOkFlagsCapped ? ', ...' : ''}
            </span>
          ) : null}
          {truncated ? (
            <span className="rounded border border-danger/40 bg-danger/10 px-1.5 py-0.5 text-danger" data-testid={`m11-station-variable-${variable}-truncated`}>
              truncated
            </span>
          ) : null}
          {validation.capped ? (
            <span className="rounded border border-warning/50 bg-warning/10 px-1.5 py-0.5 text-neutral-900" data-testid={`m11-station-variable-${variable}-capped`}>
              capped {validation.renderedPoints.length}/{validation.reportedPointCount}
            </span>
          ) : null}
        </div>
      </div>
      <StationVariableEcharts variable={variable} unit={validation.unit} points={validation.renderedPoints} />
      <div className="mt-1 text-[11px] text-neutral-700" data-testid={`m11-station-variable-${variable}-metadata`}>
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
      color: ['#0f8fbf'],
      grid: { left: 44, right: 12, top: 10, bottom: 28 },
      tooltip: {
        trigger: 'axis',
        renderMode: 'richText',
        valueFormatter: (value: number) => `${Number(value).toFixed(3)} ${unit}`,
      },
      xAxis: { type: 'time', axisLabel: { color: '#64748b' } },
      yAxis: { type: 'value', name: unit, axisLabel: { color: '#64748b' } },
      series: [
        {
          type: 'line',
          name: variable,
          showSymbol: points.length <= 48,
          symbolSize: 4,
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
    <div className="rounded-md border border-dashed border-neutral-300 bg-neutral-50 p-2 text-xs text-neutral-700" data-testid={testId}>
      <div className="font-semibold text-neutral-900">{variable}</div>
      <div className="mt-1">{children}</div>
    </div>
  )
}

function PopupHeader({ title, subtitle, onClose }: { title: string; subtitle: string; onClose?: () => void }) {
  return (
    <div className="flex items-start justify-between gap-2 border-b border-neutral-300 px-4 py-3">
      <div className="min-w-0">
        <div className="flex items-center gap-2 text-sm font-semibold text-neutral-900">
          <CloudRain className="h-4 w-4 shrink-0 text-primary-600" aria-hidden="true" />
          <span className="truncate" title={title}>
            {title}
          </span>
        </div>
        <div className="mt-0.5 text-xs text-neutral-700">{subtitle}</div>
      </div>
      {onClose ? (
        <button
          type="button"
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded text-neutral-500 hover:bg-neutral-100"
          aria-label="关闭弹窗"
          onClick={onClose}
        >
          <X className="h-4 w-4" aria-hidden="true" />
        </button>
      ) : null}
    </div>
  )
}

function LoadingState({ children, testId }: { children: React.ReactNode; testId: string }) {
  return (
    <div
      className="m-4 flex items-center gap-2 rounded border border-neutral-300 bg-neutral-50 p-3 text-sm text-neutral-700"
      role="status"
      data-testid={testId}
    >
      <Loader2 className="h-4 w-4 animate-spin text-primary-600" aria-hidden="true" />
      {children}
    </div>
  )
}

function EmptyState({ children, testId }: { children: React.ReactNode; testId: string }) {
  return (
    <div className="m-4 rounded border border-warning/40 bg-warning/10 p-3 text-sm text-neutral-900" role="status" data-testid={testId}>
      {children}
    </div>
  )
}

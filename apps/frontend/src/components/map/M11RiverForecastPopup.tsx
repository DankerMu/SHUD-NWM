import { useEffect, useState } from 'react'
import { Waves } from 'lucide-react'

import { ForecastChart } from '@/components/charts/ForecastChart'
import { cn } from '@/lib/cn'
import {
  M11PopupEmpty,
  M11PopupHeader,
  M11PopupLoading,
  M11PopupShell,
  M11PopupSourceControls,
} from '@/components/map/M11PopupChrome'
import { useHydroMetPopupProduct } from '@/components/map/useHydroMetPopupProduct'
import {
  formatHydroMetRiverForecastMessage,
  formatHydroMetRiverForecastUiString,
  loadHydroMetRiverForecast,
  riverForecastRequestKey,
  validateHydroMetRiverForecastForChart,
  type HydroMetRiverForecastPayload,
  type HydroMetRiverForecastProductIdentity,
  type HydroMetRiverForecastSegmentIdentity,
} from '@/lib/hydroMet/riverForecast'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import type { ForecastData } from '@/stores/forecast'
import type { QhhLatestProduct } from '@/pages/hydroMet/bootstrap'

/** 选中河段（来自地图 feature.properties / basinSegments）的必要身份字段。 */
export interface M11RiverPopupSegment {
  river_segment_id: string
  segment_id: string
  river_network_version_id: string
  basin_version_id: string
  name?: string | null
}

type RiverForecastLoadState =
  | { kind: 'idle' }
  | { kind: 'loading'; requestKey: string }
  | { kind: 'loaded'; requestKey: string; response: HydroMetRiverForecastPayload }
  | { kind: 'error'; requestKey: string; message: string }

function riverForecastProductIdentity(product: QhhLatestProduct): HydroMetRiverForecastProductIdentity {
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

function riverForecastSegmentIdentity(segment: M11RiverPopupSegment): HydroMetRiverForecastSegmentIdentity {
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

/**
 * 河段 q_down 预报曲线 popup（M26 全屏单页）。玻璃质感 + 弹窗内 source/起报选择。
 * 弹窗自持 source（默认随地图解析源），改 source → 以新源重取 latest-product 再取曲线。
 * 预报变量仅 q_down（产品唯一预报变量），如实保留单项标注。
 * honest 红线：身份/契约校验失败（ok:false）→ 显示原因，绝不绘制曲线；product=null → honest 空态。
 */
export function M11RiverForecastPopup({
  basinId,
  initialSource,
  segment,
  onClose,
}: {
  basinId: string | null
  /** 地图解析到的具体源（best/compare 未解析时为 null → 弹窗回退 GFS）。 */
  initialSource: HydroMetSource | null
  segment: M11RiverPopupSegment
  onClose?: () => void
}) {
  const segmentIdentity = riverForecastSegmentIdentity(segment)
  const popupProduct = useHydroMetPopupProduct({ basinId, initialSource })
  const { product } = popupProduct
  const [state, setState] = useState<RiverForecastLoadState>({ kind: 'idle' })

  useEffect(() => {
    if (!product) {
      setState({ kind: 'idle' })
      return
    }
    const identity = riverForecastProductIdentity(product)
    const requestKey = riverForecastRequestKey(identity, segmentIdentity.river_segment_id)
    let cancelled = false
    setState({ kind: 'loading', requestKey })
    void loadHydroMetRiverForecast({ product: identity, segment: segmentIdentity }).then(
      (response) => {
        if (!cancelled) setState({ kind: 'loaded', requestKey, response })
      },
      (error) => {
        if (!cancelled) {
          setState({
            kind: 'error',
            requestKey,
            message: formatHydroMetRiverForecastMessage(error, 'river forecast-series 不可用'),
          })
        }
      },
    )
    return () => {
      cancelled = true
    }
  }, [product, segmentIdentity.river_segment_id, segmentIdentity.basin_version_id, segmentIdentity.river_network_version_id])

  return (
    <M11PopupShell testId="m11-river-popup">
      <M11PopupHeader
        icon={Waves}
        title={`${segmentIdentity.river_segment_id} · ${segmentIdentity.name}`}
        subtitle="河段 q_down 流量预报"
        onClose={onClose}
      />
      <M11PopupSourceControls
        source={popupProduct.source}
        onSourceChange={popupProduct.setSource}
        issueTimes={popupProduct.issueTimes}
        issueTime={popupProduct.issueTime}
        onIssueTimeChange={popupProduct.setIssueTime}
      />

      {!product ? (
        <M11PopupEmpty testId="m11-river-popup-no-product">{popupProduct.reason ?? '等待 Best Available 解析'}</M11PopupEmpty>
      ) : state.kind === 'loading' ? (
        <M11PopupLoading testId="m11-river-popup-loading">
          正在加载 {segmentIdentity.river_segment_id} 的 q_down forecast-series...
        </M11PopupLoading>
      ) : state.kind === 'error' ? (
        <M11PopupEmpty testId="m11-river-popup-error">{state.message}</M11PopupEmpty>
      ) : state.kind === 'loaded' ? (
        <RiverForecastBody product={product} segment={segmentIdentity} response={state.response} />
      ) : null}
    </M11PopupShell>
  )
}

function RiverForecastBody({
  product,
  segment,
  response,
}: {
  product: QhhLatestProduct
  segment: HydroMetRiverForecastSegmentIdentity
  response: HydroMetRiverForecastPayload
}) {
  const identity = riverForecastProductIdentity(product)
  const validation = validateHydroMetRiverForecastForChart(response, identity, segment)

  if (!validation.ok) {
    return (
      <div className="px-4 py-3" data-testid="m11-river-popup-invalid">
        <M11PopupEmpty testId="m11-river-popup-invalid-reasons">
          <ul className="space-y-1">
            {validation.messages.map((message, index) => (
              <li key={`${index}-${message}`}>{message}</li>
            ))}
          </ul>
        </M11PopupEmpty>
      </div>
    )
  }

  const forecastData: ForecastData = {
    segmentId: segment.river_segment_id,
    basinVersionId: segment.basin_version_id,
    riverNetworkVersionId: segment.river_network_version_id,
    cycle: validation.cycleTime ?? product.cycle_time,
    issueTime: validation.issueTime ?? validation.cycleTime ?? product.cycle_time,
    unit: validation.unit,
    sourceAttribution: `${validation.sourceId} / ${validation.scenarioId}`,
    cycleAttribution: validation.issueTime ?? validation.cycleTime ?? product.cycle_time,
    series: [
      {
        scenario: validation.scenarioId,
        source: validation.sourceId,
        isAnalysis: false,
        label: 'q_down river discharge',
        color: validation.sourceId === 'IFS' ? '#34d399' : '#22d3ee',
        cycleTime: validation.cycleTime,
        availableLeadHours: validation.series.availableLeadHours,
        points: validation.renderedPoints.map((point) => ({ time: point.timestamp, value: point.value })),
      },
    ],
  }

  const leadHours = validation.series.availableLeadHours
  const horizonText = leadHours != null ? `预见期 ${leadHours}h` : '预见期'

  const points = validation.renderedPoints.filter((point): point is typeof point & { value: number } => point.value != null)
  const currentFlow = points.length > 0 ? points[0].value : null
  const peak = points.length > 0 ? points.reduce((best, point) => (point.value > best.value ? point : best), points[0]) : null
  const peakFlow = peak?.value ?? null
  const peakTime = peak?.timestamp ?? null

  return (
    <div className="max-h-[60vh] space-y-2.5 overflow-y-auto px-4 pb-3.5 pt-2.5" data-testid="m11-river-popup-loaded">
      <div className="flex items-center justify-between gap-2">
        <span
          className="inline-flex items-center rounded-full bg-cyan-400/10 px-2.5 py-0.5 text-[11px] font-medium tracking-wide text-cyan-200 ring-1 ring-inset ring-cyan-400/25"
          data-testid="m11-river-popup-variable"
        >
          流量 · q_down
        </span>
        <span
          className={cn('text-[11px] tabular-nums', validation.horizonShorter ? 'font-medium text-amber-300' : 'text-slate-400')}
          title={validation.horizonLabel}
          data-testid="m11-river-popup-horizon"
        >
          {validation.horizonShorter ? `${horizonText}（短于预期）` : horizonText}
          {validation.capped ? ` · ${validation.renderedPoints.length}/${validation.pointCount}` : ''}
        </span>
      </div>
      <RiverForecastKpiStrip current={currentFlow} peak={peakFlow} peakTime={peakTime} unit={validation.unit} />
      <ForecastChart data={forecastData} segmentName={segment.name} variant="compact" appearance="dark" />
    </div>
  )
}

/** KPI 条：当前/峰值流量，等宽数字 + 青色发光强调，深色指挥舱风格。仅展示真实渲染点导出的数值。 */
function RiverForecastKpiStrip({
  current,
  peak,
  peakTime,
  unit,
}: {
  current: number | null
  peak: number | null
  peakTime: number | null
  unit: string
}) {
  const formatFlow = (value: number | null) => (value == null ? '—' : value.toFixed(value >= 100 ? 0 : 1))
  const peakClock = peakTime != null ? formatPeakClock(peakTime) : null
  return (
    <div className="grid grid-cols-2 gap-2.5" data-testid="m11-river-popup-kpi">
      <div className="rounded-xl bg-white/[0.04] px-3.5 py-2 ring-1 ring-inset ring-white/10">
        <div className="text-[10px] uppercase tracking-[0.12em] text-slate-400">当前流量</div>
        <div className="mt-1 flex items-baseline gap-1.5">
          <span className="text-xl font-semibold tabular-nums text-slate-50" data-testid="m11-river-popup-kpi-current">
            {formatFlow(current)}
          </span>
          <span className="text-[10px] text-slate-400">{unit}</span>
        </div>
      </div>
      <div className="rounded-xl bg-cyan-400/[0.08] px-3.5 py-2 ring-1 ring-inset ring-cyan-400/25">
        <div className="flex items-center justify-between">
          <span className="text-[10px] uppercase tracking-[0.12em] text-cyan-300/90">峰值流量</span>
          {peakClock ? <span className="text-[10px] tabular-nums text-cyan-300/70">{peakClock}</span> : null}
        </div>
        <div className="mt-1 flex items-baseline gap-1.5">
          <span
            className="text-xl font-semibold tabular-nums text-cyan-200 drop-shadow-[0_0_10px_rgba(34,211,238,0.35)]"
            data-testid="m11-river-popup-kpi-peak"
          >
            {formatFlow(peak)}
          </span>
          <span className="text-[10px] text-cyan-300/70">{unit}</span>
        </div>
      </div>
    </div>
  )
}

function formatPeakClock(ms: number): string {
  const date = new Date(ms)
  if (Number.isNaN(date.getTime())) return ''
  const mm = String(date.getUTCMonth() + 1).padStart(2, '0')
  const dd = String(date.getUTCDate()).padStart(2, '0')
  const hh = String(date.getUTCHours()).padStart(2, '0')
  return `${mm}-${dd} ${hh}:00`
}

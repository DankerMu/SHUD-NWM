import { useEffect, useState } from 'react'
import { Loader2, Waves, X } from 'lucide-react'

import { ForecastChart } from '@/components/charts/ForecastChart'
import { ReturnPeriodSection } from '@/components/m11/ReturnPeriodSection'
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

// 从 QhhLatestProduct 构造 river forecast product identity（照搬 HydroMetPage 的派生口径）。
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
 * 河段 q_down 预报曲线 popup（M26-4）。逻辑照搬 HydroMetPage river 段：
 * 构造 product/segment identity → loadHydroMetRiverForecast → validateHydroMetRiverForecastForChart；
 * ok:true 渲染 ForecastChart(q_down) + ReturnPeriodSection 三态；ok:false 显示原因，不画曲线。
 * product=null（best 未解析 / 无 basin）→ honest 空态。
 */
export function M11RiverForecastPopup({
  product,
  segment,
  productReason,
  onClose,
}: {
  product: QhhLatestProduct | null
  segment: M11RiverPopupSegment
  /** product=null 时的 honest 原因（"等待 Best Available 解析" / "请选择流域"）。 */
  productReason?: string | null
  onClose?: () => void
}) {
  const segmentIdentity = riverForecastSegmentIdentity(segment)
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
    // segmentIdentity 由 segment 派生，依赖其稳定字段即可。
  }, [product, segmentIdentity.river_segment_id, segmentIdentity.basin_version_id, segmentIdentity.river_network_version_id])

  return (
    <div className="w-[min(26rem,80vw)]" data-testid="m11-river-popup">
      <PopupHeader
        title={`${segmentIdentity.river_segment_id} · ${segmentIdentity.name}`}
        subtitle="河段 q_down 流量预报"
        onClose={onClose}
      />

      {!product ? (
        <EmptyState testId="m11-river-popup-no-product">
          {productReason ?? '等待 Best Available 解析'}
        </EmptyState>
      ) : state.kind === 'loading' ? (
        <LoadingState testId="m11-river-popup-loading">
          正在加载 {segmentIdentity.river_segment_id} 的 q_down forecast-series...
        </LoadingState>
      ) : state.kind === 'error' ? (
        <EmptyState testId="m11-river-popup-error">{state.message}</EmptyState>
      ) : state.kind === 'loaded' ? (
        <RiverForecastBody product={product} segment={segmentIdentity} response={state.response} />
      ) : null}
    </div>
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
    // honest 红线：身份/契约校验失败 → 显示原因空态，绝不绘制 q_down 曲线。
    return (
      <div className="space-y-3 px-4 py-3" data-testid="m11-river-popup-invalid">
        <EmptyState testId="m11-river-popup-invalid-reasons">
          <ul className="space-y-1">
            {validation.messages.map((message, index) => (
              <li key={`${index}-${message}`}>{message}</li>
            ))}
          </ul>
        </EmptyState>
        <ReturnPeriodSection product={product} />
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
        color: validation.sourceId === 'IFS' ? '#2ca02c' : '#2266cc',
        cycleTime: validation.cycleTime,
        availableLeadHours: validation.series.availableLeadHours,
        points: validation.renderedPoints.map((point) => ({ time: point.timestamp, value: point.value })),
      },
    ],
  }

  return (
    <div className="space-y-3 px-4 py-3" data-testid="m11-river-popup-loaded">
      <div
        className={
          validation.horizonShorter
            ? 'rounded border border-warning/40 bg-warning/10 p-2 text-xs text-neutral-900'
            : 'rounded border border-primary-100 bg-primary-50 p-2 text-xs text-neutral-900'
        }
        data-testid="m11-river-popup-horizon"
      >
        {validation.horizonLabel}
        {validation.capped ? `; capped ${validation.renderedPoints.length}/${validation.pointCount}` : ''}
      </div>
      <ForecastChart data={forecastData} segmentName={segment.name} />
      <ReturnPeriodSection product={product} />
    </div>
  )
}

function PopupHeader({
  title,
  subtitle,
  onClose,
}: {
  title: string
  subtitle: string
  onClose?: () => void
}) {
  return (
    <div className="flex items-start justify-between gap-2 border-b border-neutral-300 px-4 py-3">
      <div className="min-w-0">
        <div className="flex items-center gap-2 text-sm font-semibold text-neutral-900">
          <Waves className="h-4 w-4 shrink-0 text-primary-600" aria-hidden="true" />
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
    <div
      className="m-4 rounded border border-warning/40 bg-warning/10 p-3 text-sm text-neutral-900"
      role="status"
      data-testid={testId}
    >
      {children}
    </div>
  )
}

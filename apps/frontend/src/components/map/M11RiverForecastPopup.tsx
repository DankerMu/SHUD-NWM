import { useEffect, useState } from 'react'
import { Waves } from 'lucide-react'

import { ForecastChart } from '@/components/charts/ForecastChart'
import { ReturnPeriodSection } from '@/components/m11/ReturnPeriodSection'
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
      <div className="space-y-3 px-4 py-3" data-testid="m11-river-popup-invalid">
        <M11PopupEmpty testId="m11-river-popup-invalid-reasons">
          <ul className="space-y-1">
            {validation.messages.map((message, index) => (
              <li key={`${index}-${message}`}>{message}</li>
            ))}
          </ul>
        </M11PopupEmpty>
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
      <div className="text-xs font-medium text-neutral-700" data-testid="m11-river-popup-variable">
        预报变量：q_down（产品唯一预报变量）
      </div>
      <div
        className={
          validation.horizonShorter
            ? 'rounded border border-warning/40 bg-warning/10 p-2 text-xs text-neutral-900'
            : 'rounded border border-primary-100 bg-primary-50/70 p-2 text-xs text-neutral-900'
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

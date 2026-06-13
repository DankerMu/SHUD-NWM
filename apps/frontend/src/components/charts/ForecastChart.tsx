import { useMemo } from 'react'
import ReactEChartsCore from 'echarts-for-react/lib/core'

import { echarts } from '@/components/charts/echartsCore'
import { FORECAST_CHART_POINT_BUDGET, forecastPointBudgetMessage } from '@/lib/forecastRenderingBudget'
import type { ForecastData } from '@/stores/forecast'

const IFS_SIX_DAY_LEAD_HOURS = 144
const HOUR_MS = 60 * 60 * 1000
const THRESHOLD_KEYS = ['Q2', 'Q5', 'Q10', 'Q20', 'Q50', 'Q100'] as const
const THRESHOLD_COLORS: Record<(typeof THRESHOLD_KEYS)[number], string> = {
  Q2: '#38a169',
  Q5: '#2b6cb0',
  Q10: '#d97706',
  Q20: '#dc2626',
  Q50: '#9333ea',
  Q100: '#111827',
}

interface ForecastChartProps {
  data: ForecastData | null
  segmentName?: string
  /**
   * compact：去掉内部标题/图例、收紧网格、压低高度。
   * 用于弹窗等已在外层 header 给出河段/起报/资料来源的场景，避免信息重复、更现代。
   */
  variant?: 'full' | 'compact'
  /** dark：深色玻璃弹窗内的指挥舱主题（暗坐标系 + 渐变面积 + 辉光曲线）；默认 light 不变。 */
  appearance?: 'light' | 'dark'
}

/** series.color(hex) → rgba，用于深色主题的面积渐变/辉光，保持与曲线同色相。 */
function hexToRgba(hex: string, alpha: number): string {
  const match = /^#?([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i.exec(hex.trim())
  if (!match) return `rgba(34, 211, 238, ${alpha})`
  let digits = match[1]
  if (digits.length === 3) digits = digits.split('').map((c) => c + c).join('')
  if (digits.length === 8) digits = digits.slice(0, 6)
  const value = Number.parseInt(digits, 16)
  return `rgba(${(value >> 16) & 0xff}, ${(value >> 8) & 0xff}, ${value & 0xff}, ${alpha})`
}

function timestampValue(time: string | number) {
  const numeric = Number(time)
  if (Number.isFinite(numeric)) return numeric
  return Date.parse(String(time))
}

function axisTimeLabel(value: number) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const month = String(date.getUTCMonth() + 1).padStart(2, '0')
  const day = String(date.getUTCDate()).padStart(2, '0')
  const hour = String(date.getUTCHours()).padStart(2, '0')
  return `${month}-${day} ${hour}:00`
}

function tooltipTimeLabel(value: number) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const year = date.getUTCFullYear()
  const month = String(date.getUTCMonth() + 1).padStart(2, '0')
  const day = String(date.getUTCDate()).padStart(2, '0')
  const hour = String(date.getUTCHours()).padStart(2, '0')
  const minute = String(date.getUTCMinutes()).padStart(2, '0')
  return `${year}-${month}-${day} ${hour}:${minute}`
}

function issueTimeMarkLineData(issueTimeMs: number, issueTimeLabel: string, dark: boolean) {
  return {
    name: '起报时间',
    xAxis: issueTimeMs,
    lineStyle: { color: dark ? '#94a3b8' : '#64748b', type: 'dashed', width: 1.5 },
    label: {
      formatter: '起报时间',
      color: dark ? '#e2e8f0' : '#1f2937',
      backgroundColor: dark ? 'rgba(15, 23, 42, 0.85)' : '#ffffff',
      padding: [2, 4],
    },
    tooltip: {
      renderMode: 'richText',
      formatter: () => `起报时间 ${issueTimeLabel}\n左侧为真实场 analysis，右侧为预报`,
    },
  }
}

function ifsSixDayMarkLineData(endpointMs: number, dark: boolean) {
  return {
    name: 'IFS 6d',
    xAxis: endpointMs,
    lineStyle: { color: dark ? '#34d399' : '#2ca02c', type: 'dashed', width: 1.5 },
    label: {
      formatter: 'IFS 6d',
      color: dark ? '#6ee7b7' : '#166534',
      backgroundColor: dark ? 'rgba(15, 23, 42, 0.85)' : '#ffffff',
      padding: [2, 4],
      position: 'insideEndTop',
    },
    tooltip: {
      renderMode: 'richText',
      formatter: () => `IFS 6d ${tooltipTimeLabel(endpointMs)}`,
    },
  }
}

function buildMarkLine(data: object[]) {
  if (data.length === 0) return undefined
  return {
    silent: false,
    symbol: 'none',
    data,
  }
}

function thresholdMarkLineData(data: ForecastData | null | undefined, dark: boolean) {
  const thresholds = data?.frequencyThresholds
  if (!thresholds) return []
  return THRESHOLD_KEYS.flatMap((key) => {
    const value = Number(thresholds[key])
    if (!Number.isFinite(value)) return []
    const color = dark && key === 'Q100' ? '#cbd5e1' : THRESHOLD_COLORS[key]
    return {
      name: key,
      yAxis: value,
      lineStyle: { color, type: 'dashed', width: 1.2 },
      label: {
        formatter: `${key} ${value.toFixed(0)}`,
        color,
        backgroundColor: dark ? 'rgba(15, 23, 42, 0.85)' : '#ffffff',
        padding: [2, 4],
      },
    }
  })
}

function isIfsSeries(series: { scenario: string; source?: string }) {
  return `${series.source ?? ''} ${series.scenario}`.toLowerCase().includes('ifs')
}

function sixDayEndpointMs(series: { cycleTime?: string | null; availableLeadHours?: number | null }) {
  if (series.availableLeadHours !== IFS_SIX_DAY_LEAD_HOURS || !series.cycleTime) return null
  const cycleMs = Date.parse(series.cycleTime)
  if (!Number.isFinite(cycleMs)) return null
  return cycleMs + IFS_SIX_DAY_LEAD_HOURS * HOUR_MS
}

interface TooltipParam {
  axisValue?: string | number
  marker?: string
  seriesName?: string
  value?: number | string | Array<number | string>
}

function tooltipValue(param: TooltipParam) {
  if (Array.isArray(param.value)) return Number(param.value[1])
  return Number(param.value)
}

function tooltipFormatter(params: TooltipParam | TooltipParam[], unit?: string) {
  const items = Array.isArray(params) ? params : [params]
  const first = items[0]
  const axisValue = Number(first?.axisValue ?? (Array.isArray(first?.value) ? first.value[0] : NaN))
  const lines = [`时间: ${Number.isFinite(axisValue) ? tooltipTimeLabel(axisValue) : ''}`]

  items.forEach((param) => {
    const value = tooltipValue(param)
    if (!Number.isFinite(value)) return
    lines.push(`${param.marker ?? ''}${param.seriesName ?? 'series'}: ${value.toFixed(2)} ${unit ?? 'm3/s'}`)
  })

  return lines.join('\n')
}

export function ForecastChart({ data, segmentName, variant = 'full', appearance = 'light' }: ForecastChartProps) {
  if (data?.pointBudgetStatus?.overBudget) {
    return (
      <div
        className={
          appearance === 'dark'
            ? 'grid min-h-72 place-items-center rounded-lg border border-amber-400/30 bg-amber-400/10 p-4 text-center text-sm text-amber-100'
            : 'grid min-h-72 place-items-center rounded-md border border-amber-300 bg-amber-50 p-4 text-center text-sm text-amber-950'
        }
        role="status"
      >
        {forecastPointBudgetMessage(data.pointBudgetStatus)}
      </div>
    )
  }

  return <ForecastChartInner data={data} segmentName={segmentName} variant={variant} appearance={appearance} />
}

function ForecastChartInner({ data, segmentName, variant = 'full', appearance = 'light' }: ForecastChartProps) {
  const compact = variant === 'compact'
  const dark = appearance === 'dark'
  const axisColor = dark ? '#94a3b8' : '#64748b'
  const normalizedSeries = useMemo(
    () => {
      let retainedPointCount = 0
      return (data?.series ?? [])
        .map((series) => {
          const remaining = Math.max(0, FORECAST_CHART_POINT_BUDGET - retainedPointCount)
          const ifs = isIfsSeries(series)
          const endpointMs = ifs ? sixDayEndpointMs(series) : null
          const seriesData = series.points
            .slice(0, remaining)
            .map((point) => [timestampValue(point.time), point.value])
            .filter(
              ([time, value]) =>
                Number.isFinite(time) &&
                Number.isFinite(value) &&
                (endpointMs === null || time <= endpointMs),
            )
          retainedPointCount += seriesData.length

          return {
            ...series,
            data: seriesData,
            isIfs: ifs,
            sixDayEndpointMs: endpointMs,
          }
        })
        .filter((series) => series.data.length > 0)
    },
    [data?.series],
  )

  const option = useMemo(() => {
    const issueTimeMs = data?.issueTime ? Date.parse(data.issueTime) : NaN
    const showIssueDivider =
      Number.isFinite(issueTimeMs) && normalizedSeries.some((series) => series.isAnalysis)
    const thresholds = thresholdMarkLineData(data, dark)

    return {
      color: normalizedSeries.map((series) => series.color),
      title: compact
        ? undefined
        : {
          text: `${segmentName ?? data?.segmentId ?? '河段'} 预报曲线`,
          subtext: `起报时间 ${data?.cycleAttribution || data?.issueTime || 'latest'}${
            data?.sourceAttribution ? `\n资料来源 ${data.sourceAttribution}` : ''
          }`,
          left: 0,
          textStyle: { fontSize: 15, fontWeight: 650, color: dark ? '#f1f5f9' : '#1f2937' },
          subtextStyle: { color: axisColor, lineHeight: 18 },
        },
      legend: compact ? undefined : { top: 48, left: 0, itemWidth: 18, itemHeight: 8, textStyle: { color: axisColor } },
      grid: compact ? { left: 48, right: 16, top: 16, bottom: 28 } : { left: 52, right: 18, top: 98, bottom: 52 },
      tooltip: {
        trigger: 'axis',
        renderMode: 'richText',
        formatter: (params: TooltipParam | TooltipParam[]) => tooltipFormatter(params, data?.unit),
        ...(dark
          ? {
            backgroundColor: 'rgba(8, 14, 32, 0.92)',
            borderColor: 'rgba(34, 211, 238, 0.35)',
            textStyle: { color: '#e2e8f0' },
          }
          : {}),
      },
      xAxis: {
        type: 'time',
        axisLabel: {
          color: axisColor,
          formatter: axisTimeLabel,
          hideOverlap: true,
          ...(compact ? { fontSize: 10 } : {}),
        },
        ...(dark ? { axisLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.25)' } } } : {}),
      },
      yAxis: {
        type: 'value',
        name: '流量 (m³/s)',
        nameGap: 32,
        scale: true,
        axisLabel: { color: axisColor },
        ...(dark
          ? {
            nameTextStyle: { color: axisColor },
            splitLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.14)' } },
          }
          : {}),
      },
      series: normalizedSeries.map((series, index) => ({
        type: 'line',
        name: series.label,
        smooth: true,
        symbolSize: 6,
        data: series.data,
        lineStyle: {
          width: 2.5,
          color: series.color,
          type: series.isIfs ? 'dashed' : 'solid',
          ...(dark ? { shadowBlur: 12, shadowColor: hexToRgba(series.color, 0.45), shadowOffsetY: 3 } : {}),
        },
        itemStyle: { color: series.color },
        ...(dark
          ? {
            showSymbol: false,
            areaStyle: {
              color: {
                type: 'linear',
                x: 0,
                y: 0,
                x2: 0,
                y2: 1,
                colorStops: [
                  { offset: 0, color: hexToRgba(series.color, 0.28) },
                  { offset: 1, color: hexToRgba(series.color, 0.02) },
                ],
              },
            },
          }
          : {}),
        markLine: buildMarkLine([
          ...(index === 0 && showIssueDivider
            ? [issueTimeMarkLineData(issueTimeMs, data?.issueTime ?? 'latest', dark)]
            : []),
          ...(index === 0 ? thresholds : []),
          ...(series.isIfs && series.sixDayEndpointMs !== null
            ? [ifsSixDayMarkLineData(series.sixDayEndpointMs, dark)]
            : []),
        ]),
      })),
    }
  }, [data, normalizedSeries, segmentName, compact, dark, axisColor])

  if (!data || normalizedSeries.length === 0) {
    return (
      <div
        className={
          dark
            ? 'grid min-h-72 place-items-center rounded-lg border border-dashed border-white/15 p-4 text-center text-sm text-slate-400'
            : 'grid min-h-72 place-items-center rounded-md border border-dashed border-border p-4 text-center text-sm text-muted'
        }
      >
        暂无预报数据
      </div>
    )
  }

  return (
    <ReactEChartsCore
      echarts={echarts}
      option={option}
      notMerge
      lazyUpdate
      style={compact ? { height: 240, minHeight: 216, width: '100%' } : { height: 360, minHeight: 320, width: '100%' }}
    />
  )
}

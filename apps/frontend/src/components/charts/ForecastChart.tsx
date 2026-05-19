import { useMemo } from 'react'
import ReactEChartsCore from 'echarts-for-react/lib/core'

import { echarts } from '@/components/charts/echartsCore'
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

function issueTimeMarkLineData(issueTimeMs: number, issueTimeLabel: string) {
  return {
    name: '起报时间',
    xAxis: issueTimeMs,
    lineStyle: { color: '#64748b', type: 'dashed', width: 1.5 },
    label: {
      formatter: '起报时间',
      color: '#1f2937',
      backgroundColor: '#ffffff',
      padding: [2, 4],
    },
    tooltip: {
      renderMode: 'richText',
      formatter: () => `起报时间 ${issueTimeLabel}\n左侧为真实场 analysis，右侧为预报`,
    },
  }
}

function ifsSixDayMarkLineData(endpointMs: number) {
  return {
    name: 'IFS 6d',
    xAxis: endpointMs,
    lineStyle: { color: '#2ca02c', type: 'dashed', width: 1.5 },
    label: {
      formatter: 'IFS 6d',
      color: '#166534',
      backgroundColor: '#ffffff',
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

function thresholdMarkLineData(data: ForecastData | null | undefined) {
  const thresholds = data?.frequencyThresholds
  if (!thresholds) return []
  return THRESHOLD_KEYS.flatMap((key) => {
    const value = Number(thresholds[key])
    if (!Number.isFinite(value)) return []
    return {
      name: key,
      yAxis: value,
      lineStyle: { color: THRESHOLD_COLORS[key], type: 'dashed', width: 1.2 },
      label: {
        formatter: `${key} ${value.toFixed(0)}`,
        color: THRESHOLD_COLORS[key],
        backgroundColor: '#ffffff',
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

export function ForecastChart({ data, segmentName }: ForecastChartProps) {
  const normalizedSeries = useMemo(
    () =>
      (data?.series ?? [])
        .map((series) => {
          const ifs = isIfsSeries(series)
          const endpointMs = ifs ? sixDayEndpointMs(series) : null
          const seriesData = series.points
            .map((point) => [timestampValue(point.time), point.value])
            .filter(
              ([time, value]) =>
                Number.isFinite(time) &&
                Number.isFinite(value) &&
                (endpointMs === null || time <= endpointMs),
            )

          return {
            ...series,
            data: seriesData,
            isIfs: ifs,
            sixDayEndpointMs: endpointMs,
          }
        })
        .filter((series) => series.data.length > 0),
    [data?.series],
  )

  const option = useMemo(() => {
    const issueTimeMs = data?.issueTime ? Date.parse(data.issueTime) : NaN
    const showIssueDivider =
      Number.isFinite(issueTimeMs) && normalizedSeries.some((series) => series.isAnalysis)
    const thresholds = thresholdMarkLineData(data)

    return {
      color: normalizedSeries.map((series) => series.color),
      title: {
        text: `${segmentName ?? data?.segmentId ?? '河段'} 预报曲线`,
        subtext: `起报时间 ${data?.cycleAttribution || data?.issueTime || 'latest'}${
          data?.sourceAttribution ? `\n资料来源 ${data.sourceAttribution}` : ''
        }`,
        left: 0,
        textStyle: { fontSize: 15, fontWeight: 650, color: '#1f2937' },
        subtextStyle: { color: '#64748b', lineHeight: 18 },
      },
      legend: { top: 48, left: 0, itemWidth: 18, itemHeight: 8, textStyle: { color: '#64748b' } },
      grid: { left: 52, right: 18, top: 98, bottom: 52 },
      tooltip: {
        trigger: 'axis',
        renderMode: 'richText',
        formatter: (params: TooltipParam | TooltipParam[]) => tooltipFormatter(params, data?.unit),
      },
      xAxis: {
        type: 'time',
        axisLabel: {
          color: '#64748b',
          formatter: axisTimeLabel,
        },
      },
      yAxis: {
        type: 'value',
        name: '流量 (m³/s)',
        nameGap: 32,
        scale: true,
        axisLabel: { color: '#64748b' },
      },
      series: normalizedSeries.map((series, index) => ({
        type: 'line',
        name: series.label,
        smooth: true,
        symbolSize: 6,
        data: series.data,
        lineStyle: { width: 2.5, color: series.color, type: series.isIfs ? 'dashed' : 'solid' },
        itemStyle: { color: series.color },
        markLine: buildMarkLine([
          ...(index === 0 && showIssueDivider
            ? [issueTimeMarkLineData(issueTimeMs, data?.issueTime ?? 'latest')]
            : []),
          ...(index === 0 ? thresholds : []),
          ...(series.isIfs && series.sixDayEndpointMs !== null
            ? [ifsSixDayMarkLineData(series.sixDayEndpointMs)]
            : []),
        ]),
      })),
    }
  }, [data, normalizedSeries, segmentName])

  if (!data || normalizedSeries.length === 0) {
    return (
      <div className="grid min-h-72 place-items-center rounded-md border border-dashed border-border p-4 text-center text-sm text-muted">
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
      style={{ height: 360, minHeight: 320, width: '100%' }}
    />
  )
}

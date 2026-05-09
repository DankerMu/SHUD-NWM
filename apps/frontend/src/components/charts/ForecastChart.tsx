import { useMemo } from 'react'
import ReactEChartsCore from 'echarts-for-react/lib/core'

import { echarts } from '@/components/charts/echartsCore'
import type { ForecastData } from '@/stores/forecast'

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
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  const hour = String(date.getHours()).padStart(2, '0')
  return `${month}-${day} ${hour}:00`
}

function issueTimeMarkLine(issueTimeMs: number, issueTimeLabel: string) {
  return {
    silent: false,
    symbol: 'none',
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
    data: [{ name: '起报时间', xAxis: issueTimeMs }],
  }
}

export function ForecastChart({ data, segmentName }: ForecastChartProps) {
  const normalizedSeries = useMemo(
    () =>
      (data?.series ?? [])
        .map((series) => ({
          ...series,
          data: series.points
            .map((point) => [timestampValue(point.time), point.value])
            .filter(([time, value]) => Number.isFinite(time) && Number.isFinite(value)),
        }))
        .filter((series) => series.data.length > 0),
    [data?.series],
  )

  const option = useMemo(() => {
    const issueTimeMs = data?.issueTime ? Date.parse(data.issueTime) : NaN
    const showIssueDivider =
      Number.isFinite(issueTimeMs) && normalizedSeries.some((series) => series.isAnalysis)

    return {
      color: normalizedSeries.map((series) => series.color),
      title: {
        text: `${segmentName ?? data?.segmentId ?? '河段'} 预报曲线`,
        subtext: `起报时间 ${data?.issueTime ?? 'latest'}${
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
        valueFormatter: (value: number) => `${Number(value).toFixed(2)} ${data?.unit ?? 'm3/s'}`,
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
        lineStyle: { width: 2.5, color: series.color },
        itemStyle: { color: series.color },
        markLine:
          index === 0 && showIssueDivider
            ? issueTimeMarkLine(issueTimeMs, data?.issueTime ?? 'latest')
            : undefined,
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

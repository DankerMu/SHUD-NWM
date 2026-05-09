import { useMemo } from 'react'
import ReactEChartsCore from 'echarts-for-react/lib/core'

import { echarts } from '@/components/charts/echartsCore'

export interface TrendLineSeries {
  name: string
  data: number[]
}

interface TrendLineProps {
  title: string
  dates: string[]
  series: TrendLineSeries[]
  unit?: 'seconds' | 'percent'
}

function valueLabel(value: number, unit: TrendLineProps['unit']) {
  if (unit === 'percent') return `${Number(value).toFixed(1)}%`
  return `${Number(value).toFixed(0)}s`
}

export function TrendLine({ title, dates, series, unit = 'seconds' }: TrendLineProps) {
  const option = useMemo(
    () => ({
      color: ['#0f8fbf', '#ef7d22', '#14804a', '#b42318', '#2266cc', '#7c3aed', '#64748b'],
      title: { text: title, left: 0, textStyle: { fontSize: 14, fontWeight: 650, color: '#1f2937' } },
      grid: { left: 48, right: 16, top: series.length > 1 ? 58 : 46, bottom: 36 },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (value: number) => valueLabel(Number(value), unit),
      },
      legend: series.length > 1 ? { top: 24, type: 'scroll', textStyle: { color: '#64748b' } } : undefined,
      xAxis: { type: 'category', data: dates, axisLabel: { color: '#64748b' } },
      yAxis: {
        type: 'value',
        name: unit === 'percent' ? '%' : '秒',
        min: unit === 'percent' ? 0 : undefined,
        max: unit === 'percent' ? 100 : undefined,
        axisLabel: {
          color: '#64748b',
          formatter: unit === 'percent' ? '{value}%' : '{value}',
        },
      },
      series: series.map((item) => ({
        type: 'line',
        name: item.name,
        smooth: true,
        symbolSize: 5,
        areaStyle: series.length === 1 ? { opacity: 0.12 } : undefined,
        data: item.data,
      })),
    }),
    [dates, series, title, unit],
  )

  if (!dates.length || !series.length) {
    return <div className="rounded-md border border-dashed border-border p-4 text-sm text-muted">暂无趋势数据</div>
  }

  return (
    <ReactEChartsCore
      echarts={echarts}
      option={option}
      notMerge
      lazyUpdate
      style={{ height: 280, width: '100%' }}
    />
  )
}

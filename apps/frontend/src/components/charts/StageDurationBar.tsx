import { useMemo } from 'react'
import ReactEChartsCore from 'echarts-for-react/lib/core'

import { echarts } from '@/components/charts/echartsCore'
import { useChartFollowsContainer } from '@/components/charts/useChartFollowsContainer'
import { STAGE_NAMES } from '@/lib/constants'
import { formatDuration } from '@/lib/format'
import type { PipelineStage } from '@/stores/monitoring'

interface StageDurationBarProps {
  stages: PipelineStage[]
}

function stageLabel(stage: string) {
  return STAGE_NAMES[stage as keyof typeof STAGE_NAMES] ?? stage
}

export function StageDurationBar({ stages }: StageDurationBarProps) {
  const option = useMemo(
    () => ({
      color: ['#0f8fbf'],
      grid: { left: 76, right: 18, top: 18, bottom: 28 },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (value: number) => formatDuration(Number(value)),
      },
      xAxis: { type: 'value', name: '秒' },
      yAxis: {
        type: 'category',
        data: stages.map((stage) => stageLabel(stage.stage)),
        axisLabel: { interval: 0, color: '#64748b' },
      },
      series: [
        {
          type: 'bar',
          data: stages.map((stage) => stage.duration_seconds ?? 0),
          barWidth: 14,
        },
      ],
    }),
    [stages],
  )

  const hasData = stages.length > 0
  // 空数据时不渲染图表：hook 必须在提前 return 之前调用，入参为 false 时断开容器观察。
  const followContainer = useChartFollowsContainer(hasData)

  if (!hasData) {
    return <div className="rounded-md border border-dashed border-border p-4 text-sm text-muted">暂无耗时数据</div>
  }

  return (
    <ReactEChartsCore
      echarts={echarts}
      option={option}
      notMerge
      lazyUpdate
      onChartReady={followContainer}
      style={{ height: 260, width: '100%' }}
    />
  )
}

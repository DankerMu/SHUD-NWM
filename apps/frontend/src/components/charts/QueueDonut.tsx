import { useMemo } from 'react'
import ReactEChartsCore from 'echarts-for-react/lib/core'

import { echarts } from '@/components/charts/echartsCore'
import type { QueueState } from '@/stores/monitoring'

interface QueueDonutProps {
  queue: QueueState | null
  error?: string | null
}

export function QueueDonut({ queue, error }: QueueDonutProps) {
  const values = queue ?? { running: 0, pending: 0, idle: 0 }
  const total = values.running + values.pending + values.idle

  const option = useMemo(
    () => ({
      color: ['#2266cc', '#ef7d22', '#94a3b8'],
      tooltip: { trigger: 'item' },
      legend: {
        bottom: 0,
        left: 'center',
        itemWidth: 12,
        itemHeight: 8,
        textStyle: { color: '#64748b' },
      },
      title: {
        text: String(total),
        subtext: 'queue',
        left: 'center',
        top: '34%',
        textStyle: { color: '#1f2937', fontSize: 20, fontWeight: 700 },
        subtextStyle: { color: '#64748b', fontSize: 11 },
      },
      series: [
        {
          type: 'pie',
          radius: ['50%', '72%'],
          center: ['50%', '42%'],
          avoidLabelOverlap: true,
          label: { formatter: '{b}: {c}', color: '#1f2937' },
          data: [
            { name: 'running', value: values.running },
            { name: 'pending', value: values.pending },
            { name: 'idle', value: values.idle },
          ],
        },
      ],
    }),
    [total, values.idle, values.pending, values.running],
  )

  return (
    <div>
      <ReactEChartsCore
        echarts={echarts}
        option={option}
        notMerge
        lazyUpdate
        style={{ height: 180, width: '100%' }}
      />
      {error ? <p className="mt-1 text-xs text-danger">{error}</p> : null}
    </div>
  )
}

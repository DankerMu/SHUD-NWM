import { useEffect, useMemo, useState } from 'react'

import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import { TrendLine, type TrendLineSeries } from '@/components/charts/TrendLine'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { STAGE_NAMES } from '@/lib/constants'
import type { StageDurationMetric, SuccessRateMetric } from '@/stores/monitoring'

interface TrendPanelProps {
  refreshKey?: number
}

const stageOrder = Object.keys(STAGE_NAMES) as Array<keyof typeof STAGE_NAMES>

async function fetchStageDurationMetrics() {
  const { data, error } = await client.GET('/api/v1/metrics/stage-duration', {
    params: { query: { days: 7 } },
  })
  if (error) throw new Error(getApiErrorMessage(error, '阶段耗时趋势加载失败'))
  return unwrapApiData<StageDurationMetric[]>(data, '阶段耗时趋势加载失败')
}

async function fetchSuccessRateMetrics() {
  const { data, error } = await client.GET('/api/v1/metrics/success-rate', {
    params: { query: { days: 7 } },
  })
  if (error) throw new Error(getApiErrorMessage(error, '成功率趋势加载失败'))
  return unwrapApiData<SuccessRateMetric[]>(data, '成功率趋势加载失败')
}

export function TrendPanel({ refreshKey = 0 }: TrendPanelProps) {
  const [stageRows, setStageRows] = useState<StageDurationMetric[]>([])
  const [successRows, setSuccessRows] = useState<SuccessRateMetric[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setLoading(true)
    setError(null)

    Promise.all([fetchStageDurationMetrics(), fetchSuccessRateMetrics()])
      .then(([stageDuration, successRate]) => {
        if (!active) return
        setStageRows(stageDuration)
        setSuccessRows(successRate)
      })
      .catch((error) => {
        if (!active) return
        setError(getApiErrorMessage(error, '趋势数据加载失败'))
      })
      .finally(() => {
        if (active) setLoading(false)
      })

    return () => {
      active = false
    }
  }, [refreshKey])

  const stageTrend = useMemo(() => {
    const dates = [...new Set(stageRows.map((row) => row.date))].sort()
    const byStageDate = new Map(
      stageRows.map((row) => [`${row.stage}:${row.date}`, row.average_duration_seconds ?? 0]),
    )
    const series: TrendLineSeries[] = stageOrder.map((stage) => ({
      name: STAGE_NAMES[stage],
      data: dates.map((date) => byStageDate.get(`${stage}:${date}`) ?? 0),
    }))
    return { dates, series }
  }, [stageRows])

  const successTrend = useMemo(
    () => ({
      dates: successRows.map((row) => row.date),
      series: [
        {
          name: 'success_rate',
          data: successRows.map((row) => Math.round((row.success_rate ?? 0) * 1000) / 10),
        },
      ],
    }),
    [successRows],
  )

  return (
    <Card className="min-w-0">
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <CardTitle>趋势</CardTitle>
        <span className="text-sm text-muted">{loading ? '加载中' : '近 7 天'}</span>
      </CardHeader>
      <CardContent className="space-y-4">
        {error ? <div className="rounded-md border border-danger/30 bg-danger/10 p-3 text-sm text-danger">{error}</div> : null}
        <TrendLine title="阶段平均耗时" dates={stageTrend.dates} series={stageTrend.series} unit="seconds" />
        <TrendLine title="每周期成功率" dates={successTrend.dates} series={successTrend.series} unit="percent" />
      </CardContent>
    </Card>
  )
}

import { X } from 'lucide-react'
import { useEffect, useMemo } from 'react'
import ReactEChartsCore from 'echarts-for-react/lib/core'

import { echarts } from '@/components/charts/echartsCore'
import { alertLevelColor, alertLevelLabel } from '@/components/flood/alertLevels'
import { Button } from '@/components/ui/button'
import { useToast } from '@/hooks/useToast'
import { getApiErrorMessage } from '@/api/response'
import type { M11Source } from '@/lib/m11/queryState'
import type { ForecastData } from '@/stores/forecast'
import { useForecastStore } from '@/stores/forecast'
import type {
  FloodAlertRankingItem,
  FloodAlertTimeline,
  FloodFrequencyThresholds,
} from '@/stores/floodAlert'
import { useFloodAlertStore } from '@/stores/floodAlert'

interface SegmentAlertDetailProps {
  segment: FloodAlertRankingItem | null
  basinVersionId?: string | null
  forecastSource?: M11Source | null
  forecastIssueTime?: string | null
  onClose: () => void
}

const THRESHOLD_LINES = [
  ['Q2', 'elevated'],
  ['Q5', 'watch'],
  ['Q10', 'warning'],
  ['Q20', 'high_risk'],
  ['Q50', 'severe'],
  ['Q100', 'extreme'],
] as const

function timeValue(value: string | number) {
  const numeric = Number(value)
  if (Number.isFinite(numeric)) return numeric
  return Date.parse(String(value))
}

function thresholdValue(thresholds: FloodFrequencyThresholds | null | undefined, key: string) {
  if (!thresholds) return null
  const value = thresholds[key as keyof FloodFrequencyThresholds] ?? thresholds[key.toLowerCase() as keyof FloodFrequencyThresholds]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function chartTime(value: number) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const day = String(date.getUTCDate()).padStart(2, '0')
  const hour = String(date.getUTCHours()).padStart(2, '0')
  return `${day}日 ${hour}Z`
}

function buildForecastOption(data: ForecastData | null, timeline: FloodAlertTimeline | null, segmentName?: string | null) {
  const thresholds = timeline?.frequencyThresholds
  const series = (data?.series ?? [])
    .map((item) => ({
      name: item.label,
      data: item.points
        .map((point) => [timeValue(point.time), point.value])
        .filter(([time, value]) => Number.isFinite(time) && Number.isFinite(value)),
      color: item.color,
    }))
    .filter((item) => item.data.length > 0)

  return {
    color: series.map((item) => item.color),
    grid: { left: 52, right: 34, top: 48, bottom: 42 },
    tooltip: { trigger: 'axis', renderMode: 'richText' },
    legend: { top: 4, left: 0, textStyle: { color: '#64748b' } },
    xAxis: { type: 'time', axisLabel: { color: '#64748b', formatter: chartTime } },
    yAxis: { type: 'value', name: 'm³/s', scale: true, axisLabel: { color: '#64748b' } },
    title: {
      text: `${segmentName || data?.segmentId || '河段'} 预报曲线`,
      left: 0,
      textStyle: { fontSize: 14, fontWeight: 650, color: '#1f2937' },
    },
    series: [
      ...series.map((item, index) => ({
        type: 'line',
        name: item.name,
        smooth: true,
        symbolSize: 5,
        data: item.data,
        lineStyle: { width: 2.2, color: item.color },
        itemStyle: { color: item.color },
        markLine:
          index === 0
            ? {
                symbol: 'none',
                data: THRESHOLD_LINES.map(([key, level]) => {
                  const value = thresholdValue(thresholds, key)
                  if (value === null) return null
                  return {
                    name: key,
                    yAxis: value,
                    lineStyle: { color: alertLevelColor(level), type: 'dashed', width: 1.4 },
                    label: { formatter: `${key} ${value.toFixed(0)}`, color: alertLevelColor(level) },
                  }
                }).filter(Boolean),
              }
            : undefined,
      })),
    ],
  }
}

function buildTimelineOption(timeline: FloodAlertTimeline | null) {
  const data = (timeline?.timesteps ?? [])
    .map((point) => [Date.parse(point.validTime), point.returnPeriod ?? 0, point.warningLevel])
    .filter(([time]) => Number.isFinite(time))

  return {
    grid: { left: 42, right: 18, top: 22, bottom: 36 },
    tooltip: {
      trigger: 'axis',
      renderMode: 'richText',
      formatter: (params: Array<{ value: [number, number, string | null] }> | { value: [number, number, string | null] }) => {
        const item = Array.isArray(params) ? params[0] : params
        const [time, returnPeriod, level] = item.value
        return `时间: ${chartTime(time)}\nT: ${Number(returnPeriod).toFixed(1)}\n等级: ${alertLevelLabel(level)}`
      },
    },
    xAxis: { type: 'time', axisLabel: { color: '#64748b', formatter: chartTime } },
    yAxis: { type: 'value', name: 'T', min: 0, axisLabel: { color: '#64748b' } },
    series: [
      {
        type: 'line',
        name: '重现期',
        smooth: true,
        symbolSize: 7,
        data,
        encode: { x: 0, y: 1 },
        lineStyle: { width: 2.4, color: '#2266cc' },
        itemStyle: {
          color: (params: { value: [number, number, string | null] }) => alertLevelColor(params.value[2]),
        },
      },
    ],
  }
}

export function SegmentAlertDetail({
  segment,
  basinVersionId,
  forecastSource = null,
  forecastIssueTime = null,
  onClose,
}: SegmentAlertDetailProps) {
  const toast = useToast((state) => state.toast)
  const timeline = useFloodAlertStore((state) => state.timelineData)
  const timelineLoading = useFloodAlertStore((state) => state.timelineLoading)
  const fetchTimeline = useFloodAlertStore((state) => state.fetchTimeline)
  const forecastData = useForecastStore((state) => state.forecastData)
  const forecastLoading = useForecastStore((state) => state.loading)
  const forecastError = useForecastStore((state) => state.error)
  const selectForecastSegment = useForecastStore((state) => state.selectSegment)
  const fetchForecast = useForecastStore((state) => state.fetchForecast)

  useEffect(() => {
    if (!segment) return

    void fetchTimeline(segment.riverSegmentId).catch((error) => {
      toast({
        title: '预警时间线加载失败',
        description: getApiErrorMessage(error, '河段预警详情加载失败'),
        variant: 'destructive',
      })
    })
  }, [fetchTimeline, segment, toast])

  useEffect(() => {
    if (!segment || !basinVersionId) return

    selectForecastSegment({
      segmentId: segment.riverSegmentId,
      name: segment.segmentName ?? undefined,
      basinVersionId,
    })
    void fetchForecast({
      includeAnalysis: true,
      ignoreActiveRequestContext: true,
      source: forecastSource,
      issueTime: forecastIssueTime,
    }).catch(() => undefined)
  }, [basinVersionId, fetchForecast, forecastIssueTime, forecastSource, segment, selectForecastSegment])

  const forecastOption = useMemo(
    () => buildForecastOption(forecastData, timeline, segment?.segmentName || segment?.riverSegmentId),
    [forecastData, segment, timeline],
  )
  const timelineOption = useMemo(() => buildTimelineOption(timeline), [timeline])

  if (!segment) {
    return (
      <aside className="flex min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-panel">
        <div className="grid min-h-72 flex-1 place-items-center p-4 text-center text-sm text-muted">
          选择河段查看预警详情
        </div>
      </aside>
    )
  }

  const currentLevel = segment.warningLevel ?? timeline?.peak?.warningLevel ?? null

  return (
    <aside className="flex min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-panel">
      <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <h2 className="truncate text-base font-semibold text-foreground">
            {segment.segmentName || segment.riverSegmentId}
          </h2>
          <p className="mt-1 text-xs text-muted">{segment.basinName || segment.basinVersionId || basinVersionId}</p>
        </div>
        <Button type="button" size="icon" variant="ghost" className="size-8 shrink-0" onClick={onClose} aria-label="关闭详情">
          <X className="size-4" />
        </Button>
      </div>

      <div className="min-h-0 flex-1 space-y-4 overflow-auto p-4">
        <div className="rounded-md border border-border p-3">
          <div className="text-xs text-muted">当前等级</div>
          <div className="mt-1 text-2xl font-semibold" style={{ color: alertLevelColor(currentLevel) }}>
            {alertLevelLabel(currentLevel)}
          </div>
          <div className="mt-2 grid grid-cols-2 gap-3 text-sm">
            <div>
              <div className="text-xs text-muted">Q</div>
              <div className="font-medium text-foreground">{segment.qValue?.toFixed(1) ?? '-'} {segment.qUnit ?? 'm³/s'}</div>
            </div>
            <div>
              <div className="text-xs text-muted">T</div>
              <div className="font-medium text-foreground">{segment.returnPeriod?.toFixed(1) ?? '-'}</div>
            </div>
          </div>
        </div>

        {forecastLoading || timelineLoading ? (
          <div className="grid min-h-48 place-items-center rounded-md border border-dashed border-border text-sm text-muted">
            详情加载中...
          </div>
        ) : null}

        {forecastError ? (
          <div className="rounded-md border border-danger/30 bg-danger/10 p-3 text-sm text-danger">
            {forecastError}
          </div>
        ) : null}

        {forecastData ? (
          <ReactEChartsCore echarts={echarts} option={forecastOption} notMerge lazyUpdate style={{ height: 300, width: '100%' }} />
        ) : (
          <div className="grid min-h-48 place-items-center rounded-md border border-dashed border-border text-sm text-muted">
            暂无预报曲线
          </div>
        )}

        {timeline?.timesteps.length ? (
          <ReactEChartsCore echarts={echarts} option={timelineOption} notMerge lazyUpdate style={{ height: 220, width: '100%' }} />
        ) : (
          <div className="grid min-h-40 place-items-center rounded-md border border-dashed border-border text-sm text-muted">
            暂无预警时间线
          </div>
        )}
      </div>
    </aside>
  )
}

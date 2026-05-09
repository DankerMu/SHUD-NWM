import { Loader2, RefreshCw, X } from 'lucide-react'

import { ForecastChart } from '@/components/charts/ForecastChart'
import { SegmentInfo } from '@/components/forecast/SegmentInfo'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { formatDate } from '@/lib/format'
import type { ForecastData, ForecastSegmentInfo } from '@/stores/forecast'

interface ForecastPanelProps {
  segment: ForecastSegmentInfo
  forecastData: ForecastData | null
  loading: boolean
  error: string | null
  includeAnalysis: boolean
  onClose: () => void
  onRetry: () => void
}

export function ForecastPanel({
  segment,
  forecastData,
  loading,
  error,
  includeAnalysis,
  onClose,
  onRetry,
}: ForecastPanelProps) {
  return (
    <aside className="flex h-full min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-panel">
      <header className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="truncate text-base font-semibold text-foreground">预报工作台</h2>
            {includeAnalysis ? <Badge variant="secondary">analysis</Badge> : null}
          </div>
          <p className="mt-1 truncate text-xs text-muted">{segment.name ?? segment.segmentId}</p>
        </div>
        <Button size="icon" variant="ghost" onClick={onClose} aria-label="关闭预报面板" title="关闭">
          <X className="size-4" />
        </Button>
      </header>

      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-auto p-4">
        <SegmentInfo segment={segment} />

        {forecastData ? (
          <div className="grid gap-2 rounded-md border border-border bg-background/60 p-3 text-xs">
            <div className="flex items-center justify-between gap-3">
              <span className="text-muted">起报时间</span>
              <span className="min-w-0 truncate font-medium text-foreground">
                {formatDate(forecastData.issueTime)}
              </span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span className="text-muted">资料来源</span>
              <span className="min-w-0 truncate font-medium text-foreground">
                {forecastData.sourceAttribution || '-'}
              </span>
            </div>
          </div>
        ) : null}

        <section className="min-h-0 flex-1">
          {loading ? (
            <div className="grid min-h-72 place-items-center rounded-md border border-dashed border-border p-4 text-sm text-muted">
              <div className="flex items-center gap-2">
                <Loader2 className="size-4 animate-spin" />
                加载中...
              </div>
            </div>
          ) : error ? (
            <div className="grid min-h-72 place-items-center rounded-md border border-danger/30 bg-danger/10 p-4 text-center text-sm text-danger">
              <div className="space-y-3">
                <p>{error}</p>
                <Button size="sm" variant="outline" onClick={onRetry}>
                  <RefreshCw className="size-4" />
                  重试
                </Button>
              </div>
            </div>
          ) : (
            <ForecastChart data={forecastData} segmentName={segment.name ?? segment.segmentId} />
          )}
        </section>
      </div>
    </aside>
  )
}

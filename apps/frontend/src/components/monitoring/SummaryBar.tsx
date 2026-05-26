import { RefreshCw } from 'lucide-react'

import { QueueDonut } from '@/components/charts/QueueDonut'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { formatDate } from '@/lib/format'
import type { PipelineCycle, QueueState } from '@/stores/monitoring'

interface SummaryBarProps {
  source: string
  cycleTime: string
  cycle: PipelineCycle | null
  queue: QueueState | null
  queueError?: string | null
  isRefreshing?: boolean
  disabled?: boolean
  onRefresh: () => void
}

const emptyJobCounts = { succeeded: 0, failed: 0, running: 0, pending: 0 }

export function SummaryBar({
  source,
  cycleTime,
  cycle,
  queue,
  queueError,
  isRefreshing,
  disabled,
  onRefresh,
}: SummaryBarProps) {
  const counts = cycle?.job_counts ?? emptyJobCounts

  return (
    <section className="grid gap-4 min-[900px]:grid-cols-[minmax(0,1.2fr)_minmax(22rem,0.9fr)_minmax(18rem,0.8fr)]">
      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0">
          <CardTitle>当前周期</CardTitle>
          <Button size="sm" variant="outline" onClick={onRefresh} disabled={isRefreshing || disabled}>
            <RefreshCw className={isRefreshing ? 'size-4 animate-spin' : 'size-4'} />
            {isRefreshing ? '刷新中' : '刷新'}
          </Button>
        </CardHeader>
        <CardContent>
          <div className="grid gap-3 text-sm sm:grid-cols-3">
            <div>
              <div className="text-xs font-medium uppercase text-muted">Source</div>
              <div className="mt-1 font-semibold text-foreground">{cycle?.source ?? source}</div>
            </div>
            <div>
              <div className="text-xs font-medium uppercase text-muted">Cycle Time</div>
              <div className="mt-1 font-semibold text-foreground">{formatDate(cycle?.cycle_time ?? cycleTime)}</div>
            </div>
            <div>
              <div className="text-xs font-medium uppercase text-muted">Current State</div>
              <div className="mt-1 font-semibold text-foreground">{cycle?.current_state ?? '-'}</div>
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>作业计数</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 min-[900px]:grid-cols-2">
            <Badge className="justify-between border-emerald-200 bg-emerald-50 text-emerald-700">
              成功 <strong>{counts.succeeded}</strong>
            </Badge>
            <Badge className="justify-between border-danger/30 bg-danger/10 text-danger">
              失败 <strong>{counts.failed}</strong>
            </Badge>
            <Badge className="justify-between border-accent/30 bg-accent/10 text-accent">
              运行中 <strong>{counts.running}</strong>
            </Badge>
            <Badge className="justify-between border-border bg-muted/10 text-muted">
              等待 <strong>{counts.pending}</strong>
            </Badge>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Slurm 队列深度</CardTitle>
        </CardHeader>
        <CardContent>
          <QueueDonut queue={queue} error={queueError} />
        </CardContent>
      </Card>
    </section>
  )
}

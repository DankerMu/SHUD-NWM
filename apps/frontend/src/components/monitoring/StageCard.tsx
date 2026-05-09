import { cn } from '@/lib/cn'
import { STAGE_NAMES, STATUS_COLORS, type PipelineStatus } from '@/lib/constants'
import { formatDuration } from '@/lib/format'
import type { PipelineStage } from '@/stores/monitoring'

interface StageCardProps {
  stage: PipelineStage
  expanded?: boolean
  onToggle?: () => void
}

const statusIcons: Record<PipelineStatus, string> = {
  succeeded: '✓',
  failed: '✗',
  running: '◉',
  pending: '○',
  partially_failed: '⚠',
  skipped: '⊘',
}

function stageLabel(stage: string) {
  return STAGE_NAMES[stage as keyof typeof STAGE_NAMES] ?? stage
}

function stageStatus(stage: PipelineStage) {
  return (stage.display_status ?? stage.status ?? 'pending') as PipelineStatus
}

function completionRate(stage: PipelineStage) {
  const progress = stage.basin_progress ?? { completed: 0, total: 0, failed: 0 }
  return progress.total ? Math.round((progress.completed / progress.total) * 100) : 0
}

export function StageCard({ stage, expanded, onToggle }: StageCardProps) {
  const status = stageStatus(stage)
  const progress = stage.basin_progress ?? { completed: 0, total: 0, failed: 0 }
  const rate = completionRate(stage)
  const canExpand = status === 'failed' || status === 'partially_failed'

  return (
    <button
      type="button"
      className={cn(
        'w-full rounded-lg border bg-panel p-3 text-left transition-colors focus:outline-none focus:ring-2 focus:ring-accent',
        STATUS_COLORS[status],
        canExpand ? 'cursor-pointer hover:bg-background' : 'cursor-default',
      )}
      aria-expanded={canExpand ? expanded : undefined}
      onClick={canExpand ? onToggle : undefined}
    >
      <div className="flex items-start gap-3">
        <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-panel text-base font-bold">
          {statusIcons[status]}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-semibold text-foreground">{stageLabel(stage.stage)}</span>
            <span className="text-xs font-medium text-muted">{status}</span>
          </span>
          <span className="mt-2 grid gap-1 text-xs text-muted sm:grid-cols-2">
            <span>耗时 {formatDuration(stage.duration_seconds)}</span>
            <span>
              完成率 {rate}% ({progress.completed}/{progress.total || 0})
            </span>
          </span>
          <span className="mt-3 block h-2 overflow-hidden rounded-full bg-border/70">
            <span
              className={cn(
                'block h-full rounded-full',
                status === 'failed' ? 'bg-danger' : status === 'partially_failed' ? 'bg-river-strong' : 'bg-accent',
              )}
              style={{ width: `${Math.min(100, Math.max(0, rate))}%` }}
            />
          </span>
        </span>
      </div>
    </button>
  )
}

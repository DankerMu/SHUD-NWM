import type { PipelineStage } from '@/stores/monitoring'

interface BasinFailuresProps {
  stage: PipelineStage
}

const failedStatuses = new Set(['failed', 'submission_failed', 'permanently_failed', 'cancelled', 'partially_failed'])

export function BasinFailures({ stage }: BasinFailuresProps) {
  const failures = (stage.basin_results ?? []).filter(
    (item) => failedStatuses.has(item.status) || Boolean(item.error_code),
  )

  if (!failures.length) {
    return (
      <div className="ml-10 rounded-md border border-dashed border-border bg-background/70 p-3 text-sm text-muted">
        暂无 per-basin 失败明细
      </div>
    )
  }

  return (
    <div className="ml-10 overflow-hidden rounded-md border border-border bg-background/70">
      <div className="grid grid-cols-[minmax(6rem,0.7fr)_minmax(7rem,0.7fr)_minmax(0,1.6fr)] gap-3 border-b border-border px-3 py-2 text-xs font-semibold uppercase text-muted">
        <span>model_id</span>
        <span>error_code</span>
        <span>error_message</span>
      </div>
      {failures.map((item, index) => (
        <div
          key={`${item.model_id ?? item.basin_id ?? 'basin'}-${index}`}
          className="grid grid-cols-[minmax(6rem,0.7fr)_minmax(7rem,0.7fr)_minmax(0,1.6fr)] gap-3 border-b border-border px-3 py-2 text-sm last:border-b-0"
        >
          <span className="truncate font-medium text-foreground">{item.model_id ?? '-'}</span>
          <span className="truncate text-danger">{item.error_code ?? item.status ?? '-'}</span>
          <span className="min-w-0 break-words text-muted">{item.error_message ?? '-'}</span>
        </div>
      ))}
    </div>
  )
}

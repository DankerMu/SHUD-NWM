export const STAGE_NAMES = {
  ingest: '资料接入',
  forcing: '强迫生成',
  model_run: '模型计算',
  routing: '河道演算',
  forecast: '预报生成',
  publish: '产品发布',
  archive: '归档',
} as const

export type PipelineStatus =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'partially_failed'
  | 'failed'
  | 'skipped'

export const STATUS_COLORS: Record<PipelineStatus, string> = {
  pending: 'bg-muted/10 text-muted border-border',
  running: 'bg-accent/10 text-accent border-accent/30',
  succeeded: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  partially_failed: 'bg-river-strong/10 text-river-strong border-river-strong/30',
  failed: 'bg-danger/10 text-danger border-danger/30',
  skipped: 'bg-muted/10 text-muted border-border',
}

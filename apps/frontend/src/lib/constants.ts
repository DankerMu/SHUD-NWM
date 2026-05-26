export const STAGE_NAMES = {
  download: '下载',
  convert: '标准化',
  forcing: '强迫场',
  forecast: '预报',
  parse: '解析',
  frequency: '频率计算',
  publish: '发布',
} as const

export type PipelineStatus =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'partially_failed'
  | 'failed'
  | 'skipped'

export type JobStatus =
  | 'pending'
  | 'queued'
  | 'submitted'
  | 'running'
  | 'succeeded'
  | 'partially_failed'
  | 'failed'
  | 'cancelled'
  | 'submission_failed'
  | 'permanently_failed'
  | 'skipped'

export const STATUS_COLORS: Record<PipelineStatus, string> = {
  pending: 'bg-muted/10 text-muted border-border',
  running: 'bg-accent/10 text-accent border-accent/30',
  succeeded: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  partially_failed: 'bg-river-strong/10 text-river-strong border-river-strong/30',
  failed: 'bg-danger/10 text-danger border-danger/30',
  skipped: 'bg-muted/10 text-muted border-border',
}

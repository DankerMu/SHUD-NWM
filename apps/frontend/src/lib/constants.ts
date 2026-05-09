export const STAGE_NAMES = {
  ingest: '资料接入',
  forcing: '强迫生成',
  model_run: '模型计算',
  routing: '河道演算',
  forecast: '预报生成',
  publish: '产品发布',
  archive: '归档',
} as const

export type PipelineStatus = 'pending' | 'running' | 'success' | 'failed' | 'partial'

export const STATUS_COLORS: Record<PipelineStatus, string> = {
  pending: 'bg-muted/10 text-muted border-border',
  running: 'bg-accent/10 text-accent border-accent/30',
  success: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  failed: 'bg-danger/10 text-danger border-danger/30',
  partial: 'bg-river-strong/10 text-river-strong border-river-strong/30',
}

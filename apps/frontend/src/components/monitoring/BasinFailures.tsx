import { ClipboardCopy } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { useToast } from '@/hooks/useToast'
import {
  buildBasinDiagnosticPayload,
  buildStageDiagnosticPayload,
  type DiagnosticContext,
} from '@/components/monitoring/diagnostics'
import type { PipelineStage } from '@/stores/monitoring'

interface BasinFailuresProps {
  diagnosticContext?: DiagnosticContext | null
  diagnosticsDisplayReadonly?: boolean
  diagnosticsEnabled?: boolean
  stage: PipelineStage
}

const failedStatuses = new Set(['failed', 'submission_failed', 'permanently_failed', 'cancelled', 'partially_failed'])

export function BasinFailures({
  diagnosticContext = null,
  diagnosticsDisplayReadonly = false,
  diagnosticsEnabled = false,
  stage,
}: BasinFailuresProps) {
  const { toast } = useToast()
  const failures = (stage.basin_results ?? []).filter(
    (item) => failedStatuses.has(item.status) || Boolean(item.error_code),
  )
  const canCopyDiagnostics = diagnosticsEnabled && Boolean(diagnosticContext)
  const manualRecoveryGuidance = diagnosticsDisplayReadonly
    ? '阶段失败诊断用于交给 22 compute-control 节点处理；27 display_readonly 页面只复制只读证据，不写入数据库或审计 API。'
    : '阶段失败诊断用于交给 22 compute-control 节点处理；当前运维页面复制诊断不写入数据库或审计 API。'

  const copyStageDiagnostic = async () => {
    if (!diagnosticContext) return
    await navigator.clipboard?.writeText(JSON.stringify(buildStageDiagnosticPayload(stage, diagnosticContext), null, 2))
    toast({ title: '诊断已复制' })
  }

  const copyBasinDiagnostic = async (failure: (typeof failures)[number]) => {
    if (!diagnosticContext) return
    await navigator.clipboard?.writeText(JSON.stringify(buildBasinDiagnosticPayload(stage, failure, diagnosticContext), null, 2))
    toast({ title: '诊断已复制' })
  }

  const guidance = canCopyDiagnostics ? (
    <div
      className="rounded-md border border-border bg-background p-3 text-xs text-muted"
      data-testid="ops-stage-manual-recovery-guidance"
    >
      {manualRecoveryGuidance}
    </div>
  ) : null

  if (!failures.length) {
    return (
      <div className="ml-10 space-y-3 rounded-md border border-dashed border-border bg-background/70 p-3 text-sm text-muted">
        {guidance}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span>暂无 per-basin 失败明细</span>
          {canCopyDiagnostics ? (
            <Button variant="outline" size="sm" onClick={() => void copyStageDiagnostic()}>
              <ClipboardCopy className="size-3.5" />
              复制阶段诊断
            </Button>
          ) : null}
        </div>
      </div>
    )
  }

  return (
    <div className="ml-10 space-y-3 rounded-md border border-border bg-background/70 p-3">
      {guidance}
      <div className="overflow-x-auto rounded-md border border-border">
        <div className="grid min-w-[42rem] grid-cols-[minmax(6rem,0.7fr)_minmax(7rem,0.7fr)_minmax(0,1.4fr)_minmax(8rem,auto)] gap-3 border-b border-border px-3 py-2 text-xs font-semibold uppercase text-muted">
          <span>model_id</span>
          <span>error_code</span>
          <span>error_message</span>
          <span>diagnostic</span>
        </div>
        {failures.map((item, index) => (
          <div
            key={`${item.model_id ?? item.basin_id ?? 'basin'}-${index}`}
            className="grid min-w-[42rem] grid-cols-[minmax(6rem,0.7fr)_minmax(7rem,0.7fr)_minmax(0,1.4fr)_minmax(8rem,auto)] items-center gap-3 border-b border-border px-3 py-2 text-sm last:border-b-0"
          >
            <span className="truncate font-medium text-foreground">{item.model_id ?? '-'}</span>
            <span className="truncate text-danger">{item.error_code ?? item.status ?? '-'}</span>
            <span className="min-w-0 break-words text-muted">{item.error_message ?? '-'}</span>
            <span>
              {canCopyDiagnostics ? (
                <Button variant="outline" size="sm" onClick={() => void copyBasinDiagnostic(item)}>
                  <ClipboardCopy className="size-3.5" />
                  复制流域诊断
                </Button>
              ) : (
                <span className="text-muted">-</span>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

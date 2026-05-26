import { ArrowDown } from 'lucide-react'
import { useMemo, useState } from 'react'

import { StageDurationBar } from '@/components/charts/StageDurationBar'
import { BasinFailures } from '@/components/monitoring/BasinFailures'
import { StageCard } from '@/components/monitoring/StageCard'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { STAGE_NAMES } from '@/lib/constants'
import type { PipelineStage } from '@/stores/monitoring'

interface StageListProps {
  stages: PipelineStage[]
  unavailableReason?: string | null
  showPendingPlaceholders?: boolean
}

const stageOrder = Object.keys(STAGE_NAMES)

function pendingStage(stage: string): PipelineStage {
  return {
    stage,
    display_status: 'pending',
    status: 'pending',
    duration_seconds: null,
    basin_progress: { completed: 0, total: 0, failed: 0 },
    basin_results_limit: 50,
    basin_results_total: 0,
    basin_results_returned: 0,
    basin_results_truncated: false,
    basin_results: [],
  }
}

export function StageList({ stages, unavailableReason, showPendingPlaceholders = true }: StageListProps) {
  const [expandedStage, setExpandedStage] = useState<string | null>(null)

  const orderedStages = useMemo(() => {
    const byName = new Map(stages.map((stage) => [stage.stage, stage]))
    const ordered = showPendingPlaceholders
      ? stageOrder.map((stage) => byName.get(stage) ?? pendingStage(stage))
      : stageOrder.flatMap((stage) => {
        const row = byName.get(stage)
        return row ? [row] : []
      })
    const extras = stages.filter((stage) => !stageOrder.includes(stage.stage))
    return [...ordered, ...extras]
  }, [showPendingPlaceholders, stages])

  if (unavailableReason) {
    return (
      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>七阶段流水线</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="rounded-md border border-dashed border-border p-4 text-sm text-muted" role="status">
            当前 source/cycle 的流水线阶段不可用：{unavailableReason}
          </div>
        </CardContent>
      </Card>
    )
  }

  if (!orderedStages.length) {
    return (
      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>七阶段流水线</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="rounded-md border border-dashed border-border p-4 text-sm text-muted" role="status">
            当前 source/cycle 暂无后端阶段记录。
          </div>
        </CardContent>
      </Card>
    )
  }

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>七阶段流水线</CardTitle>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="space-y-2">
          {orderedStages.map((stage, index) => {
            const status = stage.display_status ?? stage.status ?? 'pending'
            const canExpand = status === 'failed' || status === 'partially_failed'
            const expanded = expandedStage === stage.stage

            return (
              <div key={stage.stage} className="space-y-2">
                <StageCard
                  stage={stage}
                  expanded={expanded}
                  onToggle={() => setExpandedStage(expanded ? null : stage.stage)}
                />
                {canExpand && expanded ? <BasinFailures stage={stage} /> : null}
                {index < orderedStages.length - 1 ? (
                  <div className="flex justify-center text-muted" aria-hidden="true">
                    <ArrowDown className="size-4" />
                  </div>
                ) : null}
              </div>
            )
          })}
        </div>
        <StageDurationBar stages={orderedStages} />
      </CardContent>
    </Card>
  )
}

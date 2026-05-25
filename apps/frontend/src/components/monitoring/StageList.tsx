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

export function StageList({ stages }: StageListProps) {
  const [expandedStage, setExpandedStage] = useState<string | null>(null)

  const orderedStages = useMemo(() => {
    const byName = new Map(stages.map((stage) => [stage.stage, stage]))
    const ordered = stageOrder.map((stage) => byName.get(stage) ?? pendingStage(stage))
    const extras = stages.filter((stage) => !stageOrder.includes(stage.stage))
    return [...ordered, ...extras]
  }, [stages])

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

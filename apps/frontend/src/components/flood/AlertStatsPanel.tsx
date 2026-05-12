import { ALERT_LEVELS, ALERT_LEVEL_META, type AlertLevel } from '@/components/flood/alertLevels'
import { cn } from '@/lib/cn'
import type { FloodAlertSummary } from '@/stores/floodAlert'

interface AlertStatsPanelProps {
  summary: FloodAlertSummary | null
  selectedLevel?: AlertLevel | null
  loading?: boolean
  onLevelSelect: (level: AlertLevel) => void
}

export function AlertStatsPanel({
  summary,
  selectedLevel,
  loading = false,
  onLevelSelect,
}: AlertStatsPanelProps) {
  const countByLevel = new Map(summary?.levels.map((level) => [level.level, level.count]) ?? [])

  return (
    <aside className="flex min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-panel">
      <div className="border-b border-border px-4 py-3">
        <h2 className="text-base font-semibold text-foreground">预警统计</h2>
        <p className="mt-1 text-xs text-muted">
          {summary ? `${summary.usableCurves}/${summary.totalSegments} 条河段有可用频率曲线` : '等待预警数据'}
        </p>
      </div>

      <div className="flex-1 space-y-1 overflow-auto p-3">
        {loading ? (
          <div className="grid min-h-48 place-items-center text-sm text-muted">统计加载中...</div>
        ) : (
          ALERT_LEVELS.map((level) => {
            const meta = ALERT_LEVEL_META[level]
            const count = countByLevel.get(level) ?? 0
            const active = selectedLevel === level

            return (
              <button
                key={level}
                type="button"
                className={cn(
                  'grid w-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3 rounded-md px-3 py-2 text-left transition-colors hover:bg-background',
                  active && 'bg-background ring-1 ring-accent/30',
                )}
                onClick={() => onLevelSelect(level)}
                aria-pressed={active}
              >
                <span className="size-3 rounded-full" style={{ backgroundColor: meta.color }} aria-hidden />
                <span className="min-w-0">
                  <span className="block text-sm font-medium text-foreground">{meta.label}</span>
                  <span className="block text-xs text-muted">{meta.range}</span>
                </span>
                <span className="tabular-nums text-sm font-semibold text-foreground">{count} 条</span>
              </button>
            )
          })
        )}
      </div>

      {summary?.qualityNote || summary?.unavailableCount ? (
        <div className="border-t border-border px-4 py-3 text-xs text-muted">
          {summary.qualityNote ?? `无可用曲线河段 ${summary.unavailableCount} 条`}
        </div>
      ) : null}
    </aside>
  )
}

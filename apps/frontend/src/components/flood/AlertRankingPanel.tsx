import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { alertLevelColor, alertLevelLabel } from '@/components/flood/alertLevels'
import { cn } from '@/lib/cn'
import type { FloodAlertRanking, FloodAlertRankingItem } from '@/stores/floodAlert'

interface AlertRankingPanelProps {
  ranking: FloodAlertRanking | null
  limit: 10 | 20 | 50
  basinId?: string
  loading?: boolean
  onLimitChange: (limit: 10 | 20 | 50) => void
  onBasinChange: (basinId: string) => void
  onRowSelect: (item: FloodAlertRankingItem) => void
}

function formatNumber(value: number | null | undefined, digits = 1) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '-'
}

export function AlertRankingPanel({
  ranking,
  limit,
  basinId = '',
  loading = false,
  onLimitChange,
  onBasinChange,
  onRowSelect,
}: AlertRankingPanelProps) {
  return (
    <aside className="flex min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-panel">
      <div className="border-b border-border px-4 py-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-foreground">风险排名</h2>
            <p className="mt-1 text-xs text-muted">按重现期降序排列</p>
          </div>
          <div className="flex rounded-md border border-border p-0.5">
            {[10, 20, 50].map((option) => (
              <Button
                key={option}
                type="button"
                size="sm"
                variant={limit === option ? 'default' : 'ghost'}
                className="h-7 px-2 text-xs"
                onClick={() => onLimitChange(option as 10 | 20 | 50)}
              >
                {option}
              </Button>
            ))}
          </div>
        </div>
        <input
          aria-label="Basin filter"
          value={basinId}
          onChange={(event) => onBasinChange(event.target.value)}
          placeholder="流域过滤"
          className="mt-3 h-9 w-full rounded-md border border-border bg-panel px-3 text-sm text-foreground outline-none focus:ring-2 focus:ring-accent"
        />
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {loading ? (
          <div className="grid min-h-64 place-items-center text-sm text-muted">排名加载中...</div>
        ) : ranking?.items.length ? (
          <table className="w-full text-left text-sm">
            <thead className="sticky top-0 bg-panel text-xs text-muted">
              <tr className="border-b border-border">
                <th className="px-3 py-2 font-medium">#</th>
                <th className="px-2 py-2 font-medium">河段</th>
                <th className="px-2 py-2 text-right font-medium">T</th>
                <th className="px-3 py-2 font-medium">等级</th>
              </tr>
            </thead>
            <tbody>
              {ranking.items.map((item) => (
                <tr
                  key={`${item.riverSegmentId}-${item.rank}`}
                  className="cursor-pointer border-b border-border/70 hover:bg-background"
                  onClick={() => onRowSelect(item)}
                >
                  <td className="px-3 py-2 text-xs text-muted">{item.rank}</td>
                  <td className="max-w-32 px-2 py-2">
                    <div className="truncate font-medium text-foreground">
                      {item.segmentName || item.riverSegmentId}
                    </div>
                    <div className="truncate text-xs text-muted">
                      {item.basinName || item.basinVersionId || item.riverSegmentId}
                    </div>
                    <div className="text-xs text-muted">
                      Q {formatNumber(item.qValue)} {item.qUnit ?? 'm³/s'}
                    </div>
                  </td>
                  <td className="px-2 py-2 text-right tabular-nums text-foreground">
                    {formatNumber(item.returnPeriod)}
                  </td>
                  <td className="px-3 py-2">
                    <Badge
                      variant="outline"
                      className={cn('border-current bg-panel')}
                      style={{ color: alertLevelColor(item.warningLevel) }}
                    >
                      {alertLevelLabel(item.warningLevel)}
                    </Badge>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="grid min-h-64 place-items-center px-4 text-center text-sm text-muted">
            暂无排名数据
          </div>
        )}
      </div>
    </aside>
  )
}

import { Pause, Play } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { formatDate } from '@/lib/format'

interface AlertTimelineProps {
  validTimes: string[]
  selectedValidTime: string | null
  playing: boolean
  onSelect: (validTime: string | null) => void
  onTogglePlayback: () => void
}

function compactTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const day = String(date.getUTCDate()).padStart(2, '0')
  const hour = String(date.getUTCHours()).padStart(2, '0')
  return `${day}日 ${hour}Z`
}

export function AlertTimeline({
  validTimes,
  selectedValidTime,
  playing,
  onSelect,
  onTogglePlayback,
}: AlertTimelineProps) {
  return (
    <div className="rounded-lg border border-border bg-panel p-3">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-foreground">预报时刻</h2>
          <p className="text-xs text-muted">{selectedValidTime ? formatDate(selectedValidTime) : '最严重时刻'}</p>
        </div>
        <Button type="button" size="sm" variant="outline" onClick={onTogglePlayback} disabled={validTimes.length === 0}>
          {playing ? <Pause className="size-4" /> : <Play className="size-4" />}
          {playing ? '暂停' : '播放'}
        </Button>
      </div>
      <div className="flex gap-2 overflow-x-auto pb-1">
        <button
          type="button"
          className={`shrink-0 rounded-md border px-3 py-2 text-xs ${
            selectedValidTime === null
              ? 'border-accent bg-accent text-white'
              : 'border-border bg-panel text-foreground hover:bg-background'
          }`}
          onClick={() => onSelect(null)}
        >
          最严重
        </button>
        {validTimes.map((validTime) => (
          <button
            key={validTime}
            type="button"
            className={`shrink-0 rounded-md border px-3 py-2 text-xs ${
              selectedValidTime === validTime
                ? 'border-accent bg-accent text-white'
                : 'border-border bg-panel text-foreground hover:bg-background'
            }`}
            onClick={() => onSelect(validTime)}
          >
            {compactTime(validTime)}
          </button>
        ))}
      </div>
    </div>
  )
}

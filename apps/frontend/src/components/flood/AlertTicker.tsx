import { useEffect, useMemo, useState } from 'react'

import { alertLevelColor, alertLevelLabel, isAlertLevel, SUPER_WARNING_LEVELS } from '@/components/flood/alertLevels'
import type { FloodAlertRankingItem } from '@/stores/floodAlert'

interface AlertTickerProps {
  items: FloodAlertRankingItem[]
  onItemSelect: (item: FloodAlertRankingItem) => void
}

function formatValue(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(1) : '-'
}

export function AlertTicker({ items, onItemSelect }: AlertTickerProps) {
  const superWarningItems = useMemo(
    () => items.filter((item) => isAlertLevel(item.warningLevel) && SUPER_WARNING_LEVELS.has(item.warningLevel)),
    [items],
  )
  const [index, setIndex] = useState(0)

  useEffect(() => {
    if (superWarningItems.length <= 1) return undefined
    const timer = window.setInterval(() => {
      setIndex((current) => (current + 1) % superWarningItems.length)
    }, 3000)
    return () => window.clearInterval(timer)
  }, [superWarningItems.length])

  useEffect(() => {
    if (index >= superWarningItems.length) setIndex(0)
  }, [index, superWarningItems.length])

  const item = superWarningItems[index]

  if (!item) {
    return (
      <div className="rounded-md border border-border bg-panel px-4 py-2 text-sm text-muted" role="status">
        当前无超警河段
      </div>
    )
  }

  return (
    <button
      type="button"
      className="grid w-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3 rounded-md border border-border bg-panel px-4 py-2 text-left text-sm shadow-sm hover:bg-background"
      onClick={() => onItemSelect(item)}
    >
      <span
        className="rounded px-2 py-0.5 text-xs font-semibold text-white"
        style={{ backgroundColor: alertLevelColor(item.warningLevel) }}
      >
        {alertLevelLabel(item.warningLevel)}
      </span>
      <span className="min-w-0 truncate text-foreground">
        {item.segmentName || item.riverSegmentId} / {item.basinName || item.basinVersionId || '-'} / Q{' '}
        {formatValue(item.qValue)} / T {formatValue(item.returnPeriod)}
      </span>
      <span className="text-xs text-muted">
        {index + 1}/{superWarningItems.length}
      </span>
    </button>
  )
}

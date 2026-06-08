import type { ReactNode } from 'react'
import { Loader2, X } from 'lucide-react'

import { cn } from '@/lib/cn'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import { M11_POPUP_SOURCES } from '@/components/map/useHydroMetPopupProduct'

// 弹窗玻璃质感外壳：半透明 + 强 backdrop-blur + 细描边 + 大圆角 + ring + 阴影。
export const M11_POPUP_GLASS =
  'rounded-xl border border-white/50 bg-white/80 shadow-2xl ring-1 ring-black/5 backdrop-blur-xl supports-[backdrop-filter]:bg-white/70'

export function M11PopupShell({ children, testId }: { children: ReactNode; testId: string }) {
  return (
    <div className={cn('w-[min(25rem,82vw)] overflow-hidden', M11_POPUP_GLASS)} data-testid={testId}>
      {children}
    </div>
  )
}

export function M11PopupHeader({
  icon: Icon,
  title,
  subtitle,
  onClose,
}: {
  icon: typeof X
  title: string
  subtitle: string
  onClose?: () => void
}) {
  return (
    <div className="flex items-start justify-between gap-2.5 border-b border-white/40 px-4 py-3">
      <div className="flex min-w-0 items-start gap-2.5">
        <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-primary-600/10 text-primary-600">
          <Icon className="h-4 w-4" aria-hidden="true" />
        </span>
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold leading-tight text-neutral-900" title={title}>
            {title}
          </div>
          <div className="mt-0.5 text-[11px] uppercase tracking-wide text-neutral-500">{subtitle}</div>
        </div>
      </div>
      {onClose ? (
        <button
          type="button"
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-neutral-400 transition-colors hover:bg-black/5 hover:text-neutral-700"
          aria-label="关闭弹窗"
          onClick={onClose}
        >
          <X className="h-4 w-4" aria-hidden="true" />
        </button>
      ) : null}
    </div>
  )
}

/**
 * 弹窗内 source（GFS/IFS）+ 起报时间选择条。
 * 起报时间因后端仅 latest-product 至多一项；为空时诚实显示「暂无可用起报时间」。
 */
export function M11PopupSourceControls({
  source,
  onSourceChange,
  issueTimes,
  issueTime,
}: {
  source: HydroMetSource
  onSourceChange: (source: HydroMetSource) => void
  issueTimes: string[]
  issueTime: string | null
}) {
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-white/40 px-4 py-2.5" data-testid="m11-popup-source-controls">
      <div className="inline-flex items-center rounded-lg bg-black/[0.04] p-0.5" role="group" aria-label="预报源选择">
        {M11_POPUP_SOURCES.map((option) => (
          <button
            key={option}
            type="button"
            className={cn(
              'cursor-pointer rounded-md px-3 py-1 text-xs font-medium transition-all',
              source === option
                ? 'bg-white text-primary-700 shadow-sm ring-1 ring-black/5'
                : 'text-neutral-500 hover:text-neutral-800',
            )}
            aria-pressed={source === option}
            data-testid={`m11-popup-source-${option}`}
            onClick={() => onSourceChange(option)}
          >
            {option}
          </button>
        ))}
      </div>
      <label className="flex min-w-0 flex-1 items-center justify-end gap-1.5 text-[11px] text-neutral-500">
        <span className="shrink-0 uppercase tracking-wide">起报</span>
        {issueTimes.length > 0 ? (
          <select
            aria-label="起报时间选择"
            data-testid="m11-popup-issue-time"
            className="h-7 min-w-0 max-w-[12rem] flex-1 rounded-md border border-white/60 bg-white/60 px-1.5 font-mono text-[11px] text-neutral-800"
            value={issueTime ?? issueTimes[0]}
            onChange={() => undefined}
          >
            {issueTimes.map((time) => (
              <option key={time} value={time}>
                {time}
              </option>
            ))}
          </select>
        ) : (
          <span className="text-neutral-400" data-testid="m11-popup-issue-time-empty">
            暂无可用起报时间
          </span>
        )}
      </label>
    </div>
  )
}

export function M11PopupLoading({ children, testId }: { children: ReactNode; testId: string }) {
  return (
    <div
      className="m-4 flex items-center gap-2 rounded border border-white/50 bg-white/50 p-3 text-sm text-neutral-700"
      role="status"
      data-testid={testId}
    >
      <Loader2 className="h-4 w-4 animate-spin text-primary-600" aria-hidden="true" />
      {children}
    </div>
  )
}

export function M11PopupEmpty({ children, testId }: { children: ReactNode; testId: string }) {
  return (
    <div className="m-4 rounded border border-warning/40 bg-warning/10 p-3 text-sm text-neutral-900" role="status" data-testid={testId}>
      {children}
    </div>
  )
}

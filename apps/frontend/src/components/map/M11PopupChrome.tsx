import type { ReactNode } from 'react'
import { Loader2, X } from 'lucide-react'

import { cn } from '@/lib/cn'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import { M11_POPUP_SOURCES } from '@/components/map/useHydroMetPopupProduct'

// 弹窗玻璃质感外壳：半透明 + backdrop-blur + 细描边 + 圆角 + 阴影。
export const M11_POPUP_GLASS =
  'rounded-lg border border-white/40 bg-white/80 shadow-xl backdrop-blur-md supports-[backdrop-filter]:bg-white/65'

export function M11PopupShell({ children, testId }: { children: ReactNode; testId: string }) {
  return (
    <div className={cn('w-[min(26rem,82vw)] overflow-hidden', M11_POPUP_GLASS)} data-testid={testId}>
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
    <div className="flex items-start justify-between gap-2 border-b border-white/40 px-4 py-3">
      <div className="min-w-0">
        <div className="flex items-center gap-2 text-sm font-semibold text-neutral-900">
          <Icon className="h-4 w-4 shrink-0 text-primary-600" aria-hidden="true" />
          <span className="truncate" title={title}>
            {title}
          </span>
        </div>
        <div className="mt-0.5 text-xs text-neutral-700">{subtitle}</div>
      </div>
      {onClose ? (
        <button
          type="button"
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded text-neutral-500 hover:bg-white/60"
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
    <div className="flex flex-wrap items-center gap-3 border-b border-white/40 px-4 py-2" data-testid="m11-popup-source-controls">
      <div className="flex items-center gap-1" role="group" aria-label="预报源选择">
        {M11_POPUP_SOURCES.map((option) => (
          <button
            key={option}
            type="button"
            className={cn(
              'cursor-pointer rounded border px-2.5 py-1 text-xs font-medium transition-colors',
              source === option
                ? 'border-primary-600 bg-primary-600/15 text-primary-700'
                : 'border-white/50 bg-white/40 text-neutral-700 hover:bg-white/60',
            )}
            aria-pressed={source === option}
            data-testid={`m11-popup-source-${option}`}
            onClick={() => onSourceChange(option)}
          >
            {option}
          </button>
        ))}
      </div>
      <label className="flex min-w-0 flex-1 items-center gap-1.5 text-xs text-neutral-700">
        <span className="shrink-0">起报</span>
        {issueTimes.length > 0 ? (
          <select
            aria-label="起报时间选择"
            data-testid="m11-popup-issue-time"
            className="h-7 min-w-0 flex-1 rounded border border-white/50 bg-white/60 px-1 font-mono text-xs text-neutral-900"
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
          <span className="text-neutral-500" data-testid="m11-popup-issue-time-empty">
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

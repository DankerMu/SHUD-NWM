import type { ReactNode } from 'react'
import { Loader2, X } from 'lucide-react'

import { cn } from '@/lib/cn'
import type { HydroMetSource } from '@/lib/hydroMet/queryState'
import { M11_POPUP_SOURCES } from '@/components/map/useHydroMetPopupProduct'

// 弹窗玻璃质感外壳：深蓝玻璃 + 强 backdrop-blur + 细描边 + 大圆角 + 深投影（指挥舱风格）。
export const M11_POPUP_GLASS =
  'rounded-2xl border border-white/10 bg-slate-900/80 text-slate-100 shadow-[0_24px_64px_-16px_rgba(2,8,28,0.65)] ring-1 ring-white/10 backdrop-blur-2xl supports-[backdrop-filter]:bg-slate-900/65'

// 起报时间显示：ISO → 「MM-DD HH:mm UTC」，下拉更易读；option value 仍用原始 ISO。
function formatIssueTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const mm = String(date.getUTCMonth() + 1).padStart(2, '0')
  const dd = String(date.getUTCDate()).padStart(2, '0')
  const hh = String(date.getUTCHours()).padStart(2, '0')
  const mi = String(date.getUTCMinutes()).padStart(2, '0')
  return `${mm}-${dd} ${hh}:${mi} UTC`
}

export function M11PopupShell({ children, testId }: { children: ReactNode; testId: string }) {
  return (
    <div className={cn('w-[min(30rem,90vw)] overflow-hidden', M11_POPUP_GLASS)} data-testid={testId}>
      <div className="h-px bg-gradient-to-r from-transparent via-cyan-400/60 to-transparent" aria-hidden="true" />
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
    <div className="flex items-start justify-between gap-2.5 border-b border-white/10 px-4 py-3">
      <div className="flex min-w-0 items-start gap-2.5">
        <span className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-cyan-400/10 text-cyan-300 ring-1 ring-inset ring-cyan-400/30 shadow-[0_0_16px_rgba(34,211,238,0.2)]">
          <Icon className="h-4 w-4" aria-hidden="true" />
        </span>
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold leading-tight text-slate-50" title={title}>
            {title}
          </div>
          <div className="mt-0.5 text-[11px] uppercase tracking-[0.14em] text-cyan-300/80">{subtitle}</div>
        </div>
      </div>
      {onClose ? (
        <button
          type="button"
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-slate-400 transition-colors hover:bg-white/10 hover:text-slate-100"
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
  onIssueTimeChange,
}: {
  source: HydroMetSource
  onSourceChange: (source: HydroMetSource) => void
  issueTimes: string[]
  issueTime: string | null
  onIssueTimeChange?: (issueTime: string) => void
}) {
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-white/10 px-4 py-2.5" data-testid="m11-popup-source-controls">
      <div className="inline-flex items-center rounded-lg bg-white/5 p-0.5 ring-1 ring-inset ring-white/10" role="group" aria-label="预报源选择">
        {M11_POPUP_SOURCES.map((option) => (
          <button
            key={option}
            type="button"
            className={cn(
              'cursor-pointer rounded-md px-3 py-1 text-xs font-medium transition-all',
              source === option
                ? 'bg-cyan-400/15 text-cyan-200 ring-1 ring-inset ring-cyan-400/40'
                : 'text-slate-400 hover:text-slate-100',
            )}
            aria-pressed={source === option}
            data-testid={`m11-popup-source-${option}`}
            onClick={() => onSourceChange(option)}
          >
            {option}
          </button>
        ))}
      </div>
      <label className="flex min-w-0 flex-1 items-center justify-end gap-1.5 text-[11px] text-slate-400">
        <span className="shrink-0 uppercase tracking-wide">起报</span>
        {issueTimes.length > 0 ? (
          <select
            aria-label="起报时间选择"
            data-testid="m11-popup-issue-time"
            className="h-7 min-w-0 max-w-[12rem] flex-1 cursor-pointer rounded-md border border-white/15 bg-white/5 px-1.5 font-mono text-[11px] text-slate-100 transition-colors [color-scheme:dark] hover:border-cyan-400/50 focus:border-cyan-400 focus:outline-none"
            value={issueTime ?? issueTimes[0]}
            onChange={(event) => onIssueTimeChange?.(event.target.value)}
            disabled={!onIssueTimeChange}
          >
            {issueTimes.map((time) => (
              <option key={time} value={time}>
                {formatIssueTime(time)}
              </option>
            ))}
          </select>
        ) : (
          <span className="text-slate-500" data-testid="m11-popup-issue-time-empty">
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
      className="m-4 flex items-center gap-2 rounded-lg border border-white/10 bg-white/5 p-3 text-sm text-slate-200"
      role="status"
      data-testid={testId}
    >
      <Loader2 className="h-4 w-4 animate-spin text-cyan-300" aria-hidden="true" />
      {children}
    </div>
  )
}

export function M11PopupEmpty({ children, testId }: { children: ReactNode; testId: string }) {
  return (
    <div className="m-4 rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-sm text-amber-100" role="status" data-testid={testId}>
      {children}
    </div>
  )
}

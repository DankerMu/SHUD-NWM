import { ShieldAlert, Waves } from 'lucide-react'

import { cn } from '@/lib/cn'
import type { QhhLatestProduct } from '@/pages/hydroMet/bootstrap'

// 静态领域知识：洪水重现期分级图例。纯前端常量，不经任何 API、不代表已发布产品。
export type ReturnPeriodLegendEntry = {
  level: string
  label: string
  color: string
}

export const RETURN_PERIOD_LEGEND: readonly ReturnPeriodLegendEntry[] = [
  { level: '2y', label: '2 年一遇', color: '#2563eb' },
  { level: '5y', label: '5 年一遇', color: '#0d9488' },
  { level: '10y', label: '10 年一遇', color: '#ca8a04' },
  { level: '20y', label: '20 年一遇', color: '#ea580c' },
  { level: '50y', label: '50 年一遇', color: '#dc2626' },
  { level: '100y', label: '100 年一遇', color: '#7e22ce' },
]

export const RETURN_PERIOD_RESULT_UNAVAILABLE = 'RETURN_PERIOD_RESULT_UNAVAILABLE'

type ReturnPeriodStatus = QhhLatestProduct['availability']['return_period_status']

function readReturnPeriodStatus(product: QhhLatestProduct): ReturnPeriodStatus {
  // 独立 supplemental 字段；与产品整体 ready 解耦（产品可 ready 但此字段 unavailable）。
  return product.availability?.return_period_status ?? 'unavailable'
}

export function ReturnPeriodSection({ product }: { product: QhhLatestProduct }) {
  const status = readReturnPeriodStatus(product)
  const unavailable = !productReady(product) || status !== 'ready'

  return (
    <section className="rounded-md border border-neutral-300 bg-white p-4" data-testid="hydro-met-return-period-section">
      <div className="flex items-center gap-2">
        <Waves className="h-4 w-4 text-primary-600" aria-hidden="true" />
        <h2 className="text-base font-semibold text-neutral-900">洪水重现期</h2>
      </div>

      {unavailable ? (
        <div
          className="mt-3 flex items-start gap-2 rounded border border-warning/40 bg-warning/10 p-3 text-sm text-neutral-900"
          role="status"
          data-testid="hydro-met-return-period-unavailable"
        >
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden="true" />
          <div>
            <div className="font-semibold">暂未发布正式产品</div>
            <p className="mt-1 text-neutral-700">
              当前流域尚无洪水重现期基线，正式洪水重现期产品暂未发布；不展示任何河段重现期数据。下方仅为静态分级说明。
            </p>
          </div>
        </div>
      ) : null}

      <ReturnPeriodLegend />
    </section>
  )
}

export function ReturnPeriodLegend() {
  return (
    <div className="mt-3" data-testid="hydro-met-return-period-legend">
      <div className="text-xs font-medium uppercase text-neutral-700">分级图例（静态领域知识）</div>
      <ul className="mt-2 grid grid-cols-2 gap-2 text-xs min-[420px]:grid-cols-3">
        {RETURN_PERIOD_LEGEND.map((entry) => (
          <li
            key={entry.level}
            className="flex items-center gap-2 rounded border border-neutral-300 p-2"
            data-testid="hydro-met-return-period-legend-item"
            data-level={entry.level}
          >
            <span
              className="h-3 w-3 shrink-0 rounded-sm border border-neutral-400"
              style={{ backgroundColor: entry.color }}
              aria-hidden="true"
            />
            <span className="font-mono font-semibold text-neutral-900">{entry.level}</span>
            <span className="text-neutral-700">{entry.label}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

type ProductStatusTone = 'ready' | 'degraded' | 'unavailable'

type ProductStatusEntry = {
  key: string
  label: string
  tone: ProductStatusTone
  detail: string
}

const STATUS_TONE_META: Record<ProductStatusTone, { label: string; className: string; dot: string }> = {
  ready: { label: 'ready', className: 'border-primary-200 bg-primary-50 text-primary-900', dot: 'bg-primary-600' },
  degraded: { label: 'degraded', className: 'border-warning/40 bg-warning/10 text-neutral-900', dot: 'bg-warning' },
  unavailable: { label: 'unavailable', className: 'border-danger/30 bg-danger/10 text-danger', dot: 'bg-danger' },
}

function reasonCodes(reasons: { code?: string }[] | undefined): string[] {
  return (reasons ?? []).map((reason) => reason?.code).filter((code): code is string => typeof code === 'string')
}

function isForcingCode(code: string): boolean {
  return code.startsWith('FORCING_')
}

function isQDownCode(code: string): boolean {
  return code.includes('RIVER') || code.includes('Q_DOWN') || code.includes('RUN_STATUS')
}

// 诚实展示红线：只有产品整体 availability.ready === true 时，任一维度才有资格显示 ready。
// 任何未被识别的 reason code（既不属 forcing 桶也不属 q_down 桶）不得静默落入绿色 ready，
// 而是归入 q_down 桶显示为不可用——宁可保守标"不可用/未知"，绝不虚假肯定。
function productReady(product: QhhLatestProduct): boolean {
  return product.availability?.ready === true
}

function forcingTone(product: QhhLatestProduct): ProductStatusEntry {
  const reasons = reasonCodes(product.availability?.unavailable_reasons).filter(isForcingCode)
  if (reasons.length > 0) {
    return { key: 'forcing', label: '气象 forcing', tone: 'unavailable', detail: reasons.join(', ') }
  }
  if (!productReady(product)) {
    return { key: 'forcing', label: '气象 forcing', tone: 'unavailable', detail: '产品整体不可用' }
  }
  if (product.shorter_horizon) {
    return { key: 'forcing', label: '气象 forcing', tone: 'degraded', detail: '可用时效短于预期' }
  }
  return { key: 'forcing', label: '气象 forcing', tone: 'ready', detail: '真实 forcing inventory 可用' }
}

function qDownTone(product: QhhLatestProduct): ProductStatusEntry {
  const allReasons = reasonCodes(product.availability?.unavailable_reasons)
  // q_down 桶吸收自身明确 code，以及任何未被 forcing 桶认领的"未知" reason code，
  // 避免未分类 code 漏到绿色 ready。
  const reasons = allReasons.filter((code) => isQDownCode(code) || !isForcingCode(code))
  if (reasons.length > 0) {
    return { key: 'q_down', label: '河段流量 q_down', tone: 'unavailable', detail: reasons.join(', ') }
  }
  if (!productReady(product)) {
    return { key: 'q_down', label: '河段流量 q_down', tone: 'unavailable', detail: '产品整体不可用' }
  }
  if (product.segment_count <= 0) {
    return { key: 'q_down', label: '河段流量 q_down', tone: 'degraded', detail: '无河段流量候选' }
  }
  return { key: 'q_down', label: '河段流量 q_down', tone: 'ready', detail: `${product.segment_count} 河段候选` }
}

function returnPeriodTone(product: QhhLatestProduct): ProductStatusEntry {
  // return_period_status 独立于产品 ready；只有 ready/unavailable 两态（无 degraded）。
  const status = readReturnPeriodStatus(product)
  if (!productReady(product)) {
    return { key: 'return_period', label: '洪水重现期', tone: 'unavailable', detail: '产品整体不可用' }
  }
  if (status === 'ready') {
    return { key: 'return_period', label: '洪水重现期', tone: 'ready', detail: '已有重现期基线' }
  }
  const detail = reasonCodes(product.availability?.return_period_reasons)[0] ?? RETURN_PERIOD_RESULT_UNAVAILABLE
  return { key: 'return_period', label: '洪水重现期', tone: 'unavailable', detail }
}

export function ProductStatusBar({ product }: { product: QhhLatestProduct }) {
  const entries = [forcingTone(product), qDownTone(product), returnPeriodTone(product)]

  return (
    <section
      className="rounded-md border border-neutral-300 bg-white p-4"
      data-testid="hydro-met-product-status-bar"
      aria-label="产品状态条"
    >
      <div className="text-xs font-medium uppercase text-neutral-700">产品状态</div>
      <ul className="mt-2 flex flex-wrap gap-2">
        {entries.map((entry) => {
          const meta = STATUS_TONE_META[entry.tone]
          return (
            <li
              key={entry.key}
              className={cn('flex items-center gap-2 rounded border px-2 py-1 text-xs', meta.className)}
              data-testid={`hydro-met-status-${entry.key}`}
              data-tone={entry.tone}
              title={entry.detail}
            >
              <span className={cn('h-2 w-2 shrink-0 rounded-full', meta.dot)} aria-hidden="true" />
              <span className="font-semibold">{entry.label}</span>
              <span className="font-mono text-neutral-700">{meta.label}</span>
            </li>
          )
        })}
      </ul>
    </section>
  )
}

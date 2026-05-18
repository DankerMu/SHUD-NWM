import type { CSSProperties, ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ChevronRight, Clock, Layers, ListFilter, MapPinned, Search } from 'lucide-react'

import { cn } from '@/lib/cn'
import type { M11QueryState } from '@/lib/m11/queryState'
import { serializeM11QueryState } from '@/lib/m11/queryState'
import { m11VisualTokens } from '@/lib/m11/visualTokens'

interface M11LayoutProps {
  title: string
  subtitle: string
  state: M11QueryState
  left: ReactNode
  right: ReactNode
  mapLabel: string
  mapTitle: string
  mapMeta: string
  timelineLabel?: string
  children?: ReactNode
}

export function M11Layout({
  title,
  subtitle,
  state,
  left,
  right,
  mapLabel,
  mapTitle,
  mapMeta,
  timelineLabel = 'Analysis / Forecast',
  children,
}: M11LayoutProps) {
  const timelineQuery = serializeM11QueryState(state)

  return (
    <div
      className="grid min-h-[calc(100vh-88px)] gap-0 overflow-hidden rounded-md border border-neutral-300 bg-white shadow-md xl:grid-cols-[var(--m11-left-panel-width)_minmax(0,1fr)_var(--m11-right-panel-width)] xl:grid-rows-[minmax(0,1fr)_var(--m11-timeline-height)]"
      data-testid="m11-shell"
      style={{
        '--m11-left-panel-width': m11VisualTokens.leftPanelWidth,
        '--m11-right-panel-width': m11VisualTokens.rightPanelWidth,
        '--m11-timeline-height': m11VisualTokens.timelineHeight,
      } as CSSProperties}
    >
      <aside className="min-h-0 border-b border-neutral-300 bg-white xl:border-b-0 xl:border-r" aria-label="M11 左侧面板">
        <PanelHeader title={title} subtitle={subtitle} />
        <div className="space-y-5 p-4 text-sm">{left}</div>
      </aside>

      <section className="relative min-h-[30rem] overflow-hidden bg-[#d7e7ef] xl:min-h-0" aria-label={mapLabel}>
        <div className="absolute inset-0 bg-[linear-gradient(135deg,rgba(21,101,192,0.14)_0,rgba(79,195,247,0.18)_42%,rgba(76,175,80,0.13)_100%)]" />
        <div className="absolute inset-5 rounded-md border border-white/70 bg-white/20 shadow-inner" />
        <div className="absolute left-8 top-8 rounded-md bg-white/95 p-4 shadow-lg">
          <div className="flex items-center gap-2 text-base font-semibold text-neutral-900">
            <MapPinned className="h-5 w-5 text-primary-600" aria-hidden="true" />
            {mapTitle}
          </div>
          <p className="mt-1 max-w-md text-sm text-neutral-700">{mapMeta}</p>
        </div>
        <div className="absolute bottom-8 left-8 right-8 grid gap-3 sm:grid-cols-3">
          {[
            ['数据源', state.source.toUpperCase()],
            ['图层', state.layer],
            ['底图', state.basemap],
          ].map(([label, value]) => (
            <div key={label} className="rounded-md border border-white/80 bg-white/90 p-3 shadow-sm">
              <div className="text-xs text-neutral-700">{label}</div>
              <div className="mt-1 font-mono text-sm text-neutral-900">{value}</div>
            </div>
          ))}
        </div>
        {children}
      </section>

      <aside
        className="min-h-0 border-t border-neutral-300 bg-white xl:border-l xl:border-t-0"
        aria-label="M11 右侧面板"
      >
        <PanelHeader title="运行态势" subtitle="摘要与图例" />
        <div className="space-y-5 p-4 text-sm">{right}</div>
      </aside>

      <section
        className="flex h-16 items-center gap-4 border-t border-neutral-300 bg-white px-4 text-sm xl:col-span-3"
        aria-label="M11 时间轴"
      >
        <Clock className="h-4 w-4 text-primary-600" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center justify-between gap-3">
            <span className="font-medium text-neutral-900">{state.validTime ?? '等待图层有效时间'}</span>
            <span className="text-xs text-neutral-700">{timelineLabel}</span>
          </div>
          <div className="mt-2 h-2 rounded-full bg-neutral-100">
            <div className="h-2 w-1/2 rounded-full bg-primary-600" />
          </div>
        </div>
        <code className="hidden max-w-[24rem] truncate text-xs text-neutral-700 lg:block">
          {timelineQuery ? `?${timelineQuery}` : 'default query state'}
        </code>
      </section>
    </div>
  )
}

function PanelHeader({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <div className="border-b border-neutral-300 bg-primary-50 px-4 py-3">
      <h1 className="text-base font-semibold leading-6 text-primary-700">{title}</h1>
      <p className="mt-0.5 text-xs leading-5 text-neutral-700">{subtitle}</p>
    </div>
  )
}

export function StateReadout({ state, basinId }: { state: M11QueryState; basinId?: string }) {
  return (
    <dl className="grid grid-cols-[7.5rem_minmax(0,1fr)] gap-x-3 gap-y-2 rounded-md border border-neutral-300 bg-neutral-50 p-3 text-xs">
      {basinId ? (
        <>
          <dt className="text-neutral-700">basinId</dt>
          <dd className="font-mono text-neutral-900">{basinId}</dd>
        </>
      ) : null}
      {Object.entries(state).map(([key, value]) => (
        <div key={key} className="contents">
          <dt className="text-neutral-700">{key}</dt>
          <dd className="min-w-0 truncate font-mono text-neutral-900">{value ?? '-'}</dd>
        </div>
      ))}
    </dl>
  )
}

export function LayerList({ activeLayer }: { activeLayer: string }) {
  const layers = [
    ['discharge', '河段径流'],
    ['water-level', '河段水位'],
    ['flood-return-period', '洪水重现期'],
    ['warning-level', '预警等级'],
  ]

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-sm font-semibold text-neutral-900">
        <Layers className="h-4 w-4 text-primary-600" aria-hidden="true" />
        水文图层
      </div>
      <div className="space-y-1">
        {layers.map(([value, label]) => (
          <div
            key={value}
            className={cn(
              'flex items-center justify-between rounded px-2 py-1.5 text-sm',
              activeLayer === value ? 'bg-primary-100 text-primary-700' : 'text-neutral-700',
            )}
          >
            <span>{label}</span>
            <span className="h-2.5 w-2.5 rounded-full bg-primary-600" />
          </div>
        ))}
      </div>
    </div>
  )
}

export function BasinLink({ to, children }: { to: string; children: ReactNode }) {
  return (
    <Link
      to={to}
      className="flex h-9 items-center justify-between rounded border border-primary-600 px-3 text-sm font-medium text-primary-600 transition-colors hover:bg-primary-50"
    >
      {children}
      <ChevronRight className="h-4 w-4" aria-hidden="true" />
    </Link>
  )
}

export function SegmentSearchStub({ query }: { query: string | null }) {
  return (
    <div className="space-y-2">
      <label className="flex h-9 items-center gap-2 rounded border border-neutral-300 bg-white px-3 text-sm">
        <Search className="h-4 w-4 text-neutral-500" aria-hidden="true" />
        <span className="min-w-0 truncate text-neutral-700">{query ?? '搜索河段'}</span>
      </label>
      <div className="flex items-center gap-2 text-xs text-neutral-700">
        <ListFilter className="h-4 w-4" aria-hidden="true" />
        预警筛选与河段列表将在后续数据合同中接入
      </div>
    </div>
  )
}

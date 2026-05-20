import { useState, type CSSProperties, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ChevronRight, Clock, MapPinned, PanelLeftClose, PanelRightClose } from 'lucide-react'

import type { components } from '@/api/types'
import type { BasinSegmentRow, LayerState, OverviewBasin, SourceScenarioSelectionState } from '@/lib/m11/overviewDataContracts'
import type { M11QueryPatch, M11QueryState } from '@/lib/m11/queryState'
import { serializeM11QueryState } from '@/lib/m11/queryState'
import { m11VisualTokens } from '@/lib/m11/visualTokens'
import { type M11TimelineDerivedTimes, M11MapSurface, M11Timeline } from '@/pages/m11/M11Controls'
import type { M11MapCameraFit, M11MapCameraFlyTo, M11MapOverlayInteraction } from '@/components/map/M11MapLibreSurface'
import { cn } from '@/lib/cn'

interface M11LayoutProps {
  title: string
  subtitle: string
  state: M11QueryState
  left: ReactNode
  right: ReactNode
  mapLabel: string
  mapTitle: string
  mapMeta: string
  layers?: LayerState[]
  basins?: OverviewBasin[]
  visibleBasinIds?: string[]
  basinSegments?: BasinSegmentRow[]
  selectedSegmentId?: string | null
  selectedSegmentGeometry?: components['schemas']['GeoJsonLineString'] | null
  sourceSelection?: SourceScenarioSelectionState | null
  derivedTimeline?: M11TimelineDerivedTimes | null
  fitTo?: M11MapCameraFit | null
  flyTo?: M11MapCameraFlyTo | null
  onMapOverlayHover?: (interaction: M11MapOverlayInteraction | null) => void
  onMapOverlayClick?: (interaction: M11MapOverlayInteraction) => void
  onQueryChange?: (patch: M11QueryPatch) => void
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
  layers = [],
  basins = [],
  visibleBasinIds,
  basinSegments = [],
  selectedSegmentId = null,
  selectedSegmentGeometry = null,
  sourceSelection = null,
  derivedTimeline = null,
  fitTo = null,
  flyTo = null,
  onMapOverlayHover,
  onMapOverlayClick,
  onQueryChange,
  children,
}: M11LayoutProps) {
  const timelineQuery = serializeM11QueryState(state)
  const [leftCollapsed, setLeftCollapsed] = useState(false)
  const [rightCollapsed, setRightCollapsed] = useState(false)

  return (
    <div
      className={cn(
        'grid h-[calc(100vh-var(--m11-nav-height)-32px)] min-h-[42rem] gap-0 overflow-hidden rounded-[var(--radius-md)] border border-neutral-300 bg-white shadow-[var(--shadow-md)] min-[1200px]:grid-rows-[minmax(0,1fr)_var(--m11-timeline-height)]',
        leftCollapsed && rightCollapsed
          ? 'min-[1200px]:grid-cols-[2.75rem_minmax(0,1fr)_2.75rem]'
          : leftCollapsed
            ? 'min-[1200px]:grid-cols-[2.75rem_minmax(0,1fr)_300px] min-[1440px]:grid-cols-[2.75rem_minmax(0,1fr)_var(--m11-right-panel-width)]'
            : rightCollapsed
              ? 'min-[1200px]:grid-cols-[260px_minmax(0,1fr)_2.75rem] min-[1440px]:grid-cols-[var(--m11-left-panel-width)_minmax(0,1fr)_2.75rem]'
              : 'min-[1200px]:grid-cols-[260px_minmax(0,1fr)_300px] min-[1440px]:grid-cols-[var(--m11-left-panel-width)_minmax(0,1fr)_var(--m11-right-panel-width)]',
      )}
      data-testid="m11-shell"
      data-layout="map-first-compact"
      data-left-panel={leftCollapsed ? 'collapsed' : 'expanded'}
      data-right-panel={rightCollapsed ? 'collapsed' : 'expanded'}
      style={{
        '--m11-left-panel-width': m11VisualTokens.leftPanelWidth,
        '--m11-right-panel-width': m11VisualTokens.rightPanelWidth,
        '--m11-timeline-height': m11VisualTokens.timelineHeight,
        '--m11-nav-height': m11VisualTokens.navHeight,
      } as CSSProperties}
    >
      <aside className="min-h-0 overflow-hidden border-b border-neutral-300 bg-white min-[1200px]:border-b-0 min-[1200px]:border-r" aria-label="M11 左侧面板">
        <PanelHeader title={title} subtitle={subtitle} collapsed={leftCollapsed} side="left" onToggle={() => setLeftCollapsed((value) => !value)} />
        <div className={cn('space-y-5 p-4 text-sm', leftCollapsed && 'hidden')}>{left}</div>
      </aside>

      <section className="relative min-h-[30rem] overflow-hidden bg-[#d7e7ef] min-[1200px]:min-h-0" aria-label={mapLabel}>
        <M11MapSurface
          state={state}
          layers={layers}
          basins={basins}
          visibleBasinIds={visibleBasinIds}
          basinSegments={basinSegments}
          selectedSegmentId={selectedSegmentId}
          selectedSegmentGeometry={selectedSegmentGeometry}
          onQueryChange={onQueryChange}
          fitTo={fitTo}
          flyTo={flyTo}
          onOverlayHover={onMapOverlayHover}
          onOverlayClick={onMapOverlayClick}
        />
        <div className="absolute left-8 top-8 z-[100] rounded-md bg-white/95 p-4 shadow-lg">
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
        className="min-h-0 overflow-hidden border-t border-neutral-300 bg-white min-[1200px]:border-l min-[1200px]:border-t-0"
        aria-label="M11 右侧面板"
      >
        <PanelHeader title="运行态势" subtitle="摘要与图例" collapsed={rightCollapsed} side="right" onToggle={() => setRightCollapsed((value) => !value)} />
        <div className={cn('space-y-5 p-4 text-sm', rightCollapsed && 'hidden')}>{right}</div>
      </aside>

      <div className="relative min-h-0 min-[1200px]:col-span-3" data-testid="m11-timeline-region">
        <M11Timeline
          state={state}
          layers={layers}
          sourceSelection={sourceSelection}
          derivedTimes={derivedTimeline}
          onQueryChange={onQueryChange}
        />
        <div className="pointer-events-none absolute right-4 top-1 hidden items-center gap-2 lg:flex">
          <Clock className="h-3.5 w-3.5 text-primary-600" aria-hidden="true" />
          <code className="max-w-[24rem] truncate text-xs text-neutral-700">
            {timelineQuery ? `?${timelineQuery}` : 'default query state'}
          </code>
        </div>
      </div>
    </div>
  )
}

function PanelHeader({
  title,
  subtitle,
  collapsed = false,
  side,
  onToggle,
}: {
  title: string
  subtitle: string
  collapsed?: boolean
  side?: 'left' | 'right'
  onToggle?: () => void
}) {
  const ToggleIcon = side === 'right' ? PanelRightClose : PanelLeftClose
  return (
    <div className="border-b border-neutral-300 bg-primary-50 px-4 py-3">
      <div className={cn('flex items-start justify-between gap-2', collapsed && 'min-[1200px]:justify-center')}>
        <div className={cn(collapsed && 'min-[1200px]:hidden')}>
          <h1 className="text-base font-semibold leading-6 text-primary-700">{title}</h1>
          <p className="mt-0.5 text-xs leading-5 text-neutral-700">{subtitle}</p>
        </div>
        {onToggle ? (
          <button
            type="button"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded text-primary-700 hover:bg-primary-100"
            aria-label={`${collapsed ? '展开' : '折叠'}${side === 'right' ? '右侧' : '左侧'}面板`}
            aria-expanded={!collapsed}
            onClick={onToggle}
          >
            {collapsed ? <ChevronRight className="h-4 w-4" aria-hidden="true" /> : <ToggleIcon className="h-4 w-4" aria-hidden="true" />}
          </button>
        ) : null}
      </div>
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

export function BasinLink({ to, children, disabled = false }: { to: string; children: ReactNode; disabled?: boolean }) {
  if (disabled) {
    return (
      <span
        aria-disabled="true"
        className="flex h-9 cursor-not-allowed items-center justify-between rounded border border-neutral-300 px-3 text-sm font-medium text-neutral-500"
      >
        {children}
        <ChevronRight className="h-4 w-4" aria-hidden="true" />
      </span>
    )
  }

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

import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { AlertTriangle, CloudRain, GitBranch, Loader2, RadioTower, Route, Waves, type LucideIcon } from 'lucide-react'
import { useLocation, useNavigate } from 'react-router-dom'

import { cn } from '@/lib/cn'
import {
  hydroMetSources,
  mergeHydroMetQueryState,
  needsHydroMetQueryReplacement,
  parseHydroMetQueryState,
  serializeHydroMetQueryState,
  type HydroMetQueryPatch,
} from '@/lib/hydroMet/queryState'
import { HYDRO_MET_COORDINATES_UNAVAILABLE, getHydroMetStationCoordinates, sanitizeHydroMetMessage } from '@/lib/hydroMet/runtime'
import {
  HYDRO_MET_RIVER_SEGMENT_LIMIT,
  HYDRO_MET_STATION_LIMIT,
  loadHydroMetBootstrap,
  type HydroMetBootstrapResult,
  type HydroMetRiverSegmentFeature,
  type HydroMetStation,
  type QhhLatestProduct,
} from '@/pages/hydroMet/bootstrap'

type LoadState =
  | { kind: 'loading' }
  | { kind: 'loaded'; result: HydroMetBootstrapResult }
  | { kind: 'error'; message: string }

export function HydroMetPage() {
  const location = useLocation()
  const navigate = useNavigate()
  const state = useMemo(() => parseHydroMetQueryState(location.search), [location.search])
  const [loadState, setLoadState] = useState<LoadState>({ kind: 'loading' })
  const [queryValidationMessages, setQueryValidationMessages] = useState<string[]>([])

  useEffect(() => {
    if (state.validationReasons.length > 0) setQueryValidationMessages(state.validationReasons)
  }, [state.validationReasons])

  useEffect(() => {
    if (!needsHydroMetQueryReplacement(location.search)) return
    navigate({ pathname: '/hydro-met', search: serializeHydroMetQueryState(state) }, { replace: true })
  }, [location.search, navigate, state])

  useEffect(() => {
    let cancelled = false
    setLoadState({ kind: 'loading' })
    void loadHydroMetBootstrap({ source: state.source, cycle: state.cycle }).then(
      (result) => {
        if (!cancelled) setLoadState({ kind: 'loaded', result })
      },
      (error) => {
        if (!cancelled) setLoadState({ kind: 'error', message: error instanceof Error ? error.message : '水文气象启动失败' })
      },
    )
    return () => {
      cancelled = true
    }
  }, [state.cycle, state.source])

  const updateState = (patch: HydroMetQueryPatch) => {
    const next = mergeHydroMetQueryState(state, patch)
    setQueryValidationMessages([])
    navigate({ pathname: '/hydro-met', search: serializeHydroMetQueryState(next) })
  }

  return (
    <div className="space-y-3" data-testid="hydro-met-page">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-primary-900">水文气象展示</h1>
          <p className="text-sm text-neutral-700">QHH latest-product bootstrap · 河段流量 q_down 与气象 forcing inventory</p>
        </div>
        <div className="flex rounded-md border border-neutral-300 bg-white p-1" role="tablist" aria-label="水文气象数据源">
          {hydroMetSources.map((source) => (
            <button
              key={source}
              type="button"
              className={cn(
                'flex h-9 cursor-pointer items-center gap-2 rounded px-3 text-sm font-medium transition-colors',
                state.source === source ? 'bg-primary-600 text-white' : 'text-neutral-700 hover:bg-neutral-100',
              )}
              onClick={() => updateState({ source, cycle: null })}
              role="tab"
              aria-selected={state.source === source}
            >
              <CloudRain className="h-4 w-4" aria-hidden="true" />
              {source}
            </button>
          ))}
        </div>
      </div>

      <section className="grid gap-3 rounded-md border border-neutral-300 bg-white p-3 min-[860px]:grid-cols-[minmax(0,1fr)_minmax(18rem,24rem)]">
        <div className="grid gap-2 min-[680px]:grid-cols-3">
          <ControlField label="Source">
            <span className="font-mono text-sm text-neutral-900">{state.source}</span>
          </ControlField>
          <ControlField label="Cycle">
            <input
              aria-label="水文气象 cycle"
              className="h-9 w-full rounded border border-neutral-300 px-2 font-mono text-xs"
              placeholder="latest"
              value={state.cycle ?? ''}
              onChange={(event) => updateState({ cycle: event.target.value || null })}
            />
          </ControlField>
          <ControlField label="Mode">
            <button
              type="button"
              className="h-9 cursor-pointer rounded border border-neutral-300 px-3 text-sm text-neutral-700 transition-colors hover:bg-neutral-100"
              onClick={() => updateState({ cycle: null })}
            >
              latest
            </button>
          </ControlField>
        </div>
        <div className="rounded border border-primary-100 bg-primary-50 p-3 text-xs text-neutral-700" data-testid="hydro-met-no-fake-data">
          不绘制假曲线，不手工输入 run_id、forcing_version_id、basin_version_id 或 river_network_version_id。站点 forcing 图表属于 #208，河段 q_down 流量图表属于 #209。
        </div>
      </section>

      {queryValidationMessages.length > 0 ? (
        <StatusPanel tone="warning" title="查询参数已更正" messages={queryValidationMessages} testId="hydro-met-query-validation" />
      ) : null}

      {loadState.kind === 'loading' ? <LoadingPanel /> : null}
      {loadState.kind === 'error' ? <StatusPanel tone="danger" title="水文气象启动失败" messages={[loadState.message]} testId="hydro-met-load-error" /> : null}
      {loadState.kind === 'loaded' ? <HydroMetContent result={loadState.result} /> : null}
    </div>
  )
}

function HydroMetContent({ result }: { result: HydroMetBootstrapResult }) {
  if (result.status === 'latest-unavailable') {
    return (
      <StatusPanel
        tone="danger"
        title="latest-product 不可用"
        messages={result.latestReasons.length ? result.latestReasons : ['没有可展示的 QHH latest-product。']}
        product={result.product}
        testId="hydro-met-latest-unavailable"
      />
    )
  }

  if (result.status === 'latest-incomplete') {
    return (
      <StatusPanel
        tone="warning"
        title="latest-product 不完整"
        messages={result.latestReasons.length ? result.latestReasons : ['latest-product 缺少下游启动所需身份字段。']}
        product={result.product}
        testId="hydro-met-latest-incomplete"
      />
    )
  }

  if (result.status === 'cycle-unavailable') {
    return (
      <StatusPanel
        tone="warning"
        title="指定周期不可用"
        messages={result.latestReasons}
        product={result.product}
        testId="hydro-met-cycle-unavailable"
      />
    )
  }

  const product = result.product
  if (!product) {
    return <StatusPanel tone="danger" title="latest-product 不可用" messages={['latest-product 响应为空。']} testId="hydro-met-latest-unavailable" />
  }

  return (
    <div className="grid gap-3 min-[1180px]:grid-cols-[minmax(19rem,0.8fr)_minmax(0,1.1fr)_minmax(21rem,0.9fr)]">
      <aside className="space-y-3">
        <ProductPanel product={product} />
        {result.stationError ? <StatusPanel tone="warning" title="站点 inventory 部分失败" messages={[result.stationError]} testId="hydro-met-station-partial-failure" /> : null}
        {result.riverError ? <StatusPanel tone="warning" title="河段流量候选部分失败" messages={[result.riverError]} testId="hydro-met-river-partial-failure" /> : null}
      </aside>

      <section className="space-y-3">
        <InventoryPanel
          title="气象 forcing 站点"
          icon={RadioTower}
          summary={`${result.stations.length} / ${result.stationPage?.total_count ?? product.station_count} stations`}
          emptyText="站点列表为空：未生成替代站点，也不会自动切换到其他产品。"
          testId="hydro-met-station-list"
          emptyTestId="hydro-met-empty-stations"
        >
          {result.stations.slice(0, 12).map((station) => <StationRow key={station.station_id} station={station} />)}
        </InventoryPanel>

        <InventoryPanel
          title="河段流量候选"
          icon={GitBranch}
          summary={`${result.riverSegments.length} / ${result.riverSegmentCollection?.total ?? product.segment_count} river segments`}
          emptyText="河段列表为空：没有可展示的河段流量候选，且不会填充假河段。"
          testId="hydro-met-river-list"
          emptyTestId="hydro-met-empty-rivers"
        >
          {result.riverSegments.slice(0, 12).map((feature) => <RiverSegmentRow key={feature.properties.river_segment_id} feature={feature} />)}
        </InventoryPanel>
      </section>

      <aside className="space-y-3">
        <PlaceholderPanel
          icon={CloudRain}
          title="站点 forcing 图表占位"
          lines={[
            '后续 #208 在选中站点后调用 station series API。',
            '本页只展示真实 inventory 与 latest-product provenance，不绘制假 forcing 曲线。',
          ]}
        />
        <PlaceholderPanel
          icon={Waves}
          title="河段 q_down 流量图表占位"
          lines={[
            '后续 #209 在选中河段后调用 forecast-series，变量固定为 q_down。',
            '当前 shell 只以河段流量表述 q_down，也不会用合成值补线。',
          ]}
        />
      </aside>
    </div>
  )
}

function ProductPanel({ product }: { product: QhhLatestProduct }) {
  const qualityNotes = product.availability.quality_notes.map((note) => ({
    ...note,
    message: sanitizeHydroMetMessage(note.message),
  }))
  const coverage = product.quality.station_variable_coverage

  return (
    <section className="rounded-md border border-neutral-300 bg-white p-4" data-testid="hydro-met-product-panel">
      <div className="flex items-center gap-2">
        <Route className="h-4 w-4 text-primary-600" aria-hidden="true" />
        <h2 className="text-base font-semibold text-neutral-900">QHH latest-product</h2>
      </div>
      <dl className="mt-3 grid grid-cols-[8.5rem_minmax(0,1fr)] gap-x-3 gap-y-2 text-xs">
        <MetaRow label="status" value={`${product.status} / ${product.run_status}`} />
        <MetaRow label="source" value={product.source_id} />
        <MetaRow label="cycle" value={formatDateTime(product.cycle_time)} mono />
        <MetaRow label="run_id" value={product.run_id} mono />
        <MetaRow label="model_id" value={product.model_id} mono />
        <MetaRow label="forcing_version_id" value={product.forcing_version_id} mono />
        <MetaRow label="basin_version_id" value={product.basin_version_id} mono />
        <MetaRow label="river_network_version_id" value={product.river_network_version_id} mono />
        <MetaRow label="forcing window" value={`${formatDateTime(product.forcing_valid_time_start)} - ${formatDateTime(product.forcing_valid_time_end)}`} mono />
        <MetaRow label="river window" value={`${formatDateTime(product.river_valid_time_start)} - ${formatDateTime(product.river_valid_time_end)}`} mono />
        <MetaRow label="horizon" value={product.available_horizon_hours === null ? 'unknown' : `${product.available_horizon_hours}h / expected ${product.expected_horizon_hours}h`} />
      </dl>
      {product.shorter_horizon ? (
        <div className="mt-3 rounded border border-warning/40 bg-warning/10 p-2 text-xs text-neutral-900" data-testid="hydro-met-shorter-horizon">
          IFS 或当前产品可用时效短于预期；按 actual available horizon 展示，不补齐合成值。
        </div>
      ) : null}
      {qualityNotes.length > 0 ? (
        <div className="mt-3 space-y-1 text-xs text-neutral-700" data-testid="hydro-met-quality-notes">
          {qualityNotes.map((note) => (
            <p key={`${note.code}-${note.message}`}>
              {note.code}: {note.message}
            </p>
          ))}
        </div>
      ) : null}
      {coverage.length > 0 ? (
        <div className="mt-3 grid grid-cols-2 gap-2 text-xs min-[420px]:grid-cols-3" data-testid="hydro-met-variable-coverage">
          {coverage.map((item) => (
            <div key={item.variable} className="rounded border border-neutral-300 p-2">
              <div className="font-semibold text-neutral-900">{item.variable}</div>
              <div className="text-neutral-700">{item.station_count} stations</div>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  )
}

function InventoryPanel({
  title,
  icon: Icon,
  summary,
  children,
  emptyText,
  testId,
  emptyTestId,
}: {
  title: string
  icon: LucideIcon
  summary: string
  children: ReactNode
  emptyText: string
  testId: string
  emptyTestId: string
}) {
  const isEmpty = Array.isArray(children) && children.length === 0

  return (
    <section className="rounded-md border border-neutral-300 bg-white p-4" data-testid={testId}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2">
          <Icon className="h-4 w-4 text-primary-600" aria-hidden="true" />
          <h2 className="text-base font-semibold text-neutral-900">{title}</h2>
        </div>
        <span className="shrink-0 rounded border border-neutral-300 px-2 py-1 text-xs text-neutral-700">{summary}</span>
      </div>
      <div className="mt-3 space-y-2">
        {isEmpty ? (
          <div className="rounded border border-neutral-300 bg-neutral-50 p-3 text-sm text-neutral-700" data-testid={emptyTestId}>
            {emptyText}
          </div>
        ) : (
          children
        )}
      </div>
    </section>
  )
}

function StationRow({ station }: { station: HydroMetStation }) {
  const coordinates = getHydroMetStationCoordinates(station)
  return (
    <div className="rounded border border-neutral-300 p-3 text-sm">
      <div className="flex items-center justify-between gap-3">
        <span className="font-mono font-semibold text-neutral-900">{station.station_id}</span>
        <span className="text-xs text-neutral-700">{station.station_role}</span>
      </div>
      <div className="mt-1 text-neutral-700">{station.station_name ?? '未命名站点'}</div>
      <div className="mt-1 font-mono text-xs text-neutral-500">
        {coordinates ? `${formatCoordinate(coordinates.lon)}, ${formatCoordinate(coordinates.lat)}` : HYDRO_MET_COORDINATES_UNAVAILABLE}
      </div>
    </div>
  )
}

function RiverSegmentRow({ feature }: { feature: HydroMetRiverSegmentFeature }) {
  const properties = feature.properties
  return (
    <div className="rounded border border-neutral-300 p-3 text-sm">
      <div className="flex items-center justify-between gap-3">
        <span className="font-mono font-semibold text-neutral-900">{properties.river_segment_id}</span>
        <span className="text-xs text-neutral-700">order {properties.stream_order}</span>
      </div>
      <div className="mt-1 text-neutral-700">{properties.name}</div>
      <div className="mt-1 font-mono text-xs text-neutral-500">{properties.river_network_version_id}</div>
    </div>
  )
}

function PlaceholderPanel({ icon: Icon, title, lines }: { icon: LucideIcon; title: string; lines: string[] }) {
  return (
    <section className="rounded-md border border-dashed border-neutral-300 bg-white p-4" data-testid="hydro-met-chart-placeholder">
      <div className="flex items-center gap-2">
        <Icon className="h-4 w-4 text-primary-600" aria-hidden="true" />
        <h2 className="text-base font-semibold text-neutral-900">{title}</h2>
      </div>
      <div className="mt-3 space-y-2 text-sm text-neutral-700">
        {lines.map((line) => <p key={line}>{line}</p>)}
      </div>
    </section>
  )
}

function LoadingPanel() {
  return (
    <div className="flex items-center gap-3 rounded-md border border-neutral-300 bg-white p-4 text-sm text-neutral-700" role="status" data-testid="hydro-met-loading">
      <Loader2 className="h-4 w-4 animate-spin text-primary-600" aria-hidden="true" />
      正在加载 latest-product、气象站点 inventory 和河段流量候选...
    </div>
  )
}

function StatusPanel({
  tone,
  title,
  messages,
  product,
  testId,
}: {
  tone: 'info' | 'warning' | 'danger'
  title: string
  messages: string[]
  product?: QhhLatestProduct | null
  testId: string
}) {
  const toneClass = {
    info: 'border-primary-100 bg-primary-50 text-neutral-800',
    warning: 'border-warning/40 bg-warning/10 text-neutral-900',
    danger: 'border-danger/30 bg-danger/10 text-danger',
  }[tone]
  const safeMessages = messages.map((message) => sanitizeHydroMetMessage(message))

  return (
    <section className={cn('rounded-md border p-4', toneClass)} role={tone === 'danger' ? 'alert' : 'status'} data-testid={testId}>
      <div className="flex items-center gap-2 font-semibold">
        <AlertTriangle className="h-4 w-4" aria-hidden="true" />
        {title}
      </div>
      <ul className="mt-2 space-y-1 text-sm">
        {safeMessages.map((message) => <li key={message}>{message}</li>)}
      </ul>
      {product ? (
        <dl className="mt-3 grid grid-cols-[8rem_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs text-neutral-700">
          <MetaRow label="source" value={product.source_id} />
          <MetaRow label="cycle" value={formatDateTime(product.cycle_time)} mono />
          <MetaRow label="run_id" value={product.run_id || '-'} mono />
        </dl>
      ) : null}
    </section>
  )
}

function ControlField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="space-y-1">
      <span className="block text-xs font-medium uppercase text-neutral-700">{label}</span>
      {children}
    </label>
  )
}

function MetaRow({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <>
      <dt className="text-neutral-500">{label}</dt>
      <dd className={cn('min-w-0 break-words text-neutral-900', mono && 'font-mono')}>{value}</dd>
    </>
  )
}

function formatDateTime(value: string | null | undefined) {
  if (!value) return '-'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toISOString()
}

function formatCoordinate(value: number | undefined) {
  return Number.isFinite(value) ? value.toFixed(4) : '-'
}

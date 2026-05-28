import { useEffect, useMemo, useState, type ReactNode } from 'react'
import ReactEChartsCore from 'echarts-for-react/lib/core'
import { AlertTriangle, CloudRain, GitBranch, Loader2, MapPin, RadioTower, Route, Search, Waves, type LucideIcon } from 'lucide-react'
import { useLocation, useNavigate } from 'react-router-dom'

import { echarts } from '@/components/charts/echartsCore'
import { cn } from '@/lib/cn'
import {
  hydroMetSources,
  mergeHydroMetQueryState,
  needsHydroMetQueryReplacement,
  normalizeHydroMetCycle,
  parseHydroMetQueryState,
  serializeHydroMetQueryState,
  type HydroMetQueryPatch,
} from '@/lib/hydroMet/queryState'
import { HYDRO_MET_COORDINATES_UNAVAILABLE, getHydroMetStationCoordinates } from '@/lib/hydroMet/runtime'
import {
  HYDRO_MET_RIVER_FORECAST_LIMIT,
  formatHydroMetRiverForecastMessage,
  formatHydroMetRiverForecastUiString,
  loadHydroMetRiverForecast,
  riverForecastRequestKey,
  validateHydroMetRiverForecastForChart,
  type HydroMetRiverForecastPayload,
  type HydroMetRiverForecastSegmentIdentity,
  type HydroMetRiverForecastValidation,
} from '@/lib/hydroMet/riverForecast'
import {
  HYDRO_MET_STATION_SERIES_LIMIT,
  HYDRO_MET_STATION_SERIES_MESSAGE_STRING_LIMIT,
  HYDRO_MET_STATION_SERIES_UI_STRING_LIMIT,
  HYDRO_MET_STATION_VARIABLES,
  formatHydroMetStationSeriesMessage,
  formatHydroMetStationSeriesContractValue,
  formatHydroMetStationSeriesUiString,
  isHydroMetStationSeriesUiStringCapped,
  loadHydroMetStationSeries,
  stationSeriesRequestKey,
  validateHydroMetStationSeriesIdentity,
  type HydroMetStationSeries,
  type HydroMetStationSeriesResponse,
  type HydroMetStationSeriesVariable,
} from '@/lib/hydroMet/stationSeries'
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
  | { kind: 'blocked'; message: string }
  | { kind: 'error'; message: string }

type StationSeriesLoadState =
  | { kind: 'idle' }
  | { kind: 'loading'; requestKey: string }
  | { kind: 'loaded'; requestKey: string; response: HydroMetStationSeriesResponse }
  | { kind: 'error'; requestKey: string; message: string }

type RiverForecastLoadState =
  | { kind: 'idle' }
  | { kind: 'loading'; requestKey: string }
  | { kind: 'loaded'; requestKey: string; response: HydroMetRiverForecastPayload }
  | { kind: 'error'; requestKey: string; message: string }

type ChartableStationSeriesPoint = {
  timestamp: number
  value: number
  qualityFlag: string | null
}

type HydroMetStationSeriesRecord = Record<string, unknown> & {
  variable: HydroMetStationSeriesVariable
}

type StationSeriesValidation =
  | {
      ok: true
      metadata: HydroMetStationSeries['metadata']
      unit: string | null
      sourceId: string | null
      cycleTime: string | null
      seriesTruncated: boolean
      reportedPointCount: number
      inspectedPointCount: number
      renderedPoints: ChartableStationSeriesPoint[]
      capped: boolean
      inspectionCapped: boolean
      nonOkFlags: string[]
      nonOkFlagsCapped: boolean
      qualitySummary: string
    }
  | { ok: false; messages: string[] }

const HYDRO_MET_STATION_SERIES_POINT_SENTINEL = 16
const HYDRO_MET_STATION_SERIES_POINT_INSPECTION_LIMIT = HYDRO_MET_STATION_SERIES_LIMIT + HYDRO_MET_STATION_SERIES_POINT_SENTINEL
const HYDRO_MET_STATION_SERIES_MESSAGE_LIMIT = 6
const HYDRO_MET_STATION_SERIES_QC_FLAG_LIMIT = 6
const HYDRO_MET_STATION_SERIES_QC_LABEL_LIMIT = 32
const HYDRO_MET_STATION_SERIES_UNIT_LIMIT = 32
const HYDRO_MET_STATION_SERIES_ITEM_INSPECTION_LIMIT = HYDRO_MET_STATION_VARIABLES.length * 2
const HYDRO_MET_PAGE_MESSAGE_LIST_LIMIT = 6

type BoundedHydroMetMessage = {
  kind: 'message' | 'summary'
  text: string
}

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
    if (state.strictIdentityError) {
      setLoadState({ kind: 'blocked', message: state.strictIdentityError })
      return () => {
        cancelled = true
      }
    }
    setLoadState({ kind: 'loading' })
    void loadHydroMetBootstrap({ source: state.source, cycle: state.cycle, strictIdentity: state.strictIdentity }).then(
      (result) => {
        if (!cancelled) setLoadState({ kind: 'loaded', result })
      },
      (error) => {
        if (!cancelled) setLoadState({ kind: 'error', message: formatHydroMetStatusMessage(error, '水文气象启动失败') })
      },
    )
    return () => {
      cancelled = true
    }
  }, [state.cycle, state.source, state.strictIdentity, state.strictIdentityError])

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
          不绘制假曲线，不手工输入 run_id、forcing_version_id、basin_version_id 或 river_network_version_id。站点 forcing 图表读取 station-series 真实响应，河段 q_down 流量图表属于 #209。
        </div>
      </section>

      {queryValidationMessages.length > 0 ? (
        <StatusPanel tone="warning" title="查询参数已更正" messages={queryValidationMessages} testId="hydro-met-query-validation" />
      ) : null}

      {loadState.kind === 'loading' ? <LoadingPanel /> : null}
      {loadState.kind === 'blocked' ? (
        <StatusPanel
          tone="danger"
          title="严格 handoff 无效"
          messages={[loadState.message]}
          testId="hydro-met-strict-handoff-invalid"
        />
      ) : null}
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

  if (result.status === 'strict-identity-mismatch') {
    return (
      <StatusPanel
        tone="danger"
        title="严格 handoff 不匹配"
        messages={result.latestReasons}
        product={result.product}
        testId="hydro-met-strict-identity-mismatch"
      />
    )
  }

  const product = result.product
  if (!product) {
    return <StatusPanel tone="danger" title="latest-product 不可用" messages={['latest-product 响应为空。']} testId="hydro-met-latest-unavailable" />
  }

  return <ReadyHydroMetContent result={result} product={product} />
}

export function ReadyHydroMetContent({ result, product }: { result: HydroMetBootstrapResult; product: QhhLatestProduct }) {
  const [stationQuery, setStationQuery] = useState('')
  const [selectedStationId, setSelectedStationId] = useState<string | null>(null)
  const [seriesState, setSeriesState] = useState<StationSeriesLoadState>({ kind: 'idle' })
  const [selectedRiverSegmentId, setSelectedRiverSegmentId] = useState<string | null>(null)
  const [riverForecastState, setRiverForecastState] = useState<RiverForecastLoadState>({ kind: 'idle' })

  useEffect(() => {
    setStationQuery('')
    setSelectedStationId(null)
    setSeriesState({ kind: 'idle' })
    setSelectedRiverSegmentId(null)
    setRiverForecastState({ kind: 'idle' })
  }, [product.forcing_version_id, product.source_id, product.cycle_time])

  useEffect(() => {
    if (selectedStationId !== null) return
    setSelectedStationId(result.stations[0]?.station_id ?? null)
  }, [result.stations, selectedStationId])

  useEffect(() => {
    if (selectedRiverSegmentId !== null) return
    setSelectedRiverSegmentId(firstRiverSegmentId(result.riverSegments))
  }, [result.riverSegments, selectedRiverSegmentId])

  const selectedStation = useMemo(
    () => result.stations.find((station) => station.station_id === selectedStationId) ?? null,
    [result.stations, selectedStationId],
  )
  const selectedRiverSegment = useMemo(
    () => result.riverSegments.find((feature) => riverSegmentId(feature) === selectedRiverSegmentId) ?? null,
    [result.riverSegments, selectedRiverSegmentId],
  )

  useEffect(() => {
    if (!selectedStation) {
      setSeriesState({ kind: 'idle' })
      return
    }

    const requestKey = stationSeriesRequestKey(product, selectedStation.station_id)
    let cancelled = false
    setSeriesState({ kind: 'loading', requestKey })
    void loadHydroMetStationSeries({ product, station: selectedStation, limit: HYDRO_MET_STATION_SERIES_LIMIT }).then(
      (response) => {
        if (!cancelled) setSeriesState({ kind: 'loaded', requestKey, response })
      },
      (error) => {
        if (!cancelled) {
          setSeriesState({
            kind: 'error',
            requestKey,
            message: formatHydroMetStationSeriesMessage(error, 'station-series 不可用'),
          })
        }
      },
    )

    return () => {
      cancelled = true
    }
  }, [product, selectedStation])

  const selectedStationAbsent = selectedStationId !== null && selectedStation === null
  const selectedRiverSegmentAbsent = selectedRiverSegmentId !== null && selectedRiverSegment === null

  useEffect(() => {
    if (!selectedRiverSegment) {
      setRiverForecastState({ kind: 'idle' })
      return
    }

    const segmentIdentity = riverForecastSegmentIdentity(selectedRiverSegment)
    const requestKey = riverForecastRequestKey(product, segmentIdentity.river_segment_id)
    let cancelled = false
    setRiverForecastState({ kind: 'loading', requestKey })
    void loadHydroMetRiverForecast({ product, segment: segmentIdentity }).then(
      (response) => {
        if (!cancelled) setRiverForecastState({ kind: 'loaded', requestKey, response })
      },
      (error) => {
        if (!cancelled) {
          setRiverForecastState({
            kind: 'error',
            requestKey,
            message: formatHydroMetRiverForecastMessage(error, 'river forecast-series 不可用'),
          })
        }
      },
    )

    return () => {
      cancelled = true
    }
  }, [product, selectedRiverSegment])

  return (
    <div className="grid gap-3 min-[1180px]:grid-cols-[minmax(19rem,0.76fr)_minmax(0,1.04fr)_minmax(23rem,1fr)]">
      <aside className="space-y-3">
        <ProductPanel product={product} />
        {result.stationError ? <StatusPanel tone="warning" title="站点 inventory 部分失败" messages={[result.stationError]} testId="hydro-met-station-partial-failure" /> : null}
        {result.riverError ? <StatusPanel tone="warning" title="河段流量候选部分失败" messages={[result.riverError]} testId="hydro-met-river-partial-failure" /> : null}
      </aside>

      <section className="space-y-3">
        <StationInventoryPanel
          product={product}
          stations={result.stations}
          totalCount={result.stationPage?.total_count ?? product.station_count}
          query={stationQuery}
          selectedStationId={selectedStationId}
          onQueryChange={setStationQuery}
          onSelectStation={setSelectedStationId}
        />

        <InventoryPanel
          title="河段流量候选"
          icon={GitBranch}
          summary={`${result.riverSegments.length} / ${result.riverSegmentCollection?.total ?? product.segment_count} river segments`}
          emptyText="河段列表为空：没有可展示的河段流量候选，且不会填充假河段。"
          testId="hydro-met-river-list"
          emptyTestId="hydro-met-empty-rivers"
        >
          {result.riverSegments.slice(0, 12).map((feature) => (
            <RiverSegmentRow
              key={riverSegmentId(feature)}
              feature={feature}
              selected={riverSegmentId(feature) === selectedRiverSegmentId}
              onSelect={setSelectedRiverSegmentId}
            />
          ))}
        </InventoryPanel>
      </section>

      <aside className="space-y-3">
        <StationSeriesPanel
          product={product}
          station={selectedStation}
          selectedStationAbsent={selectedStationAbsent}
          state={seriesState}
        />
        <RiverForecastPanel
          product={product}
          segment={selectedRiverSegment}
          selectedRiverSegmentAbsent={selectedRiverSegmentAbsent}
          state={riverForecastState}
        />
      </aside>
    </div>
  )
}

function ProductPanel({ product }: { product: QhhLatestProduct }) {
  const qualityNotes = boundedHydroMetMessageList(getHydroMetQualityNotes(product), formatHydroMetQualityNote, {
    summaryLabel: '质量备注',
  })
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
          {qualityNotes.map((note, index) => (
            <p key={`${note.kind}-${index}-${note.text}`} data-kind={note.kind}>
              {note.text}
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

function StationInventoryPanel({
  product,
  stations,
  totalCount,
  query,
  selectedStationId,
  onQueryChange,
  onSelectStation,
}: {
  product: QhhLatestProduct
  stations: HydroMetStation[]
  totalCount: number
  query: string
  selectedStationId: string | null
  onQueryChange: (value: string) => void
  onSelectStation: (stationId: string) => void
}) {
  const normalizedQuery = query.trim().toLowerCase()
  const filteredStations = useMemo(() => {
    if (!normalizedQuery) return stations
    return stations.filter((station) => {
      const label = `${station.station_id} ${station.station_name ?? ''}`.toLowerCase()
      return label.includes(normalizedQuery)
    })
  }, [normalizedQuery, stations])
  const markerStations = filteredStations.filter((station) => getHydroMetStationCoordinates(station))

  return (
    <section className="rounded-md border border-neutral-300 bg-white p-4" data-testid="hydro-met-station-panel">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2">
          <RadioTower className="h-4 w-4 text-primary-600" aria-hidden="true" />
          <h2 className="text-base font-semibold text-neutral-900">气象 forcing 站点</h2>
        </div>
        <span className="shrink-0 rounded border border-neutral-300 px-2 py-1 text-xs text-neutral-700" data-testid="hydro-met-station-summary">
          {filteredStations.length} / {totalCount} stations
        </span>
      </div>

      <StationMap
        stations={markerStations}
        selectedStationId={selectedStationId}
        onSelectStation={onSelectStation}
      />

      <label className="mt-3 flex h-10 items-center gap-2 rounded border border-neutral-300 px-3 focus-within:border-primary-500" data-testid="hydro-met-station-search">
        <Search className="h-4 w-4 text-neutral-500" aria-hidden="true" />
        <input
          aria-label="搜索气象站点"
          className="min-w-0 flex-1 bg-transparent text-sm text-neutral-900 outline-none"
          placeholder="搜索 station id / name"
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
        />
      </label>

      {stations.length === 0 ? (
        <div className="mt-3 rounded border border-neutral-300 bg-neutral-50 p-3 text-sm text-neutral-700" data-testid="hydro-met-empty-stations">
          站点列表为空：未生成替代站点，也不会自动切换到其他产品。
        </div>
      ) : filteredStations.length === 0 ? (
        <div className="mt-3 rounded border border-neutral-300 bg-neutral-50 p-3 text-sm text-neutral-700" data-testid="hydro-met-station-no-results">
          没有匹配的真实站点：{query}
        </div>
      ) : (
        <div className="mt-3 max-h-[26rem] space-y-2 overflow-auto pr-1" data-testid="hydro-met-station-list">
          {filteredStations.slice(0, HYDRO_MET_STATION_LIMIT).map((station) => (
            <StationRow
              key={station.station_id}
              station={station}
              product={product}
              selected={station.station_id === selectedStationId}
              onSelect={onSelectStation}
            />
          ))}
        </div>
      )}
    </section>
  )
}

function StationMap({
  stations,
  selectedStationId,
  onSelectStation,
}: {
  stations: HydroMetStation[]
  selectedStationId: string | null
  onSelectStation: (stationId: string) => void
}) {
  const bounds = useMemo(() => {
    const coordinates = stations.flatMap((station) => {
      const coordinate = getHydroMetStationCoordinates(station)
      return coordinate ? [coordinate] : []
    })
    if (coordinates.length === 0) return null
    return coordinates.reduce(
      (acc, coordinate) => ({
        minLon: Math.min(acc.minLon, coordinate.lon),
        maxLon: Math.max(acc.maxLon, coordinate.lon),
        minLat: Math.min(acc.minLat, coordinate.lat),
        maxLat: Math.max(acc.maxLat, coordinate.lat),
      }),
      { minLon: coordinates[0].lon, maxLon: coordinates[0].lon, minLat: coordinates[0].lat, maxLat: coordinates[0].lat },
    )
  }, [stations])

  return (
    <div className="mt-3 rounded-md border border-neutral-300 bg-neutral-50 p-3" data-testid="hydro-met-station-map">
      <div className="relative h-56 overflow-hidden rounded border border-neutral-200 bg-[#eef5f2]">
        <div className="absolute inset-0 bg-[linear-gradient(90deg,rgba(15,23,42,0.07)_1px,transparent_1px),linear-gradient(0deg,rgba(15,23,42,0.07)_1px,transparent_1px)] bg-[size:32px_32px]" aria-hidden="true" />
        {stations.length === 0 || !bounds ? (
          <div className="absolute inset-0 grid place-items-center px-4 text-center text-sm text-neutral-700" data-testid="hydro-met-no-station-markers">
            没有可绘制坐标的真实站点。
          </div>
        ) : (
          stations.map((station) => {
            const coordinate = getHydroMetStationCoordinates(station)
            if (!coordinate) return null
            const position = markerPosition(coordinate.lon, coordinate.lat, bounds)
            const selected = station.station_id === selectedStationId
            return (
              <button
                key={station.station_id}
                type="button"
                aria-label={`选择站点 ${station.station_id}`}
                className={cn(
                  'absolute grid h-7 w-7 -translate-x-1/2 -translate-y-1/2 cursor-pointer place-items-center rounded-full border-2 bg-white shadow-sm transition-colors',
                  selected ? 'border-danger text-danger' : 'border-primary-600 text-primary-700 hover:border-primary-800',
                )}
                style={{ left: `${position.x}%`, top: `${position.y}%` }}
                onClick={() => onSelectStation(station.station_id)}
                data-testid="hydro-met-station-marker"
                data-station-id={station.station_id}
              >
                <MapPin className="h-4 w-4" aria-hidden="true" />
              </button>
            )
          })
        )}
      </div>
      <div className="mt-2 text-xs text-neutral-700" data-testid="hydro-met-station-marker-count">
        markers {stations.length}
      </div>
    </div>
  )
}

function StationRow({
  station,
  product,
  selected,
  onSelect,
}: {
  station: HydroMetStation
  product: QhhLatestProduct
  selected: boolean
  onSelect: (stationId: string) => void
}) {
  const coordinates = getHydroMetStationCoordinates(station)
  return (
    <button
      type="button"
      className={cn(
        'w-full cursor-pointer rounded border p-3 text-left text-sm transition-colors',
        selected ? 'border-primary-600 bg-primary-50' : 'border-neutral-300 hover:bg-neutral-50',
      )}
      onClick={() => onSelect(station.station_id)}
      data-testid="hydro-met-station-row"
      data-station-id={station.station_id}
      aria-pressed={selected}
    >
      <div className="flex items-center justify-between gap-3">
        <span className="font-mono font-semibold text-neutral-900">{station.station_id}</span>
        <span className="text-xs text-neutral-700">{station.station_role}</span>
      </div>
      <div className="mt-1 text-neutral-700">{station.station_name ?? '未命名站点'}</div>
      <div className="mt-1 font-mono text-xs text-neutral-500">
        {coordinates ? `${formatCoordinate(coordinates.lon)}, ${formatCoordinate(coordinates.lat)}` : HYDRO_MET_COORDINATES_UNAVAILABLE}
      </div>
      <div className="mt-1 font-mono text-[11px] text-neutral-500">{product.forcing_version_id}</div>
    </button>
  )
}

function RiverSegmentRow({
  feature,
  selected,
  onSelect,
}: {
  feature: HydroMetRiverSegmentFeature
  selected: boolean
  onSelect: (riverSegmentId: string) => void
}) {
  const properties = feature.properties
  return (
    <button
      type="button"
      className={cn(
        'w-full cursor-pointer rounded border p-3 text-left text-sm transition-colors',
        selected ? 'border-primary-600 bg-primary-50' : 'border-neutral-300 hover:bg-neutral-50',
      )}
      onClick={() => onSelect(riverSegmentId(feature))}
      data-testid="hydro-met-river-row"
      data-river-segment-id={riverSegmentId(feature)}
      aria-pressed={selected}
    >
      <div className="flex items-center justify-between gap-3">
        <span className="font-mono font-semibold text-neutral-900">{properties.river_segment_id}</span>
        <span className="text-xs text-neutral-700">order {properties.stream_order}</span>
      </div>
      <div className="mt-1 text-neutral-700">{properties.name}</div>
      <div className="mt-1 font-mono text-xs text-neutral-500">{properties.river_network_version_id}</div>
    </button>
  )
}

function StationSeriesPanel({
  product,
  station,
  selectedStationAbsent,
  state,
}: {
  product: QhhLatestProduct
  station: HydroMetStation | null
  selectedStationAbsent: boolean
  state: StationSeriesLoadState
}) {
  if (selectedStationAbsent) {
    return (
      <StatusPanel
        tone="warning"
        title="选中站点不在 inventory 中"
        messages={['当前选中站点不属于 latest-product 返回的真实 station inventory，已停止 station-series 请求。']}
        testId="hydro-met-station-series-unavailable"
      />
    )
  }

  if (!station) {
    return (
      <StatusPanel
        tone="info"
        title="站点 forcing 不可用"
        messages={['没有可选择的真实站点；不会使用手工 ID 或假站点请求 station-series。']}
        testId="hydro-met-station-series-unavailable"
      />
    )
  }

  const currentRequestKey = stationSeriesRequestKey(product, station.station_id)
  const stale = state.kind !== 'idle' && state.requestKey !== currentRequestKey

  return (
    <section className="rounded-md border border-neutral-300 bg-white p-4" data-testid="hydro-met-station-series-panel">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <CloudRain className="h-4 w-4 text-primary-600" aria-hidden="true" />
            <h2 className="text-base font-semibold text-neutral-900">站点 forcing 图表</h2>
          </div>
          <div className="mt-1 font-mono text-xs text-neutral-600" data-testid="hydro-met-selected-station">
            {station.station_id} · {station.station_name ?? '未命名站点'}
          </div>
        </div>
        <span className="rounded border border-neutral-300 px-2 py-1 font-mono text-xs text-neutral-700">
          limit {HYDRO_MET_STATION_SERIES_LIMIT}
        </span>
      </div>

      {state.kind === 'loading' || stale ? (
        <div className="mt-3 flex items-center gap-2 rounded border border-neutral-300 bg-neutral-50 p-3 text-sm text-neutral-700" role="status" data-testid="hydro-met-station-series-loading">
          <Loader2 className="h-4 w-4 animate-spin text-primary-600" aria-hidden="true" />
          正在加载 {station.station_id} 的 station-series...
        </div>
      ) : null}

      {state.kind === 'error' && !stale ? (
        <StatusPanel tone="danger" title="station-series 加载失败" messages={[state.message]} testId="hydro-met-station-series-error" />
      ) : null}

      {state.kind === 'loaded' && !stale ? (
        <StationSeriesCharts response={state.response} product={product} stationId={station.station_id} />
      ) : null}
    </section>
  )
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isHydroMetStationSeriesVariable(value: unknown): value is HydroMetStationSeriesVariable {
  return typeof value === 'string' && (HYDRO_MET_STATION_VARIABLES as readonly string[]).includes(value)
}

function isHydroMetStationSeriesRecord(value: unknown): value is HydroMetStationSeriesRecord {
  return isRecord(value) && isHydroMetStationSeriesVariable(value.variable)
}

function hydroMetStationSeriesItems(response: unknown) {
  if (!isRecord(response)) return []
  const series = response.series
  return Array.isArray(series) ? series : []
}

function mapUniqueHydroMetStationSeries(seriesList: unknown[]) {
  const seriesByVariable = new Map<HydroMetStationSeriesVariable, HydroMetStationSeriesRecord>()
  seriesList.slice(0, HYDRO_MET_STATION_SERIES_ITEM_INSPECTION_LIMIT).forEach((series) => {
    if (!isHydroMetStationSeriesRecord(series)) return
    if (!seriesByVariable.has(series.variable)) seriesByVariable.set(series.variable, series)
  })
  return seriesByVariable
}

function validateHydroMetStationSeriesContract(
  response: HydroMetStationSeriesResponse,
  product: QhhLatestProduct,
) {
  const messages: string[] = []
  const counts = new Map<HydroMetStationSeriesVariable, number>()
  const productCycle = normalizeHydroMetCycle(product.cycle_time)
  const seriesList = hydroMetStationSeriesItems(response)

  if (!isRecord(response)) {
    return ['station-series 响应格式无效']
  }

  if (!Array.isArray(response.series)) {
    return ['station-series series 缺失或格式无效']
  }

  if (seriesList.length > HYDRO_MET_STATION_SERIES_ITEM_INSPECTION_LIMIT) {
    messages.push(`station-series series 数量 ${seriesList.length} 超过前端检查上限 ${HYDRO_MET_STATION_SERIES_ITEM_INSPECTION_LIMIT}，已停止绘图。`)
  }

  seriesList.slice(0, HYDRO_MET_STATION_SERIES_ITEM_INSPECTION_LIMIT).forEach((series, index) => {
    if (!isRecord(series)) {
      messages.push(`series[${index}] 不是对象，station-series contract 无效`)
      return
    }

    const variable = series.variable
    if (!isHydroMetStationSeriesVariable(variable)) {
      messages.push(`series[${index}] variable=${formatHydroMetStationSeriesContractValue(variable)} 不属于 station-series MVP 变量`)
      return
    }
    counts.set(variable, (counts.get(variable) ?? 0) + 1)

    const sourceId = series.source_id
    if (typeof sourceId === 'string' && sourceId !== product.source_id) {
      messages.push(`${variable}.source_id=${formatHydroMetStationSeriesContractValue(sourceId)} 与 latest-product ${formatHydroMetStationSeriesContractValue(product.source_id)} 不一致`)
    } else if (sourceId !== undefined && sourceId !== null && typeof sourceId !== 'string') {
      messages.push(`${variable}.source_id 元数据格式无效`)
    }

    const cycleTime = series.cycle_time
    if (cycleTime !== undefined && cycleTime !== null) {
      if (typeof cycleTime !== 'string') {
        messages.push(`${variable}.cycle_time 元数据格式无效`)
      } else {
        const seriesCycle = normalizeHydroMetCycle(cycleTime)
        if (!seriesCycle) {
          messages.push(`${variable}.cycle_time=${formatHydroMetStationSeriesContractValue(cycleTime)} 不是有效 RFC3339 时间`)
        } else if (productCycle && seriesCycle !== productCycle) {
          messages.push(`${variable}.cycle_time=${formatHydroMetStationSeriesContractValue(seriesCycle)} 与 latest-product ${formatHydroMetStationSeriesContractValue(productCycle)} 不一致`)
        }
      }
    }
  })

  ;(['valid_time_start', 'valid_time_end'] as const).forEach((field) => {
    if (!(field in response)) {
      messages.push(`station-series ${field} 元数据缺失`)
      return
    }
    const value = response[field]
    if (typeof value !== 'string' || !normalizeHydroMetCycle(value)) {
      messages.push(`station-series ${field}=${formatHydroMetStationSeriesContractValue(value)} 不是有效 RFC3339 时间`)
    }
  })

  counts.forEach((count, variable) => {
    if (count > 1) messages.push(`${variable} 在 station-series 响应中重复 ${count} 次`)
  })

  return capHydroMetStationSeriesMessages(messages)
}

function validateHydroMetStationSeriesForChart(series: HydroMetStationSeriesRecord): StationSeriesValidation {
  const messages: string[] = []
  const metadataValue = series.metadata
  const pointsValue = series.points
  const unitValue = series.unit
  const truncatedValue = series.truncated

  let unit: string | null = null
  if (unitValue === undefined || unitValue === null) {
    unit = null
  } else if (typeof unitValue === 'string') {
    if (isHydroMetStationSeriesUiStringCapped(unitValue, { limit: HYDRO_MET_STATION_SERIES_UNIT_LIMIT, fallback: '' })) {
      messages.push(`变量 ${series.variable} unit 过长，停止绘图`)
    } else {
      unit = formatHydroMetStationSeriesUiString(unitValue, { limit: HYDRO_MET_STATION_SERIES_UNIT_LIMIT, fallback: '' }) || null
    }
  } else {
    messages.push(`变量 ${series.variable} unit 格式无效`)
  }

  let seriesTruncated = false
  if (truncatedValue === undefined || truncatedValue === null) {
    seriesTruncated = false
  } else if (typeof truncatedValue === 'boolean') {
    seriesTruncated = truncatedValue
  } else {
    messages.push(`变量 ${series.variable} truncated 格式无效`)
  }

  if (!isRecord(metadataValue)) {
    messages.push(`变量 ${series.variable} metadata 缺失或格式无效`)
  }
  if (!Array.isArray(pointsValue)) {
    messages.push(`变量 ${series.variable} points 缺失或格式无效`)
  }
  if (messages.length > 0) return { ok: false, messages: capHydroMetStationSeriesMessages(messages) }

  messages.push(...validateStationSeriesMetadata(metadataValue, series.variable))
  if (messages.length > 0) return { ok: false, messages: capHydroMetStationSeriesMessages(messages) }

  const metadata = metadataValue as unknown as HydroMetStationSeries['metadata']
  const reportedPointCount = Math.max(metadata.returned_points, pointsValue.length)
  const inspectedPointCount = Math.min(pointsValue.length, HYDRO_MET_STATION_SERIES_POINT_INSPECTION_LIMIT)
  const inspectionCapped = pointsValue.length > inspectedPointCount

  const renderedPoints: ChartableStationSeriesPoint[] = []
  const invalidPointMessages: string[] = []
  let invalidPointCount = 0
  const qualityFlagCounts = new Map<string, number>()

  for (let index = 0; index < inspectedPointCount; index += 1) {
    const point = pointsValue[index]
    const parsed = parseChartableStationSeriesPoint(point)
    if (typeof parsed === 'string') {
      invalidPointCount += 1
      if (invalidPointMessages.length < HYDRO_MET_STATION_SERIES_MESSAGE_LIMIT) {
        invalidPointMessages.push(`变量 ${series.variable} 第 ${index + 1} 个点${parsed}`)
      }
    } else {
      if (renderedPoints.length < HYDRO_MET_STATION_SERIES_LIMIT) renderedPoints.push(parsed)
      const flag = parsed.qualityFlag ?? 'missing'
      qualityFlagCounts.set(flag, (qualityFlagCounts.get(flag) ?? 0) + 1)
    }
  }

  if (invalidPointCount > 0) {
    messages.push(...capInvalidPointMessages(series.variable, invalidPointMessages, invalidPointCount, inspectedPointCount, reportedPointCount, inspectionCapped))
  }
  if (messages.length > 0) return { ok: false, messages: capHydroMetStationSeriesMessages(messages) }

  const { nonOkFlags, nonOkFlagsCapped, qualitySummary } = qualityFlagSummary(qualityFlagCounts, inspectedPointCount, reportedPointCount, inspectionCapped)
  return {
    ok: true,
    metadata,
    unit,
    sourceId: typeof series.source_id === 'string' ? formatHydroMetStationSeriesUiString(series.source_id) : null,
    cycleTime: typeof series.cycle_time === 'string' ? normalizeHydroMetCycle(series.cycle_time) : null,
    seriesTruncated,
    reportedPointCount,
    inspectedPointCount,
    renderedPoints,
    capped: reportedPointCount > HYDRO_MET_STATION_SERIES_LIMIT,
    inspectionCapped,
    nonOkFlags,
    nonOkFlagsCapped,
    qualitySummary,
  }
}

function validateStationSeriesMetadata(metadata: Record<string, unknown>, variable: HydroMetStationSeriesVariable) {
  const messages: string[] = []
  const limit = metadata.limit
  const returnedPoints = metadata.returned_points
  const truncated = metadata.truncated

  if (typeof limit !== 'number' || !Number.isInteger(limit) || limit <= 0) {
    messages.push(`变量 ${variable} metadata.limit 缺失或格式无效`)
  }
  if (typeof returnedPoints !== 'number' || !Number.isInteger(returnedPoints) || returnedPoints < 0) {
    messages.push(`变量 ${variable} metadata.returned_points 缺失或格式无效`)
  }
  if (typeof truncated !== 'boolean') {
    messages.push(`变量 ${variable} metadata.truncated 缺失或格式无效`)
  }

  const metadataTimeFields = ['requested_from', 'requested_to', 'returned_from', 'returned_to'] as const
  metadataTimeFields.forEach((field) => {
    if (!(field in metadata)) {
      messages.push(`变量 ${variable} metadata.${field} 缺失`)
      return
    }
    const value = metadata[field]
    if (value !== null && (typeof value !== 'string' || !normalizeHydroMetCycle(value))) {
      messages.push(`变量 ${variable} metadata.${field} 不是有效 RFC3339 时间`)
    }
  })

  return messages
}

function capHydroMetStationSeriesMessages(messages: string[]) {
  const safeMessages = messages.map((message) => (
    formatHydroMetStationSeriesUiString(message, {
      limit: HYDRO_MET_STATION_SERIES_MESSAGE_STRING_LIMIT,
      fallback: 'station-series contract 问题已截断',
    })
  ))
  if (safeMessages.length <= HYDRO_MET_STATION_SERIES_MESSAGE_LIMIT) return safeMessages
  return [
    ...safeMessages.slice(0, HYDRO_MET_STATION_SERIES_MESSAGE_LIMIT),
    `另有 ${safeMessages.length - HYDRO_MET_STATION_SERIES_MESSAGE_LIMIT} 条 station-series contract 问题已截断`,
  ]
}

function capInvalidPointMessages(
  variable: HydroMetStationSeriesVariable,
  messages: string[],
  invalidPointCount: number,
  inspectedPointCount: number,
  reportedPointCount: number,
  inspectionCapped: boolean,
) {
  const reservedSummarySlots = (invalidPointCount > messages.length ? 1 : 0) + (inspectionCapped ? 1 : 0)
  const detailLimit = Math.max(1, HYDRO_MET_STATION_SERIES_MESSAGE_LIMIT - reservedSummarySlots)
  const cappedMessages = messages.slice(0, detailLimit)
  const hiddenByMessageCap = Math.max(0, invalidPointCount - cappedMessages.length)
  if (hiddenByMessageCap > 0) {
    cappedMessages.push(`变量 ${variable} 另有 ${hiddenByMessageCap} 个已检查点无效，错误详情已截断`)
  }
  if (inspectionCapped) {
    cappedMessages.push(`变量 ${variable} capped 仅检查前 ${inspectedPointCount}/${reportedPointCount} 个点，响应过大，已停止继续校验`)
  }
  return cappedMessages
}

function parseChartableStationSeriesPoint(point: unknown): ChartableStationSeriesPoint | string {
  if (!isRecord(point)) return '不是对象'

  const validTimeValue = point.valid_time
  if (typeof validTimeValue !== 'string') return '缺少有效 valid_time'
  const validTime = normalizeHydroMetCycle(validTimeValue)
  if (!validTime) return `valid_time=${formatHydroMetStationSeriesContractValue(validTimeValue)} 不是有效 RFC3339 时间`

  const value = point.value
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'value 不是有限数值'

  const qualityFlagValue = point.quality_flag
  if (qualityFlagValue !== undefined && qualityFlagValue !== null && typeof qualityFlagValue !== 'string') {
    return 'quality_flag 格式无效'
  }

  return {
    timestamp: Date.parse(validTime),
    value,
    qualityFlag: qualityFlagValue
      ? formatHydroMetStationSeriesUiString(qualityFlagValue, {
          limit: HYDRO_MET_STATION_SERIES_QC_LABEL_LIMIT,
          fallback: 'missing',
          oversizeReplacement: 'flag capped',
        })
      : null,
  }
}

function qualityFlagLabel(value: string) {
  return formatHydroMetStationSeriesUiString(value, {
    limit: HYDRO_MET_STATION_SERIES_QC_LABEL_LIMIT,
    fallback: 'missing',
    oversizeReplacement: 'flag capped',
  })
}

function qualityFlagSummary(
  counts: Map<string, number>,
  inspectedPointCount: number,
  reportedPointCount: number,
  inspectionCapped: boolean,
) {
  const entries = Array.from(counts.entries()).sort(([left], [right]) => left.localeCompare(right))
  const nonOkFlagEntries = entries.filter(([flag]) => flag !== 'missing' && flag.trim() !== '' && flag.toLowerCase() !== 'ok')
  const nonOkFlags = nonOkFlagEntries.slice(0, HYDRO_MET_STATION_SERIES_QC_FLAG_LIMIT).map(([flag]) => qualityFlagLabel(flag))
  const nonOkFlagsCapped = nonOkFlagEntries.length > nonOkFlags.length

  if (counts.size === 0) {
    return {
      nonOkFlags,
      nonOkFlagsCapped,
      qualitySummary: inspectionCapped
        ? `none; inspected ${inspectedPointCount}/${reportedPointCount}, capped`
        : 'none',
    }
  }

  const visibleEntries = entries.slice(0, HYDRO_MET_STATION_SERIES_QC_FLAG_LIMIT)
  const summary = visibleEntries.map(([flag, count]) => `${qualityFlagLabel(flag || 'empty')}:${count}`).join(', ')
  const hidden = entries.length - visibleEntries.length
  const suffixes: string[] = []
  if (hidden > 0) suffixes.push(`+${hidden} flags capped`)
  if (inspectionCapped) suffixes.push(`inspected ${inspectedPointCount}/${reportedPointCount}, capped`)

  return {
    nonOkFlags,
    nonOkFlagsCapped,
    qualitySummary: [summary, ...suffixes].join('; '),
  }
}

function StationSeriesCharts({
  response,
  product,
  stationId,
}: {
  response: HydroMetStationSeriesResponse
  product: QhhLatestProduct
  stationId: string
}) {
  const seriesList = hydroMetStationSeriesItems(response)
  const responseRecord = isRecord(response) ? response : {}
  const identityMessages = capHydroMetStationSeriesMessages([
    ...validateHydroMetStationSeriesIdentity(response, product, stationId),
    ...validateHydroMetStationSeriesContract(response, product),
  ])
  const seriesByVariable = mapUniqueHydroMetStationSeries(seriesList)

  return (
    <div className="mt-3 space-y-3" data-testid="hydro-met-station-series-loaded">
      <dl className="grid grid-cols-[7.5rem_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
        <MetaRow label="station" value={formatStationSeriesScalar(responseRecord.station_id)} mono />
        <MetaRow label="source" value={formatStationSeriesScalar(responseRecord.source_id)} />
        <MetaRow label="cycle" value={formatStationSeriesDateTime(responseRecord.cycle_time)} mono />
        <MetaRow label="forcing_version" value={formatStationSeriesScalar(responseRecord.forcing_version_id)} mono />
        <MetaRow label="valid range" value={`${formatStationSeriesDateTime(responseRecord.valid_time_start)} - ${formatStationSeriesDateTime(responseRecord.valid_time_end)}`} mono />
      </dl>

      {identityMessages.length > 0 ? (
        <StatusPanel
          tone="warning"
          title="station-series identity 不一致"
          messages={identityMessages}
          testId="hydro-met-station-series-identity-warning"
        />
      ) : (
        <div className="grid gap-3">
          {HYDRO_MET_STATION_VARIABLES.map((variable) => (
            <StationVariableChart key={variable} variable={variable} series={seriesByVariable.get(variable) ?? null} />
          ))}
        </div>
      )}
    </div>
  )
}

function StationVariableChart({
  variable,
  series,
}: {
  variable: HydroMetStationSeriesVariable
  series: HydroMetStationSeriesRecord | null
}) {
  if (!series) {
    return (
      <VariableStatePanel variable={variable} testId={`hydro-met-variable-${variable}-missing`}>
        变量 {variable} 在 station-series 响应中缺失。
      </VariableStatePanel>
    )
  }

  const validation = validateHydroMetStationSeriesForChart(series)
  if (!validation.ok) {
    return (
      <VariableStatePanel variable={variable} testId={`hydro-met-variable-${variable}-invalid`}>
        {validation.messages.join('；')}
      </VariableStatePanel>
    )
  }

  const unitMissing = !validation.unit
  const truncated = validation.seriesTruncated || validation.metadata.truncated
  const capped = validation.capped

  if (unitMissing) {
    return (
      <VariableStatePanel variable={variable} testId={`hydro-met-variable-${variable}-missing-unit`}>
        变量 {variable} 缺少 unit 元数据，停止绘图。
      </VariableStatePanel>
    )
  }

  if (validation.renderedPoints.length === 0) {
    return (
      <VariableStatePanel variable={variable} testId={`hydro-met-variable-${variable}-empty`}>
        变量 {variable} 没有可绘制点。
      </VariableStatePanel>
    )
  }

  return (
    <div className="rounded-md border border-neutral-300 p-3" data-testid={`hydro-met-variable-${variable}-chart`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-neutral-900">{variable}</h3>
          <div className="mt-1 text-xs text-neutral-700">
            {validation.unit} · {validation.sourceId ?? 'source unknown'} · cycle {formatDateTime(validation.cycleTime)}
          </div>
          <div className="mt-1 font-mono text-[11px] text-neutral-500">
            {formatDateTime(validation.metadata.returned_from)} - {formatDateTime(validation.metadata.returned_to)}
          </div>
        </div>
        <div className="flex flex-wrap gap-1 text-xs">
          {validation.nonOkFlags.length > 0 ? (
            <span className="rounded border border-warning/50 bg-warning/10 px-2 py-1 text-neutral-900" data-testid={`hydro-met-variable-${variable}-qc`}>
              QC {validation.nonOkFlags.join(', ')}{validation.nonOkFlagsCapped ? ', ...' : ''}
            </span>
          ) : null}
          {truncated ? (
            <span className="rounded border border-danger/40 bg-danger/10 px-2 py-1 text-danger" data-testid={`hydro-met-variable-${variable}-truncated`}>
              truncated
            </span>
          ) : null}
          {capped ? (
            <span className="rounded border border-warning/50 bg-warning/10 px-2 py-1 text-neutral-900" data-testid={`hydro-met-variable-${variable}-capped`}>
              capped {validation.renderedPoints.length}/{validation.reportedPointCount}
            </span>
          ) : null}
        </div>
      </div>
      <StationSeriesChart variable={variable} unit={validation.unit} points={validation.renderedPoints} />
      <div className="mt-2 text-xs text-neutral-700" data-testid={`hydro-met-variable-${variable}-metadata`}>
        returned {validation.metadata.returned_points} / limit {validation.metadata.limit}; rendered {validation.renderedPoints.length}; inspected {validation.inspectedPointCount}/{validation.reportedPointCount}; quality_flag {validation.qualitySummary}
      </div>
    </div>
  )
}

function VariableStatePanel({
  variable,
  testId,
  children,
}: {
  variable: HydroMetStationSeriesVariable
  testId: string
  children: ReactNode
}) {
  return (
    <div className="rounded-md border border-dashed border-neutral-300 bg-neutral-50 p-3 text-sm text-neutral-700" data-testid={testId}>
      <div className="font-semibold text-neutral-900">{variable}</div>
      <div className="mt-1">{children}</div>
    </div>
  )
}

function StationSeriesChart({
  variable,
  unit,
  points,
}: {
  variable: HydroMetStationSeriesVariable
  unit: string
  points: ChartableStationSeriesPoint[]
}) {
  const option = useMemo(
    () => ({
      color: ['#0f8fbf'],
      grid: { left: 48, right: 14, top: 12, bottom: 34 },
      tooltip: {
        trigger: 'axis',
        renderMode: 'richText',
        valueFormatter: (value: number) => `${Number(value).toFixed(3)} ${unit}`,
      },
      xAxis: {
        type: 'time',
        axisLabel: { color: '#64748b' },
      },
      yAxis: {
        type: 'value',
        name: unit,
        axisLabel: { color: '#64748b' },
      },
      series: [
        {
          type: 'line',
          name: variable,
          showSymbol: points.length <= 48,
          symbolSize: 5,
          data: points.map((point) => [point.timestamp, point.value]),
        },
      ],
    }),
    [points, unit, variable],
  )

  return (
    <ReactEChartsCore
      echarts={echarts}
      option={option}
      notMerge
      lazyUpdate
      style={{ height: 220, width: '100%' }}
    />
  )
}

function RiverForecastPanel({
  product,
  segment,
  selectedRiverSegmentAbsent,
  state,
}: {
  product: QhhLatestProduct
  segment: HydroMetRiverSegmentFeature | null
  selectedRiverSegmentAbsent: boolean
  state: RiverForecastLoadState
}) {
  if (selectedRiverSegmentAbsent) {
    return (
      <StatusPanel
        tone="warning"
        title="选中河段不在候选列表中"
        messages={['当前选中河段不属于 latest-product 返回的真实 river segment candidates，已停止 forecast-series 请求。']}
        testId="hydro-met-river-forecast-unavailable"
      />
    )
  }

  if (!segment) {
    return (
      <StatusPanel
        tone="info"
        title="河段流量不可用"
        messages={['没有可选择的真实河段；不会使用手工 ID 或假河段请求 q_down forecast-series。']}
        testId="hydro-met-river-forecast-unavailable"
      />
    )
  }

  const segmentIdentity = riverForecastSegmentIdentity(segment)
  const currentRequestKey = riverForecastRequestKey(product, segmentIdentity.river_segment_id)
  const stale = state.kind !== 'idle' && state.requestKey !== currentRequestKey

  return (
    <section className="rounded-md border border-neutral-300 bg-white p-4" data-testid="hydro-met-river-forecast-panel">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <Waves className="h-4 w-4 text-primary-600" aria-hidden="true" />
            <h2 className="text-base font-semibold text-neutral-900">河段 q_down 流量图表</h2>
          </div>
          <div className="mt-1 font-mono text-xs text-neutral-600" data-testid="hydro-met-selected-river">
            {segmentIdentity.river_segment_id} · {segmentIdentity.name}
          </div>
        </div>
        <span className="rounded border border-neutral-300 px-2 py-1 font-mono text-xs text-neutral-700">
          limit {HYDRO_MET_RIVER_FORECAST_LIMIT}
        </span>
      </div>

      {state.kind === 'loading' || stale ? (
        <div className="mt-3 flex items-center gap-2 rounded border border-neutral-300 bg-neutral-50 p-3 text-sm text-neutral-700" role="status" data-testid="hydro-met-river-forecast-loading">
          <Loader2 className="h-4 w-4 animate-spin text-primary-600" aria-hidden="true" />
          正在加载 {segmentIdentity.river_segment_id} 的 q_down forecast-series...
        </div>
      ) : null}

      {state.kind === 'error' && !stale ? (
        <StatusPanel tone="danger" title="river forecast-series 加载失败" messages={[state.message]} testId="hydro-met-river-forecast-error" />
      ) : null}

      {state.kind === 'loaded' && !stale ? (
        <RiverForecastChartState response={state.response} product={product} segment={segmentIdentity} />
      ) : null}
    </section>
  )
}

function RiverForecastChartState({
  response,
  product,
  segment,
}: {
  response: HydroMetRiverForecastPayload
  product: QhhLatestProduct
  segment: HydroMetRiverForecastSegmentIdentity
}) {
  const validation = validateHydroMetRiverForecastForChart(response, product, segment)

  if (!validation.ok) {
    return (
      <StatusPanel
        tone="warning"
        title="q_down river discharge 不可绘制"
        messages={validation.messages}
        testId="hydro-met-river-forecast-invalid"
      />
    )
  }

  return (
    <div className="mt-3 space-y-3" data-testid="hydro-met-river-forecast-loaded">
      <dl className="grid grid-cols-[7.5rem_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
        <MetaRow label="river segment" value={`${segment.river_segment_id} · ${segment.name}`} mono />
        <MetaRow label="variable" value={validation.variable} mono />
        <MetaRow label="unit" value={validation.unit} />
        <MetaRow label="source" value={`${validation.sourceId} / ${validation.scenarioId}`} />
        <MetaRow label="cycle" value={formatDateTime(validation.cycleTime ?? product.cycle_time)} mono />
        <MetaRow label="issue_time" value={formatDateTime(validation.issueTime)} mono />
        <MetaRow label="valid range" value={`${formatDateTime(validation.validTimeStart)} - ${formatDateTime(validation.validTimeEnd)}`} mono />
        <MetaRow label="points" value={`${validation.renderedPoints.length} rendered / ${validation.pointCount} returned`} />
      </dl>

      <RiverForecastHorizonBanner validation={validation} />
      <RiverForecastChart validation={validation} segmentName={segment.name} />
    </div>
  )
}

function RiverForecastHorizonBanner({ validation }: { validation: Extract<HydroMetRiverForecastValidation, { ok: true }> }) {
  return (
    <div
      className={cn(
        'rounded border p-2 text-xs text-neutral-900',
        validation.horizonShorter ? 'border-warning/40 bg-warning/10' : 'border-primary-100 bg-primary-50',
      )}
      data-testid="hydro-met-river-horizon"
    >
      {validation.horizonLabel}
      {validation.capped ? `; capped ${validation.renderedPoints.length}/${validation.pointCount}` : ''}
    </div>
  )
}

function RiverForecastChart({
  validation,
  segmentName,
}: {
  validation: Extract<HydroMetRiverForecastValidation, { ok: true }>
  segmentName: string
}) {
  const option = useMemo(
    () => ({
      color: [validation.sourceId === 'IFS' ? '#2ca02c' : '#0f8fbf'],
      title: {
        text: `${segmentName} q_down river discharge`,
        subtext: `${validation.sourceId} / ${validation.scenarioId}\n${validation.validTimeStart} - ${validation.validTimeEnd}`,
        left: 0,
        textStyle: { fontSize: 15, fontWeight: 650, color: '#1f2937' },
        subtextStyle: { color: '#64748b', lineHeight: 18 },
      },
      grid: { left: 52, right: 16, top: 86, bottom: 42 },
      tooltip: {
        trigger: 'axis',
        renderMode: 'richText',
        valueFormatter: (value: number) => `${Number(value).toFixed(3)} ${validation.unit}`,
      },
      xAxis: {
        type: 'time',
        axisLabel: { color: '#64748b' },
      },
      yAxis: {
        type: 'value',
        name: validation.unit,
        scale: true,
        axisLabel: { color: '#64748b' },
      },
      series: [
        {
          type: 'line',
          name: 'q_down river discharge',
          showSymbol: validation.renderedPoints.length <= 48,
          symbolSize: 5,
          data: validation.renderedPoints.map((point) => [point.timestamp, point.value]),
          lineStyle: { width: 2.5, type: validation.sourceId === 'IFS' ? 'dashed' : 'solid' },
        },
      ],
    }),
    [segmentName, validation],
  )

  return (
    <ReactEChartsCore
      echarts={echarts}
      option={option}
      notMerge
      lazyUpdate
      style={{ height: 300, minHeight: 260, width: '100%' }}
    />
  )
}

function firstRiverSegmentId(features: HydroMetRiverSegmentFeature[]) {
  return features[0] ? riverSegmentId(features[0]) : null
}

function riverSegmentId(feature: HydroMetRiverSegmentFeature) {
  return feature.properties.river_segment_id || feature.properties.segment_id
}

function riverForecastSegmentIdentity(feature: HydroMetRiverSegmentFeature): HydroMetRiverForecastSegmentIdentity {
  return {
    river_segment_id: riverSegmentId(feature),
    segment_id: feature.properties.segment_id,
    river_network_version_id: feature.properties.river_network_version_id,
    basin_version_id: feature.properties.basin_version_id,
    name: formatHydroMetRiverForecastUiString(feature.properties.name || feature.properties.river_segment_id, {
      fallback: feature.properties.river_segment_id,
    }),
  }
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
  const safeMessages = boundedHydroMetMessageList(messages, (message) => formatHydroMetStatusMessage(message), {
    summaryLabel: '状态详情',
  })

  return (
    <section className={cn('rounded-md border p-4', toneClass)} role={tone === 'danger' ? 'alert' : 'status'} data-testid={testId}>
      <div className="flex items-center gap-2 font-semibold">
        <AlertTriangle className="h-4 w-4" aria-hidden="true" />
        {title}
      </div>
      <ul className="mt-2 space-y-1 text-sm">
        {safeMessages.map((message, index) => <li key={`${message.kind}-${index}-${message.text}`} data-kind={message.kind}>{message.text}</li>)}
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

function formatHydroMetStatusMessage(value: unknown, fallback = '状态详情不可用') {
  return formatHydroMetStationSeriesMessage(value, fallback)
}

function boundedHydroMetMessageList<T>(
  items: readonly T[],
  formatItem: (item: T) => string,
  options: {
    limit?: number
    summaryLabel: string
  },
): BoundedHydroMetMessage[] {
  const limit = Math.max(1, Math.trunc(options.limit ?? HYDRO_MET_PAGE_MESSAGE_LIST_LIMIT))
  const visibleItems = items.slice(0, limit).map((item) => ({
    kind: 'message' as const,
    text: formatItem(item),
  }))
  const hiddenCount = Math.max(0, items.length - visibleItems.length)
  if (hiddenCount <= 0) return visibleItems
  return [
    ...visibleItems,
    {
      kind: 'summary',
      text: formatHydroMetStatusMessage(`另有 ${hiddenCount} 条${options.summaryLabel}已截断`, `${options.summaryLabel}已截断`),
    },
  ]
}

function getHydroMetQualityNotes(product: QhhLatestProduct): unknown[] {
  const notes = product.availability?.quality_notes
  return Array.isArray(notes) ? notes : []
}

function formatHydroMetQualityNote(note: unknown) {
  if (!isRecord(note)) return formatHydroMetStatusMessage(note, '质量备注不可用')
  const code = formatHydroMetStatusMessage(note.code, '质量备注代码不可用')
  const message = formatHydroMetStatusMessage(note.message, '质量备注不可用')
  return `${code}: ${message}`
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
  if (typeof value !== 'string' || !value) return '-'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toISOString()
}

function formatStationSeriesScalar(value: unknown) {
  if (typeof value !== 'string' || !value) return '-'
  return formatHydroMetStationSeriesUiString(value, {
    limit: HYDRO_MET_STATION_SERIES_UI_STRING_LIMIT,
    fallback: '-',
  })
}

function formatStationSeriesDateTime(value: unknown) {
  if (typeof value !== 'string' || !value) return '-'
  const normalized = normalizeHydroMetCycle(value)
  if (normalized) return normalized
  return formatHydroMetStationSeriesUiString(value, {
    limit: HYDRO_MET_STATION_SERIES_UI_STRING_LIMIT,
    fallback: 'invalid time',
    oversizeReplacement: 'invalid time (capped)',
  })
}

function formatCoordinate(value: number | undefined) {
  return Number.isFinite(value) ? value.toFixed(4) : '-'
}

function markerPosition(
  lon: number,
  lat: number,
  bounds: { minLon: number; maxLon: number; minLat: number; maxLat: number },
) {
  const lonSpan = Math.max(bounds.maxLon - bounds.minLon, 0.000001)
  const latSpan = Math.max(bounds.maxLat - bounds.minLat, 0.000001)
  return {
    x: 8 + ((lon - bounds.minLon) / lonSpan) * 84,
    y: 92 - ((lat - bounds.minLat) / latSpan) * 84,
  }
}

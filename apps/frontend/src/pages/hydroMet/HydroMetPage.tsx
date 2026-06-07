import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
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
  fetchHydroMetRiverSegments,
  fetchHydroMetStations,
  hydroMetStationFilterAvailability,
  hydroMetStreamOrderAvailable,
  loadHydroMetBootstrap,
  type HydroMetBootstrapResult,
  type HydroMetRiverSegmentCollection,
  type HydroMetRiverSegmentFeature,
  type HydroMetStation,
  type HydroMetStationPage,
  type QhhLatestProduct,
} from '@/pages/hydroMet/bootstrap'
import { BasinSelector } from '@/pages/hydroMet/BasinSelector'
import { ProductStatusBar, ReturnPeriodSection } from '@/pages/hydroMet/ReturnPeriodSection'

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
    void loadHydroMetBootstrap({
      source: state.source,
      cycle: state.cycle,
      basinId: state.basin,
      strictIdentity: state.strictIdentity,
    }).then(
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
  }, [state.basin, state.cycle, state.source, state.strictIdentity, state.strictIdentityError])

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
        <div className="flex flex-wrap items-center gap-3">
          <BasinSelector
            selectedBasinId={state.basin}
            onSelect={(basinId) => updateState({ basin: basinId, cycle: null })}
          />
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

const HYDRO_MET_LIST_SEARCH_DEBOUNCE_MS = 300
const HYDRO_MET_FORCING_VARIABLES = HYDRO_MET_STATION_VARIABLES

type StationInventoryState = {
  stations: HydroMetStation[]
  page: HydroMetStationPage | null
  totalCount: number
  loading: boolean
  error: string | null
  search: string
  setSearch: (value: string) => void
  selectedVariables: HydroMetStationSeriesVariable[]
  toggleVariable: (variable: HydroMetStationSeriesVariable) => void
  offset: number
  limit: number
  goToOffset: (offset: number) => void
  filterAvailability: ReturnType<typeof hydroMetStationFilterAvailability>
}

type RiverSegmentInventoryState = {
  riverSegments: HydroMetRiverSegmentFeature[]
  collection: HydroMetRiverSegmentCollection | null
  total: number
  loading: boolean
  error: string | null
  search: string
  setSearch: (value: string) => void
  streamOrderMin: string
  streamOrderMax: string
  setStreamOrderMin: (value: string) => void
  setStreamOrderMax: (value: string) => void
  streamOrderAvailable: boolean
  offset: number
  limit: number
  goToOffset: (offset: number) => void
}

function useDebouncedValue<T>(value: T, delayMs: number) {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const handle = setTimeout(() => setDebounced(value), delayMs)
    return () => clearTimeout(handle)
  }, [value, delayMs])
  return debounced
}

function parseStreamOrderInput(value: string): number | undefined {
  const trimmed = value.trim()
  if (!trimmed) return undefined
  const parsed = Number(trimmed)
  return Number.isFinite(parsed) ? parsed : undefined
}

function useStationInventory(
  product: QhhLatestProduct,
  initialPage: HydroMetStationPage | null,
  initialStations: HydroMetStation[],
): StationInventoryState {
  const [search, setSearch] = useState('')
  const [selectedVariables, setSelectedVariables] = useState<HydroMetStationSeriesVariable[]>([])
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState<HydroMetStationPage | null>(initialPage)
  const [stations, setStations] = useState<HydroMetStation[]>(initialStations)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const debouncedSearch = useDebouncedValue(search, HYDRO_MET_LIST_SEARCH_DEBOUNCE_MS)
  // The bootstrap already served the default query (no search/variables, offset 0) for this
  // product identity, so we mark it as served to avoid a duplicate request on mount. Any other
  // query (search / variables / page) is fetched. This is robust to effect double-invocation.
  const servedKeyRef = useRef(`${product.forcing_version_id}|${product.source_id}|${product.cycle_time}|||0`)

  // Reset to the bootstrap page whenever the product identity changes.
  useEffect(() => {
    setSearch('')
    setSelectedVariables([])
    setOffset(0)
    setPage(initialPage)
    setStations(initialStations)
    setError(null)
    servedKeyRef.current = `${product.forcing_version_id}|${product.source_id}|${product.cycle_time}|||0`
  }, [product.forcing_version_id, product.source_id, product.cycle_time, initialPage, initialStations])

  // Reset paging when filters change.
  useEffect(() => {
    setOffset(0)
  }, [debouncedSearch, selectedVariables])

  useEffect(() => {
    const queryKey = `${product.forcing_version_id}|${product.source_id}|${product.cycle_time}|${debouncedSearch}|${selectedVariables.join(',')}|${offset}`
    if (queryKey === servedKeyRef.current) return
    servedKeyRef.current = queryKey
    let cancelled = false
    setLoading(true)
    setError(null)
    void fetchHydroMetStations(product, {
      search: debouncedSearch,
      variables: selectedVariables,
      limit: HYDRO_MET_STATION_LIMIT,
      offset,
    }).then(
      (nextPage) => {
        if (cancelled) return
        setPage(nextPage)
        setStations(nextPage.items)
        setLoading(false)
      },
      (loadError) => {
        if (cancelled) return
        setError(formatHydroMetStatusMessage(loadError, '站点 inventory 加载失败'))
        setLoading(false)
      },
    )
    return () => {
      cancelled = true
    }
  }, [product, debouncedSearch, selectedVariables, offset])

  const filterAvailability = useMemo(() => hydroMetStationFilterAvailability(page), [page])

  const toggleVariable = (variable: HydroMetStationSeriesVariable) => {
    setSelectedVariables((current) =>
      current.includes(variable) ? current.filter((item) => item !== variable) : [...current, variable],
    )
  }

  return {
    stations,
    page,
    totalCount: page?.total_count ?? product.station_count,
    loading,
    error,
    search,
    setSearch,
    selectedVariables,
    toggleVariable,
    offset,
    limit: page?.limit ?? HYDRO_MET_STATION_LIMIT,
    goToOffset: setOffset,
    filterAvailability,
  }
}

function useRiverSegmentInventory(
  product: QhhLatestProduct,
  initialCollection: HydroMetRiverSegmentCollection | null,
  initialFeatures: HydroMetRiverSegmentFeature[],
): RiverSegmentInventoryState {
  const [search, setSearch] = useState('')
  const [streamOrderMin, setStreamOrderMin] = useState('')
  const [streamOrderMax, setStreamOrderMax] = useState('')
  const [offset, setOffset] = useState(0)
  const [collection, setCollection] = useState<HydroMetRiverSegmentCollection | null>(initialCollection)
  const [riverSegments, setRiverSegments] = useState<HydroMetRiverSegmentFeature[]>(initialFeatures)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const debouncedSearch = useDebouncedValue(search, HYDRO_MET_LIST_SEARCH_DEBOUNCE_MS)
  const debouncedMin = useDebouncedValue(streamOrderMin, HYDRO_MET_LIST_SEARCH_DEBOUNCE_MS)
  const debouncedMax = useDebouncedValue(streamOrderMax, HYDRO_MET_LIST_SEARCH_DEBOUNCE_MS)
  const servedKeyRef = useRef(`${product.forcing_version_id}|${product.source_id}|${product.cycle_time}||||0`)

  useEffect(() => {
    setSearch('')
    setStreamOrderMin('')
    setStreamOrderMax('')
    setOffset(0)
    setCollection(initialCollection)
    setRiverSegments(initialFeatures)
    setError(null)
    servedKeyRef.current = `${product.forcing_version_id}|${product.source_id}|${product.cycle_time}||||0`
  }, [product.forcing_version_id, product.source_id, product.cycle_time, initialCollection, initialFeatures])

  useEffect(() => {
    setOffset(0)
  }, [debouncedSearch, debouncedMin, debouncedMax])

  useEffect(() => {
    const minValue = parseStreamOrderInput(debouncedMin)
    const maxValue = parseStreamOrderInput(debouncedMax)
    const queryKey = `${product.forcing_version_id}|${product.source_id}|${product.cycle_time}|${debouncedSearch}|${minValue ?? ''}|${maxValue ?? ''}|${offset}`
    if (queryKey === servedKeyRef.current) return
    servedKeyRef.current = queryKey
    let cancelled = false
    setLoading(true)
    setError(null)
    void fetchHydroMetRiverSegments(product, {
      search: debouncedSearch,
      streamOrderMin: minValue,
      streamOrderMax: maxValue,
      limit: HYDRO_MET_RIVER_SEGMENT_LIMIT,
      offset,
    }).then(
      (nextCollection) => {
        if (cancelled) return
        setCollection(nextCollection)
        setRiverSegments(nextCollection.features)
        setLoading(false)
      },
      (loadError) => {
        if (cancelled) return
        setError(formatHydroMetStatusMessage(loadError, '河段流量候选加载失败'))
        setLoading(false)
      },
    )
    return () => {
      cancelled = true
    }
  }, [product, debouncedSearch, debouncedMin, debouncedMax, offset])

  // stream_order is only offered when the underlying river data carries the field.
  const streamOrderAvailable = useMemo(() => hydroMetStreamOrderAvailable(riverSegments), [riverSegments])

  return {
    riverSegments,
    collection,
    total: collection?.total ?? product.segment_count,
    loading,
    error,
    search,
    setSearch,
    streamOrderMin,
    streamOrderMax,
    setStreamOrderMin,
    setStreamOrderMax,
    streamOrderAvailable,
    offset,
    limit: collection?.limit ?? HYDRO_MET_RIVER_SEGMENT_LIMIT,
    goToOffset: setOffset,
  }
}

export function ReadyHydroMetContent({ result, product }: { result: HydroMetBootstrapResult; product: QhhLatestProduct }) {
  const [selectedStationId, setSelectedStationId] = useState<string | null>(null)
  const [seriesState, setSeriesState] = useState<StationSeriesLoadState>({ kind: 'idle' })
  const [selectedRiverSegmentId, setSelectedRiverSegmentId] = useState<string | null>(null)
  const [riverForecastState, setRiverForecastState] = useState<RiverForecastLoadState>({ kind: 'idle' })

  const stationInventory = useStationInventory(product, result.stationPage, result.stations)
  const riverInventory = useRiverSegmentInventory(product, result.riverSegmentCollection, result.riverSegments)
  const stations = stationInventory.stations
  const riverSegments = riverInventory.riverSegments

  useEffect(() => {
    setSelectedStationId(null)
    setSeriesState({ kind: 'idle' })
    setSelectedRiverSegmentId(null)
    setRiverForecastState({ kind: 'idle' })
  }, [product.forcing_version_id, product.source_id, product.cycle_time])

  useEffect(() => {
    if (selectedStationId !== null) return
    setSelectedStationId(stations[0]?.station_id ?? null)
  }, [stations, selectedStationId])

  useEffect(() => {
    if (selectedRiverSegmentId !== null) return
    setSelectedRiverSegmentId(firstRiverSegmentId(riverSegments))
  }, [riverSegments, selectedRiverSegmentId])

  const selectedStation = useMemo(
    () => stations.find((station) => station.station_id === selectedStationId) ?? null,
    [stations, selectedStationId],
  )
  const selectedRiverSegment = useMemo(
    () => riverSegments.find((feature) => riverSegmentId(feature) === selectedRiverSegmentId) ?? null,
    [riverSegments, selectedRiverSegmentId],
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
        <ProductStatusBar product={product} />
        <ProductPanel product={product} />
        <ReturnPeriodSection product={product} />
        {result.stationError ? <StatusPanel tone="warning" title="站点 inventory 部分失败" messages={[result.stationError]} testId="hydro-met-station-partial-failure" /> : null}
        {result.riverError ? <StatusPanel tone="warning" title="河段流量候选部分失败" messages={[result.riverError]} testId="hydro-met-river-partial-failure" /> : null}
      </aside>

      <section className="space-y-3">
        <StationInventoryPanel
          product={product}
          inventory={stationInventory}
          selectedStationId={selectedStationId}
          onSelectStation={setSelectedStationId}
        />

        <RiverSegmentInventoryPanel
          inventory={riverInventory}
          selectedRiverSegmentId={selectedRiverSegmentId}
          onSelectRiverSegment={setSelectedRiverSegmentId}
        />
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

function StationInventoryPanel({
  product,
  inventory,
  selectedStationId,
  onSelectStation,
}: {
  product: QhhLatestProduct
  inventory: StationInventoryState
  selectedStationId: string | null
  onSelectStation: (stationId: string) => void
}) {
  const { stations, totalCount, loading, error, search, filterAvailability } = inventory
  const markerStations = stations.filter((station) => getHydroMetStationCoordinates(station))

  return (
    <section className="rounded-md border border-neutral-300 bg-white p-4" data-testid="hydro-met-station-panel">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2">
          <RadioTower className="h-4 w-4 text-primary-600" aria-hidden="true" />
          <h2 className="text-base font-semibold text-neutral-900">气象 forcing 站点</h2>
        </div>
        <span className="shrink-0 rounded border border-neutral-300 px-2 py-1 text-xs text-neutral-700" data-testid="hydro-met-station-summary">
          {stations.length} / {totalCount} stations
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
          placeholder="搜索 station id / name（后端 search）"
          value={search}
          onChange={(event) => inventory.setSearch(event.target.value)}
        />
      </label>

      {filterAvailability.variables ? (
        <div className="mt-3" data-testid="hydro-met-station-variable-filter">
          <span className="block text-xs font-medium text-neutral-700">按 forcing 变量覆盖筛选</span>
          <div className="mt-2 flex flex-wrap gap-2">
            {HYDRO_MET_FORCING_VARIABLES.map((variable) => {
              const active = inventory.selectedVariables.includes(variable)
              return (
                <button
                  key={variable}
                  type="button"
                  className={cn(
                    'h-8 cursor-pointer rounded border px-2 text-xs font-medium transition-colors',
                    active ? 'border-primary-600 bg-primary-600 text-white' : 'border-neutral-300 text-neutral-700 hover:bg-neutral-100',
                  )}
                  onClick={() => inventory.toggleVariable(variable)}
                  aria-pressed={active}
                  data-testid={`hydro-met-station-variable-${variable}`}
                >
                  {variable}
                </button>
              )
            })}
          </div>
        </div>
      ) : (
        <div className="mt-3 rounded border border-dashed border-neutral-300 bg-neutral-50 p-2 text-xs text-neutral-600" data-testid="hydro-met-station-variable-unavailable">
          变量覆盖筛选不可用：当前产品身份未提供 model_id，无法按变量覆盖筛选站点。
        </div>
      )}

      {filterAvailability.qcStatus ? null : (
        <div className="mt-2 rounded border border-dashed border-neutral-300 bg-neutral-50 p-2 text-xs text-neutral-600" data-testid="hydro-met-station-qc-unavailable">
          QC 状态筛选不可用：站点 inventory 不含 QC 字段。
        </div>
      )}

      {loading ? (
        <div className="mt-3 flex items-center gap-2 rounded border border-neutral-300 bg-neutral-50 p-3 text-sm text-neutral-700" role="status" data-testid="hydro-met-station-loading">
          <Loader2 className="h-4 w-4 animate-spin text-primary-600" aria-hidden="true" />
          正在按后端 search/筛选加载站点...
        </div>
      ) : null}

      {error ? (
        <StatusPanel tone="danger" title="站点 inventory 加载失败" messages={[error]} testId="hydro-met-station-load-error" />
      ) : null}

      {!loading && !error && stations.length === 0 ? (
        <div className="mt-3 rounded border border-neutral-300 bg-neutral-50 p-3 text-sm text-neutral-700" data-testid="hydro-met-station-no-results">
          没有匹配的真实站点：{search || '当前筛选'}（不会生成替代站点，也不会自动切换到其他产品）。
        </div>
      ) : null}

      {stations.length > 0 ? (
        <div className="mt-3 max-h-[26rem] space-y-2 overflow-auto pr-1" data-testid="hydro-met-station-list">
          {stations.map((station) => (
            <StationRow
              key={station.station_id}
              station={station}
              product={product}
              selected={station.station_id === selectedStationId}
              onSelect={onSelectStation}
            />
          ))}
        </div>
      ) : null}

      <InventoryPagination
        offset={inventory.offset}
        limit={inventory.limit}
        totalCount={totalCount}
        loaded={stations.length}
        loading={loading}
        onGoToOffset={inventory.goToOffset}
        testId="hydro-met-station-pagination"
      />
    </section>
  )
}

function RiverSegmentInventoryPanel({
  inventory,
  selectedRiverSegmentId,
  onSelectRiverSegment,
}: {
  inventory: RiverSegmentInventoryState
  selectedRiverSegmentId: string | null
  onSelectRiverSegment: (riverSegmentId: string) => void
}) {
  const { riverSegments, total, loading, error, search, streamOrderAvailable } = inventory

  return (
    <section className="rounded-md border border-neutral-300 bg-white p-4" data-testid="hydro-met-river-list">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2">
          <GitBranch className="h-4 w-4 text-primary-600" aria-hidden="true" />
          <h2 className="text-base font-semibold text-neutral-900">河段流量候选</h2>
        </div>
        <span className="shrink-0 rounded border border-neutral-300 px-2 py-1 text-xs text-neutral-700" data-testid="hydro-met-river-summary">
          {riverSegments.length} / {total} river segments
        </span>
      </div>

      <label className="mt-3 flex h-10 items-center gap-2 rounded border border-neutral-300 px-3 focus-within:border-primary-500" data-testid="hydro-met-river-search">
        <Search className="h-4 w-4 text-neutral-500" aria-hidden="true" />
        <input
          aria-label="搜索河段"
          className="min-w-0 flex-1 bg-transparent text-sm text-neutral-900 outline-none"
          placeholder="搜索 segment id / name（后端 search）"
          value={search}
          onChange={(event) => inventory.setSearch(event.target.value)}
        />
      </label>

      {streamOrderAvailable ? (
        <div className="mt-3 flex flex-wrap items-end gap-2" data-testid="hydro-met-river-stream-order-filter">
          <label className="space-y-1">
            <span className="block text-xs font-medium text-neutral-700">stream order ≥</span>
            <input
              aria-label="stream order 最小值"
              type="number"
              className="h-9 w-24 rounded border border-neutral-300 px-2 text-sm"
              value={inventory.streamOrderMin}
              onChange={(event) => inventory.setStreamOrderMin(event.target.value)}
              data-testid="hydro-met-river-stream-order-min"
            />
          </label>
          <label className="space-y-1">
            <span className="block text-xs font-medium text-neutral-700">stream order ≤</span>
            <input
              aria-label="stream order 最大值"
              type="number"
              className="h-9 w-24 rounded border border-neutral-300 px-2 text-sm"
              value={inventory.streamOrderMax}
              onChange={(event) => inventory.setStreamOrderMax(event.target.value)}
              data-testid="hydro-met-river-stream-order-max"
            />
          </label>
        </div>
      ) : (
        <div className="mt-3 rounded border border-dashed border-neutral-300 bg-neutral-50 p-2 text-xs text-neutral-600" data-testid="hydro-met-river-stream-order-unavailable">
          stream order 过滤不可用：底层河段数据不含 stream_order 字段。
        </div>
      )}

      {loading ? (
        <div className="mt-3 flex items-center gap-2 rounded border border-neutral-300 bg-neutral-50 p-3 text-sm text-neutral-700" role="status" data-testid="hydro-met-river-loading">
          <Loader2 className="h-4 w-4 animate-spin text-primary-600" aria-hidden="true" />
          正在按后端 search/分页加载河段...
        </div>
      ) : null}

      {error ? (
        <StatusPanel tone="danger" title="河段流量候选加载失败" messages={[error]} testId="hydro-met-river-load-error" />
      ) : null}

      {!loading && !error && riverSegments.length === 0 ? (
        <div className="mt-3 rounded border border-neutral-300 bg-neutral-50 p-3 text-sm text-neutral-700" data-testid="hydro-met-empty-rivers">
          河段列表为空：没有匹配的河段流量候选，且不会填充假河段。
        </div>
      ) : null}

      {riverSegments.length > 0 ? (
        <div className="mt-3 max-h-[26rem] space-y-2 overflow-auto pr-1" data-testid="hydro-met-river-segment-list">
          {riverSegments.map((feature) => (
            <RiverSegmentRow
              key={riverSegmentId(feature)}
              feature={feature}
              selected={riverSegmentId(feature) === selectedRiverSegmentId}
              onSelect={onSelectRiverSegment}
            />
          ))}
        </div>
      ) : null}

      <InventoryPagination
        offset={inventory.offset}
        limit={inventory.limit}
        totalCount={total}
        loaded={riverSegments.length}
        loading={loading}
        onGoToOffset={inventory.goToOffset}
        testId="hydro-met-river-pagination"
      />
    </section>
  )
}

function InventoryPagination({
  offset,
  limit,
  totalCount,
  loaded,
  loading,
  onGoToOffset,
  testId,
}: {
  offset: number
  limit: number
  totalCount: number
  loaded: number
  loading: boolean
  onGoToOffset: (offset: number) => void
  testId: string
}) {
  const safeLimit = Math.max(1, limit)
  const pageStart = totalCount === 0 ? 0 : offset + 1
  const pageEnd = offset + loaded
  const hasPrev = offset > 0
  const hasNext = offset + safeLimit < totalCount

  if (totalCount <= safeLimit && offset === 0) {
    return (
      <div className="mt-3 text-xs text-neutral-600" data-testid={testId}>
        共 {totalCount} 条，已按页加载（limit {safeLimit}，不全量加载）。
      </div>
    )
  }

  return (
    <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-neutral-700" data-testid={testId}>
      <span data-testid={`${testId}-range`}>
        显示 {pageStart}-{pageEnd} / 共 {totalCount}（limit {safeLimit}，按页加载）
      </span>
      <div className="flex gap-2">
        <button
          type="button"
          className="h-8 cursor-pointer rounded border border-neutral-300 px-3 text-neutral-700 transition-colors hover:bg-neutral-100 disabled:cursor-not-allowed disabled:opacity-40"
          disabled={!hasPrev || loading}
          onClick={() => onGoToOffset(Math.max(0, offset - safeLimit))}
          data-testid={`${testId}-prev`}
        >
          上一页
        </button>
        <button
          type="button"
          className="h-8 cursor-pointer rounded border border-neutral-300 px-3 text-neutral-700 transition-colors hover:bg-neutral-100 disabled:cursor-not-allowed disabled:opacity-40"
          disabled={!hasNext || loading}
          onClick={() => onGoToOffset(offset + safeLimit)}
          data-testid={`${testId}-next`}
        >
          下一页
        </button>
      </div>
    </div>
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

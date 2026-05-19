import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { Activity, CloudRain, GitBranch, MapPinned, RefreshCw, ThermometerSun, Waves } from 'lucide-react'

import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'
import { ForecastChart } from '@/components/charts/ForecastChart'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/cn'
import {
  getM11SelectedSegmentGeometryBudgetStatus,
  type M11SelectedSegmentGeometryBudgetStatus,
} from '@/lib/m11/overviewDataContracts'
import { m11QueryHref, needsM11QueryReplacement, parseM11QueryState, serializeM11QueryState } from '@/lib/m11/queryState'
import { useForecastStore, type ForecastData, type ForecastSegmentInfo } from '@/stores/forecast'

type RiverSegment = components['schemas']['RiverSegment']
type LineageResponse = components['schemas']['LineageResponse']
type MetStation = components['schemas']['MetStation']
type TimeseriesResponse = components['schemas']['TimeseriesResponse']

const THRESHOLD_KEYS = ['Q2', 'Q5', 'Q10', 'Q20', 'Q50', 'Q100'] as const
const SCENARIO_TOGGLES = [
  { key: 'analysis', label: 'Analysis' },
  { key: 'gfs', label: 'GFS' },
  { key: 'ifs', label: 'IFS' },
] as const

type ScenarioToggle = (typeof SCENARIO_TOGGLES)[number]['key']

interface SegmentDetailModel {
  segmentId: string
  basinVersionId: string
  riverNetworkVersionId: string
  segment: RiverSegment
  geometryStatus: M11SelectedSegmentGeometryBudgetStatus
  forecastData: ForecastData | null
  lineage: LineageResponse | null
  currentQ: number | null
  peakQ: number | null
  peakTime: string | null
  unit: string
}

type StationForcingVariable = 'PRCP' | 'TEMP'

interface StationForcingSeriesRow {
  variable: StationForcingVariable
  unit: string
  points: { time: string | number; value: number }[]
}

interface StationForcingViewModel {
  status: 'available' | 'restricted' | 'unavailable'
  restrictedReason: string | null
  unavailableReason: string | null
  station: {
    id: string
    name: string | null
    role: string | null
    source: string | null
    location: string | null
    elevationM: number | null
  } | null
  series: StationForcingSeriesRow[]
}

function finiteNumber(value: unknown): number | null {
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric : null
}

function formatMetric(value: number | null | undefined, unit = '') {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-'
  return `${value.toLocaleString('en-US', { maximumFractionDigits: 2 })}${unit ? ` ${unit}` : ''}`
}

function formatDateTime(value: string | number | null | undefined) {
  if (!value) return '-'
  const timestamp = typeof value === 'number' ? value : Date.parse(value)
  if (!Number.isFinite(timestamp)) return String(value)
  return new Date(timestamp).toISOString().slice(0, 16).replace('T', ' ') + ' UTC'
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

function pointTimeMs(value: string | number) {
  const numeric = Number(value)
  if (Number.isFinite(numeric)) return numeric
  return Date.parse(String(value))
}

function sourceKey(series: ForecastData['series'][number]): ScenarioToggle {
  if (series.isAnalysis) return 'analysis'
  const source = `${series.source ?? ''} ${series.scenario}`.toLowerCase()
  if (source.includes('ifs')) return 'ifs'
  return 'gfs'
}

function buildScenarioFilteredData(data: ForecastData | null, visible: Record<ScenarioToggle, boolean>): ForecastData | null {
  if (!data) return null
  return {
    ...data,
    series: data.series.filter((series) => visible[sourceKey(series)]),
  }
}

function allForecastPoints(data: ForecastData | null) {
  return (data?.series ?? [])
    .flatMap((series) => series.points.map((point) => ({ ...point, source: sourceKey(series), isAnalysis: series.isAnalysis })))
    .map((point) => ({ ...point, value: finiteNumber(point.value), timeMs: pointTimeMs(point.time) }))
    .filter((point): point is typeof point & { value: number; timeMs: number } => Number.isFinite(point.timeMs) && point.value !== null)
    .sort((left, right) => left.timeMs - right.timeMs)
}

function closestValidTime(target: string, validTimes: string[]) {
  const targetMs = Date.parse(target)
  if (!Number.isFinite(targetMs) || validTimes.length === 0) return null
  return [...validTimes].sort((left, right) => Math.abs(Date.parse(left) - targetMs) - Math.abs(Date.parse(right) - targetMs))[0] ?? null
}

function buildDetailModel(
  segmentId: string,
  basinVersionId: string,
  riverNetworkVersionId: string,
  segment: RiverSegment,
  forecastData: ForecastData | null,
  lineage: LineageResponse | null,
  validTime: string | null,
): SegmentDetailModel {
  const points = allForecastPoints(forecastData)
  const selectedMs = validTime ? Date.parse(validTime) : NaN
  const currentPoint =
    (Number.isFinite(selectedMs) ? points.find((point) => point.timeMs === selectedMs) : null) ??
    points.find((point) => !point.isAnalysis) ??
    points.at(-1) ??
    null
  const peakPoint = points.filter((point) => !point.isAnalysis).sort((left, right) => right.value - left.value)[0] ?? points.at(-1) ?? null

  return {
    segmentId,
    basinVersionId,
    riverNetworkVersionId,
    segment,
    geometryStatus: getM11SelectedSegmentGeometryBudgetStatus(segment.geom),
    forecastData,
    lineage,
    currentQ: currentPoint?.value ?? null,
    peakQ: peakPoint?.value ?? null,
    peakTime: peakPoint ? new Date(peakPoint.timeMs).toISOString() : null,
    unit: forecastData?.unit ?? 'm3/s',
  }
}

function stringValue(value: unknown) {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function recordValue(value: unknown) {
  return isRecord(value) ? value : null
}

function numberValue(value: unknown) {
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric : null
}

function findRestrictedReason(...values: unknown[]): string | null {
  for (const value of values) {
    const record = recordValue(value)
    if (!record) continue
    const direct = stringValue(record.restricted_reason)
    if (direct) return direct
    const metadata = recordValue(record.metadata)
    const metadataReason = metadata ? stringValue(metadata.restricted_reason) : null
    if (metadataReason) return metadataReason
    const lineageJson = recordValue(record.lineage_json)
    const lineageReason = lineageJson ? stringValue(lineageJson.restricted_reason) : null
    if (lineageReason) return lineageReason
    const source = recordValue(record.source)
    const sourceReason = source ? stringValue(source.restricted_reason) : null
    if (sourceReason) return sourceReason
  }
  return null
}

function stationForcingContract(segment: RiverSegment | null): Record<string, unknown> | null {
  const properties = recordValue(segment?.properties_json)
  if (!properties) return null
  return recordValue(properties.station_forcing) ?? recordValue(properties.stationForcing)
}

function formatPointLocation(value: unknown) {
  const point = recordValue(value)
  const coordinates = Array.isArray(point?.coordinates) ? point.coordinates : null
  const lon = coordinates ? numberValue(coordinates[0]) : null
  const lat = coordinates ? numberValue(coordinates[1]) : null
  if (lon === null || lat === null) return null
  return `${lon.toFixed(4)}, ${lat.toFixed(4)}`
}

function normalizeStationForcingSeries(series: unknown): StationForcingSeriesRow[] {
  const payload = recordValue(series) as TimeseriesResponse | null
  const variables = recordValue(payload?.variables)
  if (!variables) return []
  const unit = stringValue(payload?.unit) ?? ''
  return (['PRCP', 'TEMP'] as const)
    .map((variable) => {
      const rawPoints = Array.isArray(variables[variable]) ? variables[variable] : []
      const points = rawPoints
        .filter((point): point is (string | number)[] => Array.isArray(point) && point.length >= 2)
        .map((point) => ({ time: point[0], value: numberValue(point[1]) }))
        .filter((point): point is { time: string | number; value: number } => point.value !== null)
      return { variable, unit, points }
    })
    .filter((row) => row.points.length > 0)
}

function buildStationForcingViewModel(
  segment: RiverSegment | null,
  lineage: LineageResponse | null,
  lineageError: string | null,
): StationForcingViewModel {
  const contract = stationForcingContract(segment)
  const restrictedReason =
    findRestrictedReason(contract, contract?.metadata, contract?.source, ...(lineage?.forcing_versions ?? [])) ?? null
  if (restrictedReason) {
    return {
      status: 'restricted',
      restrictedReason,
      unavailableReason: null,
      station: null,
      series: [],
    }
  }

  const station = recordValue(contract?.station) as Partial<MetStation> | null
  const stationId = stringValue(station?.station_id) ?? stringValue(contract?.station_id)
  const series = normalizeStationForcingSeries(contract?.series ?? contract?.forcing_series)
  if (!stationId || series.length === 0) {
    return {
      status: 'unavailable',
      restrictedReason: null,
      unavailableReason: lineageError,
      station: null,
      series: [],
    }
  }

  const source =
    stringValue(contract?.source_id) ??
    stringValue(recordValue(contract?.source)?.source_id) ??
    stringValue(recordValue(contract?.metadata)?.source_id) ??
    stringValue(contract?.forcing_version_id) ??
    stringValue(recordValue(contract?.metadata)?.forcing_version_id)

  return {
    status: 'available',
    restrictedReason: null,
    unavailableReason: null,
    station: {
      id: stationId,
      name: stringValue(station?.station_name) ?? stringValue(contract?.station_name) ?? stringValue(contract?.name),
      role: stringValue(station?.station_role) ?? stringValue(contract?.station_role),
      source,
      location: formatPointLocation(station?.geom ?? contract?.location),
      elevationM: numberValue(station?.elevation_m ?? contract?.elevation_m),
    },
    series,
  }
}

async function fetchRiverSegment(segment: ForecastSegmentInfo) {
  const { data, error } = await client.GET('/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}', {
    params: {
      path: { basin_version_id: segment.basinVersionId, segment_id: segment.segmentId },
      query: { river_network_version_id: segment.riverNetworkVersionId },
    },
  })
  if (error) throw new Error(getApiErrorMessage(error, '获取河段详情失败'))
  return unwrapApiData<RiverSegment>(data, '获取河段详情失败')
}

export function SegmentDetailPage() {
  const { segmentId: pathSegmentId = '' } = useParams()
  const location = useLocation()
  const navigate = useNavigate()
  const routeState = useMemo(() => parseM11QueryState(location.search), [location.search])
  const canonicalSegmentId = routeState.segmentId ?? pathSegmentId
  const normalizedSearch = useMemo(() => serializeM11QueryState(routeState), [routeState])
  const forecastData = useForecastStore((state) => state.forecastData)
  const forecastLoading = useForecastStore((state) => state.loading)
  const forecastError = useForecastStore((state) => state.error)
  const selectSegment = useForecastStore((state) => state.selectSegment)
  const fetchForecast = useForecastStore((state) => state.fetchForecast)
  const setRequestContext = useForecastStore((state) => state.setRequestContext)
  const [segment, setSegment] = useState<RiverSegment | null>(null)
  const [segmentLoading, setSegmentLoading] = useState(false)
  const [segmentError, setSegmentError] = useState<string | null>(null)
  const [lineage, setLineage] = useState<LineageResponse | null>(null)
  const [lineageError, setLineageError] = useState<string | null>(null)
  const [visibleScenarios, setVisibleScenarios] = useState<Record<ScenarioToggle, boolean>>({
    analysis: true,
    gfs: true,
    ifs: true,
  })

  const scopedSegment = useMemo<ForecastSegmentInfo | null>(() => {
    if (!canonicalSegmentId || !routeState.basinVersionId || !routeState.riverNetworkVersionId) return null
    return {
      segmentId: canonicalSegmentId,
      basinVersionId: routeState.basinVersionId,
      riverNetworkVersionId: routeState.riverNetworkVersionId,
    }
  }, [canonicalSegmentId, routeState.basinVersionId, routeState.riverNetworkVersionId])

  useEffect(() => {
    if (!needsM11QueryReplacement(location.search)) return
    navigate({ pathname: location.pathname, search: normalizedSearch ? `?${normalizedSearch}` : '' }, { replace: true })
  }, [location.pathname, location.search, navigate, normalizedSearch])

  useEffect(() => {
    setRequestContext({
      source: routeState.source === 'best' ? null : routeState.source,
      issueTime: routeState.cycle,
    })
  }, [routeState.cycle, routeState.source, setRequestContext])

  useEffect(() => {
    if (!scopedSegment) {
      setSegment(null)
      setSegmentError(null)
      return
    }

    let cancelled = false
    setSegmentLoading(true)
    setSegmentError(null)
    setSegment(null)
    void fetchRiverSegment(scopedSegment)
      .then((payload) => {
        if (cancelled) return
        if (payload.river_network_version_id !== scopedSegment.riverNetworkVersionId) {
          throw new Error(
            `河段详情响应与请求河网版本不匹配：请求 ${scopedSegment.riverNetworkVersionId}，返回 ${payload.river_network_version_id || 'unknown'}。`,
          )
        }
        setSegment(payload)
        selectSegment(scopedSegment)
        void fetchForecast({
          includeAnalysis: true,
          source: routeState.source === 'best' ? null : routeState.source,
          issueTime: routeState.cycle,
        }).catch(() => undefined)
      })
      .catch((error) => {
        if (cancelled) return
        setSegmentError(getApiErrorMessage(error, `未找到河段 ${canonicalSegmentId}`))
      })
      .finally(() => {
        if (!cancelled) setSegmentLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [canonicalSegmentId, fetchForecast, routeState.cycle, routeState.source, scopedSegment, selectSegment])

  useEffect(() => {
    setLineage(null)
    setLineageError(
      segment ? '当前 segment detail URL 未携带 run_id，现有 lineage API 不能在无 run_id 情况下安全查询。' : null,
    )
  }, [segment])

  useEffect(() => {
    if (!forecastData || !routeState.validTime) return
    const validTimes = [...new Set(allForecastPoints(forecastData).map((point) => new Date(point.timeMs).toISOString()))]
    if (validTimes.length === 0 || validTimes.includes(routeState.validTime)) return
    const corrected = closestValidTime(routeState.validTime, validTimes)
    if (!corrected) return
    const nextSearch = serializeM11QueryState({ ...routeState, validTime: corrected, segmentId: canonicalSegmentId })
    navigate({ pathname: location.pathname, search: nextSearch ? `?${nextSearch}` : '' }, { replace: true })
  }, [canonicalSegmentId, forecastData, location.pathname, navigate, routeState])

  const model = useMemo(
    () =>
      scopedSegment && segment
        ? buildDetailModel(
            scopedSegment.segmentId,
            scopedSegment.basinVersionId ?? '',
            scopedSegment.riverNetworkVersionId,
            segment,
            forecastData,
            lineage,
            routeState.validTime,
          )
        : null,
    [forecastData, lineage, routeState.validTime, scopedSegment, segment],
  )
  const filteredForecast = useMemo(() => buildScenarioFilteredData(forecastData, visibleScenarios), [forecastData, visibleScenarios])
  const missingIdentity = !canonicalSegmentId
    ? '缺少 segmentId'
    : !routeState.basinVersionId
      ? '缺少 basinVersionId'
      : !routeState.riverNetworkVersionId
        ? '缺少 riverNetworkVersionId'
        : null
  const loading = segmentLoading || forecastLoading
  const routeSearch = serializeM11QueryState(routeState)
  const basinHref = routeState.basinVersionId
    ? m11QueryHref(`/forecast`, routeState, {
        basinVersionId: routeState.basinVersionId,
        riverNetworkVersionId: routeState.riverNetworkVersionId,
        segmentId: canonicalSegmentId,
      })
    : '/forecast'

  if (missingIdentity) {
    return (
      <FullPageState title={missingIdentity} description="河段详情需要 basinVersionId、riverNetworkVersionId 和 segmentId 才能发起限定版本的数据请求。" />
    )
  }

  if (segmentError && !segment) {
    return <FullPageState title={`未找到河段 ${canonicalSegmentId}`} description={segmentError} />
  }

  return (
    <div className="min-h-[calc(100vh-7rem)] space-y-4">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-xs font-medium uppercase tracking-wide text-muted">Segment forecast detail</div>
          <h1 className="mt-1 text-2xl font-semibold text-foreground">{model?.segmentId ?? canonicalSegmentId}</h1>
          <p className="mt-1 max-w-4xl text-sm text-muted">
            {routeState.source.toUpperCase()} · cycle {formatDateTime(routeState.cycle)} · valid {formatDateTime(routeState.validTime)} · basin{' '}
            <span className="font-mono">{routeState.basinVersionId}</span> · river network{' '}
            <span className="font-mono">{routeState.riverNetworkVersionId}</span>
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Link className="inline-flex h-9 items-center rounded border border-primary-600 px-3 text-sm font-medium text-primary-600" to={basinHref}>
            返回水文预报
          </Link>
          <Button
            type="button"
            variant="outline"
            className="h-9 gap-2"
            onClick={() => scopedSegment && fetchForecast({ includeAnalysis: true, useSelectedScenarios: false }).catch(() => undefined)}
            disabled={!scopedSegment}
          >
            <RefreshCw className="size-4" />
            刷新
          </Button>
        </div>
      </header>

      {loading ? (
        <div className="rounded-md border border-border bg-panel px-4 py-3 text-sm text-muted" role="status">
          河段详情加载中
        </div>
      ) : null}

      {forecastError ? (
        <div className="rounded-md border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger" role="alert">
          {forecastError}
        </div>
      ) : null}

      <section className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_120px]">
        <KpiStrip model={model} />
        <LocationThumbnail geometryStatus={model?.geometryStatus ?? null} />
      </section>

      <div className="grid gap-4 xl:grid-cols-[280px_minmax(0,1fr)_320px]">
        <aside className="space-y-4">
          <StationForcingPanel segment={segment} lineage={lineage} lineageError={lineageError} />
          <IdentityPanel model={model} search={routeSearch} />
        </aside>

        <main className="min-w-0 space-y-4">
          <section className="rounded-md border border-border bg-panel p-4" aria-label="多源预报曲线">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="text-base font-semibold text-foreground">多源流量曲线</h2>
                <p className="mt-1 text-xs text-muted">起报时间分割 analysis 与 forecast；IFS 短时效按可用 lead 标注。</p>
              </div>
              <div className="flex rounded-md border border-border p-0.5">
                {SCENARIO_TOGGLES.map((item) => (
                  <Button
                    key={item.key}
                    type="button"
                    size="sm"
                    variant={visibleScenarios[item.key] ? 'default' : 'ghost'}
                    className="h-8 px-3 text-xs"
                    aria-pressed={visibleScenarios[item.key]}
                    onClick={() =>
                      setVisibleScenarios((current) => ({
                        ...current,
                        [item.key]: Object.values(current).filter(Boolean).length === 1 && current[item.key] ? true : !current[item.key],
                      }))
                    }
                  >
                    {item.label}
                  </Button>
                ))}
              </div>
            </div>
            <ForecastChart data={filteredForecast} segmentName={canonicalSegmentId} />
          </section>
          <BottomTimeline data={forecastData} selectedValidTime={routeState.validTime} />
        </main>

        <aside className="space-y-4">
          <ThresholdPanel data={forecastData} peakQ={model?.peakQ ?? null} unit={model?.unit ?? 'm3/s'} />
          <FrequencyPanel data={forecastData} peakQ={model?.peakQ ?? null} />
          <WeatherPanel />
        </aside>
      </div>
    </div>
  )
}

function FullPageState({ title, description }: { title: string; description: string }) {
  return (
    <div className="grid min-h-[calc(100vh-7rem)] place-items-center rounded-lg border border-border bg-panel p-6 text-center">
      <div>
        <h1 className="text-lg font-semibold text-foreground">{title}</h1>
        <p className="mt-2 max-w-xl text-sm text-muted">{description}</p>
      </div>
    </div>
  )
}

function KpiStrip({ model }: { model: SegmentDetailModel | null }) {
  const kpis = [
    { label: '当前 Q', value: formatMetric(model?.currentQ, model?.unit ?? 'm3/s') },
    { label: '预报峰值', value: formatMetric(model?.peakQ, model?.unit ?? 'm3/s'), meta: formatDateTime(model?.peakTime) },
    { label: '水位变化', value: '暂无水位变化' },
    { label: '河段长度', value: formatMetric(finiteNumber(model?.segment.length_m), 'm') },
  ]
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {kpis.map((item) => (
        <div key={item.label} className="rounded-md border border-border bg-panel p-4">
          <div className="text-xs text-muted">{item.label}</div>
          <div className="mt-1 text-xl font-semibold text-foreground">{item.value}</div>
          {item.meta ? <div className="mt-1 text-xs text-muted">{item.meta}</div> : null}
        </div>
      ))}
    </div>
  )
}

function LocationThumbnail({ geometryStatus }: { geometryStatus: M11SelectedSegmentGeometryBudgetStatus | null }) {
  const geometry = geometryStatus?.sanitizedGeometry
  if (!geometryStatus || !geometryStatus.ok || !geometry) {
    const overBudget = geometryStatus?.reason?.includes('budget')
    return (
      <div
        className="grid h-[90px] w-[120px] place-items-center rounded-md border border-dashed border-border bg-panel px-2 text-center text-[11px] text-muted"
        aria-label="位置缩略图"
      >
        {overBudget ? '河段几何超出缩略图预算' : '位置缩略图不可用'}
      </div>
    )
  }

  const coords = geometry.coordinates
  const xs = coords.map((point) => point[0])
  const ys = coords.map((point) => point[1])
  const minX = Math.min(...xs)
  const maxX = Math.max(...xs)
  const minY = Math.min(...ys)
  const maxY = Math.max(...ys)
  const spanX = maxX - minX || 1
  const spanY = maxY - minY || 1
  const path = coords
    .map((point, index) => {
      const x = 12 + ((point[0] - minX) / spanX) * 96
      const y = 78 - ((point[1] - minY) / spanY) * 66
      return `${index === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${y.toFixed(1)}`
    })
    .join(' ')

  return (
    <svg width="120" height="90" viewBox="0 0 120 90" className="h-[90px] w-[120px] rounded-md border border-border bg-panel" role="img" aria-label="位置缩略图">
      <rect x="8" y="8" width="104" height="74" rx="4" fill="#f8fafc" stroke="#d1d5db" />
      <path d={path} fill="none" stroke="#1d4ed8" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="12" cy="78" r="2.5" fill="#f97316" />
    </svg>
  )
}

function IdentityPanel({ model, search }: { model: SegmentDetailModel | null; search: string }) {
  return (
    <section className="rounded-md border border-border bg-panel p-4" aria-label="河段身份">
      <h2 className="flex items-center gap-2 text-base font-semibold text-foreground">
        <GitBranch className="size-4 text-primary-600" />
        身份与追溯
      </h2>
      <dl className="mt-3 grid grid-cols-[6rem_minmax(0,1fr)] gap-x-3 gap-y-2 text-xs">
        <dt className="text-muted">segment</dt>
        <dd className="truncate font-mono text-foreground">{model?.segmentId ?? '-'}</dd>
        <dt className="text-muted">basin</dt>
        <dd className="truncate font-mono text-foreground">{model?.basinVersionId ?? '-'}</dd>
        <dt className="text-muted">network</dt>
        <dd className="truncate font-mono text-foreground">{model?.riverNetworkVersionId ?? '-'}</dd>
        <dt className="text-muted">URL state</dt>
        <dd className="break-all font-mono text-foreground">{search || '-'}</dd>
      </dl>
    </section>
  )
}

function StationForcingPanel({
  segment,
  lineage,
  lineageError,
}: {
  segment: RiverSegment | null
  lineage: LineageResponse | null
  lineageError: string | null
}) {
  const viewModel = buildStationForcingViewModel(segment, lineage, lineageError)

  return (
    <section className="rounded-md border border-border bg-panel p-4" aria-label="站点与强迫数据">
      <h2 className="flex items-center gap-2 text-base font-semibold text-foreground">
        <MapPinned className="size-4 text-primary-600" />
        站点与强迫
      </h2>
      {viewModel.status === 'restricted' ? (
        <div className="mt-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
          站点或强迫数据受限：{viewModel.restrictedReason}
        </div>
      ) : viewModel.status === 'available' && viewModel.station ? (
        <div className="mt-3 space-y-3">
          <dl className="grid grid-cols-[5rem_minmax(0,1fr)] gap-x-3 gap-y-2 text-xs">
            <dt className="text-muted">station</dt>
            <dd className="truncate font-mono text-foreground">{viewModel.station.id}</dd>
            <dt className="text-muted">name</dt>
            <dd className="truncate text-foreground">{viewModel.station.name ?? '-'}</dd>
            <dt className="text-muted">location</dt>
            <dd className="font-mono text-foreground">{viewModel.station.location ?? '-'}</dd>
            <dt className="text-muted">source</dt>
            <dd className="truncate font-mono text-foreground">{viewModel.station.source ?? '-'}</dd>
            <dt className="text-muted">role</dt>
            <dd className="truncate text-foreground">{viewModel.station.role ?? '-'}</dd>
            <dt className="text-muted">elevation</dt>
            <dd className="text-foreground">{formatMetric(viewModel.station.elevationM, 'm')}</dd>
          </dl>
          <div className="space-y-2" role="list" aria-label="强迫序列">
            {viewModel.series.map((row) => (
              <ForcingSeriesRow key={row.variable} row={row} />
            ))}
          </div>
        </div>
      ) : viewModel.unavailableReason ? (
        <div className="mt-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
          站点与强迫数据暂不可用：{viewModel.unavailableReason}
        </div>
      ) : (
        <div className="mt-3 rounded border border-dashed border-border p-3 text-sm text-muted">
          站点与强迫数据暂不可用。当前前端合同没有 station_id、PRCP 或 TEMP 强迫序列，未渲染合成站点。
        </div>
      )}
    </section>
  )
}

function ForcingSeriesRow({ row }: { row: StationForcingSeriesRow }) {
  const first = row.points[0]
  const last = row.points.at(-1) ?? first
  const maxValue = Math.max(...row.points.map((point) => point.value), 1)
  const polyline = row.points
    .map((point, index) => {
      const x = row.points.length === 1 ? 48 : 6 + (index / (row.points.length - 1)) * 84
      const y = 30 - (point.value / maxValue) * 24
      return `${x.toFixed(1)},${Math.max(4, Math.min(30, y)).toFixed(1)}`
    })
    .join(' ')

  return (
    <div className="rounded border border-border px-3 py-2" role="listitem" aria-label={`强迫序列 ${row.variable}`} data-testid="station-forcing-series-row">
      <div className="flex items-center justify-between gap-3 text-sm">
        <span className="font-medium text-foreground">{row.variable}</span>
        <span className="text-xs text-muted">
          {row.points.length} points · latest {formatMetric(last.value, row.unit)}
        </span>
      </div>
      <svg className="mt-2 h-9 w-full" viewBox="0 0 96 36" role="img" aria-label={`${row.variable} chart`}>
        <polyline points={polyline} fill="none" stroke={row.variable === 'PRCP' ? '#2563eb' : '#f97316'} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <div className="mt-1 flex justify-between gap-2 text-[11px] text-muted">
        <span>{formatDateTime(first.time)}</span>
        <span>{formatDateTime(last.time)}</span>
      </div>
    </div>
  )
}

function ThresholdPanel({ data, peakQ, unit }: { data: ForecastData | null; peakQ: number | null; unit: string }) {
  return (
    <section className="rounded-md border border-border bg-panel p-4" aria-label="洪水阈值">
      <h2 className="flex items-center gap-2 text-base font-semibold text-foreground">
        <Waves className="size-4 text-primary-600" />
        阈值覆盖
      </h2>
      <div className="mt-3 space-y-2 text-sm">
        {THRESHOLD_KEYS.map((key) => {
          const value = finiteNumber(data?.frequencyThresholds?.[key])
          const exceeded = value !== null && peakQ !== null && peakQ >= value
          return (
            <div key={key} className="flex items-center justify-between gap-3 rounded border border-border px-3 py-2">
              <span className={cn('font-medium', exceeded ? 'text-danger' : 'text-foreground')}>{key}</span>
              <span className="text-muted">{value === null ? '不可用' : formatMetric(value, unit)}</span>
            </div>
          )
        })}
      </div>
    </section>
  )
}

function FrequencyPanel({ data, peakQ }: { data: ForecastData | null; peakQ: number | null }) {
  const available = THRESHOLD_KEYS.map((key) => finiteNumber(data?.frequencyThresholds?.[key])).filter((value) => value !== null)
  return (
    <section className="rounded-md border border-border bg-panel p-4" aria-label="频率曲线">
      <h2 className="flex items-center gap-2 text-base font-semibold text-foreground">
        <Activity className="size-4 text-primary-600" />
        频率上下文
      </h2>
      {available.length > 0 ? (
        <div className="mt-3 text-sm text-muted">
          可用阈值 {available.length}/6；峰值 {formatMetric(peakQ, data?.unit ?? 'm3/s')}。频率曲线参数合同暂不可用，当前仅展示离散 Q 阈值。
        </div>
      ) : (
        <div className="mt-3 rounded border border-dashed border-border p-3 text-sm text-muted">
          频率阈值不可用，无法绘制频率曲线或峰值重现期标记。
        </div>
      )}
    </section>
  )
}

function WeatherPanel() {
  const variables = [
    { key: 'PRCP', icon: CloudRain },
    { key: 'TEMP', icon: ThermometerSun },
    { key: 'RH', icon: CloudRain },
    { key: 'wind', icon: CloudRain },
    { key: 'Press', icon: CloudRain },
  ]
  return (
    <section className="rounded-md border border-border bg-panel p-4" aria-label="天气驱动">
      <h2 className="text-base font-semibold text-foreground">天气驱动</h2>
      <div className="mt-3 space-y-2">
        {variables.map(({ key, icon: Icon }) => (
          <div key={key} className="flex items-center justify-between gap-3 rounded border border-border px-3 py-2 text-sm">
            <span className="flex items-center gap-2 text-foreground">
              <Icon className="size-4 text-muted" />
              {key}
            </span>
            <span className="text-muted">不可用</span>
          </div>
        ))}
      </div>
      <p className="mt-3 text-xs text-muted">当前 segment-detail 复用现有水文合同；天气变量合同缺失时只显示部分状态，不补造数值。</p>
    </section>
  )
}

function BottomTimeline({ data, selectedValidTime }: { data: ForecastData | null; selectedValidTime: string | null }) {
  const points = allForecastPoints(data)
  if (points.length === 0) {
    return (
      <section className="rounded-md border border-border bg-panel p-4 text-sm text-muted" aria-label="底部时间线">
        暂无有效流量时间线
      </section>
    )
  }
  const first = points[0]
  const last = points.at(-1) ?? first
  return (
    <section className="rounded-md border border-border bg-panel p-4" aria-label="底部时间线">
      <div className="flex items-center justify-between text-xs text-muted">
        <span>{formatDateTime(first.timeMs)}</span>
        <span>当前 validTime {formatDateTime(selectedValidTime)}</span>
        <span>{formatDateTime(last.timeMs)}</span>
      </div>
      <div className="mt-3 h-2 rounded bg-border">
        <div className="h-2 w-full rounded bg-primary-600" />
      </div>
    </section>
  )
}

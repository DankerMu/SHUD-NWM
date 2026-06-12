import type { components } from '@/api/types'
import { ALERT_LEVEL_META } from '@/components/flood/alertLevels'
import { m11QueryHref } from '@/lib/m11/queryState'
import type { M11Layer, M11QueryState, M11Source } from '@/lib/m11/queryState'
import { m11VisualTokens } from '@/lib/m11/visualTokens'

export type ApiBasin = components['schemas']['Basin']
export type ApiBasinVersion = components['schemas']['BasinVersion']
export type ApiModelInstance = components['schemas']['ModelInstance']
export type ApiRiverSegment = components['schemas']['RiverSegment']
export type ApiRiverFeatureCollection = components['schemas']['RiverSegmentFeatureCollection']
export type ApiRiverFeature = components['schemas']['RiverSegmentFeature']
export type ApiHydroRun = components['schemas']['HydroRun']
export type ApiHydroRunPage = components['schemas']['HydroRunPage']
export type ApiFloodAlertSummary = components['schemas']['FloodAlertSummary']
export type ApiFloodAlertRanking = components['schemas']['FloodAlertRanking']
export type ApiFloodAlertRankingItem = components['schemas']['FloodAlertRankingItem']
export type ApiFloodAlertTimeline = components['schemas']['FloodAlertTimeline']
export type ApiFloodAlertSegmentList = components['schemas']['FloodAlertSegmentList']
export type ApiLayer = components['schemas']['Layer']
export type ApiPipelineStatus = components['schemas']['PipelineStatus']
export type ApiQueueDepth = components['schemas']['QueueDepth']
export type ApiLineageResponse = components['schemas']['LineageResponse']
export type ApiForecastPayload = components['schemas']['RiverSeriesResponse'] | components['schemas']['SplicedForecastResponse']

export type M11WarningLevel =
  | 'normal'
  | 'elevated'
  | 'watch'
  | 'warning'
  | 'high_risk'
  | 'severe'
  | 'extreme'
  | 'unavailable'

export type M11QualityFlag = 'ok' | 'degraded' | 'unavailable' | 'failed' | 'unknown'
export type M11ResolvedSource = 'GFS' | 'IFS' | 'GFS+IFS' | 'Unknown'

export interface M11Bbox {
  minLon: number
  minLat: number
  maxLon: number
  maxLat: number
}

export const m11BasinGeometryBudget = {
  maxPolygons: 256,
  maxRings: 1024,
  maxVertices: 50_000,
  maxCoordinateDimensions: 3,
  maxSerializedBytes: 1_000_000,
} as const

export const m11SelectedSegmentGeometryBudget = {
  maxCoordinates: 10_000,
  maxCoordinateDimensions: 3,
  maxSerializedBytes: 250_000,
} as const

// 按真实流域校准（qhh 全河网 1839 段 / ~77k 坐标 / ~1.8MB 序列化）：预算需容纳单流域
// 全河段渲染，同时仍拦截病态超大 payload。MapLibre 对该量级 GeoJSON 渲染无压力。
export const m11BasinRiverCollectionBudget = {
  maxFeatures: 5_000,
  maxCoordinates: 250_000,
  maxSerializedBytes: 6_000_000,
} as const

const m11RiverDischargeLegend: LayerLegendEntry[] = [
  { label: '<500 m3/s', color: '#E3F2FD', max: 500 },
  { label: '500-1000 m3/s', color: '#90CAF9', min: 500, max: 1000 },
  { label: '1000-5000 m3/s', color: '#42A5F5', min: 1000, max: 5000 },
  { label: '5000-10000 m3/s', color: '#1E88E5', min: 5000, max: 10000 },
  { label: '10000-50000 m3/s', color: '#FF9800', min: 10000, max: 50000 },
  { label: '>50000 m3/s', color: '#F44336', min: 50000 },
  { label: '无径流数据', color: m11DischargeColor(null) },
]

const m11RiverWaterLevelLegend: LayerLegendEntry[] = [
  { label: '<0.5 m', color: '#E0F7FA', max: 0.5 },
  { label: '0.5-1 m', color: '#80DEEA', min: 0.5, max: 1 },
  { label: '1-2 m', color: '#26C6DA', min: 1, max: 2 },
  { label: '2-4 m', color: '#00897B', min: 2, max: 4 },
  { label: '4-8 m', color: '#FDD835', min: 4, max: 8 },
  { label: '>8 m', color: '#D81B60', min: 8 },
  { label: '无水位数据', color: m11WaterLevelColor(null) },
]

const m11RiverReturnPeriodLegend: LayerLegendEntry[] = [
  { label: '正常 T<2', color: ALERT_LEVEL_META.normal.color, min: 0, max: 2 },
  { label: '偏高 2-5', color: ALERT_LEVEL_META.elevated.color, min: 2, max: 5 },
  { label: '关注 5-10', color: ALERT_LEVEL_META.watch.color, min: 5, max: 10 },
  { label: '警戒 10-20', color: ALERT_LEVEL_META.warning.color, min: 10, max: 20 },
  { label: '高风险 20-50', color: ALERT_LEVEL_META.high_risk.color, min: 20, max: 50 },
  { label: '严重 50-100', color: ALERT_LEVEL_META.severe.color, min: 50, max: 100 },
  { label: '极端 >=100', color: ALERT_LEVEL_META.extreme.color, min: 100 },
  { label: '无重现期数据', color: m11ReturnPeriodColor(null) },
]

const m11RiverWarningLevelLegend: LayerLegendEntry[] = [
  { label: '正常', color: ALERT_LEVEL_META.normal.color },
  { label: '偏高', color: ALERT_LEVEL_META.elevated.color },
  { label: '关注', color: ALERT_LEVEL_META.watch.color },
  { label: '警戒', color: ALERT_LEVEL_META.warning.color },
  { label: '高风险', color: ALERT_LEVEL_META.high_risk.color },
  { label: '严重', color: ALERT_LEVEL_META.severe.color },
  { label: '极端', color: ALERT_LEVEL_META.extreme.color },
  { label: '预警不可用', color: m11WarningLevelColor('unavailable') },
]

export interface M11BasinGeometryBudgetStatus {
  ok: boolean
  reason: string | null
  bbox: M11Bbox | null
  polygonCount: number
  ringCount: number
  vertexCount: number
  serializedBytes: number
  sanitizedGeometry: components['schemas']['GeoJsonMultiPolygon'] | null
}

export interface M11SelectedSegmentGeometryBudgetStatus {
  ok: boolean
  reason: string | null
  coordinateCount: number
  serializedBytes: number
  sanitizedGeometry: components['schemas']['GeoJsonLineString'] | null
}

export interface BasinVersionOption {
  basinVersionId: string
  versionLabel: string
  active: boolean
  validFrom: string | null
  validTo: string | null
  sourceUri: string | null
  boundary: components['schemas']['GeoJsonMultiPolygon'] | null
  bbox: M11Bbox | null
  unavailableReason: string | null
}

export interface FreshnessMetadata {
  updatedAt: string | null
  cycleTime: string | null
  validTime: string | null
  runId: string | null
  basinVersionId: string | null
  riverNetworkVersionId: string | null
  source: M11ResolvedSource | null
  isStale: boolean
  staleAfterHours: number
  unavailableReason: string | null
}

export interface SourceScenarioSelectionState {
  requestedSource: M11Source
  resolvedSource: M11ResolvedSource
  scenarioIds: string[]
  cycleTime: string | null
  validTime: string | null
  comparisonAvailable: boolean
  provenanceLabel: string
  unavailableReason: string | null
}

export interface OverviewBasin {
  basinId: string
  displayName: string
  basinGroup: string | null
  parentBasinId: string | null
  level: number
  boundary: components['schemas']['GeoJsonMultiPolygon'] | null
  bbox: M11Bbox | null
  areaKm2: number | null
  riverCount: number | null
  activeModelCount: number
  latestForecastTime: string | null
  warningCounts: Record<M11WarningLevel, number>
  basinVersions: BasinVersionOption[]
  selectedBasinVersionId: string | null
  unavailableReason: string | null
  qualityNote: string | null
}

export interface OverviewSummary {
  completedCyclesToday: number | null
  runningJobs: number | null
  warningSegmentCount: number | null
  latestUpdate: string | null
  totalBasins: number
  totalSegments: number | null
  sourceSelection: SourceScenarioSelectionState
  freshness: FreshnessMetadata
  qualityNotes: string[]
  partialErrors: string[]
}

export interface LayerLegendEntry {
  label: string
  color: string
  min?: number | null
  max?: number | null
}

export interface LayerState {
  layerId: M11Layer | string
  displayName: string
  group: 'hydrology' | 'meteorology' | 'base' | 'unknown'
  available: boolean
  metadata: components['schemas']['Layer']['metadata'] | null
  validTimes: string[]
  currentValidTime: string | null
  validTimeSource: 'api' | 'derived' | 'none'
  disabledReason: string | null
  freshness: FreshnessMetadata
  legend: LayerLegendEntry[]
}

export interface BasinDetail {
  basinId: string
  displayName: string
  basinGroup: string | null
  selectedBasinVersionId: string | null
  basinVersions: BasinVersionOption[]
  boundary: components['schemas']['GeoJsonMultiPolygon'] | null
  bbox: M11Bbox | null
  segmentCount: number | null
  warningDistribution: Record<M11WarningLevel, number>
  activeModelCount: number
  latestRun: FreshnessMetadata
  sourceSelection: SourceScenarioSelectionState
  unavailableReason: string | null
  partialErrors: string[]
}

export interface BasinSegmentRow {
  riverSegmentId: string
  riverNetworkVersionId: string
  segmentId: string
  displayName: string
  basinVersionId: string
  streamOrder: number | null
  lengthM: number | null
  currentQ: number | null
  qUnit: string
  returnPeriod: number | null
  warningLevel: M11WarningLevel
  qualityFlag: M11QualityFlag
  qualityNote: string | null
  source: M11ResolvedSource | null
  cycleTime: string | null
  validTime: string | null
  hasGeometry: boolean
  geometry: components['schemas']['GeoJsonLineString'] | null
  unavailableReason: string | null
}

export interface TrendPoint {
  validTime: string
  value: number | null
  source: M11ResolvedSource | null
  scenarioId: string
  role: string | null
  isAnalysis: boolean
}

export interface SelectedSegmentDetail {
  basinId: string | null
  basinName: string | null
  basinVersionId: string
  riverSegmentId: string
  segmentId: string
  displayName: string
  modelId: string | null
  riverNetworkVersionId: string | null
  currentQ: number | null
  qUnit: string
  returnPeriod: number | null
  warningLevel: M11WarningLevel
  qualityFlag: M11QualityFlag
  qualityNote: string | null
  sourceSelection: SourceScenarioSelectionState
  trendPoints: TrendPoint[]
  comparisonAvailable: boolean
  lineageStatus: 'available' | 'unavailable' | 'failed'
  lineageUnavailableReason: string | null
  handoffUrl: string
  geometry: components['schemas']['GeoJsonLineString'] | null
  freshness: FreshnessMetadata
  unavailableReason: string | null
}

export type AggregationEndpointDecisionReason =
  | 'reuse-existing'
  | 'too-many-initial-requests'
  | 'per-basin-n-plus-one'
  | 'missing-required-field'

export interface AggregationEndpointDecisionInput {
  initialRequestCount: number
  createsPerBasinNPlusOne: boolean
  missingRequiredFields: string[]
}

export interface AggregationEndpointDecision {
  needsAggregationEndpoint: boolean
  reason: AggregationEndpointDecisionReason
  evidence: string
}

export const emptyWarningCounts: Record<M11WarningLevel, number> = {
  normal: 0,
  elevated: 0,
  watch: 0,
  warning: 0,
  high_risk: 0,
  severe: 0,
  extreme: 0,
  unavailable: 0,
}

const layerLabels: Record<M11Layer, string> = {
  discharge: 'Discharge',
  'water-level': 'Water level',
  'flood-return-period': 'Flood return period',
  'warning-level': 'Warning level',
  'met-stations': 'Meteorological stations',
  'met-raster': 'Meteorological grid',
}

export function createSourceScenarioSelection(
  query: Pick<M11QueryState, 'source' | 'cycle' | 'validTime'>,
  availableSources: M11ResolvedSource[] = [],
): SourceScenarioSelectionState {
  const source = normalizeRequestedSource(query.source)
  const resolvedSource = resolveSelectedSource(source, availableSources)
  const scenarioIds =
    source === 'compare'
      ? ['forecast_gfs_deterministic', 'forecast_ifs_deterministic']
      : source === 'ifs'
        ? ['forecast_ifs_deterministic']
        : source === 'best'
          ? scenarioIdsForResolvedSource(resolvedSource)
          : ['forecast_gfs_deterministic']
  const comparisonAvailable = availableSources.includes('GFS') && availableSources.includes('IFS')
  const unavailableReason =
    source === 'compare' && !comparisonAvailable
      ? 'Comparison requires both GFS and IFS series.'
      : resolvedSource === 'Unknown'
        ? 'Requested source is not available in current payload.'
        : null

  return {
    requestedSource: source,
    resolvedSource,
    scenarioIds,
    cycleTime: query.cycle,
    validTime: query.validTime,
    comparisonAvailable,
    provenanceLabel: buildProvenanceLabel(source, resolvedSource, query.cycle, query.validTime),
    unavailableReason,
  }
}

export function createFreshnessMetadata(input: Partial<FreshnessMetadata> = {}): FreshnessMetadata {
  const staleAfterHours = input.staleAfterHours ?? 6
  const reference = input.validTime ?? input.updatedAt ?? input.cycleTime
  return {
    updatedAt: normalizeIsoString(input.updatedAt),
    cycleTime: normalizeIsoString(input.cycleTime),
    validTime: normalizeIsoString(input.validTime),
    runId: normalizeString(input.runId),
    basinVersionId: normalizeString(input.basinVersionId),
    riverNetworkVersionId: normalizeString(input.riverNetworkVersionId),
    source: input.source ?? null,
    isStale: isStale(reference, staleAfterHours),
    staleAfterHours,
    unavailableReason: normalizeString(input.unavailableReason),
  }
}

export function createEmptyOverviewSummary(query: Pick<M11QueryState, 'source' | 'cycle' | 'validTime'>): OverviewSummary {
  const sourceSelection = createSourceScenarioSelection(query)
  return {
    completedCyclesToday: null,
    runningJobs: null,
    warningSegmentCount: null,
    latestUpdate: null,
    totalBasins: 0,
    totalSegments: null,
    sourceSelection,
    freshness: createFreshnessMetadata({ source: sourceSelection.resolvedSource, unavailableReason: 'No overview data loaded.' }),
    qualityNotes: ['No overview data loaded.'],
    partialErrors: [],
  }
}

export function createEmptyBasinDetail(
  basinId: string,
  query: Pick<M11QueryState, 'source' | 'cycle' | 'validTime'>,
): BasinDetail {
  const sourceSelection = createSourceScenarioSelection(query)
  return {
    basinId,
    displayName: basinId,
    basinGroup: null,
    selectedBasinVersionId: null,
    basinVersions: [],
    boundary: null,
    bbox: null,
    segmentCount: null,
    warningDistribution: { ...emptyWarningCounts },
    activeModelCount: 0,
    latestRun: createFreshnessMetadata({ source: sourceSelection.resolvedSource, unavailableReason: 'No basin data loaded.' }),
    sourceSelection,
    unavailableReason: 'No basin data loaded.',
    partialErrors: [],
  }
}

export function decideAggregationEndpoint(input: AggregationEndpointDecisionInput): AggregationEndpointDecision {
  if (input.missingRequiredFields.length > 0) {
    return {
      needsAggregationEndpoint: true,
      reason: 'missing-required-field',
      evidence: `Current APIs cannot provide: ${input.missingRequiredFields.join(', ')}.`,
    }
  }

  if (input.createsPerBasinNPlusOne) {
    return {
      needsAggregationEndpoint: true,
      reason: 'per-basin-n-plus-one',
      evidence: 'Existing composition creates per-basin N+1 calls for required overview fields.',
    }
  }

  if (input.initialRequestCount > 8) {
    return {
      needsAggregationEndpoint: true,
      reason: 'too-many-initial-requests',
      evidence: `Existing composition requires ${input.initialRequestCount} initial requests, exceeding the threshold of 8.`,
    }
  }

  return {
    needsAggregationEndpoint: false,
    reason: 'reuse-existing',
    evidence: `Existing composition requires ${input.initialRequestCount} initial requests and stays within the threshold of 8.`,
  }
}

export function normalizeOverviewBasins(input: {
  basins: ApiBasin[]
  versionsByBasinId?: Record<string, ApiBasinVersion[] | undefined>
  basinVersionUnavailableReason?: string | null
  models?: ApiModelInstance[]
  runs?: ApiHydroRun[]
  rankingItems?: ApiFloodAlertRankingItem[]
}): OverviewBasin[] {
  const models = input.models ?? []
  const runs = input.runs ?? []
  const rankingItems = input.rankingItems ?? []

  return input.basins.map((basin) => {
    const versions = input.versionsByBasinId?.[basin.basin_id] ?? []
    const versionOptions = normalizeBasinVersions(versions)
    const versionIds = new Set(versionOptions.map((version) => version.basinVersionId))
    const basinModels = models.filter((model) => model.basin_id === basin.basin_id || versionIds.has(model.basin_version_id))
    const basinRuns = runs.filter((run) => versionIds.has(run.basin_version_id))
    const basinRankingItems = rankingItems.filter((item) => versionIds.has(item.basin_version_id))
    const warningCounts = warningCountsFromRanking(basinRankingItems)
    const selectedVersion = versionOptions.find((version) => version.active) ?? versionOptions[0] ?? null

    return {
      basinId: basin.basin_id,
      displayName: normalizeString(basin.basin_name) ?? basin.basin_id,
      basinGroup: normalizeString(basin.basin_group),
      parentBasinId: null,
      level: basin.basin_group ? 2 : 1,
      boundary: selectedVersion?.boundary ?? null,
      bbox: selectedVersion?.bbox ?? null,
      areaKm2: selectedVersion?.boundary ? polygonAreaKm2(selectedVersion.boundary) : null,
      riverCount: sumNullable(basinModels.map((model) => numberOrNull(model.segment_count))),
      activeModelCount: basinModels.filter((model) => model.active_flag).length,
      latestForecastTime: latestIso(basinRuns.map((run) => run.cycle_time ?? run.updated_at ?? run.created_at)),
      warningCounts,
      basinVersions: versionOptions,
      selectedBasinVersionId: selectedVersion?.basinVersionId ?? null,
      unavailableReason:
        versionOptions.length === 0 ? input.basinVersionUnavailableReason ?? 'No published basin version is available.' : null,
      qualityNote: versionOptions.some((version) => version.unavailableReason) ? 'One or more basin versions have missing geometry.' : null,
    }
  })
}

export function normalizeOverviewSummary(input: {
  query: Pick<M11QueryState, 'source' | 'cycle' | 'validTime'>
  basins?: OverviewBasin[]
  floodSummary?: ApiFloodAlertSummary | null
  ranking?: ApiFloodAlertRanking | null
  pipeline?: ApiPipelineStatus | null
  queue?: ApiQueueDepth | null
  latestRun?: ApiHydroRun | null
  runs?: ApiHydroRun[]
  partialErrors?: string[]
}): OverviewSummary {
  const availableSources = sourcesFromRuns(input.runs ?? (input.latestRun ? [input.latestRun] : []))
  const selectionQuery =
    input.query.source === 'best'
      ? { ...input.query, cycle: input.latestRun?.cycle_time ?? input.pipeline?.cycle_time ?? input.query.cycle }
      : input.query
  const sourceSelection = createSourceScenarioSelection(selectionQuery, availableSources)
  const levels = input.floodSummary?.levels ?? []
  const warningSegmentCount = levels
    .filter((level) => isSuperWarningLevel(normalizeWarningLevel(level.level)))
    .reduce((total, level) => total + numberOrZero(level.count), 0)
  const completedCyclesToday = input.pipeline?.job_counts.succeeded ?? null
  const runningJobs = input.queue?.running ?? input.pipeline?.job_counts.running ?? null
  const latestUpdate = latestIso([
    input.pipeline?.updated_at ?? null,
    input.latestRun?.updated_at ?? null,
    input.latestRun?.cycle_time ?? null,
    input.query.validTime,
  ])
  const qualityNotes = [
    normalizeString(input.floodSummary?.quality_note),
    input.floodSummary && input.floodSummary.unavailable_count > 0
      ? `${input.floodSummary.unavailable_count} flood-alert segments unavailable.`
      : null,
  ].filter((note): note is string => Boolean(note))

  return {
    completedCyclesToday,
    runningJobs,
    warningSegmentCount: input.floodSummary ? warningSegmentCount : null,
    latestUpdate,
    totalBasins: input.basins?.length ?? 0,
    totalSegments: input.floodSummary?.total_segments ?? null,
    sourceSelection,
    freshness: createFreshnessMetadata({
      updatedAt: latestUpdate,
      cycleTime: input.latestRun?.cycle_time ?? input.pipeline?.cycle_time ?? input.query.cycle,
      validTime: input.query.validTime,
      runId: input.floodSummary?.run_id ?? input.latestRun?.run_id ?? null,
      basinVersionId: input.latestRun?.basin_version_id ?? null,
      riverNetworkVersionId: input.latestRun?.river_network_version_id ?? null,
      source: sourceSelection.resolvedSource,
      unavailableReason: latestUpdate ? null : 'No freshness metadata is available.',
    }),
    qualityNotes,
    partialErrors: input.partialErrors ?? [],
  }
}

export function normalizeLayerStates(input: {
  query: Pick<M11QueryState, 'layer' | 'validTime' | 'source' | 'cycle'>
  layers: ApiLayer[]
  validTimesByLayerId: Record<string, string[] | undefined>
  derivedValidTimes?: Record<string, string[] | undefined>
  resolvedRun?: ApiHydroRun | null
}): LayerState[] {
  const apiLayersById = new Map(input.layers.map((layer) => [layer.layer_id, layer]))
  const requiredLayers: M11Layer[] = ['discharge', 'water-level', 'flood-return-period', 'warning-level']
  const layerIds = [...new Set([...requiredLayers, ...input.layers.map((layer) => layer.layer_id)])]

  return layerIds.map((layerId) => {
    const apiLayer = apiLayersById.get(layerId)
    const apiValidTimes = normalizeValidTimes(input.validTimesByLayerId[layerId])
    const derivedValidTimes = normalizeValidTimes(input.derivedValidTimes?.[layerId])
    const validTimes = apiValidTimes.length > 0 ? apiValidTimes : derivedValidTimes
    const currentValidTime = pickCurrentValidTime(validTimes, input.query.validTime)
    const isKnownRequired = (requiredLayers as string[]).includes(layerId)
    const renderable = isM11RenderableLayer(layerId)
    const available = Boolean(apiLayer) && validTimes.length > 0 && renderable
    const availableSources = input.resolvedRun ? sourcesFromRuns([input.resolvedRun]) : []
    const sourceSelection = createSourceScenarioSelection(input.query, availableSources)

    return {
      layerId,
      displayName: apiLayer?.layer_name ?? layerLabels[layerId as M11Layer] ?? layerId,
      group: layerGroup(apiLayer, layerId),
      available,
      metadata: apiLayer?.metadata ?? null,
      validTimes,
      currentValidTime,
      validTimeSource: apiValidTimes.length > 0 ? 'api' : derivedValidTimes.length > 0 ? 'derived' : 'none',
      disabledReason: available
        ? null
        : apiLayer && validTimes.length > 0 && !renderable
          ? 'Layer is registered but no renderable map source is implemented in this repository.'
          : !apiLayer && isKnownRequired
            ? 'Layer is not registered by the API.'
            : validTimes.length === 0
              ? 'Layer has no valid times.'
              : null,
      freshness: createFreshnessMetadata({
        cycleTime: input.resolvedRun?.cycle_time ?? input.query.cycle,
        validTime: currentValidTime,
        runId: input.resolvedRun?.run_id ?? null,
        basinVersionId: input.resolvedRun?.basin_version_id ?? null,
        riverNetworkVersionId: input.resolvedRun?.river_network_version_id ?? null,
        source: sourceSelection.resolvedSource,
        unavailableReason: currentValidTime ? null : 'No valid-time metadata is available.',
      }),
      legend: layerLegend(layerId),
    }
  })
}

export function getM11LayerLegend(layerId: string): LayerLegendEntry[] {
  return layerLegend(layerId)
}

export function normalizeBasinDetail(input: {
  query: Pick<M11QueryState, 'source' | 'cycle' | 'validTime' | 'basinVersionId'>
  basin: ApiBasin | null
  basinLookupAvailable?: boolean
  versions: ApiBasinVersion[]
  models?: ApiModelInstance[]
  segments?: ApiRiverFeatureCollection | null
  rankingItems?: ApiFloodAlertRankingItem[]
  latestRun?: ApiHydroRun | null
  runs?: ApiHydroRun[]
  partialErrors?: string[]
}): BasinDetail {
  const basinId = input.basin?.basin_id ?? ''
  const versions = normalizeBasinVersions(input.versions)
  const selectedVersion =
    versions.find((version) => version.basinVersionId === input.query.basinVersionId) ??
    versions.find((version) => version.active) ??
    versions[0] ??
    null
  const selectedVersionId = selectedVersion?.basinVersionId ?? null
  const models = (input.models ?? []).filter((model) => !selectedVersionId || model.basin_version_id === selectedVersionId)
  const sourceSelection = createSourceScenarioSelection(input.query, sourcesFromRuns(input.runs ?? (input.latestRun ? [input.latestRun] : [])))

  return {
    basinId,
    displayName: normalizeString(input.basin?.basin_name) ?? basinId,
    basinGroup: normalizeString(input.basin?.basin_group),
    selectedBasinVersionId: selectedVersionId,
    basinVersions: versions,
    boundary: selectedVersion?.boundary ?? null,
    bbox: selectedVersion?.bbox ?? null,
    segmentCount: input.segments?.total ?? input.segments?.features.length ?? null,
    warningDistribution: warningCountsFromRanking(input.rankingItems ?? []),
    activeModelCount: models.filter((model) => model.active_flag).length,
    latestRun: createFreshnessMetadata({
      updatedAt: input.latestRun?.updated_at ?? null,
      cycleTime: input.latestRun?.cycle_time ?? input.query.cycle,
      validTime: input.query.validTime,
      runId: input.latestRun?.run_id ?? null,
      basinVersionId: input.latestRun?.basin_version_id ?? null,
      riverNetworkVersionId: input.latestRun?.river_network_version_id ?? null,
      source: sourceSelection.resolvedSource,
      unavailableReason: input.latestRun ? null : 'No latest run is available for this basin/source.',
    }),
    sourceSelection,
    unavailableReason: !input.basin && input.basinLookupAvailable !== false
      ? 'Basin was not found.'
      : versions.length === 0
        ? 'No published basin version is available.'
        : input.segments && input.segments.features.length === 0
          ? 'Selected basin version has no river segment data.'
          : null,
    partialErrors: input.partialErrors ?? [],
  }
}

export function normalizeBasinSegmentRows(input: {
  query: Pick<M11QueryState, 'source' | 'cycle' | 'validTime' | 'warningLevel' | 'q'>
  featureCollection: ApiRiverFeatureCollection | null
  floodSegments?: ApiFloodAlertSegmentList | null
  rankingItems?: ApiFloodAlertRankingItem[]
}): BasinSegmentRow[] {
  const features = input.featureCollection?.features ?? []
  const alertById = new Map<string, ApiFloodAlertRankingItem>()
  ;(input.rankingItems ?? []).forEach((item) => {
    addAlertLookup(alertById, item.basin_version_id, item.river_network_version_id, item.river_segment_id, item)
    addAlertLookup(alertById, item.basin_version_id, item.river_network_version_id, item.segment_id, item)
  })
  ;(input.floodSegments?.segments ?? []).forEach((item) => {
    const rankingLike: ApiFloodAlertRankingItem = {
      rank: 0,
      river_segment_id: item.river_segment_id,
      segment_id: item.segment_id,
      segment_name: item.segment_name,
      basin_version_id: item.basin_version_id,
      river_network_version_id: item.river_network_version_id,
      q_value: item.q_value,
      q_unit: 'm3/s',
      return_period: item.return_period,
      warning_level: item.warning_level,
      duration: '',
      valid_time: item.valid_time,
    }
    addAlertLookup(alertById, item.basin_version_id, item.river_network_version_id, item.river_segment_id, rankingLike)
    addAlertLookup(alertById, item.basin_version_id, item.river_network_version_id, item.segment_id, rankingLike)
  })

  const budgetState = createBasinRiverGeometryBudgetState()
  return features.map((feature) => segmentRowFromFeature(feature, alertById, input.query, budgetState))
}

export function filterBasinSegmentRows(
  rows: BasinSegmentRow[],
  query: Pick<M11QueryState, 'warningLevel' | 'q'>,
): BasinSegmentRow[] {
  const normalizedFilter = normalizeWarningLevel(query.warningLevel)
  const search = query.q?.toLowerCase() ?? null

  return rows.filter((row) => {
    if (normalizedFilter && row.warningLevel !== normalizedFilter) return false
    if (!search) return true
    return `${row.displayName} ${row.riverSegmentId} ${row.segmentId}`.toLowerCase().includes(search)
  })
}

export function normalizeSelectedSegmentDetail(input: {
  query: Pick<M11QueryState, 'source' | 'cycle' | 'validTime' | 'warningLevel' | 'layer' | 'basemap' | 'q'>
  basin?: ApiBasin | null
  basinVersionId: string
  segmentId: string
  segment?: ApiRiverSegment | null
  feature?: ApiRiverFeature | null
  model?: ApiModelInstance | null
  forecast?: ApiForecastPayload | null
  floodTimeline?: ApiFloodAlertTimeline | null
  lineage?: ApiLineageResponse | null
  lineageError?: string | null
  lineageUnavailableReason?: string | null
  floodAlert?: ApiFloodAlertRankingItem | null
  resolvedRun?: ApiHydroRun | null
  resolvedQuery?: Pick<M11QueryState, 'source' | 'cycle' | 'validTime'> | null
}): SelectedSegmentDetail {
  const forecastSeries = normalizeForecastSeries(input.forecast)
  const availableSources = [
    ...new Set([
      ...forecastSeries.map((point) => point.source).filter(Boolean),
      ...sourcesFromRuns(input.resolvedRun ? [input.resolvedRun] : []),
    ]),
  ] as M11ResolvedSource[]
  const selectionQuery =
    input.query.source === 'best'
      ? { ...input.query, cycle: input.resolvedRun?.cycle_time ?? input.resolvedQuery?.cycle ?? input.query.cycle }
      : input.resolvedQuery ?? input.query
  const sourceSelection = createSourceScenarioSelection(selectionQuery, availableSources)
  const handoffSource =
    input.query.source === 'best' && (sourceSelection.resolvedSource === 'GFS' || sourceSelection.resolvedSource === 'IFS')
      ? (sourceSelection.resolvedSource.toLowerCase() as M11Source)
      : input.query.source
  const currentPoint = pickCurrentTrendPoint(forecastSeries, input.query.validTime, sourceSelection)
  const effectiveValidTime = currentPoint?.validTime ?? normalizeIsoString(input.query.validTime)
  const alert = input.floodAlert
  const timelinePeak = input.floodTimeline?.peak ?? null
  const warningLevel = normalizeWarningLevel(alert?.warning_level ?? timelinePeak?.warning_level) ?? 'unavailable'
  const lineageStatus = input.lineage ? 'available' : input.lineageError ? 'failed' : 'unavailable'
  const riverSegmentId =
    input.segment?.river_segment_id ??
    input.feature?.properties.river_segment_id ??
    input.floodTimeline?.river_segment_id ??
    input.segmentId
  const geometryStatus = getM11SelectedSegmentGeometryBudgetStatus(input.segment?.geom ?? input.feature?.geometry ?? null)

  return {
    basinId: input.basin?.basin_id ?? input.model?.basin_id ?? null,
    basinName: input.basin?.basin_name ?? input.model?.basin_name ?? null,
    basinVersionId: input.basinVersionId,
    riverSegmentId,
    segmentId: input.feature?.properties.segment_id ?? input.floodTimeline?.segment_id ?? input.segmentId,
    displayName:
      normalizeString(input.feature?.properties.name) ??
      normalizeString(alert?.segment_name) ??
      riverSegmentId,
    modelId: input.model?.model_id ?? null,
    riverNetworkVersionId:
      input.segment?.river_network_version_id ??
      input.feature?.properties.river_network_version_id ??
      input.floodTimeline?.river_network_version_id ??
      null,
    currentQ: numberOrNull(alert?.q_value) ?? currentPoint?.value ?? numberOrNull(timelinePeak?.q_value),
    qUnit: normalizeUnit(alert?.q_unit ?? forecastUnit(input.forecast)),
    returnPeriod: numberOrNull(alert?.return_period) ?? numberOrNull(timelinePeak?.return_period),
    warningLevel,
    qualityFlag: qualityFlagFromValue(input.lineageError ? 'failed' : input.floodTimeline?.quality_note ?? null),
    qualityNote: normalizeString(input.floodTimeline?.quality_note) ?? normalizeString(input.lineageError),
    sourceSelection,
    trendPoints: forecastSeries,
    comparisonAvailable: sourceSelection.comparisonAvailable,
    lineageStatus,
    lineageUnavailableReason:
      lineageStatus === 'available'
        ? null
        : normalizeString(input.lineageError) ??
          normalizeString(input.lineageUnavailableReason) ??
          'Lineage is unavailable for this segment/time.',
    handoffUrl: m11QueryHref('/', {
      source: handoffSource,
      cycle: selectionQuery.cycle,
      validTime: effectiveValidTime,
      layer: input.query.layer,
      basemap: input.query.basemap,
      basinVersionId: input.basinVersionId,
      riverNetworkVersionId:
        input.segment?.river_network_version_id ??
        input.feature?.properties.river_network_version_id ??
        input.floodTimeline?.river_network_version_id ??
        null,
      segmentId: riverSegmentId,
      warningLevel: input.query.warningLevel,
      q: input.query.q,
    }),
    geometry: geometryStatus.sanitizedGeometry,
    freshness: createFreshnessMetadata({
      updatedAt: input.forecast && 'issue_time' in input.forecast ? input.forecast.issue_time : input.resolvedRun?.updated_at ?? null,
      cycleTime: input.resolvedRun?.cycle_time ?? selectionQuery.cycle,
      validTime: effectiveValidTime,
      runId: input.floodTimeline?.run_id ?? input.resolvedRun?.run_id ?? null,
      basinVersionId: input.resolvedRun?.basin_version_id ?? null,
      riverNetworkVersionId: input.floodTimeline?.river_network_version_id ?? input.resolvedRun?.river_network_version_id ?? null,
      source: sourceSelection.resolvedSource,
      unavailableReason: forecastSeries.length > 0 || alert ? null : 'No forecast or flood-alert values are available.',
    }),
    unavailableReason:
      (!input.segment && !input.feature ? 'Segment geometry/detail is unavailable.' : null) ?? geometryStatus.reason,
  }
}

function normalizeRequestedSource(source: M11Source): M11Source {
  return source === 'ifs' || source === 'compare' || source === 'best' ? source : 'gfs'
}

function resolveSelectedSource(source: M11Source, availableSources: M11ResolvedSource[]): M11ResolvedSource {
  if (source === 'compare') return availableSources.includes('GFS') && availableSources.includes('IFS') ? 'GFS+IFS' : 'Unknown'
  if (source === 'best') {
    if (availableSources.includes('GFS')) return 'GFS'
    if (availableSources.includes('IFS')) return 'IFS'
    return availableSources[0] ?? 'Unknown'
  }
  const expected = source.toUpperCase() as M11ResolvedSource
  return availableSources.length === 0 || availableSources.includes(expected) ? expected : 'Unknown'
}

function scenarioIdsForResolvedSource(source: M11ResolvedSource): string[] {
  if (source === 'GFS') return ['forecast_gfs_deterministic']
  if (source === 'IFS') return ['forecast_ifs_deterministic']
  if (source === 'GFS+IFS') return ['forecast_gfs_deterministic', 'forecast_ifs_deterministic']
  return []
}

function buildProvenanceLabel(source: M11Source, resolved: M11ResolvedSource, cycle: string | null, validTime: string | null) {
  const sourceLabel = source === 'best' ? `Best Available (${resolved})` : resolved
  const cycleLabel = cycle ? `cycle ${cycle}` : 'latest cycle'
  const validLabel = validTime ? `valid ${validTime}` : 'current valid time'
  return `${sourceLabel} / ${cycleLabel} / ${validLabel}`
}

function normalizeString(value: unknown): string | null {
  return typeof value === 'string' && value.trim().length > 0 ? value.trim() : null
}

function numberOrNull(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const numberValue = Number(value)
  return Number.isFinite(numberValue) ? numberValue : null
}

function finiteNumberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function numberOrZero(value: unknown): number {
  return numberOrNull(value) ?? 0
}

function sumNullable(values: Array<number | null>): number | null {
  const usable = values.filter((value): value is number => value !== null)
  return usable.length > 0 ? usable.reduce((total, value) => total + value, 0) : null
}

function normalizeIsoString(value: unknown): string | null {
  const stringValue = normalizeString(value)
  if (!stringValue) return null
  const timestamp = Date.parse(stringValue)
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : stringValue
}

function latestIso(values: Array<string | null | undefined>): string | null {
  const timestamps = values
    .map((value) => {
      const normalized = normalizeIsoString(value)
      return normalized ? { normalized, timestamp: Date.parse(normalized) } : null
    })
    .filter((entry): entry is { normalized: string; timestamp: number } => entry !== null && Number.isFinite(entry.timestamp))
  timestamps.sort((a, b) => b.timestamp - a.timestamp)
  return timestamps[0]?.normalized ?? null
}

function isStale(value: unknown, staleAfterHours: number): boolean {
  const normalized = normalizeIsoString(value)
  if (!normalized) return false
  return Date.now() - Date.parse(normalized) > staleAfterHours * 3_600_000
}

export function normalizeWarningLevel(value: unknown): M11WarningLevel | null {
  if (typeof value !== 'string') return null
  const normalized = value.trim().toLowerCase().replace('-', '_')
  if (
    normalized === 'normal' ||
    normalized === 'elevated' ||
    normalized === 'watch' ||
    normalized === 'warning' ||
    normalized === 'high_risk' ||
    normalized === 'severe' ||
    normalized === 'extreme' ||
    normalized === 'unavailable'
  ) {
    return normalized
  }
  if (normalized === 'orange') return 'warning'
  if (normalized === 'red') return 'severe'
  if (normalized === 'major' || normalized === 'danger' || normalized === 'high') return 'high_risk'
  return null
}

function isSuperWarningLevel(level: M11WarningLevel | null): boolean {
  return level === 'warning' || level === 'high_risk' || level === 'severe' || level === 'extreme'
}

function qualityFlagFromValue(value: unknown): M11QualityFlag {
  if (typeof value !== 'string') return 'unknown'
  const normalized = value.trim().toLowerCase().replace('-', '_')
  if (normalized === 'ok' || normalized === 'good' || normalized === 'passed') return 'ok'
  if (normalized === 'degraded' || normalized === 'partial' || normalized === 'warning') return 'degraded'
  if (normalized === 'missing' || normalized === 'unavailable' || normalized === 'no_curve') return 'unavailable'
  if (normalized === 'failed' || normalized === 'error') return 'failed'
  return 'unknown'
}

function normalizeUnit(value: unknown): string {
  const unit = normalizeString(value)
  if (!unit) return 'm3/s'
  return unit === 'm³/s' ? 'm3/s' : unit
}

function forecastUnit(forecast: ApiForecastPayload | null | undefined): string | null {
  return forecast && 'unit' in forecast ? forecast.unit : null
}

function normalizeBasinVersions(versions: ApiBasinVersion[]): BasinVersionOption[] {
  return versions.map((version) => {
    const geometryStatus = getM11BasinGeometryBudgetStatus(version.geom)
    const bbox = geometryStatus.ok ? geometryStatus.bbox : null
    const boundary = geometryStatus.ok ? geometryStatus.sanitizedGeometry : null
    return {
      basinVersionId: version.basin_version_id,
      versionLabel: version.version_label,
      active: version.active_flag,
      validFrom: normalizeIsoString(version.valid_from),
      validTo: normalizeIsoString(version.valid_to),
      sourceUri: normalizeString(version.source_uri),
      boundary,
      bbox,
      unavailableReason: geometryStatus.reason ?? (bbox ? null : 'Basin geometry is unavailable.'),
    }
  })
}

export function getM11BasinGeometryBudgetStatus(
  geom: components['schemas']['GeoJsonMultiPolygon'] | null | undefined,
): M11BasinGeometryBudgetStatus {
  if (!geom?.coordinates) {
    return geometryStatus(false, 'Basin geometry is unavailable.', null, 0, 0, 0, 0, null)
  }
  if (geom.type !== 'MultiPolygon' || !Array.isArray(geom.coordinates)) {
    return geometryStatus(false, 'Basin geometry is malformed.', null, 0, 0, 0, serializedByteLength(geom), null)
  }

  let bbox: M11Bbox | null = null
  let polygonCount = 0
  let ringCount = 0
  let vertexCount = 0
  const sanitizedCoordinates: number[][][][] = []

  for (const polygon of geom.coordinates as unknown[]) {
    if (!Array.isArray(polygon)) return geometryMalformed(polygonCount, ringCount, vertexCount)
    polygonCount += 1
    if (polygonCount > m11BasinGeometryBudget.maxPolygons) return geometryTooLarge(polygonCount, ringCount, vertexCount)
    const sanitizedPolygon: number[][][] = []

    for (const ring of polygon) {
      if (!Array.isArray(ring)) return geometryMalformed(polygonCount, ringCount, vertexCount)
      ringCount += 1
      if (ringCount > m11BasinGeometryBudget.maxRings) return geometryTooLarge(polygonCount, ringCount, vertexCount)
      const sanitizedRing: number[][] = []

      for (const coordinate of ring) {
        if (!Array.isArray(coordinate) || coordinate.length < 2) return geometryMalformed(polygonCount, ringCount, vertexCount)
        if (coordinate.length > m11BasinGeometryBudget.maxCoordinateDimensions) {
          return geometryTooWide(polygonCount, ringCount, vertexCount + 1)
        }
        vertexCount += 1
        if (vertexCount > m11BasinGeometryBudget.maxVertices) return geometryTooLarge(polygonCount, ringCount, vertexCount)

        const lon = numberOrNull(coordinate[0])
        const lat = numberOrNull(coordinate[1])
        if (lon === null || lat === null) return geometryMalformed(polygonCount, ringCount, vertexCount)
        const elevation = coordinate.length >= 3 ? numberOrNull(coordinate[2]) : null
        if (coordinate.length >= 3 && elevation === null) return geometryMalformed(polygonCount, ringCount, vertexCount)
        sanitizedRing.push(elevation === null ? [lon, lat] : [lon, lat, elevation])
        bbox = bbox
          ? {
              minLon: Math.min(bbox.minLon, lon),
              minLat: Math.min(bbox.minLat, lat),
              maxLon: Math.max(bbox.maxLon, lon),
              maxLat: Math.max(bbox.maxLat, lat),
            }
          : { minLon: lon, minLat: lat, maxLon: lon, maxLat: lat }
      }
      if (sanitizedRing.length < 4 || !basinRingIsClosed(sanitizedRing)) {
        return geometryMalformed(polygonCount, ringCount, vertexCount)
      }
      sanitizedPolygon.push(sanitizedRing)
    }
    sanitizedCoordinates.push(sanitizedPolygon)
  }

  if (!bbox) return geometryStatus(false, 'Basin geometry is unavailable.', null, polygonCount, ringCount, vertexCount, 0, null)
  const sanitizedGeometry = { type: 'MultiPolygon' as const, coordinates: sanitizedCoordinates }
  const serializedBytes = serializedByteLength(sanitizedGeometry)
  if (serializedBytes > m11BasinGeometryBudget.maxSerializedBytes) {
    return geometryTooManyBytes(polygonCount, ringCount, vertexCount, serializedBytes)
  }
  return geometryStatus(true, null, bbox, polygonCount, ringCount, vertexCount, serializedBytes, sanitizedGeometry)
}

export function getM11SelectedSegmentGeometryBudgetStatus(
  geom: components['schemas']['GeoJsonLineString'] | null | undefined,
): M11SelectedSegmentGeometryBudgetStatus {
  if (!geom?.coordinates) {
    return selectedSegmentGeometryStatus(false, 'Selected segment geometry is unavailable.', 0, 0, null)
  }
  if (geom.type !== 'LineString' || !Array.isArray(geom.coordinates)) {
    return selectedSegmentGeometryStatus(false, 'Selected segment geometry is malformed.', 0, serializedByteLength(geom), null)
  }

  const sanitizedCoordinates: number[][] = []
  let coordinateCount = 0

  for (const coordinate of geom.coordinates as unknown[]) {
    if (!Array.isArray(coordinate) || coordinate.length < 2) {
      return selectedSegmentGeometryStatus(false, 'Selected segment geometry is malformed.', coordinateCount, 0, null)
    }
    if (coordinate.length > m11SelectedSegmentGeometryBudget.maxCoordinateDimensions) {
      return selectedSegmentGeometryStatus(
        false,
        `Selected segment geometry coordinate dimensions exceed client rendering budget (${m11SelectedSegmentGeometryBudget.maxCoordinateDimensions}).`,
        coordinateCount + 1,
        0,
        null,
      )
    }
    coordinateCount += 1
    if (coordinateCount > m11SelectedSegmentGeometryBudget.maxCoordinates) {
      return selectedSegmentGeometryStatus(
        false,
        `Selected segment geometry exceeds client rendering budget (${coordinateCount}/${m11SelectedSegmentGeometryBudget.maxCoordinates} coordinates).`,
        coordinateCount,
        0,
        null,
      )
    }

    const lon = finiteNumberOrNull(coordinate[0])
    const lat = finiteNumberOrNull(coordinate[1])
    if (lon === null || lat === null) {
      return selectedSegmentGeometryStatus(false, 'Selected segment geometry is malformed.', coordinateCount, 0, null)
    }
    const elevation = coordinate.length >= 3 ? finiteNumberOrNull(coordinate[2]) : null
    if (coordinate.length >= 3 && elevation === null) {
      return selectedSegmentGeometryStatus(false, 'Selected segment geometry is malformed.', coordinateCount, 0, null)
    }
    sanitizedCoordinates.push(elevation === null ? [lon, lat] : [lon, lat, elevation])
  }

  if (sanitizedCoordinates.length < 2) {
    return selectedSegmentGeometryStatus(false, 'Selected segment geometry requires at least two coordinates.', coordinateCount, 0, null)
  }

  const sanitizedGeometry = { type: 'LineString' as const, coordinates: sanitizedCoordinates }
  const serializedBytes = serializedByteLength(sanitizedGeometry)
  if (serializedBytes > m11SelectedSegmentGeometryBudget.maxSerializedBytes) {
    return selectedSegmentGeometryStatus(
      false,
      `Selected segment geometry exceeds client serialized-size budget (${serializedBytes}/${m11SelectedSegmentGeometryBudget.maxSerializedBytes} bytes).`,
      coordinateCount,
      serializedBytes,
      null,
    )
  }

  return selectedSegmentGeometryStatus(true, null, coordinateCount, serializedBytes, sanitizedGeometry)
}

function geometryTooLarge(polygonCount: number, ringCount: number, vertexCount: number): M11BasinGeometryBudgetStatus {
  return geometryStatus(
    false,
    `Basin geometry exceeds client rendering budget (${vertexCount}/${m11BasinGeometryBudget.maxVertices} vertices).`,
    null,
    polygonCount,
    ringCount,
    vertexCount,
    0,
    null,
  )
}

function geometryTooWide(polygonCount: number, ringCount: number, vertexCount: number): M11BasinGeometryBudgetStatus {
  return geometryStatus(
    false,
    `Basin geometry coordinate dimensions exceed client rendering budget (${m11BasinGeometryBudget.maxCoordinateDimensions}).`,
    null,
    polygonCount,
    ringCount,
    vertexCount,
    0,
    null,
  )
}

function geometryTooManyBytes(
  polygonCount: number,
  ringCount: number,
  vertexCount: number,
  serializedBytes: number,
): M11BasinGeometryBudgetStatus {
  return geometryStatus(
    false,
    `Basin geometry exceeds client serialized-size budget (${serializedBytes}/${m11BasinGeometryBudget.maxSerializedBytes} bytes).`,
    null,
    polygonCount,
    ringCount,
    vertexCount,
    serializedBytes,
    null,
  )
}

function geometryMalformed(polygonCount: number, ringCount: number, vertexCount: number): M11BasinGeometryBudgetStatus {
  return geometryStatus(false, 'Basin geometry is malformed.', null, polygonCount, ringCount, vertexCount, 0, null)
}

function basinRingIsClosed(ring: number[][]) {
  const first = ring[0]
  const last = ring[ring.length - 1]
  if (!first || !last || first.length !== last.length) return false
  return first.every((coordinate, index) => coordinate === last[index])
}

function geometryStatus(
  ok: boolean,
  reason: string | null,
  bbox: M11Bbox | null,
  polygonCount: number,
  ringCount: number,
  vertexCount: number,
  serializedBytes: number,
  sanitizedGeometry: components['schemas']['GeoJsonMultiPolygon'] | null,
): M11BasinGeometryBudgetStatus {
  return { ok, reason, bbox, polygonCount, ringCount, vertexCount, serializedBytes, sanitizedGeometry }
}

function selectedSegmentGeometryStatus(
  ok: boolean,
  reason: string | null,
  coordinateCount: number,
  serializedBytes: number,
  sanitizedGeometry: components['schemas']['GeoJsonLineString'] | null,
): M11SelectedSegmentGeometryBudgetStatus {
  return { ok, reason, coordinateCount, serializedBytes, sanitizedGeometry }
}

function serializedByteLength(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value)).length
}

function isM11RenderableLayer(layerId: string) {
  return layerId === 'discharge' || layerId === 'water-level' || layerId === 'flood-return-period' || layerId === 'warning-level'
}

function polygonAreaKm2(geom: components['schemas']['GeoJsonMultiPolygon']): number | null {
  if (!geom.coordinates.length) return null
  const earthRadiusKm = 6371.0088
  let area = 0

  geom.coordinates.forEach((polygon) => {
    polygon.forEach((ring, ringIndex) => {
      if (ring.length < 4) return
      const ringArea = Math.abs(sphericalRingArea(ring, earthRadiusKm))
      area += ringIndex === 0 ? ringArea : -ringArea
    })
  })

  return area > 0 ? Math.round(area) : null
}

function sphericalRingArea(ring: number[][], earthRadiusKm: number): number {
  let sum = 0
  for (let index = 0; index < ring.length; index += 1) {
    const current = ring[index]
    const next = ring[(index + 1) % ring.length]
    if (!current || !next || current.length < 2 || next.length < 2) continue
    const lon1 = degreesToRadians(current[0])
    const lon2 = degreesToRadians(next[0])
    const lat1 = degreesToRadians(current[1])
    const lat2 = degreesToRadians(next[1])
    sum += (lon2 - lon1) * (2 + Math.sin(lat1) + Math.sin(lat2))
  }
  return (sum * earthRadiusKm * earthRadiusKm) / 2
}

function degreesToRadians(value: number) {
  return (value * Math.PI) / 180
}

function warningCountsFromRanking(items: ApiFloodAlertRankingItem[]): Record<M11WarningLevel, number> {
  const counts = { ...emptyWarningCounts }
  items.forEach((item) => {
    const level = normalizeWarningLevel(item.warning_level) ?? 'unavailable'
    counts[level] += 1
  })
  return counts
}

function normalizeValidTimes(values: string[] | undefined): string[] {
  return [...new Set((values ?? []).map(normalizeIsoString).filter((value): value is string => Boolean(value)))].sort(
    (a, b) => Date.parse(a) - Date.parse(b),
  )
}

function pickCurrentValidTime(validTimes: string[], queryValidTime: string | null): string | null {
  if (validTimes.length === 0) return null
  const normalizedQuery = normalizeIsoString(queryValidTime)
  if (normalizedQuery && validTimes.includes(normalizedQuery)) return normalizedQuery
  return validTimes[validTimes.length - 1]
}

function layerGroup(layer: ApiLayer | undefined, layerId: string): LayerState['group'] {
  const type = `${layer?.layer_type ?? ''} ${layerId}`.toLowerCase()
  if (type.includes('met') || type.includes('precip') || type.includes('temperature')) return 'meteorology'
  if (type.includes('base') || type.includes('boundary') || type.includes('dem')) return 'base'
  if (type.includes('hydro') || type.includes('flood') || type.includes('warning') || type.includes('discharge')) return 'hydrology'
  return 'unknown'
}

function layerLegend(layerId: string): LayerLegendEntry[] {
  if (layerId === 'warning-level') return m11RiverWarningLevelLegend.map((entry) => ({ ...entry }))
  if (layerId === 'flood-return-period') return m11RiverReturnPeriodLegend.map((entry) => ({ ...entry }))
  if (layerId === 'discharge') return m11RiverDischargeLegend.map((entry) => ({ ...entry }))
  if (layerId === 'water-level') return m11RiverWaterLevelLegend.map((entry) => ({ ...entry }))
  return []
}

export function m11BasinRiverLayerColor(row: Pick<BasinSegmentRow, 'currentQ' | 'returnPeriod' | 'warningLevel'>, layer: M11Layer) {
  if (layer === 'warning-level') return m11WarningLevelColor(row.warningLevel)
  if (layer === 'flood-return-period') return m11ReturnPeriodColor(row.returnPeriod)
  if (layer === 'discharge') return m11DischargeColor(row.currentQ)
  return '#94A3B8'
}

export function m11DischargeColor(value: number | null) {
  if (value === null) return '#CBD5E1'
  if (value >= 50_000) return '#F44336'
  if (value >= 10_000) return '#FF9800'
  if (value >= 5_000) return '#1E88E5'
  if (value >= 1_000) return '#42A5F5'
  if (value >= 500) return '#90CAF9'
  return '#E3F2FD'
}

export function m11WaterLevelColor(value: number | null) {
  if (value === null) return '#CBD5E1'
  if (value >= 8) return '#D81B60'
  if (value >= 4) return '#FDD835'
  if (value >= 2) return '#00897B'
  if (value >= 1) return '#26C6DA'
  if (value >= 0.5) return '#80DEEA'
  return '#E0F7FA'
}

export function m11ReturnPeriodColor(value: number | null) {
  if (value === null) return m11VisualTokens.warningLevels.unavailable
  if (value >= 100) return ALERT_LEVEL_META.extreme.color
  if (value >= 50) return ALERT_LEVEL_META.severe.color
  if (value >= 20) return ALERT_LEVEL_META.high_risk.color
  if (value >= 10) return ALERT_LEVEL_META.warning.color
  if (value >= 5) return ALERT_LEVEL_META.watch.color
  if (value >= 2) return ALERT_LEVEL_META.elevated.color
  return ALERT_LEVEL_META.normal.color
}

export function m11WarningLevelColor(level: M11WarningLevel) {
  if (level === 'high_risk') return ALERT_LEVEL_META.high_risk.color
  if (level === 'severe') return ALERT_LEVEL_META.severe.color
  if (level === 'extreme') return ALERT_LEVEL_META.extreme.color
  if (level === 'warning') return ALERT_LEVEL_META.warning.color
  if (level === 'watch') return ALERT_LEVEL_META.watch.color
  if (level === 'elevated') return ALERT_LEVEL_META.elevated.color
  if (level === 'normal') return ALERT_LEVEL_META.normal.color
  return m11VisualTokens.warningLevels.unavailable
}

function versionedSegmentKey(basinVersionId: string, riverNetworkVersionId: string, segmentId: string): string {
  return `${basinVersionId}::${riverNetworkVersionId}::${segmentId}`
}

function legacyVersionedSegmentKey(basinVersionId: string, segmentId: string): string {
  return `${basinVersionId}::legacy::${segmentId}`
}

function addAlertLookup(
  alertById: Map<string, ApiFloodAlertRankingItem>,
  basinVersionId: string,
  riverNetworkVersionId: string | null | undefined,
  segmentId: string,
  item: ApiFloodAlertRankingItem,
) {
  if (riverNetworkVersionId) {
    alertById.set(versionedSegmentKey(basinVersionId, riverNetworkVersionId, segmentId), item)
  } else {
    alertById.set(legacyVersionedSegmentKey(basinVersionId, segmentId), item)
  }
}

interface BasinRiverGeometryBudgetState {
  featureCount: number
  coordinateCount: number
  serializedBytes: number
}

function createBasinRiverGeometryBudgetState(): BasinRiverGeometryBudgetState {
  return {
    featureCount: 0,
    coordinateCount: 0,
    serializedBytes: serializedByteLength({ type: 'FeatureCollection', features: [] }),
  }
}

function retainBasinRiverGeometryWithinBudget(
  geometryStatus: M11SelectedSegmentGeometryBudgetStatus,
  state: BasinRiverGeometryBudgetState,
): M11SelectedSegmentGeometryBudgetStatus {
  if (!geometryStatus.sanitizedGeometry) return geometryStatus

  const geometryBytes = serializedByteLength(geometryStatus.sanitizedGeometry)
  const nextFeatureCount = state.featureCount + 1
  const nextCoordinateCount = state.coordinateCount + geometryStatus.coordinateCount
  const nextSerializedBytes = state.serializedBytes + geometryBytes + (state.featureCount > 0 ? 1 : 0)

  if (
    nextFeatureCount > m11BasinRiverCollectionBudget.maxFeatures ||
    nextCoordinateCount > m11BasinRiverCollectionBudget.maxCoordinates ||
    nextSerializedBytes > m11BasinRiverCollectionBudget.maxSerializedBytes
  ) {
    return selectedSegmentGeometryStatus(
      false,
      `Basin river geometry exceeds aggregate client rendering budget (${nextFeatureCount}/${m11BasinRiverCollectionBudget.maxFeatures} features, ${nextCoordinateCount}/${m11BasinRiverCollectionBudget.maxCoordinates} coordinates, ${nextSerializedBytes}/${m11BasinRiverCollectionBudget.maxSerializedBytes} bytes).`,
      geometryStatus.coordinateCount,
      geometryStatus.serializedBytes,
      null,
    )
  }

  state.featureCount = nextFeatureCount
  state.coordinateCount = nextCoordinateCount
  state.serializedBytes = nextSerializedBytes
  return geometryStatus
}

function segmentRowFromFeature(
  feature: ApiRiverFeature,
  alertById: Map<string, ApiFloodAlertRankingItem>,
  query: Pick<M11QueryState, 'source' | 'cycle' | 'validTime'>,
  budgetState: BasinRiverGeometryBudgetState,
): BasinSegmentRow {
  const props = feature.properties
  const alert =
    alertById.get(versionedSegmentKey(props.basin_version_id, props.river_network_version_id, props.river_segment_id)) ??
    alertById.get(versionedSegmentKey(props.basin_version_id, props.river_network_version_id, props.segment_id)) ??
    alertById.get(legacyVersionedSegmentKey(props.basin_version_id, props.river_segment_id)) ??
    alertById.get(legacyVersionedSegmentKey(props.basin_version_id, props.segment_id))
  const sourceSelection = createSourceScenarioSelection(query, alert ? [sourceFromQuery(query.source)] : [])
  const warningLevel = normalizeWarningLevel(alert?.warning_level) ?? 'unavailable'
  const geometryStatus = retainBasinRiverGeometryWithinBudget(getM11SelectedSegmentGeometryBudgetStatus(feature.geometry), budgetState)
  return {
    riverSegmentId: props.river_segment_id,
    riverNetworkVersionId: props.river_network_version_id,
    segmentId: props.segment_id,
    displayName: normalizeString(props.name) ?? props.river_segment_id,
    basinVersionId: props.basin_version_id,
    streamOrder: numberOrNull(props.stream_order),
    lengthM: numberOrNull(props.length_m),
    currentQ: numberOrNull(alert?.q_value),
    qUnit: normalizeUnit(alert?.q_unit),
    returnPeriod: numberOrNull(alert?.return_period),
    warningLevel,
    qualityFlag: alert ? 'ok' : 'unavailable',
    qualityNote: null,
    source: sourceSelection.resolvedSource,
    cycleTime: query.cycle,
    validTime: normalizeIsoString(alert?.valid_time) ?? query.validTime,
    hasGeometry: Boolean(geometryStatus.sanitizedGeometry),
    geometry: geometryStatus.sanitizedGeometry,
    unavailableReason: geometryStatus.reason ?? (alert ? null : 'No flood-alert value is available for this segment/time.'),
  }
}

function normalizeForecastSeries(forecast: ApiForecastPayload | null | undefined): TrendPoint[] {
  if (!forecast) return []
  if ('segments' in forecast) {
    return forecast.segments.flatMap((segment) => {
      const scenarioId = segment.scenario_id ?? segment.scenario
      const source = sourceFromScenario(scenarioId, segment.source_id ?? segment.source)
      return segment.data.map((point) => ({
        validTime: normalizeIsoString(point.valid_time) ?? point.valid_time,
        value: numberOrNull(point.value),
        source,
        scenarioId,
        role: segment.segment_role,
        isAnalysis: segment.segment_role === 'past_7_days' || scenarioId.includes('analysis'),
      }))
    })
  }

  return forecast.series.flatMap((segment) => {
    const source = sourceFromScenario(segment.scenario_id, segment.source_id)
    return segment.points
      .filter((point) => point.length >= 2)
      .map((point) => ({
        validTime: normalizeIsoString(point[0]) ?? String(point[0]),
        value: numberOrNull(point[1]),
        source,
        scenarioId: segment.scenario_id,
        role: segment.segment_role,
        isAnalysis: segment.segment_role === 'past_7_days' || segment.scenario_id.includes('analysis'),
      }))
  })
}

function sourceFromScenario(scenarioId: string, explicitSource?: string | null): M11ResolvedSource {
  const value = `${explicitSource ?? ''} ${scenarioId}`.toLowerCase()
  if (value.includes('ifs')) return 'IFS'
  if (value.includes('gfs')) return 'GFS'
  return 'Unknown'
}

function sourceFromQuery(source: M11Source): M11ResolvedSource {
  if (source === 'ifs') return 'IFS'
  if (source === 'compare') return 'GFS+IFS'
  if (source === 'best') return 'Unknown'
  return 'GFS'
}

function sourcesFromRuns(runs: ApiHydroRun[]): M11ResolvedSource[] {
  return [
    ...new Set(
      runs
        .map((run) => sourceFromScenario(run.scenario_id, run.source_id))
        .filter((source): source is M11ResolvedSource => source !== 'Unknown'),
    ),
  ]
}

function pickCurrentTrendPoint(
  points: TrendPoint[],
  validTime: string | null,
  sourceSelection: SourceScenarioSelectionState,
): TrendPoint | null {
  const usable = points.filter((point) => {
    if (sourceSelection.resolvedSource === 'GFS+IFS') return true
    return point.source === sourceSelection.resolvedSource || point.isAnalysis
  })
  if (usable.length === 0) return null
  const normalizedValidTime = normalizeIsoString(validTime)
  return (
    (normalizedValidTime ? usable.find((point) => point.validTime === normalizedValidTime) : null) ??
    [...usable].sort((a, b) => Date.parse(b.validTime) - Date.parse(a.validTime))[0] ??
    null
  )
}

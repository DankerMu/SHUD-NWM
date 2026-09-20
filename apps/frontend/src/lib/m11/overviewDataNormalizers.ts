import type { components } from '@/api/types'
import {
  isStale,
  latestIso,
  normalizeIsoString,
  normalizeString,
  numberOrNull,
  sourcesFromRuns,
  sumNullable,
} from '@/lib/m11/overviewDataContractPrimitives'
import type {
  AggregationEndpointDecision,
  AggregationEndpointDecisionInput,
  ApiBasin,
  ApiBasinVersion,
  ApiHydroRun,
  ApiModelInstance,
  ApiPipelineStatus,
  ApiQueueDepth,
  BasinVersionOption,
  FreshnessMetadata,
  M11ResolvedSource,
  OverviewBasin,
  OverviewSummary,
  SourceScenarioSelectionState,
} from '@/lib/m11/overviewDataContractTypes'
import { getM11BasinGeometryBudgetStatus } from '@/lib/m11/overviewDataGeometryBudget'
import type { M11QueryState, M11Source } from '@/lib/m11/queryState'

/**
 * 全国尺度的 `best → gfs` 归一（spec map-layer-timeline-controls
 * 「a restored URL with `source=best` at national scale MUST resolve to `gfs`」）。
 *
 * 只在 selection 层的**全国调用点**生效，绝不在 `parseM11QueryState` 里做：
 * `parseM11QueryState('source=best').source` 必须仍是 `'best'`，否则 serialize 往返会把
 * 用户 URL 里的 `best` 改写掉；`createSourceScenarioSelection` 的默认臂也不得无条件归一，
 * 否则会静默吃掉 "Best Available exposes provenance"。
 */
export function resolveNationalScaleSource(source: M11Source): M11Source {
  return source === 'best' ? 'gfs' : source
}

export function createSourceScenarioSelection(
  query: Pick<M11QueryState, 'source' | 'cycle' | 'validTime'>,
  availableSources: M11ResolvedSource[] = [],
  // 默认不归一（`best` 保留 Best Available provenance；`normalizeLayerStates` 的图层 freshness
  // 走此臂），只有全国 summary 调用点显式传 `'national'`，把 `best` 归一为 `gfs`。
  options: { scale?: 'national' | 'basin' } = {},
): SourceScenarioSelectionState {
  const scopedSource = options.scale === 'national' ? resolveNationalScaleSource(query.source) : query.source
  const source = normalizeRequestedSource(scopedSource)
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
  const sourceSelection = createSourceScenarioSelection(query, [], { scale: 'national' })
  return {
    completedCyclesToday: null,
    runningJobs: null,
    latestUpdate: null,
    totalBasins: 0,
    sourceSelection,
    freshness: createFreshnessMetadata({ source: sourceSelection.resolvedSource, unavailableReason: 'No overview data loaded.' }),
    qualityNotes: ['No overview data loaded.'],
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
}): OverviewBasin[] {
  const models = input.models ?? []
  const runs = input.runs ?? []

  return input.basins.map((basin) => {
    const versions = input.versionsByBasinId?.[basin.basin_id] ?? []
    const versionOptions = normalizeBasinVersions(versions)
    const versionIds = new Set(versionOptions.map((version) => version.basinVersionId))
    const basinModels = models.filter((model) => model.basin_id === basin.basin_id || versionIds.has(model.basin_version_id))
    const basinRuns = runs.filter((run) => versionIds.has(run.basin_version_id))
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
  pipeline?: ApiPipelineStatus | null
  queue?: ApiQueueDepth | null
  latestRun?: ApiHydroRun | null
  runs?: ApiHydroRun[]
  partialErrors?: string[]
}): OverviewSummary {
  const availableSources = sourcesFromRuns(input.runs ?? (input.latestRun ? [input.latestRun] : []))
  // 周期回退推广到所有非 compare 源（与 `pipelineRequestParams` 同形）：默认源由 `best` 翻成 `gfs`
  // 后，若仍只在 best 分支回退，默认全国总览的 `sourceSelection.cycleTime` 恒为 null——时间轴的
  // 分析/预报分界线（M11Controls 用它算 dividerIndex）与 provenance 里的周期会静默消失。
  // 显式 URL 周期优先于 run 周期（run 本就是按该周期过滤出来的）。
  const selectionQuery =
    input.query.source === 'compare'
      ? input.query
      : { ...input.query, cycle: input.query.cycle ?? input.latestRun?.cycle_time ?? input.pipeline?.cycle_time ?? null }
  const sourceSelection = createSourceScenarioSelection(selectionQuery, availableSources, { scale: 'national' })
  const completedCyclesToday = input.pipeline?.job_counts.succeeded ?? null
  const runningJobs = input.queue?.running ?? input.pipeline?.job_counts.running ?? null
  const latestUpdate = latestIso([
    input.pipeline?.updated_at ?? null,
    input.latestRun?.updated_at ?? null,
    input.latestRun?.cycle_time ?? null,
    input.query.validTime,
  ])

  return {
    completedCyclesToday,
    runningJobs,
    latestUpdate,
    totalBasins: input.basins?.length ?? 0,
    sourceSelection,
    freshness: createFreshnessMetadata({
      updatedAt: latestUpdate,
      cycleTime: input.latestRun?.cycle_time ?? input.pipeline?.cycle_time ?? input.query.cycle,
      validTime: input.query.validTime,
      runId: input.latestRun?.run_id ?? null,
      basinVersionId: input.latestRun?.basin_version_id ?? null,
      riverNetworkVersionId: input.latestRun?.river_network_version_id ?? null,
      source: sourceSelection.resolvedSource,
      unavailableReason: latestUpdate ? null : 'No freshness metadata is available.',
    }),
    qualityNotes: [],
    partialErrors: input.partialErrors ?? [],
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

import { create } from 'zustand'

import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'
import {
  createEmptyBasinDetail,
  createEmptyOverviewSummary,
  decideAggregationEndpoint,
  normalizeBasinDetail,
  normalizeBasinSegmentRows,
  normalizeLayerStates,
  normalizeOverviewBasins,
  normalizeOverviewSummary,
  normalizeSelectedSegmentDetail,
  type AggregationEndpointDecision,
  type ApiBasin,
  type ApiBasinVersion,
  type ApiFloodAlertRanking,
  type ApiFloodAlertSummary,
  type ApiFloodAlertTimeline,
  type ApiForecastPayload,
  type ApiHydroRun,
  type ApiHydroRunPage,
  type ApiLayer,
  type ApiLineageResponse,
  type ApiModelInstance,
  type ApiPipelineStatus,
  type ApiQueueDepth,
  type ApiRiverFeature,
  type ApiRiverFeatureCollection,
  type ApiRiverSegment,
  type BasinDetail,
  type BasinSegmentRow,
  type LayerState,
  type OverviewBasin,
  type OverviewSummary,
  type SelectedSegmentDetail,
} from '@/lib/m11/overviewDataContracts'
import type { M11QueryState } from '@/lib/m11/queryState'

type ModelInstancePage = components['schemas']['ModelInstancePage']

export interface OverviewDataSnapshot {
  basins: OverviewBasin[]
  summary: OverviewSummary
  layers: LayerState[]
  aggregationDecision: AggregationEndpointDecision
}

export interface BasinDataSnapshot {
  detail: BasinDetail
  segments: BasinSegmentRow[]
  selectedSegment: SelectedSegmentDetail | null
  layers: LayerState[]
}

interface OverviewDataState {
  overview: OverviewDataSnapshot | null
  basinDetail: BasinDataSnapshot | null
  loading: boolean
  basinLoading: boolean
  error: string | null
  basinError: string | null
  loadOverview: (query: M11QueryState) => Promise<OverviewDataSnapshot>
  loadBasinDetail: (basinId: string, query: M11QueryState) => Promise<BasinDataSnapshot>
  clearCache: () => void
}

type CacheEntry<T> = {
  promise?: Promise<T>
  value?: T
}

type OverviewRequestPlan = {
  baseRequestCount: number
  layerValidTimeRequestCount: number
  versionRequestCount: number
  pipelineRequestCount: number
  initialRequestCount: number
  createsPerBasinNPlusOne: boolean
  missingRequiredFields: string[]
  shouldFetchVersions: boolean
}

type ResolvedSegmentIdentifiers = {
  requestedId: string
  riverSegmentId: string
  segmentId: string
  detailEndpointSegmentId: string
  forecastSegmentId: string
  timelineSegmentId: string
  lineageSegmentId: string
  feature: ApiRiverFeature | null
  row: BasinSegmentRow | null
}

const COMPARE_FLOOD_SUMMARY_UNAVAILABLE = '对比模式洪水摘要需要 GFS+IFS 聚合端点'
const COMPARE_FLOOD_RANKING_UNAVAILABLE = '对比模式洪水排名需要 GFS+IFS 聚合端点'
const COMPARE_FLOOD_TIMELINE_UNAVAILABLE = '对比模式洪水时间线需要 GFS+IFS 聚合端点'
const COMPARE_LINEAGE_UNAVAILABLE = '对比模式河段追溯需要 GFS+IFS 聚合端点'

const cache = new Map<string, CacheEntry<unknown>>()
const CACHE_TTL_MS = 60_000
const OVERVIEW_INITIAL_REQUEST_THRESHOLD = 8
const overviewLoads = new Map<string, Promise<OverviewDataSnapshot>>()
const basinLoads = new Map<string, Promise<BasinDataSnapshot>>()
let overviewRequestNonce = 0
let basinRequestNonce = 0
let activeOverviewRequestKey: string | null = null
let activeBasinRequestKey: string | null = null

export function clearOverviewDataCache() {
  cache.clear()
  overviewLoads.clear()
  basinLoads.clear()
  overviewRequestNonce += 1
  basinRequestNonce += 1
  activeOverviewRequestKey = null
  activeBasinRequestKey = null
}

function cacheKey(path: string, params?: unknown) {
  return `${path}:${JSON.stringify(params ?? {})}`
}

async function cached<T>(key: string, loader: () => Promise<T>): Promise<T> {
  const existing = cache.get(key) as CacheEntry<T> | undefined
  if (existing?.value !== undefined) return existing.value
  if (existing?.promise) return existing.promise

  const promise = loader()
    .then((value) => {
      cache.set(key, { value })
      window.setTimeout?.(() => {
        const current = cache.get(key) as CacheEntry<T> | undefined
        if (current?.value === value) cache.delete(key)
      }, CACHE_TTL_MS)
      return value
    })
    .catch((error) => {
      cache.delete(key)
      throw error
    })

  cache.set(key, { promise })
  return promise
}

async function getApi<T>(path: string, options?: unknown, fallback = '请求失败') {
  const { data, error } = await (client.GET as (path: string, options?: unknown) => Promise<{ data?: unknown; error?: unknown }>)(
    path,
    options,
  )
  if (error) throw new Error(getApiErrorMessage(error, fallback))
  return unwrapApiData<T>(data, fallback)
}

function safeM11ErrorMessage(label: string, fallback = '暂不可用') {
  return `${label}: ${fallback}`
}

function settledValue<T>(result: PromiseSettledResult<T>, errors: string[], label: string): T | null {
  if (result.status === 'fulfilled') return result.value
  errors.push(safeM11ErrorMessage(label))
  return null
}

function latestPublishedRun(runs: ApiHydroRunPage | null): ApiHydroRun | null {
  const items = runs?.items ?? []
  return [...items].sort((a, b) => {
    const bTime = Date.parse(b.cycle_time ?? b.updated_at ?? b.created_at)
    const aTime = Date.parse(a.cycle_time ?? a.updated_at ?? a.created_at)
    return (Number.isFinite(bTime) ? bTime : 0) - (Number.isFinite(aTime) ? aTime : 0)
  })[0] ?? null
}

function shouldUseSingleRunFloodSurfaces(query: M11QueryState) {
  return query.source !== 'compare'
}

function sourceForApi(source: M11QueryState['source']) {
  if (source === 'ifs') return 'IFS'
  if (source === 'best') return 'best_available'
  if (source === 'compare') return undefined
  return 'GFS'
}

function layerIdsForOverview(query: M11QueryState) {
  return [...new Set([query.layer, 'flood-return-period'])]
}

function pipelineRequestEligible(query: M11QueryState) {
  return Boolean(query.cycle && query.source !== 'compare')
}

function buildOverviewRequestPlan(query: M11QueryState, basinCount: number, hasLatestRun: boolean): OverviewRequestPlan {
  const layerValidTimeRequestCount = layerIdsForOverview(query).length
  const pipelineRequestCount = pipelineRequestEligible(query) ? 1 : 0
  const baseRequestCount = 5 + (hasLatestRun ? 2 : 0)
  const initialWithoutVersions = baseRequestCount + pipelineRequestCount + layerValidTimeRequestCount
  const createsPerBasinNPlusOne = basinCount > 1
  const versionRequestCount = basinCount === 1 ? 1 : 0
  const missingRequiredFields = versionRequestCount === basinCount ? [] : ['basin_versions', 'basin_bbox']
  return {
    baseRequestCount,
    layerValidTimeRequestCount,
    versionRequestCount,
    pipelineRequestCount,
    initialRequestCount: initialWithoutVersions + versionRequestCount,
    createsPerBasinNPlusOne,
    missingRequiredFields,
    shouldFetchVersions: versionRequestCount === basinCount,
  }
}

function scenariosForQuery(source: M11QueryState['source']) {
  if (source === 'ifs') return 'forecast_ifs_deterministic'
  if (source === 'compare') return 'forecast_gfs_deterministic,forecast_ifs_deterministic'
  if (source === 'best') return 'forecast_best_available'
  return 'forecast_gfs_deterministic'
}

async function fetchBasins() {
  return cached(
    cacheKey('/api/v1/basins', { limit: 200, offset: 0 }),
    () =>
      getApi<ApiBasin[]>(
        '/api/v1/basins',
        { params: { query: { limit: 200, offset: 0 } } },
        '获取流域列表失败',
      ),
  )
}

async function fetchBasinVersions(basinId: string) {
  return cached(
    cacheKey('/api/v1/basins/{basin_id}/versions', { basinId }),
    () =>
      getApi<ApiBasinVersion[]>(
        '/api/v1/basins/{basin_id}/versions',
        { params: { path: { basin_id: basinId }, query: { limit: 50, offset: 0 } } },
        '获取流域版本失败',
      ),
  )
}

async function fetchModels(basinVersionId?: string) {
  return cached(
    cacheKey('/api/v1/models', { basinVersionId: basinVersionId ?? 'all-active' }),
    () =>
      getApi<ModelInstancePage>(
        '/api/v1/models',
        { params: { query: { basin_version_id: basinVersionId, active: 'true', limit: 200, offset: 0 } } },
        '获取模型资产失败',
      ),
  )
}

async function fetchRuns(query: M11QueryState, basinId?: string) {
  const source = sourceForApi(query.source)
  return cached(
    cacheKey('/api/v1/runs', { basinId, source, cycleTime: query.cycle ?? 'latest', status: 'frequency_done' }),
    () =>
      getApi<ApiHydroRunPage>(
        '/api/v1/runs',
        {
          params: {
            query: {
              basin_id: basinId,
              source,
              cycle_time: query.cycle ?? undefined,
              status: 'frequency_done',
              limit: 20,
              offset: 0,
            },
          },
        },
        '获取运行列表失败',
      ),
  )
}

async function fetchFloodSummary(runId: string, validTime: string | null) {
  return cached(
    cacheKey('/api/v1/flood-alerts/summary', { runId, validTime }),
    () =>
      getApi<ApiFloodAlertSummary>(
        '/api/v1/flood-alerts/summary',
        { params: { query: { run_id: runId, valid_time: validTime ?? undefined } } },
        '获取洪水预警摘要失败',
      ),
  )
}

async function fetchFloodRanking(runId: string, query: M11QueryState, basinId?: string) {
  return cached(
    cacheKey('/api/v1/flood-alerts/ranking', { runId, basinId, validTime: query.validTime }),
    () =>
      getApi<ApiFloodAlertRanking>(
        '/api/v1/flood-alerts/ranking',
        {
          params: {
            query: {
              run_id: runId,
              basin_id: basinId,
              valid_time: query.validTime ?? undefined,
              limit: 200,
              offset: 0,
            },
          },
        },
        '获取洪水预警排名失败',
      ),
  )
}

async function fetchPipelineStatus(query: M11QueryState) {
  if (!query.cycle || query.source === 'compare') return null
  return cached(
    cacheKey('/api/v1/pipeline/status', { source: sourceForApi(query.source), cycle: query.cycle }),
    () =>
      getApi<ApiPipelineStatus>(
        '/api/v1/pipeline/status',
        { params: { query: { source: sourceForApi(query.source) ?? 'GFS', cycle_time: query.cycle as string } } },
        '获取流水线状态失败',
      ),
  )
}

async function fetchQueueDepth() {
  return cached(cacheKey('/api/v1/queue/depth'), () => getApi<ApiQueueDepth>('/api/v1/queue/depth', undefined, '获取队列深度失败'))
}

async function fetchLayers() {
  return cached(
    cacheKey('/api/v1/layers', { limit: 100, offset: 0 }),
    () =>
      getApi<ApiLayer[]>(
        '/api/v1/layers',
        { params: { query: { limit: 100, offset: 0 } } },
        '获取图层列表失败',
      ),
  )
}

async function fetchLayerValidTimes(layerId: string) {
  return cached(
    cacheKey('/api/v1/layers/{layer_id}/valid-times', { layerId }),
    () =>
      getApi<string[]>(
        '/api/v1/layers/{layer_id}/valid-times',
        { params: { path: { layer_id: layerId } } },
        '获取图层有效时间失败',
      ),
  )
}

async function fetchRiverSegments(basinVersionId: string) {
  return cached(
    cacheKey('/api/v1/basin-versions/{basin_version_id}/river-segments', { basinVersionId, limit: 1000, offset: 0 }),
    () =>
      getApi<ApiRiverFeatureCollection>(
        '/api/v1/basin-versions/{basin_version_id}/river-segments',
        { params: { path: { basin_version_id: basinVersionId }, query: { limit: 1000, offset: 0 } } },
        '获取河段列表失败',
      ),
  )
}

async function fetchRiverSegment(basinVersionId: string, segmentId: string) {
  return cached(
    cacheKey('/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}', { basinVersionId, segmentId }),
    () =>
      getApi<ApiRiverSegment>(
        '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}',
        { params: { path: { basin_version_id: basinVersionId, segment_id: segmentId } } },
        '获取河段详情失败',
      ),
  )
}

async function fetchForecast(basinVersionId: string, segmentId: string, query: M11QueryState) {
  return cached(
    cacheKey('/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series', {
      basinVersionId,
      segmentId,
      source: query.source,
      cycle: query.cycle ?? 'latest',
    }),
    () =>
      getApi<ApiForecastPayload>(
        '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series',
        {
          params: {
            path: { basin_version_id: basinVersionId, segment_id: segmentId },
            query: {
              issue_time: query.cycle ?? 'latest',
              variables: 'q_down',
              scenarios: scenariosForQuery(query.source),
              include_analysis: true,
            },
          },
        },
        '获取河段预报失败',
      ),
  )
}

async function fetchFloodTimeline(runId: string, segmentId: string) {
  return cached(
    cacheKey('/api/v1/flood-alerts/timeline', { runId, segmentId }),
    () =>
      getApi<ApiFloodAlertTimeline>(
        '/api/v1/flood-alerts/timeline',
        { params: { query: { run_id: runId, segment_id: segmentId } } },
        '获取洪水预警时间线失败',
      ),
  )
}

async function fetchLineage(runId: string, segmentId: string, query: M11QueryState) {
  return cached(
    cacheKey('/api/v1/lineage/river-point', { runId, segmentId, validTime: query.validTime, variable: 'q_down' }),
    () =>
      getApi<ApiLineageResponse>(
        '/api/v1/lineage/river-point',
        { params: { query: { run_id: runId, segment_id: segmentId, valid_time: query.validTime ?? undefined, variable: 'q_down' } } },
        '获取河段追溯失败',
      ),
  )
}

export const useOverviewDataStore = create<OverviewDataState>((set) => ({
  overview: null,
  basinDetail: null,
  loading: false,
  basinLoading: false,
  error: null,
  basinError: null,
  clearCache: clearOverviewDataCache,
  loadOverview: async (query) => {
    const requestKey = cacheKey('overview', query)
    const existingLoad = overviewLoads.get(requestKey)
    if (existingLoad && activeOverviewRequestKey === requestKey) return existingLoad

    const requestNonce = ++overviewRequestNonce
    activeOverviewRequestKey = requestKey
    set({ loading: true, error: null })

    const load = (async () => {
      const partialErrors: string[] = []
      const [basinsResult, modelsResult, runsResult, layersResult, queueResult, pipelineResult] = await Promise.allSettled([
        fetchBasins(),
        fetchModels(),
        fetchRuns(query),
        fetchLayers(),
        fetchQueueDepth(),
        fetchPipelineStatus(query),
      ])
      const basins = settledValue(basinsResult, partialErrors, 'basins') ?? []
      const models = settledValue(modelsResult, partialErrors, 'models')?.items ?? []
      const runs = settledValue(runsResult, partialErrors, 'runs')
      const latestRun = latestPublishedRun(runs)
      const useSingleRunFloodSurfaces = shouldUseSingleRunFloodSurfaces(query)
      const layers = settledValue(layersResult, partialErrors, 'layers') ?? []
      const queue = settledValue(queueResult, partialErrors, 'queue')
      const pipeline = settledValue(pipelineResult, partialErrors, 'pipeline')
      const requestPlan = buildOverviewRequestPlan(query, basins.length, Boolean(latestRun && useSingleRunFloodSurfaces))

      if (!useSingleRunFloodSurfaces) {
        partialErrors.push(`flood summary: ${COMPARE_FLOOD_SUMMARY_UNAVAILABLE}`)
        partialErrors.push(`flood ranking: ${COMPARE_FLOOD_RANKING_UNAVAILABLE}`)
      }

      const [summaryResult, rankingResult, ...versionAndValidTimeResults] = await Promise.allSettled([
        latestRun && useSingleRunFloodSurfaces ? fetchFloodSummary(latestRun.run_id, query.validTime) : Promise.resolve(null),
        latestRun && useSingleRunFloodSurfaces ? fetchFloodRanking(latestRun.run_id, query) : Promise.resolve(null),
        ...(requestPlan.shouldFetchVersions ? basins.map((basin) => fetchBasinVersions(basin.basin_id)) : []),
        ...layerIdsForOverview(query).map((layerId) => fetchLayerValidTimes(layerId)),
      ])

      const floodSummary = settledValue(summaryResult, partialErrors, 'flood summary')
      const ranking = settledValue(rankingResult, partialErrors, 'flood ranking')
      const versionsByBasinId: Record<string, ApiBasinVersion[]> = {}
      const validTimeResults = versionAndValidTimeResults.slice(requestPlan.versionRequestCount)
      if (requestPlan.shouldFetchVersions) {
        basins.forEach((basin, index) => {
          versionsByBasinId[basin.basin_id] =
            settledValue(versionAndValidTimeResults[index] as PromiseSettledResult<ApiBasinVersion[]>, partialErrors, 'basin versions') ?? []
        })
      }
      const validTimesByLayerId: Record<string, string[]> = {}
      layerIdsForOverview(query).forEach((layerId, index) => {
        validTimesByLayerId[layerId] = settledValue(validTimeResults[index], partialErrors, `layer ${layerId} valid times`) ?? []
      })

      const overviewBasins = normalizeOverviewBasins({
        basins,
        versionsByBasinId,
        models: models as ApiModelInstance[],
        runs: runs?.items ?? [],
        rankingItems: ranking?.items ?? [],
      })
      const summary = normalizeOverviewSummary({
        query,
        basins: overviewBasins,
        floodSummary,
        ranking,
        pipeline,
        queue,
        latestRun: useSingleRunFloodSurfaces ? latestRun : null,
        runs: runs?.items ?? [],
        partialErrors,
      })
      const layerStates = normalizeLayerStates({ query, layers, validTimesByLayerId })
      const aggregationDecision = decideAggregationEndpoint(requestPlan)
      const snapshot: OverviewDataSnapshot = { basins: overviewBasins, summary, layers: layerStates, aggregationDecision }
      if (requestNonce === overviewRequestNonce && activeOverviewRequestKey === requestKey) {
        set({ overview: snapshot, loading: false, error: partialErrors[0] ?? null })
      }
      return snapshot
    })()

    overviewLoads.set(requestKey, load)

    try {
      return await load
    } catch (error) {
      if (requestNonce === overviewRequestNonce && activeOverviewRequestKey === requestKey) {
        const message = '加载总览数据失败'
        const fallback: OverviewDataSnapshot = {
          basins: [],
          summary: createEmptyOverviewSummary(query),
          layers: [],
          aggregationDecision: decideAggregationEndpoint({
            initialRequestCount: 0,
            createsPerBasinNPlusOne: false,
            missingRequiredFields: [],
          }),
        }
        set({ overview: fallback, loading: false, error: message })
      }
      throw error
    } finally {
      if (overviewLoads.get(requestKey) === load) overviewLoads.delete(requestKey)
    }
  },
  loadBasinDetail: async (basinId, query) => {
    const requestKey = cacheKey('basin-detail', { basinId, query })
    const existingLoad = basinLoads.get(requestKey)
    if (existingLoad && activeBasinRequestKey === requestKey) return existingLoad

    const requestNonce = ++basinRequestNonce
    activeBasinRequestKey = requestKey
    set({ basinLoading: true, basinError: null })

    const load = (async () => {
      const partialErrors: string[] = []
      const [basinsResult, versionsResult, runsResult, layersResult] = await Promise.allSettled([
        fetchBasins(),
        fetchBasinVersions(basinId),
        fetchRuns(query, basinId),
        fetchLayers(),
      ])
      const basins = settledValue(basinsResult, partialErrors, 'basins') ?? []
      const basin = basins.find((item) => item.basin_id === basinId) ?? null
      const versions = settledValue(versionsResult, partialErrors, 'basin versions') ?? []
      const runPage = settledValue(runsResult, partialErrors, 'runs')
      const latestRun = latestPublishedRun(runPage)
      const useSingleRunFloodSurfaces = shouldUseSingleRunFloodSurfaces(query)
      const layers = settledValue(layersResult, partialErrors, 'layers') ?? []
      const selectedVersion =
        versions.find((version) => version.basin_version_id === query.basinVersionId) ??
        versions.find((version) => version.active_flag) ??
        versions[0] ??
        null

      const [modelsResult, segmentsResult, rankingResult, ...validTimeResults] = await Promise.allSettled([
        selectedVersion ? fetchModels(selectedVersion.basin_version_id) : Promise.resolve(null),
        selectedVersion ? fetchRiverSegments(selectedVersion.basin_version_id) : Promise.resolve(null),
        latestRun && useSingleRunFloodSurfaces ? fetchFloodRanking(latestRun.run_id, query, basinId) : Promise.resolve(null),
        ...layerIdsForOverview(query).map((layerId) => fetchLayerValidTimes(layerId)),
      ])

      const models = settledValue(modelsResult, partialErrors, 'models')?.items ?? []
      const segments = settledValue(segmentsResult, partialErrors, 'river segments')
      const ranking = settledValue(rankingResult, partialErrors, 'flood ranking')
      if (!useSingleRunFloodSurfaces) {
        partialErrors.push(`flood ranking: ${COMPARE_FLOOD_RANKING_UNAVAILABLE}`)
      }
      const validTimesByLayerId: Record<string, string[]> = {}
      layerIdsForOverview(query).forEach((layerId, index) => {
        validTimesByLayerId[layerId] = settledValue(validTimeResults[index], partialErrors, `layer ${layerId} valid times`) ?? []
      })

      const detail = normalizeBasinDetail({
        query,
        basin,
        versions,
        models: models as ApiModelInstance[],
        segments,
        rankingItems: ranking?.items ?? [],
        latestRun: useSingleRunFloodSurfaces ? latestRun : null,
        runs: runPage?.items ?? [],
        partialErrors,
      })
      const rows = normalizeBasinSegmentRows({ query, featureCollection: segments, rankingItems: ranking?.items ?? [] })
      const selectedIdentifiers = resolveSelectedSegmentIdentifiers(query.segmentId, rows, segments)
      let selectedSegment: SelectedSegmentDetail | null = null

      if (selectedVersion && selectedIdentifiers) {
        if (!useSingleRunFloodSurfaces) {
          partialErrors.push(`flood timeline: ${COMPARE_FLOOD_TIMELINE_UNAVAILABLE}`)
          partialErrors.push(`lineage: ${COMPARE_LINEAGE_UNAVAILABLE}`)
        }
        const selectedRanking = ranking?.items.find(
          (item) =>
            item.river_segment_id === selectedIdentifiers.riverSegmentId ||
            item.segment_id === selectedIdentifiers.segmentId,
        )
        const [segmentResult, forecastResult, timelineResult] = await Promise.allSettled([
          fetchRiverSegment(selectedVersion.basin_version_id, selectedIdentifiers.detailEndpointSegmentId),
          fetchForecast(selectedVersion.basin_version_id, selectedIdentifiers.forecastSegmentId, query),
          latestRun && useSingleRunFloodSurfaces ? fetchFloodTimeline(latestRun.run_id, selectedIdentifiers.timelineSegmentId) : Promise.resolve(null),
        ])
        const segment = settledValue(segmentResult, partialErrors, 'river segment detail')
        const forecast = settledValue(forecastResult, partialErrors, 'forecast series')
        const timeline = settledValue(timelineResult, partialErrors, 'flood timeline')
        let lineage: ApiLineageResponse | null = null
        let lineageError: string | null = null
        const lineageUnavailableReason = useSingleRunFloodSurfaces ? null : COMPARE_LINEAGE_UNAVAILABLE
        if (latestRun && useSingleRunFloodSurfaces) {
          try {
            lineage = await fetchLineage(latestRun.run_id, selectedIdentifiers.lineageSegmentId, query)
          } catch (error) {
            lineageError = '河段追溯暂不可用'
            partialErrors.push(`lineage: ${lineageError}`)
          }
        }
        selectedSegment = normalizeSelectedSegmentDetail({
          query,
          basin,
          basinVersionId: selectedVersion.basin_version_id,
          segmentId: selectedIdentifiers.requestedId,
          segment,
          feature: selectedIdentifiers.feature,
          model: (models as ApiModelInstance[])[0] ?? null,
          forecast,
          floodTimeline: timeline,
          lineage,
          lineageError,
          lineageUnavailableReason,
          floodAlert: selectedRanking ?? null,
        })
      }

      const layerStates = normalizeLayerStates({ query, layers, validTimesByLayerId })
      const snapshot: BasinDataSnapshot = { detail, segments: rows, selectedSegment, layers: layerStates }
      if (requestNonce === basinRequestNonce && activeBasinRequestKey === requestKey) {
        set({ basinDetail: snapshot, basinLoading: false, basinError: partialErrors[0] ?? null })
      }
      return snapshot
    })()

    basinLoads.set(requestKey, load)

    try {
      return await load
    } catch (error) {
      if (requestNonce === basinRequestNonce && activeBasinRequestKey === requestKey) {
        const message = '加载流域数据失败'
        const fallback: BasinDataSnapshot = {
          detail: createEmptyBasinDetail(basinId, query),
          segments: [],
          selectedSegment: null,
          layers: [],
        }
        set({ basinDetail: fallback, basinLoading: false, basinError: message })
      }
      throw error
    } finally {
      if (basinLoads.get(requestKey) === load) basinLoads.delete(requestKey)
    }
  },
}))

function findFeature(collection: ApiRiverFeatureCollection | null, segmentId: string): ApiRiverFeature | null {
  return (
    collection?.features.find(
      (feature) => feature.properties.river_segment_id === segmentId || feature.properties.segment_id === segmentId,
    ) ?? null
  )
}

function resolveSelectedSegmentIdentifiers(
  querySegmentId: string | null,
  rows: BasinSegmentRow[],
  collection: ApiRiverFeatureCollection | null,
): ResolvedSegmentIdentifiers | null {
  const row = querySegmentId
    ? rows.find((item) => item.segmentId === querySegmentId || item.riverSegmentId === querySegmentId) ?? null
    : rows[0] ?? null
  const requestedId = querySegmentId ?? row?.riverSegmentId ?? null
  if (!requestedId) return null

  const feature = findFeature(collection, requestedId) ?? (!querySegmentId && row ? findFeature(collection, row.riverSegmentId) : null)
  if (querySegmentId && !row && !feature) return null

  const riverSegmentId = row?.riverSegmentId ?? feature?.properties.river_segment_id ?? requestedId
  const segmentId = row?.segmentId ?? feature?.properties.segment_id ?? requestedId

  return {
    requestedId,
    riverSegmentId,
    segmentId,
    detailEndpointSegmentId: riverSegmentId,
    forecastSegmentId: riverSegmentId,
    timelineSegmentId: riverSegmentId,
    lineageSegmentId: riverSegmentId,
    feature,
    row,
  }
}

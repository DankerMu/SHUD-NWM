import { create } from 'zustand'

import { apiFetch } from '@/api/base'
import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'
import { DEFAULT_FLOOD_RETURN_PERIOD_DURATION } from '@/lib/floodReturnPeriodDuration'
import {
  createEmptyBasinDetail,
  createEmptyOverviewSummary,
  decideAggregationEndpoint,
  filterBasinSegmentRows,
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
import { defaultM11QueryState, serializeM11QueryState, type M11QueryState } from '@/lib/m11/queryState'

export interface M11SnapshotRequestScope {
  queryKey: string
  dataKey: string
  source: M11QueryState['source']
  layer: M11QueryState['layer']
  cycle: string | null
  validTime: string | null
  basemap: M11QueryState['basemap']
  basinVersionId: string | null
  riverNetworkVersionId: string | null
  segmentId: string | null
  warningLevel: M11QueryState['warningLevel']
  q: string | null
}

export interface M11OverviewRequestScope extends M11SnapshotRequestScope {
  kind: 'overview'
}

export interface M11BasinRequestScope extends M11SnapshotRequestScope {
  kind: 'basin-detail'
  basinId: string
}

type ModelInstancePage = components['schemas']['ModelInstancePage']

export interface OverviewDataSnapshot {
  requestScope: M11OverviewRequestScope
  basins: OverviewBasin[]
  summary: OverviewSummary
  layers: LayerState[]
  aggregationDecision: AggregationEndpointDecision
}

export interface BasinDataSnapshot {
  requestScope: M11BasinRequestScope
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
  timeoutId?: number
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
  riverNetworkVersionId: string
  segmentId: string
  detailEndpointSegmentId: string
  detailEndpointRiverNetworkVersionId: string
  forecastSegmentId: string
  timelineSegmentId: string
  lineageSegmentId: string
  feature: ApiRiverFeature | null
  row: BasinSegmentRow | null
}

type BasinVersionRunFetchResult = {
  page: ApiHydroRunPage | null
  reachedCap: boolean
  failed: boolean
}

type ReadyRunStatusPages = Partial<Record<ReadyRunStatus, ApiHydroRunPage>>

type ReadyRunPage = ApiHydroRunPage & {
  readyStatusPages?: ReadyRunStatusPages
}

type ReadyRunCursor = {
  status: ReadyRunStatus
  offset: number
  total: number
}

type RiverSegmentFetchResult = {
  collection: ApiRiverFeatureCollection
  reachedCap: boolean
  truncated: boolean
}

type BasinActiveRiverNetwork = {
  model: ApiModelInstance | null
  riverNetworkVersionId: string | null
}

const COMPARE_FLOOD_SUMMARY_UNAVAILABLE = '对比模式洪水摘要需要 GFS+IFS 聚合端点'
const COMPARE_FLOOD_RANKING_UNAVAILABLE = '对比模式洪水排名需要 GFS+IFS 聚合端点'
const COMPARE_FLOOD_TIMELINE_UNAVAILABLE = '对比模式洪水时间线需要 GFS+IFS 聚合端点'
const COMPARE_LINEAGE_UNAVAILABLE = '对比模式河段追溯需要 GFS+IFS 聚合端点'
const RUN_LOOKUP_PAGE_LIMIT = 200
const RUN_LOOKUP_MAX_EXTRA_PAGES = 5
const RUN_LOOKUP_MAX_RETAINED_ITEMS = 1_000
const RIVER_SEGMENT_PAGE_LIMIT = 1_000
const RIVER_SEGMENT_MAX_PAGES = 10
const RIVER_SEGMENT_MAX_ITEMS = 10_000
const READY_RUN_STATUSES = ['frequency_done', 'published'] as const
type ReadyRunStatus = (typeof READY_RUN_STATUSES)[number]

const cache = new Map<string, CacheEntry<unknown>>()
const CACHE_TTL_MS = 60_000
const CACHE_MAX_ENTRIES = 64
const OVERVIEW_INITIAL_REQUEST_THRESHOLD = 8
const overviewLoads = new Map<string, Promise<OverviewDataSnapshot>>()
const basinLoads = new Map<string, Promise<BasinDataSnapshot>>()
let overviewRequestNonce = 0
let basinRequestNonce = 0
let activeOverviewRequestKey: string | null = null
let activeBasinRequestKey: string | null = null
let cacheGeneration = 0

export function clearOverviewDataCache() {
  cacheGeneration += 1
  for (const key of cache.keys()) {
    deleteCacheEntry(key)
  }
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

function requestScopeQueryKey(query: M11QueryState) {
  // basinId 由 requestScope.basinId 单独匹配，故从序列化键中剔除：
  // 加 basinId 字段后键的输出与改动前字节完全一致，零缓存 churn（R1 缓解）。
  return serializeM11QueryState({ ...query, basinId: null, basemap: defaultM11QueryState.basemap, validTime: null })
}

function requestScopeDataKey(query: M11QueryState) {
  return serializeM11QueryState({ ...query, basinId: null, basemap: defaultM11QueryState.basemap })
}

function basinRequestIdentityQuery(query: M11QueryState): M11QueryState {
  return { ...query, warningLevel: null, q: null }
}

function overviewRequestScope(query: M11QueryState): M11OverviewRequestScope {
  return {
    kind: 'overview',
    queryKey: requestScopeQueryKey(query),
    dataKey: requestScopeDataKey(query),
    source: query.source,
    layer: query.layer,
    cycle: query.cycle,
    validTime: query.validTime,
    basemap: query.basemap,
    basinVersionId: query.basinVersionId,
    riverNetworkVersionId: query.riverNetworkVersionId,
    segmentId: query.segmentId,
    warningLevel: query.warningLevel,
    q: query.q,
  }
}

function basinRequestScope(basinId: string, query: M11QueryState): M11BasinRequestScope {
  return {
    ...overviewRequestScope(query),
    kind: 'basin-detail',
    basinId,
  }
}

export function overviewSnapshotMatchesQuery(snapshot: OverviewDataSnapshot | null | undefined, query: M11QueryState) {
  return snapshot?.requestScope?.dataKey === requestScopeDataKey(query)
}

export function overviewSnapshotMetadataMatchesQuery(snapshot: OverviewDataSnapshot | null | undefined, query: M11QueryState) {
  return snapshot?.requestScope?.queryKey === requestScopeQueryKey(query)
}

export function basinSnapshotMatchesQuery(
  snapshot: BasinDataSnapshot | null | undefined,
  basinId: string,
  query: M11QueryState,
) {
  return snapshot?.requestScope?.kind === 'basin-detail' &&
    snapshot.requestScope.basinId === basinId &&
    snapshot.requestScope.dataKey === requestScopeDataKey(basinRequestIdentityQuery(query))
}

export function basinSnapshotMetadataMatchesQuery(
  snapshot: BasinDataSnapshot | null | undefined,
  basinId: string,
  query: M11QueryState,
) {
  return snapshot?.requestScope?.kind === 'basin-detail' &&
    snapshot.requestScope.basinId === basinId &&
    snapshot.requestScope.queryKey === requestScopeQueryKey(basinRequestIdentityQuery(query))
}

function deleteCacheEntry(key: string) {
  const existing = cache.get(key)
  if (existing?.timeoutId !== undefined) window.clearTimeout?.(existing.timeoutId)
  cache.delete(key)
}

function setCacheEntry<T>(key: string, entry: CacheEntry<T>) {
  const existing = cache.get(key)
  if (existing?.timeoutId !== undefined) window.clearTimeout?.(existing.timeoutId)
  cache.set(key, entry as CacheEntry<unknown>)

  while (cache.size > CACHE_MAX_ENTRIES) {
    const oldestKey = cache.keys().next().value as string | undefined
    if (!oldestKey) break
    deleteCacheEntry(oldestKey)
  }
}

async function cached<T>(key: string, loader: () => Promise<T>): Promise<T> {
  const existing = cache.get(key) as CacheEntry<T> | undefined
  if (existing?.value !== undefined) return existing.value
  if (existing?.promise) return existing.promise

  const generation = cacheGeneration
  const promise = loader()
    .then((value) => {
      if (generation !== cacheGeneration) return value
      const timeoutId = window.setTimeout?.(() => {
        const current = cache.get(key) as CacheEntry<T> | undefined
        if (current?.value === value) deleteCacheEntry(key)
      }, CACHE_TTL_MS)
      setCacheEntry(key, { value, timeoutId })
      return value
    })
    .catch((error) => {
      if (generation === cacheGeneration) deleteCacheEntry(key)
      throw error
    })

  setCacheEntry(key, { promise })
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

function latestPublishedRun(runs: ApiHydroRunPage | null, query?: M11QueryState): ApiHydroRun | null {
  const items = runs?.items ?? []
  const readyRuns = items.filter(isReadyFloodRun)
  const candidates = query?.source === 'best' ? readyRuns.filter((run) => concreteSourceFromRun(run)) : readyRuns
  return [...candidates].sort((a, b) => {
    const bCycleTime = Date.parse(b.cycle_time ?? '')
    const aCycleTime = Date.parse(a.cycle_time ?? '')
    const cycleOrder = (Number.isFinite(bCycleTime) ? bCycleTime : 0) - (Number.isFinite(aCycleTime) ? aCycleTime : 0)
    if (cycleOrder !== 0) return cycleOrder

    const bUpdateTime = Date.parse(b.updated_at ?? b.created_at)
    const aUpdateTime = Date.parse(a.updated_at ?? a.created_at)
    return (Number.isFinite(bUpdateTime) ? bUpdateTime : 0) - (Number.isFinite(aUpdateTime) ? aUpdateTime : 0)
  })[0] ?? null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function isReadyFloodRun(run: ApiHydroRun) {
  const quality = run.product_quality?.flood_return_period
  return isRecord(quality) && quality.quality_state === 'ready'
}

function mergeRunPages(pages: ApiHydroRunPage[], statuses: readonly ReadyRunStatus[] = READY_RUN_STATUSES): ReadyRunPage {
  const byRunId = new Map<string, ApiHydroRun>()
  pages.forEach((page) => {
    page.items.forEach((run) => {
      if (!isReadyFloodRun(run)) return
      byRunId.set(run.run_id, run)
    })
  })
  const limit = Math.max(...pages.map((page) => page.limit ?? page.items.length), 0)
  const total = Math.max(...pages.map((page) => page.total ?? page.items.length), byRunId.size)
  const readyStatusPages = statuses.reduce<ReadyRunStatusPages>((acc, status, index) => {
    const page = pages[index]
    if (page) acc[status] = page
    return acc
  }, {})
  return {
    items: [...byRunId.values()],
    total,
    limit,
    offset: pages[0]?.offset ?? 0,
    readyStatusPages,
  }
}

function latestPublishedRunForBasinVersion(
  runs: ApiHydroRunPage | null,
  basinVersionId: string | null | undefined,
  query?: M11QueryState,
): ApiHydroRun | null {
  if (!basinVersionId) return null
  return latestPublishedRun(
    {
      items: (runs?.items ?? []).filter((run) => run.basin_version_id === basinVersionId),
      total: runs?.total ?? 0,
      limit: runs?.limit ?? 0,
      offset: runs?.offset ?? 0,
    },
    query,
  )
}

function shouldUseSingleRunFloodSurfaces(query: M11QueryState) {
  return query.source !== 'compare'
}

function sourceForApi(source: M11QueryState['source']) {
  if (source === 'ifs') return 'IFS'
  if (source === 'best') return undefined
  if (source === 'compare') return undefined
  return 'GFS'
}

function concreteSourceFromRun(run: ApiHydroRun | null | undefined): 'gfs' | 'ifs' | null {
  const value = `${run?.source_id ?? ''} ${run?.scenario_id ?? ''}`.toLowerCase()
  if (value.includes('ifs')) return 'ifs'
  if (value.includes('gfs')) return 'gfs'
  return null
}

function concreteQueryForSurfaces(query: M11QueryState, run: ApiHydroRun | null): M11QueryState {
  if (query.source !== 'best') return query
  const source = concreteSourceFromRun(run)
  return source ? { ...query, source } : query
}

function hasResolvedSurfaceSource(query: M11QueryState, run: ApiHydroRun | null): boolean {
  return query.source !== 'best' || Boolean(concreteSourceFromRun(run))
}

function resolveActiveRiverNetwork(models: ApiModelInstance[], latestRun: ApiHydroRun | null): BasinActiveRiverNetwork {
  const runModel = latestRun?.model_id ? models.find((model) => model.model_id === latestRun.model_id) : null
  const selectedModel = runModel ?? models[0] ?? null
  return {
    model: selectedModel,
    riverNetworkVersionId: selectedModel?.river_network_version_id ?? null,
  }
}

async function resolveBasinRiverNetwork(
  models: ApiModelInstance[],
  latestRun: ApiHydroRun | null,
  errors: string[],
): Promise<BasinActiveRiverNetwork> {
  const runModel = latestRun?.model_id ? models.find((model) => model.model_id === latestRun.model_id) : null
  if (runModel || !latestRun?.model_id) return resolveActiveRiverNetwork(models, latestRun)

  try {
    const exactRunModel = await fetchModel(latestRun.model_id)
    return {
      model: exactRunModel,
      riverNetworkVersionId: exactRunModel.river_network_version_id ?? null,
    }
  } catch {
    errors.push(safeM11ErrorMessage('model detail'))
    return resolveActiveRiverNetwork(models, latestRun)
  }
}

function runsForSourceSelection(query: M11QueryState, runs: ApiHydroRun[], latestRun: ApiHydroRun | null): ApiHydroRun[] {
  return query.source === 'best' ? (latestRun ? [latestRun] : []) : runs
}

function layerIdsForOverview(query: M11QueryState) {
  return [...new Set([query.layer, 'flood-return-period'])]
}

function pipelineRequestParams(query: M11QueryState, run: ApiHydroRun | null = null): { source: string; cycle: string } | null {
  if (query.source === 'compare') return null
  const concreteQuery = concreteQueryForSurfaces(query, run)
  const source = sourceForApi(concreteQuery.source)
  const cycle = query.cycle ?? (query.source === 'best' ? run?.cycle_time : null)
  return source && cycle ? { source, cycle } : null
}

function buildOverviewRequestPlan(
  query: M11QueryState,
  basinCount: number,
  hasLatestRun: boolean,
  hasPipelineRequest: boolean,
): OverviewRequestPlan {
  const layerValidTimeRequestCount = layerIdsForOverview(query).length
  const pipelineRequestCount = hasPipelineRequest ? 1 : 0
  const baseRequestCount = 5 + (hasLatestRun ? 2 : 0)
  const initialWithoutVersions = baseRequestCount + pipelineRequestCount + layerValidTimeRequestCount
  const createsPerBasinNPlusOne = basinCount > 1
  const plannedVersionRequestCount = basinCount === 1 ? 1 : 0
  const initialRequestCount = initialWithoutVersions + plannedVersionRequestCount
  const shouldFetchVersions =
    plannedVersionRequestCount === basinCount &&
    !createsPerBasinNPlusOne &&
    initialRequestCount <= OVERVIEW_INITIAL_REQUEST_THRESHOLD
  const versionRequestCount = shouldFetchVersions ? plannedVersionRequestCount : 0
  const missingRequiredFields = basinCount > 1 ? ['basin_versions', 'basin_bbox'] : []
  return {
    baseRequestCount,
    layerValidTimeRequestCount,
    versionRequestCount,
    pipelineRequestCount,
    initialRequestCount,
    createsPerBasinNPlusOne,
    missingRequiredFields,
    shouldFetchVersions,
  }
}

function scenariosForQuery(source: M11QueryState['source']) {
  if (source === 'ifs') return 'forecast_ifs_deterministic'
  if (source === 'compare') return 'forecast_gfs_deterministic,forecast_ifs_deterministic'
  if (source === 'best') return null
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
    cacheKey('/api/v1/models', { basinVersionId: basinVersionId ?? 'all', active: 'true' }),
    () =>
      getApi<ModelInstancePage>(
        '/api/v1/models',
        { params: { query: { basin_version_id: basinVersionId, active: 'true', limit: 200, offset: 0 } } },
        '获取模型资产失败',
      ),
  )
}

async function fetchModel(modelId: string) {
  return cached(
    cacheKey('/api/v1/models/{model_id}', { modelId }),
    () =>
      getApi<ApiModelInstance>(
        '/api/v1/models/{model_id}',
        { params: { path: { model_id: modelId } } },
        '获取模型资产详情失败',
      ),
  )
}

async function fetchRunsPageByStatus(
  query: M11QueryState,
  basinId: string | undefined,
  limit: number,
  offset: number,
  status: ReadyRunStatus,
) {
  const source = sourceForApi(query.source)
  return cached(
    cacheKey('/api/v1/runs', { basinId, source, cycleTime: query.cycle ?? 'latest', status, limit, offset }),
    () =>
      getApi<ApiHydroRunPage>(
        '/api/v1/runs',
        {
          params: {
            query: {
              basin_id: basinId,
              source,
              cycle_time: query.cycle ?? undefined,
              status,
              flood_product_ready: true,
              limit,
              offset,
            },
          },
        },
        '获取运行列表失败',
      ),
  )
}

async function fetchRunsPage(query: M11QueryState, basinId: string | undefined, limit: number, offset: number) {
  const pages = await Promise.all(READY_RUN_STATUSES.map((status) => fetchRunsPageByStatus(query, basinId, limit, offset, status)))
  return mergeRunPages(pages)
}

async function fetchRuns(query: M11QueryState, basinId?: string) {
  return fetchRunsPage(query, basinId, 20, 0)
}

async function fetchRunsForBasinVersion(
  query: M11QueryState,
  basinId: string,
  basinVersionId: string | null | undefined,
  initialPage: ReadyRunPage | null,
): Promise<BasinVersionRunFetchResult> {
  if (!basinVersionId || !initialPage) return { page: initialPage, reachedCap: false, failed: false }

  const initialStatusPages = initialPage.readyStatusPages ?? { frequency_done: initialPage }
  const byRunId = new Map<string, ApiHydroRun>()
  const addPageItems = (page: ApiHydroRunPage) => {
    for (const run of page.items) {
      if (run.basin_version_id !== basinVersionId) continue
      if (byRunId.size >= RUN_LOOKUP_MAX_RETAINED_ITEMS && !byRunId.has(run.run_id)) break
      byRunId.set(run.run_id, run)
    }
  }
  READY_RUN_STATUSES.forEach((status) => {
    const page = initialStatusPages[status]
    if (page) addPageItems(page)
  })
  const pageFromItems = (offset = initialPage.offset ?? 0): ApiHydroRunPage => ({
    items: [...byRunId.values()],
    total: Math.max(...READY_RUN_STATUSES.map((status) => initialStatusPages[status]?.total ?? 0), byRunId.size),
    limit: byRunId.size,
    offset,
  })
  const cursors: ReadyRunCursor[] = READY_RUN_STATUSES.flatMap((status) => {
    const page = initialStatusPages[status]
    if (!page) return []
    const pageOffset = page.offset ?? 0
    const fetched = page.limit || page.items.length || 20
    return [
      {
        status,
        offset: pageOffset + fetched,
        total: page.total ?? page.items.length,
      },
    ]
  })
  let page: ApiHydroRunPage = {
    ...pageFromItems(),
  }
  if (latestPublishedRunForBasinVersion(page, basinVersionId, query)) return { page, reachedCap: false, failed: false }

  let extraPages = 0
  let reachedCap = false

  while (
    cursors.some((cursor) => cursor.offset < cursor.total) &&
    extraPages < RUN_LOOKUP_MAX_EXTRA_PAGES &&
    byRunId.size < RUN_LOOKUP_MAX_RETAINED_ITEMS
  ) {
    let nextPages: Array<{ cursor: ReadyRunCursor; page: ApiHydroRunPage }>
    try {
      nextPages = await Promise.all(
        cursors
          .filter((cursor) => cursor.offset < cursor.total)
          .map(async (cursor) => ({
            cursor,
            page: await fetchRunsPageByStatus(query, basinId, RUN_LOOKUP_PAGE_LIMIT, cursor.offset, cursor.status),
          })),
      )
    } catch {
      return { page, reachedCap: false, failed: true }
    }
    extraPages += 1
    nextPages.forEach(({ cursor, page: statusPage }) => {
      addPageItems(statusPage)
      cursor.total = statusPage.total ?? cursor.total
      const fetched = statusPage.limit || statusPage.items.length || RUN_LOOKUP_PAGE_LIMIT
      cursor.offset += fetched
    })
    page = pageFromItems()
    if (latestPublishedRunForBasinVersion(page, basinVersionId, query)) return { page, reachedCap: false, failed: false }
    if (nextPages.every(({ page: statusPage }) => (statusPage.limit || statusPage.items.length || RUN_LOOKUP_PAGE_LIMIT) <= 0)) break
  }

  reachedCap =
    cursors.some((cursor) => cursor.offset < cursor.total) &&
    (extraPages >= RUN_LOOKUP_MAX_EXTRA_PAGES || byRunId.size >= RUN_LOOKUP_MAX_RETAINED_ITEMS)
  return { page, reachedCap, failed: false }
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

async function fetchPipelineStatus(query: M11QueryState, run: ApiHydroRun | null = null) {
  const params = pipelineRequestParams(query, run)
  if (!params) return null
  return cached(
    cacheKey('/api/v1/pipeline/status', params),
    () =>
      getApi<ApiPipelineStatus>(
        '/api/v1/pipeline/status',
        { params: { query: { source: params.source, cycle_time: params.cycle } } },
        '获取流水线状态失败',
      ),
  )
}

async function fetchQueueDepth() {
  return cached(cacheKey('/api/v1/queue/depth'), () => getApi<ApiQueueDepth>('/api/v1/queue/depth', undefined, '获取队列深度失败'))
}

async function fetchLayers(runId?: string | null) {
  const query = { limit: 100, offset: 0, runId: runId ?? null }
  return cached(
    cacheKey('/api/v1/layers', query),
    () =>
      getApi<ApiLayer[]>(
        '/api/v1/layers',
        { params: { query: { limit: 100, offset: 0, run_id: runId ?? undefined } } },
        '获取图层列表失败',
      ).catch(
        async () => {
          const params = new URLSearchParams({ limit: '100', offset: '0' })
          if (runId) params.set('run_id', runId)
          const response = await apiFetch(`/api/v1/layers?${params.toString()}`)
          if (!response.ok) throw new Error('获取图层列表失败')
          return unwrapApiData<ApiLayer[]>(await response.json(), '获取图层列表失败')
        },
      ),
  )
}

async function fetchLayerValidTimes(layerId: string, runId?: string | null) {
  const duration = layerId === 'flood-return-period' || layerId === 'warning-level' ? DEFAULT_FLOOD_RETURN_PERIOD_DURATION : null
  return cached(
    cacheKey('/api/v1/layers/{layer_id}/valid-times', { layerId, runId: runId ?? null, duration }),
    () =>
      getApi<components['schemas']['LayerValidTimes'] | string[]>(
        '/api/v1/layers/{layer_id}/valid-times',
        { params: { path: { layer_id: layerId }, query: { run_id: runId ?? undefined, duration: duration ?? undefined } } },
        '获取图层有效时间失败',
      )
        .then(normalizeLayerValidTimesResponse)
        .catch(async () => {
          const params = new URLSearchParams()
          if (runId) params.set('run_id', runId)
          if (duration) params.set('duration', duration)
          const suffix = params.size > 0 ? `?${params.toString()}` : ''
          const response = await apiFetch(`/api/v1/layers/${encodeURIComponent(layerId)}/valid-times${suffix}`)
          if (!response.ok) throw new Error('获取图层有效时间失败')
          return normalizeLayerValidTimesResponse(
            unwrapApiData<components['schemas']['LayerValidTimes'] | string[]>(
              await response.json(),
              '获取图层有效时间失败',
            ),
          )
        }),
  )
}

function normalizeLayerValidTimesResponse(value: components['schemas']['LayerValidTimes'] | string[]): string[] {
  return Array.isArray(value) ? value : value.valid_times
}

async function fetchRiverSegmentsPage(
  basinVersionId: string,
  riverNetworkVersionId: string | null,
  limit: number,
  offset: number,
) {
  return cached(
    cacheKey('/api/v1/basin-versions/{basin_version_id}/river-segments', {
      basinVersionId,
      riverNetworkVersionId: riverNetworkVersionId ?? 'all',
      limit,
      offset,
    }),
    () =>
      getApi<ApiRiverFeatureCollection>(
        '/api/v1/basin-versions/{basin_version_id}/river-segments',
        {
          params: {
            path: { basin_version_id: basinVersionId },
            query: { river_network_version_id: riverNetworkVersionId ?? undefined, limit, offset },
          },
        },
        '获取河段列表失败',
      ),
  )
}

function containsSegment(collection: ApiRiverFeatureCollection, segmentId: string | null): boolean {
  return Boolean(
    segmentId &&
      collection.features.some(
        (feature) => feature.properties.river_segment_id === segmentId || feature.properties.segment_id === segmentId,
      ),
  )
}

async function fetchRiverSegments(
  basinVersionId: string,
  riverNetworkVersionId: string | null,
  segmentId: string | null,
): Promise<RiverSegmentFetchResult> {
  const firstPage = await fetchRiverSegmentsPage(basinVersionId, riverNetworkVersionId, RIVER_SEGMENT_PAGE_LIMIT, 0)
  const total = firstPage.total ?? firstPage.feature_total ?? firstPage.features.length
  const firstPageFeatures = firstPage.features.slice(0, RIVER_SEGMENT_MAX_ITEMS)
  const truncated = firstPage.features.length > firstPageFeatures.length
  const features = [...firstPageFeatures]
  let collection: ApiRiverFeatureCollection = {
    ...firstPage,
    features,
    total,
    feature_total: firstPage.feature_total ?? total,
    limit: features.length,
    offset: 0,
  }
  let reportedTotal = total
  let offset = (firstPage.offset ?? 0) + (firstPage.limit || firstPage.features.length || RIVER_SEGMENT_PAGE_LIMIT)
  let pages = 1
  const shouldFindRequestedSegment = Boolean(segmentId)

  while (
    shouldFindRequestedSegment &&
    offset < reportedTotal &&
    pages < RIVER_SEGMENT_MAX_PAGES &&
    features.length < RIVER_SEGMENT_MAX_ITEMS &&
    !containsSegment(collection, segmentId)
  ) {
    const nextPage = await fetchRiverSegmentsPage(basinVersionId, riverNetworkVersionId, RIVER_SEGMENT_PAGE_LIMIT, offset)
    pages += 1
    const remaining = RIVER_SEGMENT_MAX_ITEMS - features.length
    features.push(...nextPage.features.slice(0, remaining))
    reportedTotal = nextPage.total ?? nextPage.feature_total ?? reportedTotal
    collection = {
      ...nextPage,
      features,
      total: reportedTotal,
      feature_total: nextPage.feature_total ?? reportedTotal,
      limit: features.length,
      offset: 0,
    }

    const fetched = nextPage.limit || nextPage.features.length || RIVER_SEGMENT_PAGE_LIMIT
    offset += fetched
    if (fetched <= 0) break
  }

  const reachedCap =
    shouldFindRequestedSegment &&
    (offset < reportedTotal || truncated) &&
    !containsSegment(collection, segmentId) &&
    (pages >= RIVER_SEGMENT_MAX_PAGES || features.length >= RIVER_SEGMENT_MAX_ITEMS)
  return { collection, reachedCap, truncated }
}

async function fetchRiverSegment(basinVersionId: string, riverNetworkVersionId: string, segmentId: string) {
  return cached(
    cacheKey('/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}', {
      basinVersionId,
      riverNetworkVersionId,
      segmentId,
    }),
    () =>
      getApi<ApiRiverSegment>(
        '/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}',
        {
          params: {
            path: { basin_version_id: basinVersionId, segment_id: segmentId },
            query: { river_network_version_id: riverNetworkVersionId },
          },
        },
        '获取河段详情失败',
      ),
  )
}

async function fetchForecast(basinVersionId: string, riverNetworkVersionId: string, segmentId: string, query: M11QueryState) {
  const scenarios = scenariosForQuery(query.source)
  if (!scenarios) return null

  return cached(
    cacheKey('/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series', {
      basinVersionId,
      riverNetworkVersionId,
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
              river_network_version_id: riverNetworkVersionId,
              issue_time: query.cycle ?? 'latest',
              variables: 'q_down',
              scenarios,
              include_analysis: true,
            },
          },
        },
        '获取河段预报失败',
      ),
  )
}

async function fetchFloodTimeline(runId: string, segmentId: string, riverNetworkVersionId: string) {
  return cached(
    cacheKey('/api/v1/flood-alerts/timeline', { runId, segmentId, riverNetworkVersionId }),
    () =>
      getApi<ApiFloodAlertTimeline>(
        '/api/v1/flood-alerts/timeline',
        { params: { query: { run_id: runId, segment_id: segmentId, river_network_version_id: riverNetworkVersionId } } },
        '获取洪水预警时间线失败',
      ),
  )
}

function rankingMatchesSelectedSegment(item: ApiFloodAlertRanking['items'][number], selected: ResolvedSegmentIdentifiers): boolean {
  return (
    item.river_network_version_id === selected.riverNetworkVersionId &&
    (item.river_segment_id === selected.riverSegmentId || item.segment_id === selected.segmentId)
  )
}

async function fetchLineage(runId: string, riverNetworkVersionId: string, segmentId: string, query: M11QueryState) {
  return cached(
    cacheKey('/api/v1/lineage/river-point', { runId, riverNetworkVersionId, segmentId, validTime: query.validTime, variable: 'q_down' }),
    () =>
      getApi<ApiLineageResponse>(
        '/api/v1/lineage/river-point',
        {
          params: {
            query: {
              run_id: runId,
              river_network_version_id: riverNetworkVersionId,
              segment_id: segmentId,
              valid_time: query.validTime ?? undefined,
              variable: 'q_down',
            },
          },
        },
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
      const [basinsResult, modelsResult, runsResult, queueResult] = await Promise.allSettled([
        fetchBasins(),
        fetchModels(),
        fetchRuns(query),
        fetchQueueDepth(),
      ])
      const basins = settledValue(basinsResult, partialErrors, 'basins') ?? []
      const models = settledValue(modelsResult, partialErrors, 'models')?.items ?? []
      const runs = settledValue(runsResult, partialErrors, 'runs')
      const latestRun = latestPublishedRun(runs, query)
      const useSingleRunFloodSurfaces = shouldUseSingleRunFloodSurfaces(query)
      const [layersResult] = await Promise.allSettled([fetchLayers(useSingleRunFloodSurfaces ? latestRun?.run_id : null)])
      const layers = settledValue(layersResult, partialErrors, 'layers') ?? []
      const queue = settledValue(queueResult, partialErrors, 'queue')
      const requestPlan = buildOverviewRequestPlan(
        query,
        basins.length,
        Boolean(latestRun && useSingleRunFloodSurfaces),
        Boolean(pipelineRequestParams(query, latestRun)),
      )

      if (!useSingleRunFloodSurfaces) {
        partialErrors.push(`flood summary: ${COMPARE_FLOOD_SUMMARY_UNAVAILABLE}`)
        partialErrors.push(`flood ranking: ${COMPARE_FLOOD_RANKING_UNAVAILABLE}`)
      }

      const concreteSurfaceQuery = concreteQueryForSurfaces(query, latestRun)
      const [pipelineResult, summaryResult, rankingResult, ...versionAndValidTimeResults] = await Promise.allSettled([
        fetchPipelineStatus(query, latestRun),
        latestRun && useSingleRunFloodSurfaces ? fetchFloodSummary(latestRun.run_id, query.validTime) : Promise.resolve(null),
        latestRun && useSingleRunFloodSurfaces ? fetchFloodRanking(latestRun.run_id, concreteSurfaceQuery) : Promise.resolve(null),
        ...(requestPlan.shouldFetchVersions ? basins.map((basin) => fetchBasinVersions(basin.basin_id)) : []),
        ...layerIdsForOverview(query).map((layerId) =>
          fetchLayerValidTimes(layerId, useSingleRunFloodSurfaces ? latestRun?.run_id : null),
        ),
      ])

      const pipeline = settledValue(pipelineResult, partialErrors, 'pipeline')
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
        basinVersionUnavailableReason:
          basins.length > 0 && !requestPlan.shouldFetchVersions ? 'Basin version and bbox require the M11 aggregation endpoint.' : null,
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
        runs: runsForSourceSelection(query, runs?.items ?? [], latestRun),
        partialErrors,
      })
      const layerStates = normalizeLayerStates({
        query: concreteSurfaceQuery,
        layers,
        validTimesByLayerId,
        resolvedRun: useSingleRunFloodSurfaces ? latestRun : null,
      })
      const aggregationDecision = decideAggregationEndpoint(requestPlan)
      const snapshot: OverviewDataSnapshot = {
        requestScope: overviewRequestScope(query),
        basins: overviewBasins,
        summary,
        layers: layerStates,
        aggregationDecision,
      }
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
          requestScope: overviewRequestScope(query),
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
    const requestQuery = basinRequestIdentityQuery(query)
    const requestKey = cacheKey('basin-detail', { basinId, query: requestQuery })
    const existingLoad = basinLoads.get(requestKey)
    if (existingLoad && activeBasinRequestKey === requestKey) return existingLoad

    const requestNonce = ++basinRequestNonce
    activeBasinRequestKey = requestKey
    set({ basinLoading: true, basinError: null })

    const load = (async () => {
      const partialErrors: string[] = []
      const [basinsResult, versionsResult, runsResult] = await Promise.allSettled([
        fetchBasins(),
        fetchBasinVersions(basinId),
        fetchRuns(requestQuery, basinId),
      ])
      const basinLookupAvailable = basinsResult.status === 'fulfilled'
      const basins = settledValue(basinsResult, partialErrors, 'basins') ?? []
      const basin = basins.find((item) => item.basin_id === basinId) ?? null
      const versions = settledValue(versionsResult, partialErrors, 'basin versions') ?? []
      const runPage = settledValue(runsResult, partialErrors, 'runs')
      const selectedVersion =
        versions.find((version) => version.basin_version_id === query.basinVersionId) ??
        versions.find((version) => version.active_flag) ??
        versions[0] ??
        null
      const versionRunsResult = await fetchRunsForBasinVersion(requestQuery, basinId, selectedVersion?.basin_version_id, runPage)
      const versionCompleteRunPage = versionRunsResult.page
      const latestRun = latestPublishedRunForBasinVersion(versionCompleteRunPage, selectedVersion?.basin_version_id, requestQuery)
      const concreteSurfaceQuery = concreteQueryForSurfaces(requestQuery, latestRun)
      const useSingleRunFloodSurfaces = shouldUseSingleRunFloodSurfaces(requestQuery)
      const [layersResult] = await Promise.allSettled([fetchLayers(useSingleRunFloodSurfaces ? latestRun?.run_id : null)])
      const layers = settledValue(layersResult, partialErrors, 'layers') ?? []
      const canFetchConcreteSurface =
        requestQuery.source === 'compare' ? true : Boolean(latestRun && hasResolvedSurfaceSource(requestQuery, latestRun))
      const sameVersionRankingUnavailableReason =
        selectedVersion && useSingleRunFloodSurfaces && !latestRun
          ? 'No same-version concrete run is available for this basin/source.'
          : null
      if (versionRunsResult.reachedCap && selectedVersion && !latestRun) {
        partialErrors.push(
          `runs: Stopped same-version run lookup after ${RUN_LOOKUP_MAX_EXTRA_PAGES} extra pages or ${RUN_LOOKUP_MAX_RETAINED_ITEMS} retained runs.`,
        )
      }
      if (versionRunsResult.failed && selectedVersion && !latestRun) {
        partialErrors.push('runs: Same-version run lookup failed before resolving the selected basin version run.')
      }

      let models: ApiModelInstance[] = []
      if (selectedVersion) {
        const [modelsResult] = await Promise.allSettled([fetchModels(selectedVersion.basin_version_id)])
        models = (settledValue(modelsResult, partialErrors, 'models')?.items ?? []) as ApiModelInstance[]
      }
      const activeRiverNetwork = await resolveBasinRiverNetwork(models, latestRun, partialErrors)
      const [segmentsResult, rankingResult, ...validTimeResults] = await Promise.allSettled([
        selectedVersion
          ? fetchRiverSegments(selectedVersion.basin_version_id, activeRiverNetwork.riverNetworkVersionId, query.segmentId)
          : Promise.resolve(null),
        latestRun && useSingleRunFloodSurfaces ? fetchFloodRanking(latestRun.run_id, concreteSurfaceQuery, basinId) : Promise.resolve(null),
        ...layerIdsForOverview(requestQuery).map((layerId) =>
          fetchLayerValidTimes(layerId, useSingleRunFloodSurfaces ? latestRun?.run_id : null),
        ),
      ])

      const segmentFetch = settledValue(segmentsResult, partialErrors, 'river segments')
      const segments = segmentFetch?.collection ?? null
      if (segmentFetch?.truncated) {
        partialErrors.push(
          `river segments: Retained only the first ${RIVER_SEGMENT_MAX_ITEMS} features from an oversized river-segment page; basin segment rows are partial.`,
        )
      }
      if (segmentFetch?.reachedCap) {
        partialErrors.push(
          `river segments: Stopped segment lookup after ${RIVER_SEGMENT_MAX_PAGES} pages or ${RIVER_SEGMENT_MAX_ITEMS} features before the requested segment was found.`,
        )
      }
      const ranking = settledValue(rankingResult, partialErrors, 'flood ranking')
      if (!useSingleRunFloodSurfaces) {
        partialErrors.push(`flood ranking: ${COMPARE_FLOOD_RANKING_UNAVAILABLE}`)
      } else if (sameVersionRankingUnavailableReason) {
        partialErrors.push(`flood ranking: ${sameVersionRankingUnavailableReason}`)
      }
      const sameVersionRankingItems = selectedVersion
        ? (ranking?.items ?? []).filter((item) => item.basin_version_id === selectedVersion.basin_version_id)
        : []
      const validTimesByLayerId: Record<string, string[]> = {}
      layerIdsForOverview(requestQuery).forEach((layerId, index) => {
        validTimesByLayerId[layerId] = settledValue(validTimeResults[index], partialErrors, `layer ${layerId} valid times`) ?? []
      })

      const detail = normalizeBasinDetail({
        query,
        basin,
        basinLookupAvailable,
        versions,
        models,
        segments,
        rankingItems: sameVersionRankingItems,
        latestRun: useSingleRunFloodSurfaces ? latestRun : null,
        runs: runsForSourceSelection(
          requestQuery,
          selectedVersion
            ? (versionCompleteRunPage?.items ?? []).filter((run) => run.basin_version_id === selectedVersion.basin_version_id)
            : [],
          latestRun,
        ),
        partialErrors,
      })
      const rows = normalizeBasinSegmentRows({ query: concreteSurfaceQuery, featureCollection: segments, rankingItems: sameVersionRankingItems })
      const selectedIdentifiers = resolveSelectedSegmentIdentifiers(
        query.segmentId,
        filterBasinSegmentRows(rows, query),
        segments,
        Boolean(segmentFetch?.reachedCap || segmentFetch?.truncated),
        activeRiverNetwork.riverNetworkVersionId,
      )
      let selectedSegment: SelectedSegmentDetail | null = null

      if (selectedVersion && selectedIdentifiers) {
        if (!useSingleRunFloodSurfaces) {
          partialErrors.push(`flood timeline: ${COMPARE_FLOOD_TIMELINE_UNAVAILABLE}`)
          partialErrors.push(`lineage: ${COMPARE_LINEAGE_UNAVAILABLE}`)
        } else if (!latestRun) {
          partialErrors.push('flood timeline: No same-version concrete run is available for this basin/source.')
          partialErrors.push('lineage: No same-version concrete run is available for this basin/source.')
        }
        const selectedRanking = sameVersionRankingItems.find((item) => rankingMatchesSelectedSegment(item, selectedIdentifiers))
        const [segmentResult, forecastResult, timelineResult] = await Promise.allSettled([
          fetchRiverSegment(
            selectedVersion.basin_version_id,
            selectedIdentifiers.detailEndpointRiverNetworkVersionId,
            selectedIdentifiers.detailEndpointSegmentId,
          ),
          canFetchConcreteSurface
            ? fetchForecast(
                selectedVersion.basin_version_id,
                selectedIdentifiers.detailEndpointRiverNetworkVersionId,
                selectedIdentifiers.forecastSegmentId,
                concreteSurfaceQuery,
              )
            : Promise.resolve(null),
          latestRun && useSingleRunFloodSurfaces
            ? fetchFloodTimeline(
                latestRun.run_id,
                selectedIdentifiers.timelineSegmentId,
                selectedIdentifiers.riverNetworkVersionId,
              )
            : Promise.resolve(null),
        ])
        const segment = settledValue(segmentResult, partialErrors, 'river segment detail')
        const forecast = settledValue(forecastResult, partialErrors, 'forecast series')
        const timeline = settledValue(timelineResult, partialErrors, 'flood timeline')
        let lineage: ApiLineageResponse | null = null
        let lineageError: string | null = null
        const lineageUnavailableReason = useSingleRunFloodSurfaces ? null : COMPARE_LINEAGE_UNAVAILABLE
        if (latestRun && useSingleRunFloodSurfaces) {
          try {
            lineage = await fetchLineage(
              latestRun.run_id,
              selectedIdentifiers.riverNetworkVersionId,
              selectedIdentifiers.lineageSegmentId,
              query,
            )
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
          model: activeRiverNetwork.model,
          forecast,
          floodTimeline: timeline,
          lineage,
          lineageError,
          lineageUnavailableReason,
          floodAlert: selectedRanking ?? null,
          resolvedRun: useSingleRunFloodSurfaces ? latestRun : null,
          resolvedQuery: concreteSurfaceQuery,
        })
      }

      const layerStates = normalizeLayerStates({
        query: concreteSurfaceQuery,
        layers,
        validTimesByLayerId,
        resolvedRun: useSingleRunFloodSurfaces ? latestRun : null,
      })
      const snapshot: BasinDataSnapshot = {
        requestScope: basinRequestScope(basinId, requestQuery),
        detail,
        segments: rows,
        selectedSegment,
        layers: layerStates,
      }
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
          requestScope: basinRequestScope(basinId, requestQuery),
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
  segmentCollectionPartial = false,
  scopedRiverNetworkVersionId: string | null = null,
): ResolvedSegmentIdentifiers | null {
  const row = querySegmentId
    ? rows.find((item) => item.segmentId === querySegmentId || item.riverSegmentId === querySegmentId) ?? null
    : rows[0] ?? null
  const requestedId = querySegmentId ?? row?.riverSegmentId ?? null
  if (!requestedId) return null

  const feature = findFeature(collection, requestedId) ?? (!querySegmentId && row ? findFeature(collection, row.riverSegmentId) : null)
  if (querySegmentId && !row && !feature && !segmentCollectionPartial) return null

  const riverSegmentId = row?.riverSegmentId ?? feature?.properties.river_segment_id ?? requestedId
  const observedRiverNetworkVersionId = row?.riverNetworkVersionId ?? feature?.properties.river_network_version_id ?? null
  const riverNetworkVersionId = scopedRiverNetworkVersionId ?? observedRiverNetworkVersionId
  if (!riverNetworkVersionId) return null
  const segmentId = row?.segmentId ?? feature?.properties.segment_id ?? requestedId

  return {
    requestedId,
    riverSegmentId,
    riverNetworkVersionId,
    segmentId,
    detailEndpointSegmentId: riverSegmentId,
    detailEndpointRiverNetworkVersionId: riverNetworkVersionId,
    forecastSegmentId: riverSegmentId,
    timelineSegmentId: riverSegmentId,
    lineageSegmentId: riverSegmentId,
    feature,
    row,
  }
}

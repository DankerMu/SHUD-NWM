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

/**
 * mapBootstrap critical-path snapshot：阶段 1 settle 后冻结的最小字段集，足够 OverviewPage
 * 注册 MVT hit layer（[M11MapLibreSurface::buildM11RegisteredOverlay]）使其首次河段可点击。
 *
 * 形状由 PR 3/7 固化（spec design.md D2/D3）；后续 PR 4/7 重写 normalizeLayerStates 时必须
 * 按 `{ basins, layers, layerStates, currentLayerValidTime }` 形状消费，不得重命名 layerStates
 * 等关键字段，避免沿同一文件接力时 snapshot contract drift。
 *
 * 字段语义：
 * - basins: 原始 ApiBasin[]（用于 basin 选择器与 basin 身份映射）
 * - layers: runless 图层目录 ApiLayer[]（自带 metadata.valid_times）
 * - layerStates: 按当前 query 解析后的 LayerState[]（MVT hit layer 注册条件直接消费）
 * - currentLayerValidTime: 当前 query.layer 的 valid_time（从 metadata.valid_times 解析）
 */
export interface OverviewBootstrapSnapshot {
  basins: ApiBasin[]
  layers: ApiLayer[]
  layerStates: LayerState[]
  currentLayerValidTime: string | null
}

export interface OverviewDataSnapshot {
  requestScope: M11OverviewRequestScope
  // 阶段 1 mapBootstrap 字段（settle 后可注册 MVT hit layer；enrichment 中仍可为 null 表示尚未 settle）
  bootstrap: OverviewBootstrapSnapshot | null
  basins: OverviewBasin[]
  summary: OverviewSummary
  layers: LayerState[]
  aggregationDecision: AggregationEndpointDecision
  // basin_version_id → basin_id（人类 id）映射，源自已取的 model 列表。
  // 全国总览不取 basin versions（basinCount>1），但点全国 discharge 河段开流量弹窗需要
  // 由 feature.basin_version_id 反查 basin_id 去取该流域 latest-product 曲线。
  basinVersionToBasinId: Record<string, string>
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
  // 拆分自旧 `loading: boolean` 闸门（spec D2 / scenario "Map interactivity is decoupled from enrichment loading"）。
  // - mapBootstrapLoading：地图可交互快路径（basins + runless layers + 当前 layer 的 valid_time）。
  // - enrichmentLoading：runs/models/queue/pipeline/summary/per-basin versions 等背景；阶段 2 单点 reject
  //   只产 scoped error 不挡 map（scenario "Enrichment failure does not block map"）。
  // 初始 (false, false, null) 视为「尚未 bootstrap」，不是「ready / empty」。
  mapBootstrapLoading: boolean
  enrichmentLoading: boolean
  basinLoading: boolean
  // 阶段 1 失败专属：basins / runless layers reject 时写入；与 enrichment 阶段的 partial error
  // 路径解耦（scenario "Map bootstrap rejection"）。
  bootstrapError: string | null
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
  /** 全量翻页因 MAX_PAGES/MAX_ITEMS 提前停止，河网不完整（诚实标注用）。 */
  incomplete: boolean
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
const RIVER_SEGMENT_PAGE_LIMIT = 500
const RIVER_SEGMENT_MIN_PAGE_LIMIT = 125
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
// In-flight cache for on-demand flood ranking（spec scenario "Ranking panel mounted" + "Ranking fetch
// is cancelled on unmount or layer change"）：同 key concurrent panel mount 复用同一 promise；
// 调用方 unmount/layer-switch 时主动 release 让下一次挂载发起新 fetch。
const floodRankingInFlight = new Map<string, Promise<ApiFloodAlertRanking>>()
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
  floodRankingInFlight.clear()
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
  // PR 4/7：ranking 与 `layerIdsForOverview.map(fetchLayerValidTimes)` 已从默认 loadOverview path
  // 移除（spec scenarios "Default overview bootstrap omits ranking" / "Metadata carries valid_times"），
  // 故默认 path 计入的请求数下调：
  // - layerValidTimeRequestCount = 0（metadata-first；fallback 由 normalizeLayerStates 自动忽略）。
  // - baseRequestCount: 5 个静态（basins + models + runs + queue + layers）+ hasLatestRun 时 1 个
  //   floodSummary；不再加 1 ranking。
  const layerValidTimeRequestCount = 0
  const pipelineRequestCount = hasPipelineRequest ? 1 : 0
  const baseRequestCount = 5 + (hasLatestRun ? 1 : 0)
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

// 服务端 GeoJSON 预算 413（RIVER_SEGMENT_GEOJSON_BUDGET_EXCEEDED）：减半 limit 重试。
function isRiverSegmentBudgetError(error: unknown): boolean {
  return error instanceof Error && /budget exceeded/i.test(error.message)
}

async function fetchRiverSegmentsPageAdaptive(
  basinVersionId: string,
  riverNetworkVersionId: string | null,
  limit: number,
  offset: number,
) {
  let pageLimit = limit
  for (;;) {
    try {
      return await fetchRiverSegmentsPage(basinVersionId, riverNetworkVersionId, pageLimit, offset)
    } catch (error) {
      if (!isRiverSegmentBudgetError(error) || pageLimit <= RIVER_SEGMENT_MIN_PAGE_LIMIT) throw error
      pageLimit = Math.max(RIVER_SEGMENT_MIN_PAGE_LIMIT, Math.floor(pageLimit / 2))
    }
  }
}

async function fetchRiverSegments(
  basinVersionId: string,
  riverNetworkVersionId: string | null,
  segmentId: string | null,
): Promise<RiverSegmentFetchResult> {
  const firstPage = await fetchRiverSegmentsPageAdaptive(basinVersionId, riverNetworkVersionId, RIVER_SEGMENT_PAGE_LIMIT, 0)
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
  // 本河网版本的真实要素数（feature_total）优先；total 可能含其它 river network 版本的行。
  let reportedFeatureTotal = firstPage.feature_total ?? total
  let offset = (firstPage.offset ?? 0) + (firstPage.limit || firstPage.features.length || RIVER_SEGMENT_PAGE_LIMIT)
  let pages = 1

  // 剩余页并行取齐（首屏提速）：首页已给出 feature_total 与实际页宽 stride，
  // 逐页串行等待会把 qhh（4 页 × ~850KB）的河网首显时间翻倍以上。
  // 某页因 413 减半短返会在 stride 网格上留缺口 → 丢弃其后的并行结果，
  // 交给下方串行循环按真实 offset 诚实补齐（保持原分页语义与上限保护）。
  const stride = firstPage.limit || firstPage.features.length || RIVER_SEGMENT_PAGE_LIMIT
  const plannedOffsets: number[] = []
  if (stride > 0) {
    let plannedOffset = offset
    let plannedCount = features.length
    while (
      plannedCount < Math.min(reportedFeatureTotal, RIVER_SEGMENT_MAX_ITEMS) &&
      plannedOffset < reportedTotal &&
      pages + plannedOffsets.length < RIVER_SEGMENT_MAX_PAGES
    ) {
      plannedOffsets.push(plannedOffset)
      plannedOffset += stride
      plannedCount += stride
    }
  }
  if (plannedOffsets.length > 0) {
    const parallelPages = await Promise.all(
      plannedOffsets.map((pageOffset) =>
        fetchRiverSegmentsPageAdaptive(basinVersionId, riverNetworkVersionId, stride, pageOffset),
      ),
    )
    for (const nextPage of parallelPages) {
      pages += 1
      const remaining = RIVER_SEGMENT_MAX_ITEMS - features.length
      features.push(...nextPage.features.slice(0, remaining))
      reportedTotal = nextPage.total ?? nextPage.feature_total ?? reportedTotal
      reportedFeatureTotal = nextPage.feature_total ?? reportedFeatureTotal
      collection = {
        ...nextPage,
        features,
        total: reportedTotal,
        feature_total: nextPage.feature_total ?? reportedTotal,
        limit: features.length,
        offset: 0,
      }
      const fetched = nextPage.limit || nextPage.features.length || stride
      offset += Math.max(fetched, 0)
      // 该页实取宽 ≠ stride（413 减半短返等）：其后并行页的 offset 网格失准，丢弃并交串行兜底。
      if (fetched !== stride) break
    }
  }

  // 串行兜底循环：并行批未覆盖/出现缺口时按真实 offset 取齐整个河网；
  // MAX_PAGES / MAX_ITEMS 上限保护客户端。
  while (
    features.length < Math.min(reportedFeatureTotal, RIVER_SEGMENT_MAX_ITEMS) &&
    offset < reportedTotal &&
    pages < RIVER_SEGMENT_MAX_PAGES
  ) {
    const nextPage = await fetchRiverSegmentsPageAdaptive(basinVersionId, riverNetworkVersionId, RIVER_SEGMENT_PAGE_LIMIT, offset)
    pages += 1
    const remaining = RIVER_SEGMENT_MAX_ITEMS - features.length
    features.push(...nextPage.features.slice(0, remaining))
    reportedTotal = nextPage.total ?? nextPage.feature_total ?? reportedTotal
    reportedFeatureTotal = nextPage.feature_total ?? reportedFeatureTotal
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

  const shouldFindRequestedSegment = Boolean(segmentId)
  const reachedCap =
    shouldFindRequestedSegment &&
    (offset < reportedTotal || truncated) &&
    !containsSegment(collection, segmentId) &&
    (pages >= RIVER_SEGMENT_MAX_PAGES || features.length >= RIVER_SEGMENT_MAX_ITEMS)
  const incomplete = features.length < reportedFeatureTotal
  return { collection, reachedCap, truncated, incomplete }
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

/**
 * On-demand flood ranking key（spec capability "overview-data-contracts" Requirement "Flood ranking is
 * fetched on demand, not on overview bootstrap"）。runId + serialized query + basinId 构成稳定标识。
 */
function floodRankingKey(runId: string, query: M11QueryState, basinId?: string | null) {
  return `${runId}|${serializeM11QueryState({ ...query, basemap: defaultM11QueryState.basemap })}|${basinId ?? ''}`
}

/**
 * 面板挂载或 query.layer 切到 flood-return-period / warning-level 时按需 fetch ranking。
 * 同 key 并发挂载 coalesce 到同一 promise（in-flight cache）；fetch 完成后清理 in-flight 条目（值
 * 通过模块级 `cached()` 持久），下次同 key 再触发直接命中持久缓存。
 */
export async function loadFloodRankingOnDemand(
  runId: string,
  query: M11QueryState,
  basinId?: string | null,
): Promise<ApiFloodAlertRanking> {
  const key = floodRankingKey(runId, query, basinId)
  const existing = floodRankingInFlight.get(key)
  if (existing) return existing
  const promise = fetchFloodRanking(runId, query, basinId ?? undefined)
    .finally(() => {
      // 清理 in-flight 条目；resolve 后的值由 cached() 模块级 TTL 缓存兜底（同 key 复用）。
      if (floodRankingInFlight.get(key) === promise) floodRankingInFlight.delete(key)
    })
  floodRankingInFlight.set(key, promise)
  return promise
}

/**
 * 面板 unmount 或 query.layer 切回 discharge 时调用：清掉对应 in-flight 条目，让下一次挂载/切换
 * 触发新的 fetch（spec scenario "Ranking fetch is cancelled on unmount or layer change"）。
 * 调用方仍需用 nonce / mounted ref 等模式拒绝向已卸载组件 setState（无 AbortController 抽象）。
 * runId 缺失（latestRun 尚未 settle）时清掉与该 query/basinId 相关的所有 in-flight 条目。
 */
export function releaseFloodRankingOnDemand(
  runIdOrNull: string | null | undefined,
  query: M11QueryState,
  basinId?: string | null,
): void {
  if (runIdOrNull) {
    floodRankingInFlight.delete(floodRankingKey(runIdOrNull, query, basinId))
    return
  }
  // runId 未知 → 按 query/basinId suffix 模糊清理（同 query 不同 run 都清掉）。
  const suffix = `|${serializeM11QueryState({ ...query, basemap: defaultM11QueryState.basemap })}|${basinId ?? ''}`
  for (const key of [...floodRankingInFlight.keys()]) {
    if (key.endsWith(suffix)) floodRankingInFlight.delete(key)
  }
}

/** Test-only：暴露 in-flight 大小供单测断言 cache 清理（spec scenario 第 3 句）。 */
export function _floodRankingInFlightSize(): number {
  return floodRankingInFlight.size
}

export const useOverviewDataStore = create<OverviewDataState>((set, get) => ({
  overview: null,
  basinDetail: null,
  mapBootstrapLoading: false,
  enrichmentLoading: false,
  basinLoading: false,
  bootstrapError: null,
  error: null,
  basinError: null,
  clearCache: clearOverviewDataCache,
  loadOverview: async (query) => {
    const requestKey = cacheKey('overview', query)
    const existingLoad = overviewLoads.get(requestKey)
    if (existingLoad && activeOverviewRequestKey === requestKey) return existingLoad

    const requestNonce = ++overviewRequestNonce
    activeOverviewRequestKey = requestKey
    // 两阶段同时进入 loading；spec scenario "Map bootstrap completes before enrichment" 允许两者同时为 true。
    set({ mapBootstrapLoading: true, enrichmentLoading: true, bootstrapError: null, error: null })

    // 共享谓词：写 set 前要求 nonce 仍匹配（stale 防御），否则丢弃。
    const isCurrentRequest = () => requestNonce === overviewRequestNonce && activeOverviewRequestKey === requestKey
    // 阶段 1 settle 时已写入的 bootstrap 快照（phase 2 合并到 final snapshot 时复用）。
    let bootstrapSnapshot: OverviewBootstrapSnapshot | null = null

    // 阶段 1（mapBootstrap critical path）：basins + runless layers + 当前 layer 的 valid_time。
    // 不依赖 fetchRuns/fetchModels/fetchPipelineStatus/fetchFloodSummary/fetchFloodRanking/
    // fetchBasinVersions/fetchLayerValidTimes（spec scenario "Bootstrap minimal request set"）。
    const bootstrapPromise = (async () => {
      const [basinsResult, runlessLayersResult] = await Promise.allSettled([fetchBasins(), fetchLayers(null)])

      if (basinsResult.status === 'rejected' || runlessLayersResult.status === 'rejected') {
        // scoped bootstrap error，与 enrichment partial error 不共流（spec scenario "Map bootstrap rejection"）。
        const which =
          basinsResult.status === 'rejected' && runlessLayersResult.status === 'rejected'
            ? 'basins + layers'
            : basinsResult.status === 'rejected'
              ? 'basins'
              : 'layers'
        if (isCurrentRequest()) {
          set({ mapBootstrapLoading: false, bootstrapError: safeM11ErrorMessage(which) })
        }
        return null
      }

      const basins = basinsResult.value
      const runlessLayers = runlessLayersResult.value
      // 直接从 apiLayer.metadata.valid_times 解析（spec D3 / scenario "Bootstrap minimal request set"
      // 不调用 /layers/<id>/valid-times；PR 4/7 在 normalizeLayerStates 内固化 metadata-first 路径）。
      const metadataValidTimesByLayerId: Record<string, string[]> = {}
      for (const layer of runlessLayers) {
        const metaTimes = layer.metadata?.valid_times
        if (Array.isArray(metaTimes)) metadataValidTimesByLayerId[layer.layer_id] = metaTimes
      }
      const bootstrapLayerStates = normalizeLayerStates({
        query,
        layers: runlessLayers,
        validTimesByLayerId: metadataValidTimesByLayerId,
        resolvedRun: null,
      })
      const currentLayerState = bootstrapLayerStates.find((state) => state.layerId === query.layer) ?? null
      const currentLayerValidTime = currentLayerState?.currentValidTime ?? null

      const snapshot: OverviewBootstrapSnapshot = {
        basins,
        layers: runlessLayers,
        layerStates: bootstrapLayerStates,
        currentLayerValidTime,
      }
      bootstrapSnapshot = snapshot
      if (isCurrentRequest()) {
        // 阶段 1 settle：写入 bootstrap 快照（OverviewPage surfaceSettling 解除）+ 同时初始化最小
        // overview 快照。
        // basins 字段用 bootstrap 已取的真实 basins normalize（models/runs/ranking 留空 → 详细面板
        // 的依赖字段在 enrichment settle 时被 phase-2 写入覆盖）。这样首屏即可显示 basin 边界 / 静态
        // 河网回填，不闪「暂无可用流域数据」误导提示；enrichment 阶段最终用同一份 basins 加 models/
        // versions 重 normalize 后覆盖本 placeholder。
        const placeholderBasins = normalizeOverviewBasins({
          basins,
          versionsByBasinId: {},
          // 多 basin 时跨 basin versions 不可得：phase 1 不发 per-basin version 请求（spec 关键路径）。
          basinVersionUnavailableReason:
            basins.length > 1 ? 'Basin version and bbox require the M11 aggregation endpoint.' : null,
          models: [],
          runs: [],
          // 不传 rankingItems → warningCounts === undefined（pending），等按需 ranking 面板挂载再覆盖。
        })
        const currentOverview = get().overview
        const placeholderOverview: OverviewDataSnapshot = currentOverview && overviewSnapshotMetadataMatchesQuery(currentOverview, query)
          ? { ...currentOverview, bootstrap: snapshot, layers: bootstrapLayerStates, basins: placeholderBasins }
          : {
              requestScope: overviewRequestScope(query),
              bootstrap: snapshot,
              basins: placeholderBasins,
              summary: createEmptyOverviewSummary(query),
              layers: bootstrapLayerStates,
              aggregationDecision: decideAggregationEndpoint({
                initialRequestCount: 0,
                createsPerBasinNPlusOne: false,
                missingRequiredFields: [],
              }),
              basinVersionToBasinId: {},
            }
        set({ mapBootstrapLoading: false, overview: placeholderOverview })
      }
      return snapshot
    })()

    // 阶段 2（enrichment）：与阶段 1 并行；不 await bootstrapPromise。
    // 阶段 2 内单点 reject 仅产 scoped partial error，不传播到 map / bootstrap 状态
    // （spec scenario "Enrichment failure does not block map"）。
    const enrichmentPromise = (async () => {
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
        // ranking 已从默认 path 删除（PR 4/7；spec scenario "Default overview bootstrap omits ranking"），
        // 但对比模式下 ranking on-demand 路径仍不可用：保留 partial error 让 panel-level UI 暴露
        // 「对比模式下排名不可用」的诚实状态，无需先 fetch 再失败。
        partialErrors.push(`flood ranking: ${COMPARE_FLOOD_RANKING_UNAVAILABLE}`)
      }

      const concreteSurfaceQuery = concreteQueryForSurfaces(query, latestRun)
      // PR 4/7：默认 path 不再 fan-out ranking + per-layer valid-times 请求
      // （spec scenarios "Default overview bootstrap omits ranking" / "Metadata carries valid_times"）。
      // ranking 走 loadFloodRankingOnDemand；layer valid-times 由 normalizeLayerStates metadata-first 消费。
      const [pipelineResult, summaryResult, ...versionResults] = await Promise.allSettled([
        fetchPipelineStatus(query, latestRun),
        latestRun && useSingleRunFloodSurfaces ? fetchFloodSummary(latestRun.run_id, query.validTime) : Promise.resolve(null),
        ...(requestPlan.shouldFetchVersions ? basins.map((basin) => fetchBasinVersions(basin.basin_id)) : []),
      ])

      const pipeline = settledValue(pipelineResult, partialErrors, 'pipeline')
      const floodSummary = settledValue(summaryResult, partialErrors, 'flood summary')
      const versionsByBasinId: Record<string, ApiBasinVersion[]> = {}
      if (requestPlan.shouldFetchVersions) {
        basins.forEach((basin, index) => {
          versionsByBasinId[basin.basin_id] =
            settledValue(versionResults[index] as PromiseSettledResult<ApiBasinVersion[]>, partialErrors, 'basin versions') ?? []
        })
      }

      const overviewBasins = normalizeOverviewBasins({
        basins,
        versionsByBasinId,
        basinVersionUnavailableReason:
          basins.length > 0 && !requestPlan.shouldFetchVersions ? 'Basin version and bbox require the M11 aggregation endpoint.' : null,
        models: models as ApiModelInstance[],
        runs: runs?.items ?? [],
        // ranking 默认不传 → warningCounts === undefined（pending），由按需 ranking 面板 settle 后覆盖。
      })
      const summary = normalizeOverviewSummary({
        query,
        basins: overviewBasins,
        floodSummary,
        pipeline,
        queue,
        latestRun: useSingleRunFloodSurfaces ? latestRun : null,
        runs: runsForSourceSelection(query, runs?.items ?? [], latestRun),
        partialErrors,
      })
      const layerStates = normalizeLayerStates({
        query: concreteSurfaceQuery,
        layers,
        // 默认 path 不传 validTimesByLayerId：normalizeLayerStates 三态优先消费 metadata.valid_times；
        // metadata 缺失（schema gap）的 fallback 留给独立 PR / 后续按需触发。
        resolvedRun: useSingleRunFloodSurfaces ? latestRun : null,
      })
      const aggregationDecision = decideAggregationEndpoint(requestPlan)
      const basinVersionToBasinId: Record<string, string> = {}
      for (const model of models as ApiModelInstance[]) {
        if (model.basin_version_id && model.basin_id) basinVersionToBasinId[model.basin_version_id] = model.basin_id
      }
      // 等阶段 1 settle 后再合成最终快照（bootstrap 字段需存在）；bootstrap reject 时仍生成快照
      // 但 bootstrap=null（OverviewPage 将识别为 mapBootstrap 失败态而非 ready）。
      const bootstrapForSnapshot = await bootstrapPromise.catch(() => null)
      const finalSnapshot: OverviewDataSnapshot = {
        requestScope: overviewRequestScope(query),
        bootstrap: bootstrapForSnapshot,
        basins: overviewBasins,
        summary,
        layers: layerStates,
        aggregationDecision,
        basinVersionToBasinId,
      }
      if (isCurrentRequest()) {
        set({ overview: finalSnapshot, enrichmentLoading: false, error: partialErrors[0] ?? null })
      }
      return finalSnapshot
    })()

    const load = (async () => {
      // 同时等两阶段；阶段 1 reject 不阻 enrichment（bootstrapPromise 在 reject 路径已 set false）。
      const [, enrichmentResult] = await Promise.allSettled([bootstrapPromise, enrichmentPromise])
      if (enrichmentResult.status === 'fulfilled') return enrichmentResult.value
      throw enrichmentResult.reason
    })()

    overviewLoads.set(requestKey, load)

    try {
      return await load
    } catch (error) {
      if (isCurrentRequest()) {
        const message = '加载总览数据失败'
        const fallback: OverviewDataSnapshot = {
          requestScope: overviewRequestScope(query),
          bootstrap: bootstrapSnapshot,
          basins: [],
          summary: createEmptyOverviewSummary(query),
          layers: bootstrapSnapshot?.layerStates ?? [],
          aggregationDecision: decideAggregationEndpoint({
            initialRequestCount: 0,
            createsPerBasinNPlusOne: false,
            missingRequiredFields: [],
          }),
          basinVersionToBasinId: {},
        }
        set({ overview: fallback, mapBootstrapLoading: false, enrichmentLoading: false, error: message })
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
      // 投机预热 run-less 图层目录：layers 在只读副本上是慢端点（解析最新 run +
      // 洪频质量聚合），串行排在 runs 之后会把首屏推迟十几秒。latestRun 缺失时
      // 后续 fetchLayers(null) 直接命中前端 cached() 同 key，省去一次串行慢请求；
      // latestRun 存在时该预热只多付一次幂等 GET（服务端 display TTL 缓存兜底）。
      void fetchLayers(null).catch(() => undefined)
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
      if (segmentFetch?.incomplete && !segmentFetch.truncated) {
        partialErrors.push(
          `river segments: Loaded ${segmentFetch.collection.features.length} of ${segmentFetch.collection.feature_total ?? segmentFetch.collection.total ?? 'unknown'} reaches before hitting client paging caps; the map river network is partial.`,
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

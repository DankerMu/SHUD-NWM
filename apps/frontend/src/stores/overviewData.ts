import { create } from 'zustand'

import { apiFetch } from '@/api/base'
import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'
import { toSecondsPrecisionInstant } from '@/lib/m11/instants'
import {
  createEmptyBasinDetail,
  createEmptyOverviewSummary,
  decideAggregationEndpoint,
  filterBasinSegmentRows,
  mergeLayerCatalogs,
  normalizeBasinDetail,
  normalizeBasinSegmentRows,
  normalizeLayerStates,
  normalizeOverviewBasins,
  normalizeOverviewSummary,
  normalizeSelectedSegmentDetail,
  resolveNationalScaleSource,
  type ActiveCycleValidTimesOverride,
  type AggregationEndpointDecision,
  type ApiBasin,
  type ApiBasinVersion,
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
import { isDisplayReadonlyRuntimeConfig, useMonitoringStore } from '@/stores/monitoring'

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

export type DischargeCycles = components['schemas']['DischargeCycles']
export type PrecipIndex = components['schemas']['PrecipIndex']

/** 起报时次列表：拿到即 available；enrichment 失败只产 scoped `'error'`，不是 bootstrap 错误。 */
export type DischargeCyclesState = { status: 'available'; cycles: DischargeCycles } | { status: 'error' }

/**
 * 降水 index 恰好三态（spec design.md D5 / precipitation-raster-overlay）：
 * - `available`：200，index 对象
 * - `not_mirrored`：404 且 `error.code === 'PRECIP_CYCLE_NOT_MIRRORED'`（该周期无降水镜像）
 * - `error`：其余一切失败（含 404 `PRECIP_WINDOW_INCOMPLETE`、网络错误）
 * 「当前时次不在 index 内」是从 `available` 的 `valid_times[]` 派生的判定，不是第四种状态。
 */
export type PrecipIndexState =
  | { status: 'available'; index: PrecipIndex }
  | { status: 'not_mirrored' }
  | { status: 'error' }

/**
 * 非默认 `(source, cycle)` 的时次列表：拿到即 available，reject 产 scoped `'error'`。
 * 「尚未取回」由**记录缺席**表达，故本类型只有两态；缺席 → contract 层的 `pending` 覆盖。
 * 没有终态就无法把「还没到」「取失败」与「拿到空列表」区分开，图层会带着默认周期的时次
 * 报 available 并拼出跨周期瓦片 URL。
 */
export type ValidTimesState = { status: 'available'; validTimes: string[] } | { status: 'error' }

/** `(source, cycle)` 缓存键：周期一律按秒精度归一，避免同一周期两种拼写写出两份缓存。 */
export function m11SourceCycleKey(source: string, cycle: string): string {
  return `${source}|${toSecondsPrecisionInstant(cycle) ?? cycle}`
}

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
  // 以下三项一律是 enrichment（`mapBootstrapLoading` 落 false **之后**才发出的非阻塞请求），
  // 失败只产 scoped 状态，绝不写 bootstrapError / mapBootstrapLoading
  // （spec overview-data-contracts「Cycles and precipitation index requests stay off the
  // bootstrap critical path」）。
  cyclesBySource: Record<string, DischargeCyclesState>
  /** key = `m11SourceCycleKey(source, cycle)`；只为**非默认** `(source, cycle)` 写入；缺席 = 尚未取回。 */
  validTimesByCycle: Record<string, ValidTimesState>
  /** key = `m11SourceCycleKey(source, cycle)`。 */
  precipIndexByCycle: Record<string, PrecipIndexState>
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

const COMPARE_LINEAGE_UNAVAILABLE = '对比模式河段追溯需要 GFS+IFS 聚合端点'
const RUN_LOOKUP_PAGE_LIMIT = 200
const RUN_LOOKUP_MAX_EXTRA_PAGES = 5
const RUN_LOOKUP_MAX_RETAINED_ITEMS = 1_000
const RIVER_SEGMENT_PAGE_LIMIT = 500
const RIVER_SEGMENT_MIN_PAGE_LIMIT = 125
const RIVER_SEGMENT_MAX_PAGES = 10
const RIVER_SEGMENT_MAX_ITEMS = 10_000
const READY_RUN_STATUSES = ['published'] as const
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
  // 三个 layer-time 缓存与 HTTP `cache` 同寿（tasks.md「由 clearOverviewDataCache() / clearCache()
  // 清除」）：留着它们会让下一轮加载在新 nonce 下读到上一轮的 `(source, cycle)` 列表 / index。
  // 调用一律发生在模块初始化之后，故此处对 `useOverviewDataStore` 的前向引用在运行时安全。
  useOverviewDataStore.setState({ cyclesBySource: {}, validTimesByCycle: {}, precipIndexByCycle: {} })
}

function cacheKey(path: string, params?: unknown) {
  return `${path}:${JSON.stringify(params ?? {})}`
}

/**
 * 降水叠加是纯渲染开关，不参与任何取数身份：把 `precip` 带进 store 的 query 会让降水开关
 * 整轮重载 overview（`overviewRequestNonce` 递增 → 在途的 cycles / valid-times / precip index
 * enrichment 全部作废）。取数入口一律先经此归一。
 */
function dataIdentityQuery(query: M11QueryState): M11QueryState {
  return query.precip === defaultM11QueryState.precip ? query : { ...query, precip: defaultM11QueryState.precip }
}

function requestScopeQueryKey(query: M11QueryState) {
  // basinId 由 requestScope.basinId 单独匹配，故从序列化键中剔除：
  // 加 basinId 字段后键的输出与改动前字节完全一致，零缓存 churn（R1 缓解）。
  return serializeM11QueryState({
    ...dataIdentityQuery(query),
    metStations: false,
    basinId: null,
    basemap: defaultM11QueryState.basemap,
    validTime: null,
  })
}

function requestScopeDataKey(query: M11QueryState) {
  return serializeM11QueryState({
    ...dataIdentityQuery(query),
    metStations: false,
    basinId: null,
    basemap: defaultM11QueryState.basemap,
  })
}

function basinRequestIdentityQuery(query: M11QueryState): M11QueryState {
  return { ...dataIdentityQuery(query), q: null }
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

function latestPublishedRun(runs: ApiHydroRunPage | null, query: M11QueryState | undefined): ApiHydroRun | null {
  const items = runs?.items ?? []
  const candidates = query?.source === 'best' ? items.filter((run) => concreteSourceFromRun(run)) : items
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

function mergeRunPages(
  pages: ApiHydroRunPage[],
  statuses: readonly ReadyRunStatus[] = READY_RUN_STATUSES,
): ReadyRunPage {
  const byRunId = new Map<string, ApiHydroRun>()
  pages.forEach((page) => {
    page.items.forEach((run) => {
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
  query: M11QueryState | undefined,
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

function shouldUseSingleRunSurfaces(query: M11QueryState) {
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
  return [query.layer]
}

function pipelineRequestParams(query: M11QueryState, run: ApiHydroRun | null = null): { source: string; cycle: string } | null {
  if (query.source === 'compare') return null
  const concreteQuery = concreteQueryForSurfaces(query, run)
  const source = sourceForApi(concreteQuery.source)
  // 回退到 run 的周期对所有非 compare 源生效：默认源由 `best` 翻成 `gfs` 后，若仍只在 best 分支
  // 回退，默认全国总览的 cycle 恒为 null，`/api/v1/pipeline/status` 会静默不再发出（摘要卡片空掉）。
  const cycle = query.cycle ?? run?.cycle_time ?? null
  return source && cycle ? { source, cycle } : null
}

function buildOverviewRequestPlan(
  query: M11QueryState,
  basinCount: number,
  _hasLatestRun: boolean,
  hasPipelineRequest: boolean,
): OverviewRequestPlan {
  const layerValidTimeRequestCount = 0
  const pipelineRequestCount = hasPipelineRequest ? 1 : 0
  const baseRequestCount = 5
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

/**
 * 全国尺度的具体源：`best` 归一为 `gfs`（selection 层的全国口径），`compare` 解析不出具体源。
 * store 绝不发出 `cycles?source=best|compare`、`valid-times?source=best|compare`、
 * `/api/v1/precip/best|compare/...`——解析不出就一条都不发（fixture 决策 8）。
 */
function nationalConcreteSource(source: M11QueryState['source']): 'gfs' | 'ifs' | null {
  const resolved = resolveNationalScaleSource(source)
  return resolved === 'gfs' || resolved === 'ifs' ? resolved : null
}

type NationalDischargePair = { source: 'gfs' | 'ifs'; cycle: string; isDefault: boolean }

/**
 * 活动 `(source, cycle)`：周期 = `query.cycle ?? metadata.default_cycle`，秒精度。
 * `default_cycle` 为空 = fail-closed（没有任何周期覆盖全部流域）→ 返回 null，调用方据此
 * 不发 valid-times、不发 precip index、不请求瓦片，也不会拼出字面 `{cycle}`。
 */
function nationalDischargeActivePair(query: M11QueryState, layers: ApiLayer[]): NationalDischargePair | null {
  const source = nationalConcreteSource(query.source)
  if (!source) return null
  const metadata = layers.find((layer) => layer.layer_id === 'discharge')?.metadata ?? null
  const defaultCycle = toSecondsPrecisionInstant(metadata?.default_cycle ?? null)
  if (!defaultCycle) return null
  const cycle = toSecondsPrecisionInstant(query.cycle) ?? defaultCycle
  // 默认对判定同样走秒精度：`metadata.default_cycle` 是秒精度而 URL 里是毫秒形，
  // 朴素 `===` 会把默认周期误判成非默认并多发一次 valid-times。
  return { source, cycle, isDefault: cycle === defaultCycle && (metadata?.default_source ?? 'gfs') === source }
}

function apiErrorCode(error: unknown): string | null {
  if (!error || typeof error !== 'object') return null
  const envelope = (error as { error?: unknown }).error
  if (!envelope || typeof envelope !== 'object') return null
  const code = (envelope as { code?: unknown }).code
  return typeof code === 'string' ? code : null
}

async function fetchDischargeCycles(source: 'gfs' | 'ifs') {
  return cached(cacheKey('/api/v1/layers/discharge/cycles', { source }), () =>
    getApi<DischargeCycles>('/api/v1/layers/discharge/cycles', { params: { query: { source } } }, '获取起报时次失败'),
  )
}

async function fetchLayerValidTimesForCycle(layerId: string, source: 'gfs' | 'ifs', cycle: string) {
  return cached(cacheKey('/api/v1/layers/{layer_id}/valid-times', { layerId, source, cycle }), () =>
    getApi<components['schemas']['LayerValidTimes'] | string[]>(
      '/api/v1/layers/{layer_id}/valid-times',
      { params: { path: { layer_id: layerId }, query: { source, cycle } } },
      '获取图层有效时间失败',
    ).then(normalizeLayerValidTimesResponse),
  )
}

/**
 * `getApi` 只把错误转成 `new Error(message)`、**丢掉 `error.code`**，而降水 index 的两条状态
 * 恰恰靠 code 区分，所以这里走一条 code-aware 取数路径（不改 `getApi` 既有调用者的行为）。
 * 无 code 的失败（网络 / 5xx）抛出，让 `cached()` 不落缓存、下一轮可重试，调用方降级为 `'error'`。
 */
async function fetchPrecipIndex(source: 'gfs' | 'ifs', cycle: string): Promise<PrecipIndexState> {
  return cached(cacheKey('/api/v1/precip/{source}/{cycle}/index', { source, cycle }), async () => {
    const { data, error } = await (client.GET as (path: string, options?: unknown) => Promise<{ data?: unknown; error?: unknown }>)(
      '/api/v1/precip/{source}/{cycle}/index',
      { params: { path: { source, cycle } } },
    )
    if (error) {
      const code = apiErrorCode(error)
      if (code === 'PRECIP_CYCLE_NOT_MIRRORED') return { status: 'not_mirrored' }
      if (code === 'PRECIP_WINDOW_INCOMPLETE') return { status: 'error' }
      throw new Error(getApiErrorMessage(error, '获取降水索引失败'))
    }
    return { status: 'available', index: unwrapApiData<PrecipIndex>(data, '获取降水索引失败') }
  })
}

async function fetchBasins() {
  return cached(
    cacheKey('/api/v1/basins', { limit: 200, offset: 0, hasDisplayProduct: true }),
    () =>
      getApi<ApiBasin[]>(
        '/api/v1/basins',
        { params: { query: { limit: 200, offset: 0, has_display_product: true } } },
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
    cacheKey('/api/v1/runs', {
      basinId,
      source,
      cycleTime: query.cycle ?? 'latest',
      status,
      limit,
      offset,
    }),
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

  const initialStatusPages = initialPage.readyStatusPages ?? { published: initialPage }
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
  if (await isOverviewQueueDepthUnavailable()) return null
  return cached(cacheKey('/api/v1/queue/depth'), () => getApi<ApiQueueDepth>('/api/v1/queue/depth', undefined, '获取队列深度失败'))
}

async function isOverviewQueueDepthUnavailable() {
  const monitoring = useMonitoringStore.getState()
  if (isDisplayReadonlyRuntimeConfig(monitoring.runtimeConfig)) return true
  if (!monitoring.runtimeConfig && !monitoring.runtimeConfigError) {
    await monitoring.fetchRuntimeConfig()
  }
  return isDisplayReadonlyRuntimeConfig(useMonitoringStore.getState().runtimeConfig)
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
  return cached(
    cacheKey('/api/v1/layers/{layer_id}/valid-times', { layerId, runId: runId ?? null }),
    () =>
      getApi<components['schemas']['LayerValidTimes'] | string[]>(
        '/api/v1/layers/{layer_id}/valid-times',
        { params: { path: { layer_id: layerId }, query: { run_id: runId ?? undefined } } },
        '获取图层有效时间失败',
      )
        .then(normalizeLayerValidTimesResponse)
        .catch(async () => {
          const params = new URLSearchParams()
          if (runId) params.set('run_id', runId)
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

export const useOverviewDataStore = create<OverviewDataState>((set, get) => ({
  overview: null,
  basinDetail: null,
  mapBootstrapLoading: false,
  enrichmentLoading: false,
  basinLoading: false,
  bootstrapError: null,
  error: null,
  basinError: null,
  cyclesBySource: {},
  validTimesByCycle: {},
  precipIndexByCycle: {},
  clearCache: clearOverviewDataCache,
  loadOverview: async (inputQuery) => {
    const query = dataIdentityQuery(inputQuery)
    const requestKey = cacheKey('overview', query)
    const existingLoad = overviewLoads.get(requestKey)
    if (existingLoad && activeOverviewRequestKey === requestKey) return existingLoad

    const requestNonce = ++overviewRequestNonce
    activeOverviewRequestKey = requestKey
    // 两阶段同时进入 loading；spec scenario "Map bootstrap completes before enrichment" 允许两者同时为 true。
    set({ mapBootstrapLoading: true, enrichmentLoading: true, bootstrapError: null, error: null })

    // 共享谓词：写 set 前要求 nonce 仍匹配（stale 防御），否则丢弃。
    const isCurrentRequest = () => requestNonce === overviewRequestNonce && activeOverviewRequestKey === requestKey
    const writeCycles = (source: string, value: DischargeCyclesState) => {
      if (!isCurrentRequest()) return
      set((state) => ({ cyclesBySource: { ...state.cyclesBySource, [source]: value } }))
    }
    const writePrecipIndex = (key: string, value: PrecipIndexState) => {
      if (!isCurrentRequest()) return
      set((state) => ({ precipIndexByCycle: { ...state.precipIndexByCycle, [key]: value } }))
    }
    // 阶段 1 settle 时已写入的 bootstrap 快照（phase 2 合并到 final snapshot 时复用）。
    let bootstrapSnapshot: OverviewBootstrapSnapshot | null = null
    // 最近一次 normalizeLayerStates 的入参：per-cycle valid-times 晚到时据此原地重算 layer 状态。
    let layerStateInputs: { query: M11QueryState; layers: ApiLayer[]; resolvedRun: ApiHydroRun | null } | null = null

    // 单一构造路径：活动 `(source, cycle)` 非默认对时，用 store 已取回的 per-cycle 列表顶掉
    // 目录里默认周期的 metadata.valid_times（fixture 决策 4：LayerState 本身必须是活动周期的列表）。
    // pair 一律按**全国口径**的 query.source 解析，保证 enrichment 写入键与此处读取键一致。
    const buildLayerStates = (inputs: NonNullable<typeof layerStateInputs>): LayerState[] => {
      const pair = nationalDischargeActivePair(query, inputs.layers)
      // 非默认对一律传覆盖：记录缺席 = 列表还没取回 → `pending`（空列表 + 独立文案），
      // 绝不静默回落到目录里**默认周期**的 metadata.valid_times。默认对仍传 undefined
      // （metadata 路径，同一次加载零次 valid-times 请求）。
      const activeCycleValidTimes: Record<string, ActiveCycleValidTimesOverride> | undefined =
        pair && !pair.isDefault
          ? {
              discharge: get().validTimesByCycle[m11SourceCycleKey(pair.source, pair.cycle)] ?? { status: 'pending' },
            }
          : undefined
      return normalizeLayerStates({
        query: inputs.query,
        layers: inputs.layers,
        activeCycleValidTimes,
        resolvedRun: inputs.resolvedRun,
      })
    }

    // per-cycle 列表的**唯一**写入口：两条终态（available / error）都必须就地重算 layers。
    // 「pending」是记录缺席派生出来的，只写 record 不重算会把 UI 永久钉在 pending 文案上，
    // pending→error / pending→available 的转移永远渲染不出来。
    const writeValidTimes = (key: string, value: ValidTimesState) => {
      if (!isCurrentRequest()) return
      set((state) => ({ validTimesByCycle: { ...state.validTimesByCycle, [key]: value } }))
      const inputs = layerStateInputs
      if (!inputs) return
      const layers = buildLayerStates(inputs)
      set((state) => (state.overview ? { overview: { ...state.overview, layers } } : {}))
    }

    // 阶段 1（mapBootstrap critical path）：basins + runless layers + 当前 layer 的 valid_time。
    // 不依赖 fetchRuns/fetchModels/fetchPipelineStatus/fetchBasinVersions/fetchLayerValidTimes。
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
      // normalizeLayerStates 内三态 metadata-first（spec D3 / scenario "Bootstrap minimal request set"）；
      // 这里不发 /layers/<id>/valid-times、也不预解 metadata.valid_times：fallback 入参只在
      // metadata 缺失（schema gap）才被消费，而 phase-1 仍由同一份 metadata 决定，没有独立 fallback
      // 来源 → 传 fallback 等于死代码（与 enrichment 默认 path ~L1332 保持一致：不传 validTimesByLayerId）。
      layerStateInputs = { query, layers: runlessLayers, resolvedRun: null }
      const bootstrapLayerStates = buildLayerStates(layerStateInputs)
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
        // basins 字段用 bootstrap 已取的真实 basins normalize（models/runs 留空 → 详细面板
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
      const useSingleRunSurfaces = shouldUseSingleRunSurfaces(query)
      const [layersResult] = await Promise.allSettled([fetchLayers(useSingleRunSurfaces ? latestRun?.run_id : null)])
      const scopedLayers = settledValue(layersResult, partialErrors, 'layers') ?? []
      const queue = settledValue(queueResult, partialErrors, 'queue')
      const requestPlan = buildOverviewRequestPlan(
        query,
        basins.length,
        Boolean(latestRun && useSingleRunSurfaces),
        Boolean(pipelineRequestParams(query, latestRun)),
      )

      const concreteSurfaceQuery = concreteQueryForSurfaces(query, latestRun)
      const [pipelineResult, ...versionResults] = await Promise.allSettled([
        fetchPipelineStatus(query, latestRun),
        ...(requestPlan.shouldFetchVersions ? basins.map((basin) => fetchBasinVersions(basin.basin_id)) : []),
      ])

      const pipeline = settledValue(pipelineResult, partialErrors, 'pipeline')
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
      })
      const summary = normalizeOverviewSummary({
        query,
        basins: overviewBasins,
        pipeline,
        queue,
        latestRun: useSingleRunSurfaces ? latestRun : null,
        runs: runsForSourceSelection(query, runs?.items ?? [], latestRun),
        partialErrors,
      })
      const aggregationDecision = decideAggregationEndpoint(requestPlan)
      const basinVersionToBasinId: Record<string, string> = {}
      for (const model of models as ApiModelInstance[]) {
        if (model.basin_version_id && model.basin_id) basinVersionToBasinId[model.basin_version_id] = model.basin_id
      }
      // 等阶段 1 settle 后再合成最终快照（bootstrap 字段需存在）；bootstrap reject 时仍生成快照
      // 但 bootstrap=null（OverviewPage 将识别为 mapBootstrap 失败态而非 ready）。
      const bootstrapForSnapshot = await bootstrapPromise.catch(() => null)
      const layers = mergeLayerCatalogs(bootstrapForSnapshot?.layers ?? [], scopedLayers)
      // 默认 path 不传 validTimesByLayerId：normalizeLayerStates 三态优先消费 metadata.valid_times；
      // metadata 缺失（schema gap）的 fallback 留给独立 PR / 后续按需触发。
      // 本块从 `await bootstrapPromise` 到 `set` 之间没有 await，故与 enrichment 的写入互斥：
      // 列表先到 → 这里读得到；列表后到 → enrichment 在本快照之上原地重算。
      layerStateInputs = { query: concreteSurfaceQuery, layers, resolvedRun: useSingleRunSurfaces ? latestRun : null }
      const layerStates = buildLayerStates(layerStateInputs)
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

    // 阶段 3（layer-time enrichment）：cycles / per-cycle valid-times / precip index。
    // 一律在 `mapBootstrapLoading` 落 false **之后**发出（本链以 `await bootstrapPromise` 开头，
    // 而 bootstrapPromise 在 resolve 前已 set false），绝不进被 await 的 bootstrap 关键路径；
    // 三者的 reject 只产 scoped 状态，不碰 bootstrapError / mapBootstrapLoading；
    // `overviewRequestNonce` 递增后迟到的结果一律丢弃（spec overview-data-contracts ADDED 需求）。
    const layerTimeEnrichmentPromise = (async () => {
      const snapshot = await bootstrapPromise.catch(() => null)
      if (!snapshot || !isCurrentRequest()) return
      const source = nationalConcreteSource(query.source)
      // `best` 已归一为 gfs；`compare` 解析不出具体源 → 这三类请求一条都不发。
      if (!source) return
      const pair = nationalDischargeActivePair(query, snapshot.layers)

      const cyclesTask = fetchDischargeCycles(source).then(
        (cycles) => writeCycles(source, { status: 'available', cycles }),
        // scoped 降级：周期选择器限于默认周期，不是 bootstrap 错误。
        () => writeCycles(source, { status: 'error' }),
      )

      // 默认对直接用 metadata.valid_times：同一次 overview 加载**零**次 valid-times 请求。
      const validTimesTask =
        pair && !pair.isDefault
          ? fetchLayerValidTimesForCycle('discharge', pair.source, pair.cycle).then(
              (validTimes) => writeValidTimes(m11SourceCycleKey(pair.source, pair.cycle), { status: 'available', validTimes }),
              // scoped 降级：该周期不可用（禁用态 + 独立文案），不是 bootstrap 错误。
              () => writeValidTimes(m11SourceCycleKey(pair.source, pair.cycle), { status: 'error' }),
            )
          : Promise.resolve()

      const precipTask = pair
        ? fetchPrecipIndex(pair.source, pair.cycle)
            .catch((): PrecipIndexState => ({ status: 'error' }))
            .then((precipState) => writePrecipIndex(m11SourceCycleKey(pair.source, pair.cycle), precipState))
        : Promise.resolve()

      await Promise.all([cyclesTask, validTimesTask, precipTask])
    })()

    const load = (async () => {
      // 同时等两阶段；阶段 1 reject 不阻 enrichment（bootstrapPromise 在 reject 路径已 set false）。
      const [, enrichmentResult] = await Promise.allSettled([
        bootstrapPromise,
        enrichmentPromise,
        layerTimeEnrichmentPromise,
      ])
      if (enrichmentResult.status === 'fulfilled') return enrichmentResult.value
      throw enrichmentResult.reason
    })()

    overviewLoads.set(requestKey, load)

    try {
      return await load
    } catch (error) {
      if (isCurrentRequest()) {
        const message = '加载总览数据失败'
        // IIFE 内的 `bootstrapSnapshot = snapshot` 异步赋值不参与 outer 作用域的 control-flow
        // 分析；TS 会把变量 narrow 到 `null` 然后看作 `never` 上的属性访问。显式断言回声明类型
        // 让 catch 路径仍能消费已写入的 phase-1 快照（spec scenario "Map bootstrap rejection"
        // 要求 fallback layers 用 bootstrap 的 layerStates 而非空数组）。
        const settledBootstrap = bootstrapSnapshot as OverviewBootstrapSnapshot | null
        const fallback: OverviewDataSnapshot = {
          requestScope: overviewRequestScope(query),
          bootstrap: settledBootstrap,
          basins: [],
          summary: createEmptyOverviewSummary(query),
          layers: settledBootstrap?.layerStates ?? [],
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
  loadBasinDetail: async (basinId, inputQuery) => {
    const query = dataIdentityQuery(inputQuery)
    const requestQuery = basinRequestIdentityQuery(query)
    const requestKey = cacheKey('basin-detail', { basinId, query: requestQuery })
    const existingLoad = basinLoads.get(requestKey)
    if (existingLoad && activeBasinRequestKey === requestKey) return existingLoad

    const requestNonce = ++basinRequestNonce
    activeBasinRequestKey = requestKey
    set({ basinLoading: true, basinError: null })

    const load = (async () => {
      const partialErrors: string[] = []
      // 投机预热 run-less 图层目录：latestRun 缺失时后续 fetchLayers(null) 直接命中前端 cached()
      // 同 key，省去一次串行慢请求；latestRun 存在时该预热只多付一次幂等 GET。
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
      const latestRun = latestPublishedRunForBasinVersion(
        versionCompleteRunPage,
        selectedVersion?.basin_version_id,
        requestQuery,
      )
      const concreteSurfaceQuery = concreteQueryForSurfaces(requestQuery, latestRun)
      const useSingleRunSurfaces = shouldUseSingleRunSurfaces(requestQuery)
      const [layersResult] = await Promise.allSettled([fetchLayers(useSingleRunSurfaces ? latestRun?.run_id : null)])
      const layers = settledValue(layersResult, partialErrors, 'layers') ?? []
      const canFetchConcreteSurface =
        requestQuery.source === 'compare' ? true : Boolean(latestRun && hasResolvedSurfaceSource(requestQuery, latestRun))
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
      const [segmentsResult, ...validTimeResults] = await Promise.allSettled([
        selectedVersion
          ? fetchRiverSegments(selectedVersion.basin_version_id, activeRiverNetwork.riverNetworkVersionId, query.segmentId)
          : Promise.resolve(null),
        ...layerIdsForOverview(requestQuery).map((layerId) =>
          fetchLayerValidTimes(layerId, useSingleRunSurfaces ? latestRun?.run_id : null),
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
        latestRun: useSingleRunSurfaces ? latestRun : null,
        runs: runsForSourceSelection(
          requestQuery,
          selectedVersion
            ? (versionCompleteRunPage?.items ?? []).filter((run) => run.basin_version_id === selectedVersion.basin_version_id)
            : [],
          latestRun,
        ),
        partialErrors,
      })
      const rows = normalizeBasinSegmentRows({ query: concreteSurfaceQuery, featureCollection: segments })
      const selectedIdentifiers = resolveSelectedSegmentIdentifiers(
        query.segmentId,
        filterBasinSegmentRows(rows, query),
        segments,
        Boolean(segmentFetch?.reachedCap || segmentFetch?.truncated),
        activeRiverNetwork.riverNetworkVersionId,
      )
      let selectedSegment: SelectedSegmentDetail | null = null

      if (selectedVersion && selectedIdentifiers) {
        if (!useSingleRunSurfaces) {
          partialErrors.push(`lineage: ${COMPARE_LINEAGE_UNAVAILABLE}`)
        } else if (!latestRun) {
          partialErrors.push('lineage: No same-version concrete run is available for this basin/source.')
        }
        const [segmentResult, forecastResult] = await Promise.allSettled([
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
        ])
        const segment = settledValue(segmentResult, partialErrors, 'river segment detail')
        const forecast = settledValue(forecastResult, partialErrors, 'forecast series')
        let lineage: ApiLineageResponse | null = null
        let lineageError: string | null = null
        const lineageUnavailableReason = useSingleRunSurfaces ? null : COMPARE_LINEAGE_UNAVAILABLE
        if (latestRun && useSingleRunSurfaces) {
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
          lineage,
          lineageError,
          lineageUnavailableReason,
          resolvedRun: useSingleRunSurfaces ? latestRun : null,
          resolvedQuery: concreteSurfaceQuery,
        })
      }

      const layerStates = normalizeLayerStates({
        query: concreteSurfaceQuery,
        layers,
        validTimesByLayerId,
        resolvedRun: useSingleRunSurfaces ? latestRun : null,
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
    lineageSegmentId: riverSegmentId,
    feature,
    row,
  }
}

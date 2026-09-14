import { create } from 'zustand'

import { apiFetch } from '@/api/base'
import { client } from '@/api/client'
import { getApiErrorMessage, unwrapApiData } from '@/api/response'
import type { components } from '@/api/types'
import { toSecondsPrecisionInstant } from '@/lib/m11/instants'
import {
  createEmptyOverviewSummary,
  decideAggregationEndpoint,
  mergeLayerCatalogs,
  normalizeLayerStates,
  normalizeOverviewBasins,
  normalizeOverviewSummary,
  resolveNationalScaleSource,
  retimeLayerStates,
  type ActiveCycleValidTimesOverride,
  type AggregationEndpointDecision,
  type ApiBasin,
  type ApiBasinVersion,
  type ApiHydroRun,
  type ApiHydroRunPage,
  type ApiLayer,
  type ApiModelInstance,
  type ApiPipelineStatus,
  type ApiQueueDepth,
  type LayerState,
  type OverviewBasin,
  type OverviewSummary,
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

interface OverviewDataState {
  overview: OverviewDataSnapshot | null
  // 拆分自旧 `loading: boolean` 闸门（spec D2 / scenario "Map interactivity is decoupled from enrichment loading"）。
  // - mapBootstrapLoading：地图可交互快路径（basins + runless layers + 当前 layer 的 valid_time）。
  // - enrichmentLoading：runs/models/queue/pipeline/summary/per-basin versions 等背景；阶段 2 单点 reject
  //   只产 scoped error 不挡 map（scenario "Enrichment failure does not block map"）。
  // 初始 (false, false, null) 视为「尚未 bootstrap」，不是「ready / empty」。
  mapBootstrapLoading: boolean
  enrichmentLoading: boolean
  // 阶段 1 失败专属：basins / runless layers reject 时写入；与 enrichment 阶段的 partial error
  // 路径解耦（scenario "Map bootstrap rejection"）。
  bootstrapError: string | null
  error: string | null
  // 以下三项一律是 enrichment（`mapBootstrapLoading` 落 false **之后**才发出的非阻塞请求），
  // 失败只产 scoped 状态，绝不写 bootstrapError / mapBootstrapLoading
  // （spec overview-data-contracts「Cycles and precipitation index requests stay off the
  // bootstrap critical path」）。
  cyclesBySource: Record<string, DischargeCyclesState>
  /** key = `m11SourceCycleKey(source, cycle)`；只为**非默认** `(source, cycle)` 写入；缺席 = 尚未取回。 */
  validTimesByCycle: Record<string, ValidTimesState>
  /** key = `m11SourceCycleKey(source, cycle)`。 */
  precipIndexByCycle: Record<string, PrecipIndexState>
  /**
   * 本轮 `loadOverview` 的阶段 3（layer-time enrichment）已确定被整段跳过（bootstrap 失败 →
   * cycles / per-cycle valid-times / precip index 一条都不会发）。
   *
   * 上面三个 map 的「键缺席」在跳过之后不再是「还在取」——阶段 2 仍能用 run-scoped 目录把图层
   * 渲染正常，页面看上去健康，而 index 永远不会到达。消费方（`resolveM11PrecipOverlay`）据此把
   * 「在途」与「终态失败」分开（fixture #2015 决策 1 第 3 臂）。
   */
  layerTimeEnrichmentSkipped: boolean
  loadOverview: (query: M11QueryState) => Promise<OverviewDataSnapshot>
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

type ReadyRunStatusPages = Partial<Record<ReadyRunStatus, ApiHydroRunPage>>

type ReadyRunPage = ApiHydroRunPage & {
  readyStatusPages?: ReadyRunStatusPages
}

const READY_RUN_STATUSES = ['published'] as const
type ReadyRunStatus = (typeof READY_RUN_STATUSES)[number]

const cache = new Map<string, CacheEntry<unknown>>()
const CACHE_TTL_MS = 60_000
const CACHE_MAX_ENTRIES = 64
const OVERVIEW_INITIAL_REQUEST_THRESHOLD = 8
const overviewLoads = new Map<string, Promise<OverviewDataSnapshot>>()
let overviewRequestNonce = 0
let activeOverviewRequestKey: string | null = null
/**
 * 活动请求代的重派生句柄（#2127 / design D1）。`validTime` 不参与取数身份：同一身份只换
 * `validTime` 的调用经它就地重派生，不发请求、不开新一代。每一代加载（含同 query 的重载）都会
 * 替换它，`clearOverviewDataCache` 清空它；经它的写入仍受该代 `isCurrentRequest()` 守卫。
 */
type OverviewRederiveHandle = {
  currentValidTime: () => string | null
  rederive: (query: M11QueryState) => Promise<OverviewDataSnapshot>
}
let activeOverviewRederive: OverviewRederiveHandle | null = null
let cacheGeneration = 0

export function clearOverviewDataCache() {
  cacheGeneration += 1
  for (const key of cache.keys()) {
    deleteCacheEntry(key)
  }
  overviewLoads.clear()
  overviewRequestNonce += 1
  activeOverviewRequestKey = null
  activeOverviewRederive = null
  // 三个 layer-time 缓存与 HTTP `cache` 同寿（tasks.md「由 clearOverviewDataCache() / clearCache()
  // 清除」）：留着它们会让下一轮加载在新 nonce 下读到上一轮的 `(source, cycle)` 列表 / index。
  // 调用一律发生在模块初始化之后，故此处对 `useOverviewDataStore` 的前向引用在运行时安全。
  useOverviewDataStore.setState({
    cyclesBySource: {},
    validTimesByCycle: {},
    precipIndexByCycle: {},
    // 与三个 map 同寿：缓存清空后「键缺席」重新只意味着「还没取」，跳过标记留着就是谎报终态。
    layerTimeEnrichmentSkipped: false,
  })
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

/**
 * 请求身份 = 归一后的取数 query 去掉 `validTime`（与 `requestScopeQueryKey` / `requestScopeDataKey`
 * 的切分同形）。`loadOverview` 里没有任何一条请求以 `validTime` 为键：把它带进身份，时间轴每步
 * 都会整轮重载（nonce 递增、清错误、作废在途 enrichment、重发失败端点——#2127）。
 */
function overviewRequestIdentityKey(query: M11QueryState) {
  return cacheKey('overview', { ...query, validTime: null })
}

/** 冻结的 bootstrap 快照只换 `validTime`，绝不从活的 cycles / valid-times 记录重建（design D1）。 */
function retimeBootstrapSnapshot(snapshot: OverviewBootstrapSnapshot, query: M11QueryState): OverviewBootstrapSnapshot {
  const layerStates = retimeLayerStates(snapshot.layerStates, query.validTime)
  return {
    ...snapshot,
    layerStates,
    currentLayerValidTime: layerStates.find((state) => state.layerId === query.layer)?.currentValidTime ?? null,
  }
}

function requestScopeQueryKey(query: M11QueryState) {
  return serializeM11QueryState({
    ...dataIdentityQuery(query),
    metStations: false,
    basemap: defaultM11QueryState.basemap,
    validTime: null,
  })
}

function requestScopeDataKey(query: M11QueryState) {
  return serializeM11QueryState({
    ...dataIdentityQuery(query),
    metStations: false,
    basemap: defaultM11QueryState.basemap,
  })
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

export function overviewSnapshotMatchesQuery(snapshot: OverviewDataSnapshot | null | undefined, query: M11QueryState) {
  return snapshot?.requestScope?.dataKey === requestScopeDataKey(query)
}

export function overviewSnapshotMetadataMatchesQuery(snapshot: OverviewDataSnapshot | null | undefined, query: M11QueryState) {
  return snapshot?.requestScope?.queryKey === requestScopeQueryKey(query)
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

function runsForSourceSelection(query: M11QueryState, runs: ApiHydroRun[], latestRun: ApiHydroRun | null): ApiHydroRun[] {
  return query.source === 'best' ? (latestRun ? [latestRun] : []) : runs
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

/**
 * 全国尺度的具体源：`best` 归一为 `gfs`（selection 层的全国口径），`compare` 解析不出具体源。
 * store 绝不发出 `cycles?source=best|compare`、`valid-times?source=best|compare`、
 * `/api/v1/precip/best|compare/...`——解析不出就一条都不发（fixture 决策 8）。
 */
export function nationalConcreteSource(source: M11QueryState['source']): 'gfs' | 'ifs' | null {
  const resolved = resolveNationalScaleSource(source)
  return resolved === 'gfs' || resolved === 'ifs' ? resolved : null
}

type NationalDischargePair = { source: 'gfs' | 'ifs'; cycle: string; isDefault: boolean }

/** 目录声明的默认源。`metadata.default_cycle` / `valid_times` 只对这个源成立。 */
function nationalDischargeDefaultSource(layers: ApiLayer[]): string {
  return layers.find((layer) => layer.layer_id === 'discharge')?.metadata?.default_source ?? 'gfs'
}

/**
 * 某个源自己声明的默认周期（`/api/v1/layers/discharge/cycles?source=` 的 `default_cycle`）。
 * 解不出来（记录缺席 / 非 available / `default_cycle` 为空）一律返回 null；**记录本身的状态**
 * 由调用方另行区分（`buildLayerStates` 的三态分类），本函数只回答「有没有周期」。
 * `unwrapApiData` 是裸 `as T` 断言、零运行时校验，变形响应会带着 `cycles: undefined` 进来，
 * 故这里宽松判空而不是让 `.default_cycle` 在取数链里抛——响应已到达但畸形时，它与「到达且为空」
 * 归入同一个终态（都不会再有第二次到达）。
 */
function dischargeCyclesDefaultCycle(state: DischargeCyclesState | undefined): string | null {
  if (!state || state.status !== 'available') return null
  return toSecondsPrecisionInstant(state.cycles?.default_cycle ?? null)
}

/**
 * 活动 `(source, cycle)`：周期 = `query.cycle ?? 该源自己的默认周期`，秒精度。
 * 默认周期解不出来 = fail-closed → 返回 null，调用方据此不发 valid-times、不发 precip index、
 * 不请求瓦片，也不会拼出字面 `{cycle}`。
 *
 * **「该源自己的默认周期」按源分叉**（#2014 决策 13 / finding C1）：目录 metadata 的
 * `default_cycle` 是 **GFS 专有事实**——后端 `list_layers`（`apps/api/routes/hydro_display.py`）
 * 签名里没有 `source`，`_default_layer_catalog` 固定按 `NATIONAL_DISCHARGE_DEFAULT_SOURCE = 'gfs'`
 * 算。把它当成与源无关的默认值，切到 IFS 就会拼出 `(ifs, <gfs 周期>)` 这个未覆盖对，而后端
 * `national_discharge_valid_times` 对它返回 **200 + 空列表**（不是 4xx），图层落到一条假文案
 * （'Layer has no valid times.'——IFS 有时次，只是不在那个周期）。故非默认源只读该源自己的
 * `cyclesBySource[source].cycles.default_cycle`，未到达/取回失败即返回 null（复用同一条
 * fail-closed 语义）。默认源的行为逐字不变：仍只看目录 metadata。
 */
function nationalDischargeActivePair(
  query: M11QueryState,
  layers: ApiLayer[],
  cyclesBySource: Record<string, DischargeCyclesState>,
): NationalDischargePair | null {
  const source = nationalConcreteSource(query.source)
  if (!source) return null
  const metadata = layers.find((layer) => layer.layer_id === 'discharge')?.metadata ?? null
  const isDefaultSource = nationalDischargeDefaultSource(layers) === source
  const defaultCycle = isDefaultSource
    ? toSecondsPrecisionInstant(metadata?.default_cycle ?? null)
    : dischargeCyclesDefaultCycle(cyclesBySource[source])
  if (!defaultCycle) return null
  const cycle = toSecondsPrecisionInstant(query.cycle) ?? defaultCycle
  // 默认对判定同样走秒精度：`metadata.default_cycle` 是秒精度而 URL 里是毫秒形，
  // 朴素 `===` 会把默认周期误判成非默认并多发一次 valid-times。
  return { source, cycle, isDefault: cycle === defaultCycle && isDefaultSource }
}

/**
 * 非默认源的活动对**尚未解出**（该源 cycles 未到达 / 取回失败）。此时 `activeCycleValidTimes`
 * 必须传显式覆盖：`undefined` 会让 `normalizeLayerStates` 回落到 **GFS 的** `metadata.valid_times`，
 * 把假文案换成假数据——更糟。
 */
function nationalDischargeSourceUnresolved(
  query: M11QueryState,
  layers: ApiLayer[],
  cyclesBySource: Record<string, DischargeCyclesState>,
): boolean {
  const source = nationalConcreteSource(query.source)
  if (!source || source === nationalDischargeDefaultSource(layers)) return false
  return nationalDischargeActivePair(query, layers, cyclesBySource) === null
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

function normalizeLayerValidTimesResponse(value: components['schemas']['LayerValidTimes'] | string[]): string[] {
  return Array.isArray(value) ? value : value.valid_times
}

export const useOverviewDataStore = create<OverviewDataState>((set, get) => ({
  overview: null,
  mapBootstrapLoading: false,
  enrichmentLoading: false,
  bootstrapError: null,
  error: null,
  cyclesBySource: {},
  validTimesByCycle: {},
  precipIndexByCycle: {},
  layerTimeEnrichmentSkipped: false,
  clearCache: clearOverviewDataCache,
  loadOverview: async (inputQuery) => {
    const query = dataIdentityQuery(inputQuery)
    const requestKey = overviewRequestIdentityKey(query)
    // 同一身份、只换 `validTime`（在途或已落定）→ 重派生，不重载：必须在 nonce 递增与任何
    // loading / error 写入之前短路。**完全相同**的 query 不走这里：在途时照旧并入，落定后照旧
    // 开新一代（重挂载要能刷新过期数据、重试失败的 bootstrap）。
    const rederiveHandle = activeOverviewRederive
    if (rederiveHandle && activeOverviewRequestKey === requestKey && rederiveHandle.currentValidTime() !== query.validTime) {
      return rederiveHandle.rederive(query)
    }
    const existingLoad = overviewLoads.get(requestKey)
    if (existingLoad && activeOverviewRequestKey === requestKey) return existingLoad

    const requestNonce = ++overviewRequestNonce
    activeOverviewRequestKey = requestKey
    // 两阶段同时进入 loading；spec scenario "Map bootstrap completes before enrichment" 允许两者同时为 true。
    set({
      mapBootstrapLoading: true,
      enrichmentLoading: true,
      bootstrapError: null,
      error: null,
      // 新一轮加载：阶段 3 还没被判跳过，键缺席重新只意味着「还没取」。
      layerTimeEnrichmentSkipped: false,
    })

    // 共享谓词：写 set 前要求 nonce 仍匹配（stale 防御），否则丢弃。
    const isCurrentRequest = () => requestNonce === overviewRequestNonce && activeOverviewRequestKey === requestKey
    const writePrecipIndex = (key: string, value: PrecipIndexState) => {
      if (!isCurrentRequest()) return
      set((state) => ({ precipIndexByCycle: { ...state.precipIndexByCycle, [key]: value } }))
    }
    // 阶段 1 settle 时已写入的 bootstrap 快照（phase 2 合并到 final snapshot 时复用）。
    let bootstrapSnapshot: OverviewBootstrapSnapshot | null = null
    // 最近一次 normalizeLayerStates 的入参：per-cycle valid-times 晚到时据此原地重算 layer 状态。
    let layerStateInputs: { query: M11QueryState; layers: ApiLayer[]; resolvedRun: ApiHydroRun | null } | null = null
    // 本轮阶段 3 已确定被跳过（bootstrap 失败 → 一条 per-cycle valid-times 请求都不会发）。
    // 此时「记录缺席」不再是「还在取」，把派生的 pending 顶成 error 终态。
    let layerTimeEnrichmentSkipped = false
    // 本代持有的当前 query：只有 `validTime` 会被重派生替换（身份相同）。所有读 `validTime` 的
    // 派生点（layer 状态、summary、requestScope、空 summary）一律读它，而不是外层 `query`。
    let currentQuery = query
    // 阶段 2 的 summary 入参（除 query 外）：重派生据此重算 summary，不发请求。
    let summaryInputs: Omit<Parameters<typeof normalizeOverviewSummary>[0], 'query'> | null = null
    // catch 兜底快照已写入：其 layers / summary 的来源与正常快照不同，重派生沿用同一来源。
    let wroteFallback = false

    // 单一构造路径：活动 `(source, cycle)` 非默认对时，用 store 已取回的 per-cycle 列表顶掉
    // 目录里默认周期的 metadata.valid_times（fixture 决策 4：LayerState 本身必须是活动周期的列表）。
    // pair 一律按**全国口径**的 query.source 解析，保证 enrichment 写入键与此处读取键一致。
    const buildLayerStates = (inputs: NonNullable<typeof layerStateInputs>): LayerState[] => {
      const cyclesBySource = get().cyclesBySource
      const pair = nationalDischargeActivePair(query, inputs.layers, cyclesBySource)
      // 非默认对一律传覆盖：绝不静默回落到目录里**默认周期**的 metadata.valid_times。
      // 默认对仍传 undefined（metadata 路径，同一次加载零次 valid-times 请求）。
      //
      // 「活动对解不出来」不是一个二元量，它有**三个**互不相同的成因，各自对应一条独立文案
      // （#2014 round-3 finding A1：把它们塌成 error/非 error 的谓词已经第三次吃掉第三态）：
      //   - 该源的 cycles 记录**缺席** → 请求真的在途 → `pending`。诚实，之后必有终态覆盖。
      //   - 记录是 `error`（取回被拒）→ 活动对永远解不出来，不会再有 valid-times 终态来覆盖它，
      //     `pending`（"还在取"）就是谎报 → `error` 终态。
      //   - 记录是 `available` 而这里仍未解出对 = 列表**已到达且为空**（`default_cycle == null`，
      //     后端按网交集 fail-closed 时的正常 200 输出，见 `hydro_display.py` 的路由 docstring）
      //     → `fail-closed` 终态。什么都没有加载失败，故**不得**复用 error 文案。
      // 阶段 3 被跳过（bootstrap 失败 → 请求永远不会发出）只**升级**记录缺席那一态：
      // 已到达的记录自己就是终态事实，跳过与否都不改变它。
      const concreteSource = nationalConcreteSource(query.source)
      const sourceUnresolved = nationalDischargeSourceUnresolved(query, inputs.layers, cyclesBySource)
      const sourceCyclesRecord = concreteSource !== null ? cyclesBySource[concreteSource] : undefined
      const unresolvedSourceRecord = (): ActiveCycleValidTimesOverride => {
        switch (sourceCyclesRecord?.status) {
          case 'error':
            return { status: 'error' }
          case 'available':
            return { status: 'fail-closed' }
          default:
            return layerTimeEnrichmentSkipped ? { status: 'error' } : { status: 'pending' }
        }
      }
      const missingRecord: ActiveCycleValidTimesOverride = sourceUnresolved
        ? unresolvedSourceRecord()
        : layerTimeEnrichmentSkipped
          ? { status: 'error' }
          : { status: 'pending' }
      // 该对的列表**到达且为空**时要先问「该源列不列出这个周期」（#2131 / design D2）：
      //   - 该源 cycles 记录缺席（默认源不等 `/cycles`）→ 成员身份尚不可知 → `pending`，
      //     `writeCycles` 到达后重算；
      //   - 记录 `available`、列表是数组且不含该周期（秒精度）→ `cycle-not-listed` 终态；
      //   - 列出了 / 记录 `error` / 列表变形（裸 `as T`，成员身份未知）→ 原样传空列表，
      //     仍是 'Layer has no valid times.'（真实覆盖缺口或不可知）。
      const pairRecord = (target: NationalDischargePair): ActiveCycleValidTimesOverride => {
        const record = get().validTimesByCycle[m11SourceCycleKey(target.source, target.cycle)]
        if (!record) return missingRecord
        if (record.status !== 'available' || record.validTimes.length > 0) return record
        const cyclesRecord = cyclesBySource[target.source]
        if (!cyclesRecord) return { status: 'pending' }
        if (cyclesRecord.status !== 'available' || !Array.isArray(cyclesRecord.cycles?.cycles)) return record
        const listed = cyclesRecord.cycles.cycles.some((entry) => toSecondsPrecisionInstant(entry?.cycle_time) === target.cycle)
        return listed ? record : { status: 'cycle-not-listed', source: target.source, cycle: target.cycle }
      }
      const activeCycleValidTimes: Record<string, ActiveCycleValidTimesOverride> | undefined =
        pair && !pair.isDefault
          ? { discharge: pairRecord(pair) }
          : // 活动对解不出来但源是非默认源：绝不回落目录 metadata（那是 GFS 的列表）。
            sourceUnresolved
            ? { discharge: missingRecord }
            : undefined
      return normalizeLayerStates({
        // 时次一律取本代**当前** query（阶段 2 的具体源 query 只换 `validTime`、保留具体源）：
        // 迟到的 enrichment 重算与重派生都据此落在最新的时间轴位置上。
        query: { ...inputs.query, validTime: currentQuery.validTime },
        layers: inputs.layers,
        activeCycleValidTimes,
        // 与上面那份列表同批盖章：地图侧不再自行解析周期，直接读这枚章拼瓦片 URL 的 cycle 段
        // （决策 13 孪生要求）。解不出对 = null = 不注册叠加层，绝不回落目录的 GFS 周期。
        activeNationalCycle: pair?.cycle ?? null,
        resolvedRun: inputs.resolvedRun,
      })
    }

    // 按源周期列表的**唯一**写入口：与下面的 `writeValidTimes` 同构（记录 → 重算 → 合并）。
    // 非默认源的活动对由该源自己的 `cycles.default_cycle` 决定（`nationalDischargeActivePair`），
    // 没有这条反应边，切到非默认源后第一次进来会永久停在 pair 为 null 的未落定态：
    // available 臂没人去发那条 valid-times，error 臂更是连终态文案都换不上去。
    const writeCycles = (source: string, value: DischargeCyclesState) => {
      if (!isCurrentRequest()) return
      set((state) => ({ cyclesBySource: { ...state.cyclesBySource, [source]: value } }))
      const inputs = layerStateInputs
      if (!inputs) return
      const layers = buildLayerStates(inputs)
      set((state) => (state.overview ? { overview: { ...state.overview, layers } } : {}))
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

    // 阶段 3 被跳过的终态写入口：与 `writeValidTimes` 的 reject 臂同构（只是没有 per-cycle 记录可
    // 写——`snapshot` 为 null 时连活动对都解不出来），同样必须就地重算 layers，否则「跳过」这条
    // 路径会把 UI 永久钉在 pending 文案上：既无请求在途，也永远不会有终态覆盖它。
    // 顺序无关：signal 先到 → 阶段 2 的 buildLayerStates 直接读到 error；阶段 2 先到 → 这里原地重算。
    const markLayerTimeEnrichmentSkipped = () => {
      if (!isCurrentRequest()) return
      layerTimeEnrichmentSkipped = true
      // 同一事实的第二个消费面：`precipIndexByCycle` 没有「跳过」对应的终态写入口（本轮连
      // `fetchPrecipIndex` 都不会发），所以跳过信号必须自己进 state，否则降水叠加会把
      // 「永远不会到达」渲染成「在途」（fixture #2015 决策 1 第 3 臂 / IS-2）。
      set({ layerTimeEnrichmentSkipped: true })
      const inputs = layerStateInputs
      if (!inputs) return
      const layers = buildLayerStates(inputs)
      set((state) => (state.overview ? { overview: { ...state.overview, layers } } : {}))
    }

    // validTime-only 调用的重派生（design D1）：只用本代已持有的输入，零请求；nonce、loading、
    // 错误、三个 layer-time map 与跳过标记一概不碰。本代尚未写过 overview（`layerStateInputs`
    // 为 null）时 store 里是上一代的快照，不得给它盖新 requestScope——只换 `currentQuery`，
    // 之后本代的每次 `set` 自会读到它。
    activeOverviewRederive = {
      currentValidTime: () => currentQuery.validTime,
      rederive: (nextQuery) => {
        currentQuery = nextQuery
        const inputs = layerStateInputs
        if (isCurrentRequest() && inputs) {
          const retimedBootstrap = bootstrapSnapshot ? retimeBootstrapSnapshot(bootstrapSnapshot, nextQuery) : null
          bootstrapSnapshot = retimedBootstrap
          const layers = wroteFallback ? (retimedBootstrap?.layerStates ?? []) : buildLayerStates(inputs)
          const summary =
            wroteFallback || !summaryInputs
              ? createEmptyOverviewSummary(nextQuery)
              : normalizeOverviewSummary({ ...summaryInputs, query: nextQuery })
          set((state) =>
            state.overview
              ? {
                  overview: {
                    ...state.overview,
                    requestScope: overviewRequestScope(nextQuery),
                    bootstrap: retimedBootstrap,
                    layers,
                    summary,
                  },
                }
              : {},
          )
        }
        return overviewLoads.get(requestKey) ?? Promise.resolve(get().overview as OverviewDataSnapshot)
      },
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
              requestScope: overviewRequestScope(currentQuery),
              bootstrap: snapshot,
              basins: placeholderBasins,
              summary: createEmptyOverviewSummary(currentQuery),
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
      // 所有读 `validTime` 的值（layer 状态、summary、requestScope、bootstrap）都必须在这个同步块里
      // 算（design D1）：await 期间可能来过 validTime-only 调用，提前算好的值会带着旧时次落进快照。
      layerStateInputs = { query: concreteSurfaceQuery, layers, resolvedRun: useSingleRunSurfaces ? latestRun : null }
      summaryInputs = {
        basins: overviewBasins,
        pipeline,
        queue,
        latestRun: useSingleRunSurfaces ? latestRun : null,
        runs: runsForSourceSelection(query, runs?.items ?? [], latestRun),
        partialErrors,
      }
      const layerStates = buildLayerStates(layerStateInputs)
      const finalSnapshot: OverviewDataSnapshot = {
        requestScope: overviewRequestScope(currentQuery),
        // 读持有的变量而非 promise 的 resolve 值：重派生会替换它（只换 validTime 的冻结快照）。
        bootstrap: bootstrapSnapshot,
        basins: overviewBasins,
        summary: normalizeOverviewSummary({ ...summaryInputs, query: currentQuery }),
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
      // 新一轮请求已接管 store：本轮什么都不写（连 skipped signal 也不写）。
      if (!isCurrentRequest()) return
      if (!snapshot) {
        // bootstrap 失败 → 本链一条请求都不发，但阶段 2 仍会用 run-scoped 目录构造 layer 状态，
        // 非默认对的记录缺席会派生出 pending。给这条路径补终态。
        markLayerTimeEnrichmentSkipped()
        return
      }
      const source = nationalConcreteSource(query.source)
      // `best` 已归一为 gfs；`compare` 解析不出具体源 → 这三类请求一条都不发。
      if (!source) return

      const cyclesTask = fetchDischargeCycles(source).then(
        (cycles) => writeCycles(source, { status: 'available', cycles }),
        // scoped 降级：周期选择器限于默认周期，不是 bootstrap 错误。
        () => writeCycles(source, { status: 'error' }),
      )

      // 非默认源的活动周期**只能**来自这条 cycles 响应（目录的 default_cycle 是 GFS 专有事实），
      // 故必须等 `cyclesTask` 落定后再解析 pair；默认源不加这个 await，请求顺序与行为逐字不变。
      if (source !== nationalDischargeDefaultSource(snapshot.layers)) {
        await cyclesTask
        // await 期间新一轮请求可能已接管 store：本轮不再发出任何请求。
        if (!isCurrentRequest()) return
      }
      const pair = nationalDischargeActivePair(query, snapshot.layers, get().cyclesBySource)

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
      // 仍是当前代 → 交出 store 的 overview（在途期间的 validTime-only 调用方拿到最新时次与迟到的
      // enrichment 重算）；已被接管 → 交出本代自己构造的快照。
      if (enrichmentResult.status === 'fulfilled') {
        return isCurrentRequest() ? (get().overview ?? enrichmentResult.value) : enrichmentResult.value
      }
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
          requestScope: overviewRequestScope(currentQuery),
          bootstrap: settledBootstrap,
          basins: [],
          summary: createEmptyOverviewSummary(currentQuery),
          layers: settledBootstrap?.layerStates ?? [],
          aggregationDecision: decideAggregationEndpoint({
            initialRequestCount: 0,
            createsPerBasinNPlusOne: false,
            missingRequiredFields: [],
          }),
          basinVersionToBasinId: {},
        }
        wroteFallback = true
        set({ overview: fallback, mapBootstrapLoading: false, enrichmentLoading: false, error: message })
      }
      throw error
    } finally {
      if (overviewLoads.get(requestKey) === load) overviewLoads.delete(requestKey)
    }
  },
}))

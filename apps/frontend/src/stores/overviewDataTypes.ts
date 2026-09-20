import type { components } from '@/api/types'
import { toSecondsPrecisionInstant } from '@/lib/m11/instants'
import type {
  AggregationEndpointDecision,
  ApiBasin,
  ApiHydroRunPage,
  ApiLayer,
  LayerState,
  OverviewBasin,
  OverviewSummary,
} from '@/lib/m11/overviewDataContracts'
import type { M11QueryState } from '@/lib/m11/queryState'

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

export type ModelInstancePage = components['schemas']['ModelInstancePage']

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

export interface OverviewDataState {
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
  /**
   * 本轮 `loadOverview` 里被形状守卫拒收的端点标签（#2129 裁决 B / design D2）：去重、按首次出现
   * 排序；新一轮加载开始时与 `bootstrapError` / `error` 一同清空，`clearOverviewDataCache` 不清。
   */
  dataAnomalies: string[]
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

export type CacheEntry<T> = {
  promise?: Promise<T>
  value?: T
  timeoutId?: number
}

export type OverviewRequestPlan = {
  baseRequestCount: number
  layerValidTimeRequestCount: number
  versionRequestCount: number
  pipelineRequestCount: number
  initialRequestCount: number
  createsPerBasinNPlusOne: boolean
  missingRequiredFields: string[]
  shouldFetchVersions: boolean
}

export type ReadyRunStatusPages = Partial<Record<ReadyRunStatus, ApiHydroRunPage>>

export type ReadyRunPage = ApiHydroRunPage & {
  readyStatusPages?: ReadyRunStatusPages
}

export const READY_RUN_STATUSES = ['published'] as const
export type ReadyRunStatus = (typeof READY_RUN_STATUSES)[number]

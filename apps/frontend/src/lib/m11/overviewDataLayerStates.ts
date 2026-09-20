import { normalizeIsoString, sourcesFromRuns } from '@/lib/m11/overviewDataContractPrimitives'
import type { ApiHydroRun, ApiLayer, LayerLegendEntry, LayerState } from '@/lib/m11/overviewDataContractTypes'
import { createFreshnessMetadata, createSourceScenarioSelection } from '@/lib/m11/overviewDataNormalizers'
import type { M11Layer, M11QueryState } from '@/lib/m11/queryState'

const m11RiverDischargeLegend: LayerLegendEntry[] = [
  { label: '<1 m³/s', color: '#7FB8DC', max: 1 },
  { label: '1-10 m³/s', color: '#4292C6', min: 1, max: 10 },
  { label: '10-100 m³/s', color: '#2171B5', min: 10, max: 100 },
  { label: '100-1000 m³/s', color: '#08519C', min: 100, max: 1000 },
  { label: '1000-10000 m³/s', color: '#08306B', min: 1000, max: 10000 },
  { label: '>10000 m³/s', color: '#CB181D', min: 10000 },
  { label: '无径流数据', color: m11DischargeColor(null) },
]

const layerLabels: Record<M11Layer, string> = {
  discharge: 'Discharge',
}

/**
 * Metadata-first valid_times consumption（spec capability "frontend-mvt-layer-consumption"
 * Requirement "Layer valid_times are consumed from metadata.valid_times first"）。
 *
 * 三态分支：
 * - `apiLayer.metadata.valid_times` 为非空数组 → 直接消费，调用方 MUST NOT 发起
 *   `/api/v1/layers/<id>/valid-times` fan-out（spec scenario "Metadata carries valid_times"）。
 * - `apiLayer.metadata.valid_times === []` → time-less layer（如 river-network），调用方 MUST NOT
 *   发起 fallback（spec scenario "Metadata.valid_times is intentionally empty (time-less layer)"）。
 * - `apiLayer.metadata.valid_times === undefined || null` → schema gap，调用方 MAY 发起 fallback
 *   并通过 `validTimesByLayerId` 传回（spec scenario "Metadata.valid_times is missing or null (schema gap)"）。
 *
 * 返回 `requiresFallback` 让调用方据此决定是否对该 layer 发起 fallback fetch（PR 4/7 删除
 * `loadOverview` 默认 fan-out 后，此判定即「能否避免一次 RTT」的唯一开关）。
 */
export function resolveLayerValidTimesFromMetadata(metadata: ApiLayer['metadata'] | null | undefined): {
  validTimes: string[]
  requiresFallback: boolean
} {
  if (!metadata) return { validTimes: [], requiresFallback: true }
  const raw = metadata.valid_times
  if (raw === undefined || raw === null) return { validTimes: [], requiresFallback: true }
  if (!Array.isArray(raw)) return { validTimes: [], requiresFallback: true }
  return { validTimes: normalizeValidTimes(raw), requiresFallback: false }
}

/**
 * 活动 `(source, cycle)` 列表的**四态**覆盖。非默认周期时 store 必须传入本入参——否则调用方只能
 * 传 `undefined`，`normalizeLayerStates` 会回落到 `metadata.valid_times`（**默认周期**的列表），
 * 图层报 `available: true` 却带着错周期的时次，`buildM11RegisteredOverlay` 随即拼出跨周期瓦片 URL。
 *
 * 非 `available` 的三个状态**互不相同**，不得塌成一个二元谓词（#2014 round-3 finding A1 的根因）：
 * - `pending`：请求真的在途（记录缺席）。诚实的过渡态，之后必有终态覆盖。
 * - `error`：取回被拒 / 该次请求永不会发出。终态。
 * - `fail-closed`：**已到达且为空**——该源没有任何起报时次覆盖全部流域（后端按网交集的正常
 *   200 输出，见 `apps/api/routes/hydro_display.py` 的 `/layers/discharge/cycles` docstring）。
 *   同样是终态，但什么都没有加载失败，故文案取 `failClosedDischargeDisabledReason`，
 *   与目录 metadata 判定的 fail-closed 落在**同一分支等级**上。
 *
 * - `cycle-not-listed`：该对的列表**已到达且为空**，而该源已到达的周期列表里**没有**这个周期
 *   （跨源书签 `?source=ifs&cycle=<GFS 周期>`、伪造周期；后端对未覆盖对回 200 + 空列表）。
 *   终态；文案点名源与周期（#2131 / design D2），因为该源**有**时次，只是不在这个周期。
 *
 * 以上一律解析为空列表（`available: false`，overlay 为 null，零瓦片请求），各自对应一条独立文案。
 */
export type ActiveCycleValidTimesOverride =
  | { status: 'available'; validTimes: string[] }
  | { status: 'pending' }
  | { status: 'error' }
  | { status: 'fail-closed' }
  | { status: 'cycle-not-listed'; source: string; cycle: string }

/**
 * 「该源不列出这个周期」的禁用文案：点名源（大写）与周期。必须与 `'Layer has no valid times.'`
 * （保留给「列出的周期没有覆盖」）以及 fail-closed / pending / error 三条文案都不相等。
 */
export function cycleNotListedDischargeDisabledReason(source: string, cycle: string): string {
  return `Cycle ${cycle} is not listed for ${source.toUpperCase()}, so it has no valid times for this layer.`
}

/**
 * 活动周期的时次列表尚未取回时的禁用文案。必须与 `'Layer has no valid times.'` 和
 * `failClosedDischargeDisabledReason` 都不相等：这是「还在取」，不是「该周期没有时次」，
 * 也不是「无周期覆盖全部流域」。
 */
export const pendingActiveCycleValidTimesDisabledReason =
  'Valid times for the selected cycle are still loading.'

/** 活动周期的时次列表取回失败（scoped 降级，不是 bootstrap 失败）的禁用文案。 */
export const activeCycleValidTimesErrorDisabledReason =
  'Valid times for the selected cycle could not be loaded.'

/**
 * 活动周期列表处于**未定**态（pending / error）：调用方据此暂缓 validTime 自动校正，保住 URL 状态。
 * `fail-closed` **刻意不在**其中：那是终态，不会再有列表到来，校正照常进行——与目录 metadata
 * 判定的 fail-closed 行为一致（两者产出同一条 `disabledReason`）。`cycle-not-listed` 同理是终态，也不在其中。
 */
export function isM11ActiveCycleValidTimesUnresolved(layer: LayerState | null | undefined): boolean {
  return (
    layer?.disabledReason === pendingActiveCycleValidTimesDisabledReason ||
    layer?.disabledReason === activeCycleValidTimesErrorDisabledReason
  )
}

export function normalizeLayerStates(input: {
  query: Pick<M11QueryState, 'layer' | 'validTime' | 'source' | 'cycle'>
  layers: ApiLayer[]
  // Fallback override：仅当某 layer 的 metadata.valid_times 缺失（undefined/null）时使用；
  // metadata 已为数组（含空数组）时此入参对应 layer 即被忽略，避免反向重写真实 time-less 语义。
  validTimesByLayerId?: Record<string, string[] | undefined>
  // 活动 `(source, cycle)` 的列表（store 按 `(source, cycle)` 取回并缓存）。
  // 「metadata 已是数组即忽略覆盖」的规则只适用于**默认对**：非默认周期时 metadata.valid_times
  // 仍是默认周期的列表，必须由此入参顶掉，否则 LayerState/时间轴/lead 0 都停在默认周期上
  // （spec frontend-mvt-layer-consumption「Non-default cycle fetches its own list」）。
  activeCycleValidTimes?: Record<string, ActiveCycleValidTimesOverride | undefined>
  // 活动源解析出的全国起报时次，原样盖到每个 LayerState 的 `activeNationalCycle` 上（纯透传，
  // 不参与本函数任何既有字段的计算）。解析规则只有 store 的 `nationalDischargeActivePair` 一处，
  // 这里既不推算也不回落；不传 = null = 调用方没有解出周期 → 下游不注册全国叠加层。
  activeNationalCycle?: string | null
  resolvedRun?: ApiHydroRun | null
}): LayerState[] {
  const apiLayersById = new Map(input.layers.map((layer) => [layer.layer_id, layer]))
  const requiredLayers: M11Layer[] = ['discharge']
  const layerIds = [...new Set([...requiredLayers, ...input.layers.map((layer) => layer.layer_id)])]

  return layerIds.map((layerId) => {
    const apiLayer = apiLayersById.get(layerId)
    const metadata = apiLayer?.metadata ?? null
    const { validTimes: metadataValidTimes, requiresFallback } = resolveLayerValidTimesFromMetadata(metadata)
    // metadata 已是数组（含空数组）→ 完全忽略 fallback 覆盖；metadata 缺失才用调用方注入的 fallback。
    const fallbackValidTimes = requiresFallback ? normalizeValidTimes(input.validTimesByLayerId?.[layerId]) : []
    const activeCycleOverride = input.activeCycleValidTimes?.[layerId]
    // 非 available 的三态（pending / error / fail-closed）一律清空时次来源：不能落回 metadata
    // （默认周期的列表）复活 `available: true`。**状态本身**（不是「是/否未定」这个布尔）向下传给文案分支。
    const activeCycleOverrideStatus =
      activeCycleOverride && activeCycleOverride.status !== 'available' ? activeCycleOverride.status : null
    const apiValidTimes = activeCycleOverrideStatus
      ? []
      : activeCycleOverride?.status === 'available'
        ? normalizeValidTimes(activeCycleOverride.validTimes)
        : requiresFallback
          ? fallbackValidTimes
          : metadataValidTimes
    const validTimes = apiValidTimes
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
      validTimeSource: apiValidTimes.length > 0 ? 'api' : 'none',
      activeNationalCycle: input.activeNationalCycle ?? null,
      disabledReason: available
        ? null
        : apiLayer && validTimes.length > 0 && !renderable
          ? 'Layer is registered but no renderable map source is implemented in this repository.'
          : !apiLayer && isKnownRequired
            ? 'Layer is not registered by the API.'
            : // 「该源的周期列表已到达且为空」与「目录判 fail-closed」是同一件事的两个观测面
              // （前者按源、后者只对 GFS 目录成立），故必须在**同一分支等级**上求值——把它排到
              // 下面 pending/error 之后，就会被那两个过渡态文案吃掉（round-3 finding A1）。
              // 目录那半是 **GFS 专有事实**，故存在按源覆盖时一律让位（round-4 finding R4-A）：
              // 否则 fail-closed 目录会盖掉在途非默认源诚实的 pending，校正随即抹掉分享链接的
              // `validTime`。默认源在 fail-closed 目录下恒传 `undefined`，闸门对它是恒等变换。
              (activeCycleOverride === undefined && isFailClosedDischargeMetadata(layerId, metadata)) ||
                activeCycleOverrideStatus === 'fail-closed'
              ? failClosedDischargeDisabledReason
              : activeCycleOverrideStatus === 'pending'
                ? pendingActiveCycleValidTimesDisabledReason
                : activeCycleOverrideStatus === 'error'
                  ? activeCycleValidTimesErrorDisabledReason
                  : activeCycleOverride?.status === 'cycle-not-listed'
                    ? cycleNotListedDischargeDisabledReason(activeCycleOverride.source, activeCycleOverride.cycle)
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

/**
 * 合并无 run 的全局图层目录与按 run 收窄的目录。
 *
 * run-scoped `/layers` 可能把同名基础图层收窄为单流域模板，不能据此覆盖全国河网等无时次基础图层；
 * 时变同名图层则以 scoped 元数据为准，使 discharge 保留当前 run 的 source_refs/valid_times。
 */
export function mergeLayerCatalogs(runlessLayers: ApiLayer[], scopedLayers: ApiLayer[]): ApiLayer[] {
  const merged = new Map(runlessLayers.map((layer) => [layer.layer_id, layer]))
  for (const layer of scopedLayers) {
    const runless = merged.get(layer.layer_id)
    if (runless && isTimeLessLayerMetadata(runless.metadata)) continue
    merged.set(layer.layer_id, layer)
  }
  return [...merged.values()]
}

/**
 * 在展示边界再次保留 bootstrap 的 time-less 基础图层，防止异步快照切换造成图层闪退。
 *
 * **fail-closed 的全国 discharge 不算 time-less**（#2014 round-3 finding A2）：后端在样本时次为空时
 * 把 `default_cycle` 也抹成 `None`（`apps/api/routes/hydro_display.py`），故 `valid_times == []` 与
 * `default_cycle == null` 恒同现——目录 fail-closed 恰好长成 time-less 的样子。照旧钉死的话，store
 * 按所选源重算出的健康 discharge 层既到不了 DOM 也到不了瓦片，store 测试全绿而页面上什么都没变。
 * 排除它是安全的：`discharge` 在 `requiredLayers` 内、恒存在于 `snapshotLayers`，不会造成该钉死
 * 本要防的图层闪退。`mergeLayerCatalogs` 的同名判定**不动**——那是 runless vs run-scoped 的身份问题。
 */
export function mergeLayerStates(bootstrapLayers: LayerState[], snapshotLayers: LayerState[]): LayerState[] {
  const merged = new Map(bootstrapLayers.map((layer) => [layer.layerId, layer]))
  for (const layer of snapshotLayers) {
    const bootstrap = merged.get(layer.layerId)
    if (bootstrap && isTimeLessLayerMetadata(bootstrap.metadata) && !isFailClosedDischargeMetadata(layer.layerId, bootstrap.metadata))
      continue
    merged.set(layer.layerId, layer)
  }
  return [...merged.values()]
}

function isTimeLessLayerMetadata(metadata: ApiLayer['metadata'] | null | undefined) {
  return Array.isArray(metadata?.valid_times) && metadata.valid_times.length === 0
}

/**
 * fail-closed 禁用态的文案。必须与 `'Layer has no valid times.'` **不相等**：
 * I11/I12 据此区分「该图层本来就没有时间维度」与「没有任何起报时次覆盖全部流域」
 * （spec map-layer-timeline-controls「Cycle selector is fail-closed」）。
 */
export const failClosedDischargeDisabledReason =
  'No cycle covers every basin, so the national discharge layer is disabled.'

/**
 * `discharge` 的 `metadata.valid_times === []` 且 `default_cycle` 为空 = 全国交集 fail-closed 禁用态，
 * **不是** time-less 图层（spec frontend-mvt-layer-consumption「Discharge with an empty list is
 * fail-closed, not time-less」）：不发 fallback valid-times、不请求瓦片、不注册 overlay。
 * `default_cycle` 用宽松判空（`== null`）：老目录/测试 fixture 可能整个字段缺省。
 */
export function isFailClosedDischargeMetadata(
  layerId: string,
  metadata: ApiLayer['metadata'] | null | undefined,
): boolean {
  return (
    layerId === 'discharge' &&
    Boolean(metadata) &&
    Array.isArray(metadata?.valid_times) &&
    metadata.valid_times.length === 0 &&
    metadata.default_cycle == null
  )
}

export function getM11LayerLegend(layerId: string): LayerLegendEntry[] {
  return layerLegend(layerId)
}

function isM11RenderableLayer(layerId: string) {
  return layerId === 'discharge'
}

function normalizeValidTimes(values: string[] | undefined): string[] {
  return [...new Set((values ?? []).map(normalizeIsoString).filter((value): value is string => Boolean(value)))].sort(
    (a, b) => Date.parse(a) - Date.parse(b),
  )
}

/**
 * 默认位置是活动周期的**首项**（lead 0），不是末项——spec map-layer-timeline-controls
 * 「Default position is the cycle start」/「Active layer changes」：URL 无 validTime、或切换
 * source/cycle 后旧时次不在新列表里，都必须回到首项，绝不渲染陈旧数据。
 */
function pickCurrentValidTime(validTimes: string[], queryValidTime: string | null): string | null {
  if (validTimes.length === 0) return null
  const normalizedQuery = normalizeIsoString(queryValidTime)
  if (normalizedQuery && validTimes.includes(normalizedQuery)) return normalizedQuery
  return validTimes[0]
}

/**
 * 只把新的 `validTime` 重新套到既有 LayerState 上（#2127 / design D1）：`currentValidTime` 与
 * `normalizeLayerStates` 同一条 `pickCurrentValidTime`，freshness 与之同一条规则（时次 + 过期判定 +
 * 缺时次文案）；其余字段（列表、禁用文案、周期章）原样保留。用于冻结的 bootstrap 快照——它不得
 * 从活的 cycles / valid-times 记录重建。`createFreshnessMetadata` 对已归一的字段是幂等的。
 */
export function retimeLayerStates(layerStates: LayerState[], validTime: string | null): LayerState[] {
  return layerStates.map((layer) => {
    const currentValidTime = pickCurrentValidTime(layer.validTimes, validTime)
    return {
      ...layer,
      currentValidTime,
      freshness: createFreshnessMetadata({
        ...layer.freshness,
        validTime: currentValidTime,
        unavailableReason: currentValidTime ? null : 'No valid-time metadata is available.',
      }),
    }
  })
}

function layerGroup(layer: ApiLayer | undefined, layerId: string): LayerState['group'] {
  const type = `${layer?.layer_type ?? ''} ${layerId}`.toLowerCase()
  if (type.includes('met') || type.includes('precip') || type.includes('temperature')) return 'meteorology'
  if (type.includes('base') || type.includes('boundary') || type.includes('dem')) return 'base'
  if (type.includes('hydro') || type.includes('discharge')) return 'hydrology'
  return 'unknown'
}

function layerLegend(layerId: string): LayerLegendEntry[] {
  if (layerId === 'discharge') return m11RiverDischargeLegend.map((entry) => ({ ...entry }))
  return []
}

// 色带与 MVT 瓦片 paint（dischargeTileLayerPaint）同源（ColorBrewer 蓝系、log 阶分桶）。
// 桶界按实测分布定（近 2 日 q_down 分位 p50≈0.0003 / p90≈1.6 / max≈307 m3/s）：
// 线性桶或高锚 log 桶都会让山区小流域整网落最低一桶 → 统一蓝无梯度。null 用沉静蓝灰。
export function m11DischargeColor(value: number | null) {
  if (value === null) return '#94ADC7'
  if (value >= 10_000) return '#CB181D'
  if (value >= 1_000) return '#08306B'
  if (value >= 100) return '#08519C'
  if (value >= 10) return '#2171B5'
  if (value >= 1) return '#4292C6'
  return '#7FB8DC'
}

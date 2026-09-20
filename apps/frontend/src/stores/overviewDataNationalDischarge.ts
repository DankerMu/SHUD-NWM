import { toSecondsPrecisionInstant } from '@/lib/m11/instants'
import { resolveNationalScaleSource, type ApiLayer } from '@/lib/m11/overviewDataContracts'
import type { M11QueryState } from '@/lib/m11/queryState'
import type { DischargeCyclesState } from '@/stores/overviewDataTypes'

/**
 * 全国尺度的具体源：`best` 归一为 `gfs`（selection 层的全国口径），`compare` 解析不出具体源。
 * store 绝不发出 `cycles?source=best|compare`、`valid-times?source=best|compare`、
 * `/api/v1/precip/best|compare/...`——解析不出就一条都不发（fixture 决策 8）。
 */
export function nationalConcreteSource(source: M11QueryState['source']): 'gfs' | 'ifs' | null {
  const resolved = resolveNationalScaleSource(source)
  return resolved === 'gfs' || resolved === 'ifs' ? resolved : null
}

export type NationalDischargePair = { source: 'gfs' | 'ifs'; cycle: string; isDefault: boolean }

/** 目录声明的默认源。`metadata.default_cycle` / `valid_times` 只对这个源成立。 */
export function nationalDischargeDefaultSource(layers: ApiLayer[]): string {
  return layers.find((layer) => layer.layer_id === 'discharge')?.metadata?.default_source ?? 'gfs'
}

/**
 * 某个源自己声明的默认周期（`/api/v1/layers/discharge/cycles?source=` 的 `default_cycle`）。
 * 解不出来（记录缺席 / 非 available / `default_cycle` 为空）一律返回 null；**记录本身的状态**
 * 由调用方另行区分（`buildLayerStates` 的三态分类），本函数只回答「有没有周期」。
 * 变形响应已在 `fetchDischargeCycles` 的 loader 内被形状守卫拒收（design D1），落成 scoped `'error'`
 * 记录，不会以 `available` 进来；这里的宽松判空只是纵深防御。
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
export function nationalDischargeActivePair(
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
export function nationalDischargeSourceUnresolved(
  query: M11QueryState,
  layers: ApiLayer[],
  cyclesBySource: Record<string, DischargeCyclesState>,
): boolean {
  const source = nationalConcreteSource(query.source)
  if (!source || source === nationalDischargeDefaultSource(layers)) return false
  return nationalDischargeActivePair(query, layers, cyclesBySource) === null
}

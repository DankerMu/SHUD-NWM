import { toSecondsPrecisionInstant } from '@/lib/m11/instants'
import { m11SourceCycleKey, type PrecipIndexState } from '@/stores/overviewData'

/**
 * 降水叠加的隐藏原因是**闭合枚举**，不是布尔（fixture #2015 决策 1）。
 * 把它塌成 `visible: boolean` 会让三种互不相同的事实（在途 / 取回失败 / 该周期无镜像）
 * 共用一条文案，用户与 vitest 都分不出「等一下就有」和「这个周期永远没有」。
 *
 * - `disabled`            `state.precip === false`：不注册 source、无提示、无图例段
 * - `no_concrete_source`  `compare`，或 `best` 未解析出具体源：隐藏 + 提示 A
 * - `index_pending`       活动 `(source, cycle)` 未解出或 store 里键缺席（在途/未取）：隐藏，**无提示**
 * - `index_error`         store `{status:'error'}`（含 PRECIP_WINDOW_INCOMPLETE 404、网络错误），
 *                         或 `available` 但 `bounds` 长度 ≠ 4：隐藏 + 提示 B
 * - `cycle_not_mirrored`  store `{status:'not_mirrored'}`：隐藏 + 提示 C
 * - `window_incomplete`   `available` 但当前时次 ∉ `valid_times[]`（秒精度）：隐藏 + 提示 D
 */
export type M11PrecipHiddenReason =
  | 'disabled'
  | 'no_concrete_source'
  | 'index_pending'
  | 'index_error'
  | 'cycle_not_mirrored'
  | 'window_incomplete'

/** MapLibre `image` source 的四角顺序：NW, NE, SE, SW（`maplibre-gl` image_source：自左上角起顺时针）。 */
export type M11PrecipCoordinates = [[number, number], [number, number], [number, number], [number, number]]

export interface M11PrecipOverlayModel {
  /** 仅 visible 时非 null；null ⇒ 整个 `Source` 不渲染 ⇒ 绝不发出 PNG 请求。 */
  url: string | null
  coordinates: M11PrecipCoordinates | null
  /** `null` ⇔ visible ⇔ `url !== null`。 */
  hiddenReason: M11PrecipHiddenReason | null
  /** 按上表；`index_pending` 与 `disabled` 恒为 null。 */
  notice: string | null
}

export interface M11PrecipOverlayInput {
  /** `M11QueryState.precip`（URL 布尔开关，默认 true）。 */
  precip: boolean
  /**
   * 已解析的**具体**源。本函数**不**自行解析 `best`：全国调用点传
   * `nationalConcreteSource(state.source)`，流域详情将来传 `resolveSelectedSource` 的结果。
   * `null` = `compare` 或未解析出具体源 ⇒ 隐藏 + 提示 A（绝不拼 `/api/v1/precip/best|compare/…`）。
   */
  concreteSource: 'gfs' | 'ifs' | null
  /** 活动全国起报时次，唯一来源是 `resolveNationalOverlayCycle(dischargeLayerState)`。 */
  cycle: string | null
  /** 活动时次，唯一来源是同一个 discharge `LayerState.currentValidTime`（与流量层逐字同源）。 */
  validTime: string | null
  /** store 的 `precipIndexByCycle`，键为 `m11SourceCycleKey(source, cycle)`。 */
  precipIndexByCycle: Record<string, PrecipIndexState>
}

/** 提示 A：源解析不出具体的 GFS/IFS。 */
export const M11_PRECIP_NOTICE_NO_CONCRETE_SOURCE = '当前数据源无法解析为具体的 GFS/IFS 源，降水叠加已隐藏'
/** 提示 B：降水索引取回失败（含 PRECIP_WINDOW_INCOMPLETE 404、网络错误、畸形 bounds）。 */
export const M11_PRECIP_NOTICE_INDEX_ERROR = '降水索引加载失败，降水叠加已隐藏'
/** 提示 C：该周期无降水镜像（404 PRECIP_CYCLE_NOT_MIRRORED）。 */
export const M11_PRECIP_NOTICE_CYCLE_NOT_MIRRORED = '该周期无降水镜像，降水叠加已隐藏'
/** 提示 D：该时次的 24h 降水窗口不完整（时次不在 index 的 valid_times 内）。 */
export const M11_PRECIP_NOTICE_WINDOW_INCOMPLETE = '该时次 24h 降水窗口不完整，降水叠加已隐藏'

function hidden(hiddenReason: M11PrecipHiddenReason, notice: string | null = null): M11PrecipOverlayModel {
  return { url: null, coordinates: null, hiddenReason, notice }
}

/**
 * `PrecipIndex.bounds` 的类型是 `number[]`（`api/types.ts`），语义 `[w, s, e, n]`。
 * 长度 ≠ 4 是后端契约被破坏，不是「窗口不完整」：按 `index_error` fail-closed，绝不猜四角。
 */
function coordinatesFromBounds(bounds: number[]): M11PrecipCoordinates | null {
  if (bounds.length !== 4) return null
  const [west, south, east, north] = bounds
  return [
    [west, north],
    [east, north],
    [east, south],
    [west, south],
  ]
}

/**
 * 求值阶梯（前一臂命中即短路，**枚举声明顺序不是规则，本函数的分支顺序才是**）：
 * 1. `precip === false` → `disabled`
 * 2. `concreteSource === null` → `no_concrete_source`（`compare` 同时也没有活动对、键也缺席，
 *    但本臂先命中，用户看到的是「源解析不出」而不是沉默）
 * 3. 周期或时次未解出、或 store 键缺席 → `index_pending`（隐藏、**无提示**：流量层的
 *    禁用/fail-closed 文案已经覆盖了这一态，再叠一条降水提示是噪音）
 * 4. `error`，或 `available` 但 `bounds` 长度 ≠ 4 → `index_error`
 * 5. `not_mirrored` → `cycle_not_mirrored`
 * 6. `available` 但时次 ∉ `valid_times`（秒精度比对）→ `window_incomplete`
 * 7. 否则 visible。
 */
export function resolveM11PrecipOverlay(input: M11PrecipOverlayInput): M11PrecipOverlayModel {
  if (!input.precip) return hidden('disabled')

  const source = input.concreteSource
  if (!source) return hidden('no_concrete_source', M11_PRECIP_NOTICE_NO_CONCRETE_SOURCE)

  // 秒精度是后端唯一认识的拼写：`LayerState` 时次是毫秒形、index 是秒形（#2012 决策 3 同一 helper）。
  // 解析不出（null / 非法拼写）= 活动身份未解出 = 与「键缺席」同一态，一律 fail-closed 到 pending。
  const cycle = toSecondsPrecisionInstant(input.cycle)
  const validTime = toSecondsPrecisionInstant(input.validTime)
  if (!cycle || !validTime) return hidden('index_pending')

  const indexState = input.precipIndexByCycle[m11SourceCycleKey(source, cycle)]
  if (!indexState) return hidden('index_pending')

  if (indexState.status === 'error') return hidden('index_error', M11_PRECIP_NOTICE_INDEX_ERROR)
  if (indexState.status === 'not_mirrored') return hidden('cycle_not_mirrored', M11_PRECIP_NOTICE_CYCLE_NOT_MIRRORED)

  const coordinates = coordinatesFromBounds(indexState.index.bounds)
  if (!coordinates) return hidden('index_error', M11_PRECIP_NOTICE_INDEX_ERROR)

  const inWindow = indexState.index.valid_times.some((candidate) => toSecondsPrecisionInstant(candidate) === validTime)
  if (!inWindow) return hidden('window_incomplete', M11_PRECIP_NOTICE_WINDOW_INCOMPLETE)

  return {
    url: `/api/v1/precip/${source}/${cycle}/${validTime}.png`,
    coordinates,
    hiddenReason: null,
    notice: null,
  }
}

import { buildApiUrl } from '@/api/base'
import { toSecondsPrecisionInstant } from '@/lib/m11/instants'
import { m11SourceCycleKey, type PrecipIndexState } from '@/stores/overviewData'

/**
 * 降水叠加的隐藏原因是**闭合枚举**，不是布尔（fixture #2015 决策 1）。
 * 把它塌成 `visible: boolean` 会让三种互不相同的事实（在途 / 取回失败 / 该周期无镜像）
 * 共用一条文案，用户与 vitest 都分不出「等一下就有」和「这个周期永远没有」。
 *
 * - `disabled`            入参 `precip === false`，即「用户关了开关」**或**「目录已确认无 `precip`
 *                         条目」这两个**终态**（见 `M11PrecipOverlayInput.precip`）：不注册
 *                         source、无提示、无图例段。目录**在途**不属此臂——它落第 3 臂
 * - `no_concrete_source`  `compare`，或 `best` 未解析出具体源：隐藏 + 提示 A
 * - `index_pending`       活动 `(source, cycle)` 未解出，或 store 里键缺席**且本轮 enrichment 未被跳过**
 *                         （请求真的在途）：隐藏，**无提示**
 * - `index_error`         store `{status:'error'}`（含 PRECIP_WINDOW_INCOMPLETE 404、网络错误），
 *                         或键缺席而本轮 enrichment 已被跳过（index 永远不会到达），
 *                         或 `available` 但 `bounds` / `valid_times` 不是合法数组：隐藏 + 提示 B
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
  /**
   * 「叠加是否被请求」的**合成**开关 =「用户开着」**且**「目录未被确认为无 `precip` 条目」：
   * 调用点必须传 `state.precip && precipAvailability !== 'absent'`（目录可用性是三值，见
   * `M11PrecipAvailability`）。目录**确认**无条目时开关本就 `disabled` 并标「未实现」，叠加与
   * 提示必须一并归 `disabled`——否则会出现「栅格已画 / 提示已出，开关却按不动」这一格（fixture
   * #2015 决策 1 第 1 臂 / T2-IS-3，部署错位窗口可达）。而目录**在途**（`'unknown'`）必须传
   * `true`：它不是终态否定，落第 3 臂 `index_pending` 才诚实，否则 `disabled` 臂同时承载三件
   * 互不相同的事实，`hiddenReason` 这个 oracle 失去区分力（IS-5）。
   */
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
  /**
   * store 的 `layerTimeEnrichmentSkipped`：本轮 layer-time enrichment 整段被跳过（bootstrap
   * 失败），index 请求一条都不会发。此时「键缺席」是终态失败而不是在途。
   */
  enrichmentSkipped: boolean
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
 * 长度 ≠ 4（含字段整个缺席——`unwrapApiData` 是裸 `as T`，零字段校验）是后端契约被破坏，
 * 不是「窗口不完整」：按 `index_error` fail-closed，绝不猜四角，也绝不让 `.length` 抛进
 * `useMemo`（全仓无 ErrorBoundary，抛出即白屏整页）。
 */
function coordinatesFromBounds(bounds: number[]): M11PrecipCoordinates | null {
  if (!Array.isArray(bounds) || bounds.length !== 4) return null
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
 * 3. 周期或时次未解出 → `index_pending`；store 键缺席时再分两态：`enrichmentSkipped` 为真
 *    （本轮 index 请求一条都不会发）→ `index_error`（提示 B），否则请求真的在途 →
 *    `index_pending`（隐藏、**无提示**：流量层的禁用/fail-closed 文案已经覆盖了在途这一态）
 * 4. `error`，或 `available` 但 `bounds` / `valid_times` 不是合法数组 → `index_error`
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
  // 键缺席**不恒等于**在途：bootstrap 失败时整条 layer-time enrichment 被跳过，index 永远不会
  // 到达，而阶段 2 仍能把流量层渲染正常——此时「无提示地静默隐藏」是谎报（IS-2）。
  if (!indexState) {
    return input.enrichmentSkipped ? hidden('index_error', M11_PRECIP_NOTICE_INDEX_ERROR) : hidden('index_pending')
  }

  if (indexState.status === 'error') return hidden('index_error', M11_PRECIP_NOTICE_INDEX_ERROR)
  if (indexState.status === 'not_mirrored') return hidden('cycle_not_mirrored', M11_PRECIP_NOTICE_CYCLE_NOT_MIRRORED)

  const coordinates = coordinatesFromBounds(indexState.index.bounds)
  if (!coordinates) return hidden('index_error', M11_PRECIP_NOTICE_INDEX_ERROR)

  // `valid_times` 同样 fail-closed：字段缺席时 `.some` 会抛 TypeError（见 coordinatesFromBounds 注释）。
  const validTimes = indexState.index.valid_times
  if (!Array.isArray(validTimes)) return hidden('index_error', M11_PRECIP_NOTICE_INDEX_ERROR)

  const inWindow = validTimes.some((candidate) => toSecondsPrecisionInstant(candidate) === validTime)
  if (!inWindow) return hidden('window_incomplete', M11_PRECIP_NOTICE_WINDOW_INCOMPLETE)

  return {
    // `buildApiUrl` 与 `buildMvtTileUrlTemplate` 同一约定：index 走 openapi client（已带
    // `baseUrl: apiBaseUrl`），PNG 若留裸相对路径，跨源部署（`VITE_API_BASE_URL`）下会解析到
    // **页面源** → 404 → 透明栅格而模型仍报 visible（`frontend-production-readiness` spec）。
    url: buildApiUrl(`/api/v1/precip/${source}/${cycle}/${validTime}.png`),
    coordinates,
    hiddenReason: null,
    notice: null,
  }
}

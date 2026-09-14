import type { components } from '@/api/types'
import type { ApiBasin, ApiHydroRunPage, ApiLayer } from '@/lib/m11/overviewDataContracts'

/**
 * 全国总览六个端点的载荷形状守卫（#2129 裁决 B / design D1）。
 *
 * `unwrapApiData` 是裸 `as T`：后端形状漂移要么在消费处抛错，要么静默变成另一种状态。这里只校验
 * **消费方真正解引用的最小形状**（含元素形状）；多余字段忽略，可空字段仍可空。校验在各 fetcher
 * 的 `cached()` loader 内执行，抛出的 `DataShapeError` 就是一次 loader reject：不进缓存，沿既有
 * scoped 降级路径落地，并由 store 记为「数据异常」。
 */
export class DataShapeError extends Error {
  readonly label: string

  constructor(label: string) {
    super(`${label}: 数据异常`)
    this.name = 'DataShapeError'
    this.label = label
  }
}

export function isDataShapeError(error: unknown): error is DataShapeError {
  return error instanceof DataShapeError
}

const LABEL_LAYERS = '图层目录'
const LABEL_CYCLES = '起报时次'
const LABEL_VALID_TIMES = '有效时次'
const LABEL_PRECIP_INDEX = '降水索引'
const LABEL_BASINS = '流域清单'
const LABEL_RUNS = '运行记录'

type DischargeCycles = components['schemas']['DischargeCycles']
type LayerValidTimes = components['schemas']['LayerValidTimes']
type PrecipIndex = components['schemas']['PrecipIndex']

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((entry) => typeof entry === 'string')
}

function isRecordArrayWithStringKey(value: unknown, key: string): boolean {
  return Array.isArray(value) && value.every((entry) => isRecord(entry) && typeof entry[key] === 'string')
}

function isOptional(value: unknown, predicate: (value: unknown) => boolean): boolean {
  return value === undefined || value === null || predicate(value)
}

function guard<T>(label: string, valid: boolean, value: unknown): T {
  if (!valid) throw new DataShapeError(label)
  return value as T
}

/** `/api/v1/layers`：数组；元素为带字符串 `layer_id` 的对象；`metadata` 缺席 / null / 对象。 */
export function validateLayers(value: unknown): ApiLayer[] {
  const valid =
    Array.isArray(value) &&
    value.every((entry) => isRecord(entry) && typeof entry.layer_id === 'string' && isOptional(entry.metadata, isRecord))
  return guard(LABEL_LAYERS, valid, value)
}

/** `/api/v1/layers/discharge/cycles`：对象；`cycles` 为带字符串 `cycle_time` 的对象数组；`default_cycle` 缺席 / null / 字符串。 */
export function validateDischargeCycles(value: unknown): DischargeCycles {
  const valid =
    isRecord(value) &&
    isRecordArrayWithStringKey(value.cycles, 'cycle_time') &&
    isOptional(value.default_cycle, (entry) => typeof entry === 'string')
  return guard(LABEL_CYCLES, valid, value)
}

/** `/api/v1/layers/{layer_id}/valid-times`：字符串数组，或 `valid_times` 为字符串数组的对象。 */
export function validateLayerValidTimes(value: unknown): LayerValidTimes | string[] {
  const valid = isStringArray(value) || (isRecord(value) && isStringArray(value.valid_times))
  return guard(LABEL_VALID_TIMES, valid, value)
}

/** `/api/v1/precip/{source}/{cycle}/index`：对象；`bounds` 为 4 个有限数；`valid_times` 为字符串数组。 */
export function validatePrecipIndex(value: unknown): PrecipIndex {
  const valid =
    isRecord(value) &&
    Array.isArray(value.bounds) &&
    value.bounds.length === 4 &&
    value.bounds.every((entry) => typeof entry === 'number' && Number.isFinite(entry)) &&
    isStringArray(value.valid_times)
  return guard(LABEL_PRECIP_INDEX, valid, value)
}

/** `/api/v1/basins`：数组；元素为带字符串 `basin_id` 的对象。 */
export function validateBasins(value: unknown): ApiBasin[] {
  return guard(LABEL_BASINS, isRecordArrayWithStringKey(value, 'basin_id'), value)
}

/** `/api/v1/runs`（单页）：对象；`items` 为带字符串 `run_id` 的对象数组。 */
export function validateRunsPage(value: unknown): ApiHydroRunPage {
  return guard(LABEL_RUNS, isRecord(value) && isRecordArrayWithStringKey(value.items, 'run_id'), value)
}

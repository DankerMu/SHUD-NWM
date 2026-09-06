export type M11Source = 'gfs' | 'ifs' | 'best' | 'compare'
export type M11Layer = 'discharge'
export type M11Basemap = 'terrain' | 'satellite' | 'vector'

export interface M11QueryState {
  source: M11Source
  cycle: string | null
  validTime: string | null
  layer: M11Layer
  metStations: boolean
  /**
   * 降水叠加是布尔开关，不是 `M11Layer` 枚举值（spec design.md D5）。
   * URL 约定：缺省/任何非 `0` 值 → true；`precip=0` → false。
   */
  precip: boolean
  basemap: M11Basemap
  basinVersionId: string | null
  riverNetworkVersionId: string | null
  basinId: string | null
  segmentId: string | null
  q: string | null
}

export type M11QueryPatch = Partial<{
  [Key in keyof M11QueryState]: M11QueryState[Key] | null | undefined
}>

const sources = ['gfs', 'ifs', 'best', 'compare'] as const
const layers = ['discharge'] as const
const basemaps = ['terrain', 'satellite', 'vector'] as const
const legacyMetStationsLayer = 'met-stations'

export const defaultM11QueryState: M11QueryState = {
  // 全国尺度默认源为 GFS（spec map-layer-timeline-controls「Source selector renders required choices」）。
  // `best` 仍是合法可解析值（流域详情 Best Available），但不再是默认值；`best → gfs` 的归一
  // 属于 selection 层（`createSourceScenarioSelection` 的 national scale），不在本 parser 内。
  source: 'gfs',
  cycle: null,
  validTime: null,
  layer: 'discharge',
  metStations: false,
  precip: true,
  basemap: 'vector',
  basinVersionId: null,
  riverNetworkVersionId: null,
  basinId: null,
  segmentId: null,
  q: null,
}

function isOneOf<T extends readonly string[]>(value: string | null, allowed: T): value is T[number] {
  return value !== null && (allowed as readonly string[]).includes(value)
}

function normalizeSource(value: string | null): M11Source | null {
  const normalized = value?.trim().toLowerCase() ?? null
  return isOneOf(normalized, sources) ? normalized : null
}

const rfc3339InstantPattern =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$/

function parseInteger(value: string) {
  return Number.parseInt(value, 10)
}

function offsetMinutes(value: string) {
  if (value === 'Z') return 0
  if (value === '-00:00') return null
  const sign = value[0] === '-' ? -1 : 1
  const hours = parseInteger(value.slice(1, 3))
  const minutes = parseInteger(value.slice(4, 6))
  if (hours > 23 || minutes > 59) return null
  return sign * (hours * 60 + minutes)
}

function fractionalMilliseconds(value: string | undefined) {
  if (!value) return 0
  // Canonical query instants use Date.toISOString(), so fractional precision is
  // normalized to JavaScript's millisecond precision. Extra RFC3339 fractional
  // digits are accepted and truncated to the first three digits here.
  return parseInteger(value.slice(1, 4).padEnd(3, '0'))
}

function normalizeIsoInstant(value: string | null) {
  if (!value) return null
  const trimmed = value.trim()
  if (!trimmed) return null
  const match = rfc3339InstantPattern.exec(trimmed)
  if (!match) return null

  const [, yearValue, monthValue, dayValue, hourValue, minuteValue, secondValue, fractionValue, zoneValue] = match
  const year = parseInteger(yearValue)
  const month = parseInteger(monthValue)
  const day = parseInteger(dayValue)
  const hour = parseInteger(hourValue)
  const minute = parseInteger(minuteValue)
  const second = parseInteger(secondValue)
  const millisecond = fractionalMilliseconds(fractionValue)
  const offset = offsetMinutes(zoneValue)

  if (offset === null) return null
  if (month < 1 || month > 12) return null
  if (hour > 23 || minute > 59 || second > 59) return null

  const timestamp = Date.UTC(year, month - 1, day, hour, minute, second, millisecond) - offset * 60_000
  if (!Number.isFinite(timestamp)) return null

  const localDate = new Date(timestamp + offset * 60_000)
  if (
    localDate.getUTCFullYear() !== year ||
    localDate.getUTCMonth() !== month - 1 ||
    localDate.getUTCDate() !== day ||
    localDate.getUTCHours() !== hour ||
    localDate.getUTCMinutes() !== minute ||
    localDate.getUTCSeconds() !== second ||
    localDate.getUTCMilliseconds() !== millisecond
  ) {
    return null
  }

  return new Date(timestamp).toISOString()
}

export function normalizeM11Identifier(value: string | null | undefined) {
  if (!value) return null
  const trimmed = value.trim()
  return /^[A-Za-z0-9._:-]{1,96}$/.test(trimmed) ? trimmed : null
}

function normalizeSearch(value: string | null) {
  if (!value) return null
  const trimmed = value.trim()
  return trimmed.length > 0 && trimmed.length <= 120 ? trimmed : null
}

export function parseM11QueryState(input: string | URLSearchParams): M11QueryState {
  const params = typeof input === 'string' ? new URLSearchParams(input) : input
  const source = normalizeSource(params.get('source'))
  const layerValues = params.getAll('layer')
  const layer = layerValues.find((value): value is M11Layer => isOneOf(value, layers)) ?? null
  const basemap = params.get('basemap')
  const hasLegacyMetStationsLayer = layerValues.includes(legacyMetStationsLayer)

  return {
    source: source ?? defaultM11QueryState.source,
    cycle: normalizeIsoInstant(params.get('cycle')),
    validTime: normalizeIsoInstant(params.get('validTime')),
    layer: layer ?? defaultM11QueryState.layer,
    metStations: params.get('metStations') === '1' || hasLegacyMetStationsLayer,
    precip: params.get('precip') !== '0',
    basemap: isOneOf(basemap, basemaps) ? basemap : defaultM11QueryState.basemap,
    basinVersionId: normalizeM11Identifier(params.get('basinVersionId')),
    riverNetworkVersionId: normalizeM11Identifier(params.get('riverNetworkVersionId')),
    basinId: normalizeM11Identifier(params.get('basinId')),
    segmentId: normalizeM11Identifier(params.get('segmentId')),
    q: normalizeSearch(params.get('q')),
  }
}

function queryParamsFromState(state: M11QueryPatch) {
  const params = new URLSearchParams()
  Object.entries(state).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return
    if (typeof value === 'boolean') {
      if (value) params.set(key, '1')
      // 其它布尔一律「false 即丢弃」（逐字保持既有行为）；只有 precip 的 false 必须带着
      // 走完 `serializeM11QueryState` 内部那趟 parse 归一，否则会被默认值 true 吃回去。
      else if (key === 'precip') params.set(key, '0')
      return
    }
    params.set(key, String(value))
  })
  return params
}

export function serializeM11QueryState(state: M11QueryPatch) {
  const normalized = parseM11QueryState(queryParamsFromState(state))
  const params = new URLSearchParams()

  if (normalized.source !== defaultM11QueryState.source) params.set('source', normalized.source)
  if (normalized.cycle) params.set('cycle', normalized.cycle)
  if (normalized.validTime) params.set('validTime', normalized.validTime)
  if (normalized.layer !== defaultM11QueryState.layer) params.set('layer', normalized.layer)
  if (normalized.metStations) params.set('metStations', '1')
  if (!normalized.precip) params.set('precip', '0')
  if (normalized.basemap !== defaultM11QueryState.basemap) params.set('basemap', normalized.basemap)
  if (normalized.basinVersionId) params.set('basinVersionId', normalized.basinVersionId)
  if (normalized.riverNetworkVersionId) params.set('riverNetworkVersionId', normalized.riverNetworkVersionId)
  if (normalized.basinId) params.set('basinId', normalized.basinId)
  if (normalized.segmentId) params.set('segmentId', normalized.segmentId)
  if (normalized.q) params.set('q', normalized.q)

  return params.toString()
}

export function serializeM11QueryHandoff(state: M11QueryState, patch: M11QueryPatch = {}) {
  return serializeM11QueryState({ ...state, ...patch })
}

export function m11QueryHref(pathname: string, state: M11QueryState, patch: M11QueryPatch = {}) {
  const search = serializeM11QueryHandoff(state, patch)
  return `${pathname}${search ? `?${search}` : ''}`
}

export function normalizeM11QueryPatch(patch: M11QueryPatch) {
  return parseM11QueryState(queryParamsFromState(patch))
}

export function needsM11QueryReplacement(search: string) {
  const normalized = serializeM11QueryState(parseM11QueryState(search))
  const current = search.startsWith('?') ? search.slice(1) : search
  return normalized !== current
}

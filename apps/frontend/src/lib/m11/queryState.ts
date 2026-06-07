export type M11Source = 'gfs' | 'ifs' | 'best' | 'compare'
export type M11Layer = 'discharge' | 'water-level' | 'flood-return-period' | 'warning-level'
export type M11Basemap = 'terrain' | 'satellite' | 'vector'
export type M11QueryWarningLevel = 'normal' | 'elevated' | 'watch' | 'warning' | 'major' | 'severe' | 'extreme' | 'orange' | 'red'

export interface M11QueryState {
  source: M11Source
  cycle: string | null
  validTime: string | null
  layer: M11Layer
  basemap: M11Basemap
  basinVersionId: string | null
  riverNetworkVersionId: string | null
  basinId: string | null
  segmentId: string | null
  warningLevel: M11QueryWarningLevel | null
  q: string | null
}

export type M11QueryPatch = Partial<Record<keyof M11QueryState, string | null | undefined>>

const sources = ['gfs', 'ifs', 'best', 'compare'] as const
const layers = ['discharge', 'water-level', 'flood-return-period', 'warning-level'] as const
const basemaps = ['terrain', 'satellite', 'vector'] as const
const warningLevels = ['normal', 'elevated', 'watch', 'warning', 'major', 'severe', 'extreme', 'orange', 'red'] as const

export const defaultM11QueryState: M11QueryState = {
  source: 'best',
  cycle: null,
  validTime: null,
  layer: 'discharge',
  basemap: 'vector',
  basinVersionId: null,
  riverNetworkVersionId: null,
  basinId: null,
  segmentId: null,
  warningLevel: null,
  q: null,
}

function isOneOf<T extends readonly string[]>(value: string | null, allowed: T): value is T[number] {
  return value !== null && (allowed as readonly string[]).includes(value)
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
  const source = params.get('source')
  const layer = params.get('layer')
  const basemap = params.get('basemap')
  const warningLevel = params.get('warningLevel')

  return {
    source: isOneOf(source, sources) ? source : defaultM11QueryState.source,
    cycle: normalizeIsoInstant(params.get('cycle')),
    validTime: normalizeIsoInstant(params.get('validTime')),
    layer: isOneOf(layer, layers) ? layer : defaultM11QueryState.layer,
    basemap: isOneOf(basemap, basemaps) ? basemap : defaultM11QueryState.basemap,
    basinVersionId: normalizeM11Identifier(params.get('basinVersionId')),
    riverNetworkVersionId: normalizeM11Identifier(params.get('riverNetworkVersionId')),
    basinId: normalizeM11Identifier(params.get('basinId')),
    segmentId: normalizeM11Identifier(params.get('segmentId')),
    warningLevel: isOneOf(warningLevel, warningLevels) ? warningLevel : defaultM11QueryState.warningLevel,
    q: normalizeSearch(params.get('q')),
  }
}

export function serializeM11QueryState(state: M11QueryState) {
  const normalized = parseM11QueryState(new URLSearchParams(Object.entries(state).flatMap(([key, value]) => (value ? [[key, value]] : []))))
  const params = new URLSearchParams()

  if (normalized.source !== defaultM11QueryState.source) params.set('source', normalized.source)
  if (normalized.cycle) params.set('cycle', normalized.cycle)
  if (normalized.validTime) params.set('validTime', normalized.validTime)
  if (normalized.layer !== defaultM11QueryState.layer) params.set('layer', normalized.layer)
  if (normalized.basemap !== defaultM11QueryState.basemap) params.set('basemap', normalized.basemap)
  if (normalized.basinVersionId) params.set('basinVersionId', normalized.basinVersionId)
  if (normalized.riverNetworkVersionId) params.set('riverNetworkVersionId', normalized.riverNetworkVersionId)
  if (normalized.basinId) params.set('basinId', normalized.basinId)
  if (normalized.segmentId) params.set('segmentId', normalized.segmentId)
  if (normalized.warningLevel) params.set('warningLevel', normalized.warningLevel)
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
  const params = new URLSearchParams()
  Object.entries(patch).forEach(([key, value]) => {
    if (value !== undefined && value !== null) params.set(key, value)
  })
  return parseM11QueryState(params)
}

export function needsM11QueryReplacement(search: string) {
  const normalized = serializeM11QueryState(parseM11QueryState(search))
  const current = search.startsWith('?') ? search.slice(1) : search
  return normalized !== current
}

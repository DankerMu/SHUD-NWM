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
  segmentId: null,
  warningLevel: null,
  q: null,
}

function isOneOf<T extends readonly string[]>(value: string | null, allowed: T): value is T[number] {
  return value !== null && (allowed as readonly string[]).includes(value)
}

function normalizeIsoInstant(value: string | null) {
  if (!value) return null
  const trimmed = value.trim()
  if (!trimmed) return null
  const timestamp = Date.parse(trimmed)
  if (!Number.isFinite(timestamp)) return null
  return new Date(timestamp).toISOString()
}

function normalizeIdentifier(value: string | null) {
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
    basinVersionId: normalizeIdentifier(params.get('basinVersionId')),
    segmentId: normalizeIdentifier(params.get('segmentId')),
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
  if (normalized.segmentId) params.set('segmentId', normalized.segmentId)
  if (normalized.warningLevel) params.set('warningLevel', normalized.warningLevel)
  if (normalized.q) params.set('q', normalized.q)

  return params.toString()
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


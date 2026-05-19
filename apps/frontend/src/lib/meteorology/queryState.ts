export type MeteorologyTab = 'grid' | 'stations'
export type MeteorologyVariable = 'PRCP' | 'TEMP' | 'RH' | 'wind' | 'Rn' | 'Press'
export type MeteorologySource = 'GFS' | 'IFS' | 'ERA5' | 'CLDAS' | 'Best Available'
export type MeteorologySort = 'latest' | 'completeness' | 'station_id'

export interface MeteorologyQueryState {
  tab: MeteorologyTab
  variable: MeteorologyVariable
  source: MeteorologySource
  validTime: string | null
  opacity: number
  contours: boolean
  stationOverlay: boolean
  compareSource: MeteorologySource | null
  basin: string | null
  search: string | null
  sort: MeteorologySort
  stationId: string | null
}

export type MeteorologyQueryPatch = Partial<Record<keyof MeteorologyQueryState, string | number | boolean | null | undefined>>

export const meteorologyVariables: MeteorologyVariable[] = ['PRCP', 'TEMP', 'RH', 'wind', 'Rn', 'Press']
export const meteorologySources: MeteorologySource[] = ['GFS', 'IFS', 'ERA5', 'CLDAS', 'Best Available']
export const meteorologyTabs: MeteorologyTab[] = ['grid', 'stations']
export const meteorologySorts: MeteorologySort[] = ['latest', 'completeness', 'station_id']

export const defaultMeteorologyQueryState: MeteorologyQueryState = {
  tab: 'grid',
  variable: 'PRCP',
  source: 'Best Available',
  validTime: null,
  opacity: 72,
  contours: false,
  stationOverlay: true,
  compareSource: null,
  basin: null,
  search: null,
  sort: 'latest',
  stationId: null,
}

const isoInstantPattern = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$/

function isOneOf<T extends readonly string[]>(value: string | null, values: T): value is T[number] {
  return value !== null && (values as readonly string[]).includes(value)
}

function normalizeInstant(value: string | null) {
  if (!value?.trim()) return null
  const trimmed = value.trim()
  if (!isoInstantPattern.test(trimmed)) return null
  const timestamp = Date.parse(trimmed)
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : null
}

function normalizeIdentifier(value: string | null) {
  if (!value?.trim()) return null
  const trimmed = value.trim()
  return /^[A-Za-z0-9._:-]{1,96}$/.test(trimmed) ? trimmed : null
}

function normalizeSearch(value: string | null) {
  if (!value?.trim()) return null
  const trimmed = value.trim()
  return trimmed.length <= 80 ? trimmed : trimmed.slice(0, 80)
}

function normalizeOpacity(value: string | null) {
  if (!value) return defaultMeteorologyQueryState.opacity
  const parsed = Number.parseInt(value, 10)
  if (!Number.isFinite(parsed)) return defaultMeteorologyQueryState.opacity
  return Math.min(100, Math.max(10, parsed))
}

function parseBoolean(value: string | null, fallback: boolean) {
  if (value === '1' || value === 'true') return true
  if (value === '0' || value === 'false') return false
  return fallback
}

export function parseMeteorologyQueryState(input: string | URLSearchParams): MeteorologyQueryState {
  const params = typeof input === 'string' ? new URLSearchParams(input) : input
  const tab = params.get('tab')
  const variable = params.get('variable')
  const source = params.get('source')
  const compareSource = params.get('compareSource')
  const sort = params.get('sort')

  return {
    tab: isOneOf(tab, meteorologyTabs) ? tab : defaultMeteorologyQueryState.tab,
    variable: isOneOf(variable, meteorologyVariables) ? variable : defaultMeteorologyQueryState.variable,
    source: isOneOf(source, meteorologySources) ? source : defaultMeteorologyQueryState.source,
    validTime: normalizeInstant(params.get('validTime')),
    opacity: normalizeOpacity(params.get('opacity')),
    contours: parseBoolean(params.get('contours'), defaultMeteorologyQueryState.contours),
    stationOverlay: parseBoolean(params.get('stationOverlay'), defaultMeteorologyQueryState.stationOverlay),
    compareSource: isOneOf(compareSource, meteorologySources) ? compareSource : null,
    basin: normalizeIdentifier(params.get('basin')),
    search: normalizeSearch(params.get('search')),
    sort: isOneOf(sort, meteorologySorts) ? sort : defaultMeteorologyQueryState.sort,
    stationId: normalizeIdentifier(params.get('stationId')),
  }
}

export function serializeMeteorologyQueryState(state: MeteorologyQueryState) {
  const params = new URLSearchParams()
  params.set('tab', state.tab)
  if (state.variable !== defaultMeteorologyQueryState.variable) params.set('variable', state.variable)
  if (state.source !== defaultMeteorologyQueryState.source) params.set('source', state.source)
  if (state.validTime) params.set('validTime', state.validTime)
  if (state.opacity !== defaultMeteorologyQueryState.opacity) params.set('opacity', String(state.opacity))
  if (state.contours !== defaultMeteorologyQueryState.contours) params.set('contours', state.contours ? '1' : '0')
  if (state.stationOverlay !== defaultMeteorologyQueryState.stationOverlay) params.set('stationOverlay', state.stationOverlay ? '1' : '0')
  if (state.compareSource) params.set('compareSource', state.compareSource)
  if (state.basin) params.set('basin', state.basin)
  if (state.search) params.set('search', state.search)
  if (state.sort !== defaultMeteorologyQueryState.sort) params.set('sort', state.sort)
  if (state.stationId) params.set('stationId', state.stationId)
  return params.toString()
}

export function mergeMeteorologyQueryState(state: MeteorologyQueryState, patch: MeteorologyQueryPatch) {
  const params = new URLSearchParams(serializeMeteorologyQueryState(state))
  Object.entries(patch).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') {
      params.delete(key)
    } else {
      params.set(key, String(value))
    }
  })
  return parseMeteorologyQueryState(params)
}

export function needsMeteorologyQueryReplacement(search: string) {
  const normalized = serializeMeteorologyQueryState(parseMeteorologyQueryState(search))
  const current = search.startsWith('?') ? search.slice(1) : search
  return normalized !== current
}

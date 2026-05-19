export type MeteorologyTab = 'grid' | 'stations'
export type MeteorologyVariable = 'PRCP' | 'TEMP' | 'RH' | 'wind' | 'Rn' | 'Press'
export type MeteorologySource = 'GFS' | 'IFS' | 'ERA5' | 'CLDAS' | 'Best Available'
export type MeteorologySort = 'latest' | 'completeness' | 'station_id'

export interface MeteorologyQueryState {
  tab: MeteorologyTab
  variable: MeteorologyVariable
  source: MeteorologySource
  validTime: string | null
  gridQueryLon: number | null
  gridQueryLat: number | null
  areaMinLon: number | null
  areaMinLat: number | null
  areaMaxLon: number | null
  areaMaxLat: number | null
  opacity: number
  contours: boolean
  stationOverlay: boolean
  compareSource: MeteorologySource | null
  basin: string | null
  search: string | null
  searchValidationLength: number | null
  searchValidationReason: string | null
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
  gridQueryLon: null,
  gridQueryLat: null,
  areaMinLon: null,
  areaMinLat: null,
  areaMaxLon: null,
  areaMaxLat: null,
  opacity: 72,
  contours: false,
  stationOverlay: true,
  compareSource: null,
  basin: null,
  search: null,
  searchValidationLength: null,
  searchValidationReason: null,
  sort: 'latest',
  stationId: null,
}

const searchMaxLength = 80

const rfc3339InstantPattern = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$/

function isOneOf<T extends readonly string[]>(value: string | null, values: T): value is T[number] {
  return value !== null && (values as readonly string[]).includes(value)
}

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
  return parseInteger(value.slice(1, 4).padEnd(3, '0'))
}

function normalizeInstant(value: string | null) {
  if (!value?.trim()) return null
  const trimmed = value.trim()
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

function normalizeIdentifier(value: string | null) {
  if (!value?.trim()) return null
  const trimmed = value.trim()
  return /^[A-Za-z0-9._:-]{1,96}$/.test(trimmed) ? trimmed : null
}

function normalizeSearch(value: string | null) {
  if (!value?.trim()) return null
  const trimmed = value.trim()
  return trimmed.length <= searchMaxLength ? trimmed : trimmed.slice(0, searchMaxLength)
}

function normalizeSearchValidationLength(value: string | null, preservedLength: string | null) {
  const rawLength = value?.trim().length ?? 0
  const parsedPreservedLength = preservedLength ? Number.parseInt(preservedLength, 10) : NaN
  const validationLength = Number.isFinite(parsedPreservedLength) ? parsedPreservedLength : rawLength
  return validationLength > searchMaxLength ? validationLength : null
}

function normalizeSearchValidationReason(value: string | null, preservedLength: string | null) {
  const validationLength = normalizeSearchValidationLength(value, preservedLength)
  return validationLength ? `搜索词原始长度 ${validationLength} 超过 ${searchMaxLength} 字符，已按合同截断。` : null
}

function normalizeOpacity(value: string | null) {
  if (!value) return defaultMeteorologyQueryState.opacity
  const parsed = Number.parseInt(value, 10)
  if (!Number.isFinite(parsed)) return defaultMeteorologyQueryState.opacity
  return Math.min(100, Math.max(10, parsed))
}

function normalizeCoordinate(value: string | null) {
  if (!value) return null
  const parsed = Number.parseFloat(value)
  return Number.isFinite(parsed) ? parsed : null
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
    gridQueryLon: normalizeCoordinate(params.get('gridQueryLon')),
    gridQueryLat: normalizeCoordinate(params.get('gridQueryLat')),
    areaMinLon: normalizeCoordinate(params.get('areaMinLon')),
    areaMinLat: normalizeCoordinate(params.get('areaMinLat')),
    areaMaxLon: normalizeCoordinate(params.get('areaMaxLon')),
    areaMaxLat: normalizeCoordinate(params.get('areaMaxLat')),
    opacity: normalizeOpacity(params.get('opacity')),
    contours: parseBoolean(params.get('contours'), defaultMeteorologyQueryState.contours),
    stationOverlay: parseBoolean(params.get('stationOverlay'), defaultMeteorologyQueryState.stationOverlay),
    compareSource: isOneOf(compareSource, meteorologySources) ? compareSource : null,
    basin: normalizeIdentifier(params.get('basin')),
    search: normalizeSearch(params.get('search')),
    searchValidationLength: normalizeSearchValidationLength(params.get('search'), params.get('searchValidationLength')),
    searchValidationReason: normalizeSearchValidationReason(params.get('search'), params.get('searchValidationLength')),
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
  if (state.gridQueryLon !== null) params.set('gridQueryLon', state.gridQueryLon.toFixed(4))
  if (state.gridQueryLat !== null) params.set('gridQueryLat', state.gridQueryLat.toFixed(4))
  if (state.areaMinLon !== null) params.set('areaMinLon', state.areaMinLon.toFixed(4))
  if (state.areaMinLat !== null) params.set('areaMinLat', state.areaMinLat.toFixed(4))
  if (state.areaMaxLon !== null) params.set('areaMaxLon', state.areaMaxLon.toFixed(4))
  if (state.areaMaxLat !== null) params.set('areaMaxLat', state.areaMaxLat.toFixed(4))
  if (state.opacity !== defaultMeteorologyQueryState.opacity) params.set('opacity', String(state.opacity))
  if (state.contours !== defaultMeteorologyQueryState.contours) params.set('contours', state.contours ? '1' : '0')
  if (state.stationOverlay !== defaultMeteorologyQueryState.stationOverlay) params.set('stationOverlay', state.stationOverlay ? '1' : '0')
  if (state.compareSource) params.set('compareSource', state.compareSource)
  if (state.basin) params.set('basin', state.basin)
  if (state.search) params.set('search', state.search)
  if (state.searchValidationLength && state.searchValidationLength > searchMaxLength) params.set('searchValidationLength', String(state.searchValidationLength))
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
  if (Object.hasOwn(patch, 'search')) {
    params.delete('searchValidationLength')
  }
  return parseMeteorologyQueryState(params)
}

export function needsMeteorologyQueryReplacement(search: string) {
  const normalized = serializeMeteorologyQueryState(parseMeteorologyQueryState(search))
  const current = search.startsWith('?') ? search.slice(1) : search
  return normalized !== current
}

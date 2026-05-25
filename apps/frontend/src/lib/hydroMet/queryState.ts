export type HydroMetSource = 'GFS' | 'IFS'

export interface HydroMetQueryState {
  source: HydroMetSource
  cycle: string | null
  validationReasons: string[]
}

export type HydroMetQueryPatch = Partial<Pick<HydroMetQueryState, 'source' | 'cycle'>>

export const hydroMetSources: HydroMetSource[] = ['GFS', 'IFS']

export const defaultHydroMetQueryState: HydroMetQueryState = {
  source: 'GFS',
  cycle: null,
  validationReasons: [],
}

const rfc3339InstantPattern = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$/

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

export function normalizeHydroMetCycle(value: string | null | undefined) {
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

function parseSource(value: string | null, reasons: string[]) {
  if (!value?.trim()) return defaultHydroMetQueryState.source
  const normalized = value.trim().toUpperCase()
  if (normalized === 'GFS' || normalized === 'IFS') return normalized
  reasons.push(`source=${value} 不属于 GFS/IFS，已更正为 GFS。`)
  return defaultHydroMetQueryState.source
}

function parseCycle(value: string | null, reasons: string[]) {
  if (!value?.trim()) return null
  const normalized = normalizeHydroMetCycle(value)
  if (normalized) return normalized
  reasons.push(`cycle=${value} 不是有效 RFC3339 UTC 时间，已移除。`)
  return null
}

export function parseHydroMetQueryState(input: string | URLSearchParams): HydroMetQueryState {
  const params = typeof input === 'string' ? new URLSearchParams(input) : input
  const validationReasons: string[] = []
  const source = parseSource(params.get('source'), validationReasons)
  const cycle = parseCycle(params.get('cycle'), validationReasons)

  return {
    source,
    cycle,
    validationReasons,
  }
}

export function serializeHydroMetQueryState(state: HydroMetQueryState) {
  const params = new URLSearchParams()
  params.set('source', state.source)
  if (state.cycle) params.set('cycle', state.cycle)
  return params.toString()
}

export function mergeHydroMetQueryState(state: HydroMetQueryState, patch: HydroMetQueryPatch) {
  const params = new URLSearchParams(serializeHydroMetQueryState(state))
  Object.entries(patch).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') {
      params.delete(key)
    } else {
      params.set(key, String(value))
    }
  })
  return parseHydroMetQueryState(params)
}

export function needsHydroMetQueryReplacement(search: string) {
  const normalized = serializeHydroMetQueryState(parseHydroMetQueryState(search))
  const current = search.startsWith('?') ? search.slice(1) : search
  return normalized !== current
}

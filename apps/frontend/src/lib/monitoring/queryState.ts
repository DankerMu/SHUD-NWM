export type MonitoringSource = 'GFS' | 'IFS' | 'ERA5'

export interface MonitoringQueryState {
  source: MonitoringSource | null
  cycle: string | null
  sourceError: string | null
  cycleError: string | null
}

export const monitoringSources: MonitoringSource[] = ['GFS', 'IFS', 'ERA5']

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

export function normalizeMonitoringQueryCycle(value: string | null | undefined) {
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

function parseMonitoringSource(value: string | null) {
  if (value === null) return { source: null, error: null }
  const trimmed = value.trim()
  if (!trimmed) {
    return { source: null, error: 'source 参数为空；监控仅支持 GFS、IFS、ERA5。' }
  }

  const normalized = trimmed.toUpperCase()
  if (normalized === 'GFS' || normalized === 'IFS' || normalized === 'ERA5') {
    return { source: normalized, error: null }
  }

  return { source: null, error: `source=${trimmed} 不支持；监控仅支持 GFS、IFS、ERA5。` }
}

function parseMonitoringCycle(value: string | null) {
  if (value === null || !value.trim()) return { cycle: null, error: null }
  const normalized = normalizeMonitoringQueryCycle(value)
  if (normalized) return { cycle: normalized, error: null }
  return { cycle: null, error: `cycle=${value} 不是有效 RFC3339 时间。` }
}

export function parseMonitoringQueryState(input: string | URLSearchParams): MonitoringQueryState {
  const params = typeof input === 'string' ? new URLSearchParams(input) : input
  const source = parseMonitoringSource(params.get('source'))
  const cycle = parseMonitoringCycle(params.get('cycle'))

  return {
    source: source.source,
    cycle: cycle.cycle,
    sourceError: source.error,
    cycleError: cycle.error,
  }
}

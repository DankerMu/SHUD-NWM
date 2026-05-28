export type HydroMetSource = 'GFS' | 'IFS'

export interface HydroMetQueryState {
  source: HydroMetSource
  cycle: string | null
  strictIdentity: HydroMetStrictIdentity | null
  strictIdentityError: string | null
  validationReasons: string[]
}

export type HydroMetQueryPatch = Partial<Pick<HydroMetQueryState, 'source' | 'cycle'>>

export interface HydroMetStrictIdentity {
  source: HydroMetSource
  cycleTime: string
  runId: string
  modelId: string
}

export const hydroMetSources: HydroMetSource[] = ['GFS', 'IFS']

export const defaultHydroMetQueryState: HydroMetQueryState = {
  source: 'GFS',
  cycle: null,
  strictIdentity: null,
  strictIdentityError: null,
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

function parseStrictToken(value: string | null) {
  if (!value?.trim()) return null
  return value.trim()
}

function parseStrictSource(value: string | null) {
  if (!value?.trim()) return null
  const normalized = value.trim().toUpperCase()
  return normalized === 'GFS' || normalized === 'IFS' ? normalized : null
}

function parseStrictIdentity(params: URLSearchParams) {
  const strictParamPresent = params.has('cycle_time') || params.has('run_id') || params.has('model_id')
  if (!strictParamPresent) return { identity: null, error: null }

  const sourceValue = params.get('source')
  const cycleTimeValue = params.get('cycle_time')
  const runId = parseStrictToken(params.get('run_id'))
  const modelId = parseStrictToken(params.get('model_id'))
  const missing: string[] = []
  if (!sourceValue?.trim()) missing.push('source')
  if (!cycleTimeValue?.trim()) missing.push('cycle_time')
  if (!runId) missing.push('run_id')
  if (!modelId) missing.push('model_id')

  const source = parseStrictSource(sourceValue)
  const cycleTime = normalizeHydroMetCycle(cycleTimeValue)
  if (sourceValue?.trim() && !source) return { identity: null, error: `source=${sourceValue} 不属于 GFS/IFS。` }
  if (cycleTimeValue?.trim() && !cycleTime) return { identity: null, error: `cycle_time=${cycleTimeValue} 不是有效 RFC3339 UTC 时间。` }
  if (missing.length > 0) {
    return {
      identity: null,
      error: `严格 handoff 参数不完整：缺少 ${missing.join(', ')}；需要 source、cycle_time、run_id、model_id。`,
    }
  }

  return {
    identity: {
      source: source as HydroMetSource,
      cycleTime: cycleTime as string,
      runId: runId as string,
      modelId: modelId as string,
    },
    error: null,
  }
}

export function parseHydroMetQueryState(input: string | URLSearchParams): HydroMetQueryState {
  const params = typeof input === 'string' ? new URLSearchParams(input) : input
  const validationReasons: string[] = []
  const strictIdentity = parseStrictIdentity(params)
  const source = strictIdentity.identity?.source ?? parseSource(params.get('source'), validationReasons)
  const cycle = strictIdentity.identity?.cycleTime ?? parseCycle(params.get('cycle'), validationReasons)

  return {
    source,
    cycle,
    strictIdentity: strictIdentity.identity,
    strictIdentityError: strictIdentity.error,
    validationReasons,
  }
}

export function serializeHydroMetQueryState(state: HydroMetQueryState) {
  const params = new URLSearchParams()
  params.set('source', state.source)
  if (state.strictIdentity) {
    params.set('cycle_time', state.strictIdentity.cycleTime)
    params.set('run_id', state.strictIdentity.runId)
    params.set('model_id', state.strictIdentity.modelId)
  } else if (state.cycle) {
    params.set('cycle', state.cycle)
  }
  return params.toString()
}

export function mergeHydroMetQueryState(state: HydroMetQueryState, patch: HydroMetQueryPatch) {
  const params = new URLSearchParams()
  params.set('source', patch.source ?? state.source)
  const nextCycle = Object.prototype.hasOwnProperty.call(patch, 'cycle') ? patch.cycle : state.cycle
  if (nextCycle) params.set('cycle', nextCycle)
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
  const state = parseHydroMetQueryState(search)
  if (state.strictIdentityError) return false
  const normalized = serializeHydroMetQueryState(state)
  const current = search.startsWith('?') ? search.slice(1) : search
  return normalized !== current
}

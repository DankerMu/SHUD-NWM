export const HYDRO_MET_COORDINATES_UNAVAILABLE = '坐标不可用'

const HYDRO_MET_REDACTED_FILE_URI = '受限文件 URI'
const HYDRO_MET_REDACTED_LOCAL_PATH = '本地路径已隐藏'

const uriPattern = /\b[a-z][a-z0-9+.-]*:\/\/[^\s<>"'`]+/gi
const windowsAbsolutePathPattern = /(^|[\s([{:;"'=])([A-Za-z]:\\(?:[^\s\\/:*?"<>|]+\\)*[^\s\\/:*?"<>|]+)/g
const unixAbsolutePathPattern = /(^|[\s([{:;"'=])\/(?:[A-Za-z0-9._~+%@-]+\/)+[A-Za-z0-9._~+%@-]+/g

export interface HydroMetStationCoordinates {
  lon: number
  lat: number
}

interface HydroMetStationRuntimeShape {
  geom?: {
    type?: unknown
    coordinates?: unknown
  } | null
  longitude?: unknown
  latitude?: unknown
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function splitTrailingUriPunctuation(value: string) {
  let core = value
  let trailing = ''
  while (/[),.;]$/.test(core)) {
    trailing = core.slice(-1) + trailing
    core = core.slice(0, -1)
  }
  return { core, trailing }
}

function sanitizeUri(value: string) {
  if (/^file:\/\//i.test(value)) return HYDRO_MET_REDACTED_FILE_URI

  try {
    const parsed = new URL(value)
    if (parsed.protocol.toLowerCase() === 'file:') return HYDRO_MET_REDACTED_FILE_URI
    return `${parsed.protocol}//${parsed.host}${parsed.pathname}`
  } catch {
    return value.replace(/\/\/[^/@\s]+@/, '//').replace(/[?#].*$/, '')
  }
}

export function sanitizeHydroMetMessage(value: string, fallback = '详情已脱敏') {
  if (!value.trim()) return fallback
  const safeUriTokens: string[] = []
  let text = value.replace(uriPattern, (match) => {
    const { core, trailing } = splitTrailingUriPunctuation(match)
    const sanitized = sanitizeUri(core)
    if (sanitized === HYDRO_MET_REDACTED_FILE_URI) return `${sanitized}${trailing}`

    const token = `__HYDRO_MET_SAFE_URI_${safeUriTokens.length}__`
    safeUriTokens.push(sanitized)
    return `${token}${trailing}`
  })

  text = text
    .replace(windowsAbsolutePathPattern, (_match, prefix) => `${prefix}${HYDRO_MET_REDACTED_LOCAL_PATH}`)
    .replace(unixAbsolutePathPattern, (_match, prefix) => `${prefix}${HYDRO_MET_REDACTED_LOCAL_PATH}`)

  safeUriTokens.forEach((tokenValue, index) => {
    text = text.replace(`__HYDRO_MET_SAFE_URI_${index}__`, tokenValue)
  })

  const sanitized = text.replace(/\s{2,}/g, ' ').trim()
  return sanitized || fallback
}

export function getHydroMetStationCoordinates(station: unknown): HydroMetStationCoordinates | null {
  if (!station || typeof station !== 'object') return null
  const runtimeStation = station as HydroMetStationRuntimeShape
  const coordinates = runtimeStation.geom?.coordinates
  if (Array.isArray(coordinates)) {
    const lon = finiteNumber(coordinates[0])
    const lat = finiteNumber(coordinates[1])
    if (lon !== null && lat !== null) return { lon, lat }
  }

  const lon = finiteNumber(runtimeStation.longitude)
  const lat = finiteNumber(runtimeStation.latitude)
  return lon !== null && lat !== null ? { lon, lat } : null
}

export function normalizeHydroMetStation<T>(station: T): T {
  if (!station || typeof station !== 'object') return station
  const coordinates = getHydroMetStationCoordinates(station)
  if (!coordinates) return station

  const runtimeStation = station as HydroMetStationRuntimeShape
  if (Array.isArray(runtimeStation.geom?.coordinates)) return station

  return {
    ...(station as Record<string, unknown>),
    geom: {
      type: 'Point',
      coordinates: [coordinates.lon, coordinates.lat],
    },
  } as T
}

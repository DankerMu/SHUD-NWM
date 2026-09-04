import {
  RIVER_CLICK_GFS_SCENARIO,
  RIVER_CLICK_IFS_SCENARIO,
  RIVER_CLICK_PREFLIGHT_ARRAY_MAX_LENGTH,
  RIVER_CLICK_PREFLIGHT_MAX_BODY_BYTES,
  RIVER_CLICK_PREFLIGHT_MAX_DEPTH,
  RIVER_CLICK_PREFLIGHT_MAX_NODES,
  RIVER_CLICK_PREFLIGHT_OBJECT_MAX_WIDTH,
  RIVER_CLICK_VARIABLE,
} from './constants'

/**
 * Pure bounded preflight parsing for the live river-click lane (#1970).
 * This module MUST stay importable from Node (Playwright) without the `@/`
 * vite alias, so it never imports app modules; the geometry-budget limits are
 * pinned to the same constants as the app's
 * getM11SelectedSegmentGeometryBudgetStatus and locked by a parity test.
 */

export interface RiverClickPreflightResponseSource {
  url(): string
  status(): number
  headerValue(name: string): Promise<string | null>
  /**
   * Stream/retain at most maxBytes + 1 bytes so the caller can detect an
   * over-ceiling body without ever retaining byte 262145.
   */
  readBounded(maxBytes: number): Promise<Uint8Array>
}

export type RiverClickPreflightClassification =
  | { ok: true; status: number; contentType: string; bodyBytes: number; payload: unknown }
  | { ok: false; code: 'PREFLIGHT_HTTP_ERROR' | 'PREFLIGHT_RESPONSE_INVALID'; message: string }

export interface RiverClickProductSourceIdentity {
  source_id: 'GFS' | 'IFS'
  basin_id: string
  basin_version_id: string
  river_network_version_id: string
  run_id: string
  model_id: string
  cycle_time: string
  scenario: string
}

export type RiverClickPreflightProduct =
  | { ok: true; product: RiverClickProductSourceIdentity }
  | { ok: false; reason: string }

export type Geometry2D = { type: 'LineString'; coordinates: number[][] } | { type: 'MultiLineString'; coordinates: number[][][] }

export interface RiverClickPreflightSegmentOk {
  segmentId: string
  riverNetworkVersionId: string
  geometry: Geometry2D
  bbox: [[number, number], [number, number]]
  anchor: [number, number]
  coordinateCount: number
}

export type RiverClickPreflightSegment =
  | { ok: true; segment: RiverClickPreflightSegmentOk }
  | { ok: false; reason: string }

/** Mirrors the app m11SelectedSegmentGeometryBudget limits (parity-tested). */
export const RIVER_CLICK_SEGMENT_GEOMETRY_MAX_COORDINATES = 10_000
export const RIVER_CLICK_SEGMENT_GEOMETRY_MAX_DIMENSIONS = 3
export const RIVER_CLICK_SEGMENT_GEOMETRY_MAX_SERIALIZED_BYTES = 250_000

export function normalizeRiverClickEnvelope(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const envelope = value as Record<string, unknown>
  if (envelope.status !== 'ok') return null
  if (!envelope.data || typeof envelope.data !== 'object' || Array.isArray(envelope.data)) return null
  return envelope.data as Record<string, unknown>
}

function jsonComplexityWithinBounds(value: unknown): boolean {
  const stack: Array<{ value: unknown; depth: number }> = [{ value, depth: 1 }]
  let nodes = 0
  while (stack.length > 0) {
    const current = stack.pop() as { value: unknown; depth: number }
    nodes += 1
    if (nodes > RIVER_CLICK_PREFLIGHT_MAX_NODES) return false
    if (current.depth > RIVER_CLICK_PREFLIGHT_MAX_DEPTH) return false
    const item = current.value
    if (item && typeof item === 'object' && !Array.isArray(item)) {
      const keys = Object.keys(item)
      if (keys.length > RIVER_CLICK_PREFLIGHT_OBJECT_MAX_WIDTH) return false
      for (const key of keys) stack.push({ value: (item as Record<string, unknown>)[key], depth: current.depth + 1 })
    } else if (Array.isArray(item)) {
      if (item.length > RIVER_CLICK_PREFLIGHT_ARRAY_MAX_LENGTH) return false
      for (const entry of item) stack.push({ value: entry, depth: current.depth + 1 })
    }
  }
  return true
}

function decodeUtf8Fatal(bytes: Uint8Array): string | null {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  } catch {
    return null
  }
}

/**
 * Classify one bounded preflight response. 2xx only; request identity
 * content-encoding; declared content-length must not exceed 262144 bytes; the
 * bounded reader aborts before retaining byte 262145 when the length is absent
 * or false; the retained bytes must be valid UTF-8 JSON within depth 12 /
 * object width 64 / array length 10000 / 50000 nodes and decode as
 * {status:"ok",data:{...}}.
 */
export async function classifyRiverClickPreflightResponse(
  response: RiverClickPreflightResponseSource,
): Promise<RiverClickPreflightClassification> {
  // The URL is deliberately never included in a failure message: receipt text
  // must not carry raw URL/query material (fixture C2). No untrusted
  // header/product/status/cycle/basin value is ever interpolated into a
  // message; every failure text is fixed or carries only closed integers.
  const status = response.status()
  if (status < 200 || status > 299) {
    return { ok: false, code: 'PREFLIGHT_HTTP_ERROR', message: `preflight response status ${status}` }
  }

  let contentEncoding: string | null = null
  try {
    contentEncoding = await response.headerValue('content-encoding')
  } catch {
    return { ok: false, code: 'PREFLIGHT_RESPONSE_INVALID', message: 'preflight response content-encoding header is unreadable' }
  }
  if (contentEncoding && contentEncoding.trim().toLowerCase() !== 'identity') {
    return {
      ok: false,
      code: 'PREFLIGHT_RESPONSE_INVALID',
      message: 'preflight response must use identity content-encoding',
    }
  }

  let contentLength: string | null = null
  try {
    contentLength = await response.headerValue('content-length')
  } catch {
    return { ok: false, code: 'PREFLIGHT_RESPONSE_INVALID', message: 'preflight response content-length header is unreadable' }
  }
  if (contentLength !== null && contentLength.trim() !== '') {
    const parsed = Number(contentLength)
    if (!Number.isInteger(parsed) || parsed < 0 || parsed > RIVER_CLICK_PREFLIGHT_MAX_BODY_BYTES) {
      return {
        ok: false,
        code: 'PREFLIGHT_RESPONSE_INVALID',
        message: `preflight response content-length exceeds the ${RIVER_CLICK_PREFLIGHT_MAX_BODY_BYTES} byte bound`,
      }
    }
  }

  let bytes: Uint8Array
  try {
    bytes = await response.readBounded(RIVER_CLICK_PREFLIGHT_MAX_BODY_BYTES)
  } catch {
    return { ok: false, code: 'PREFLIGHT_RESPONSE_INVALID', message: 'preflight response body read failed' }
  }
  if (bytes.byteLength > RIVER_CLICK_PREFLIGHT_MAX_BODY_BYTES) {
    return {
      ok: false,
      code: 'PREFLIGHT_RESPONSE_INVALID',
      message: `preflight response body exceeds the ${RIVER_CLICK_PREFLIGHT_MAX_BODY_BYTES} byte bound`,
    }
  }

  const text = decodeUtf8Fatal(bytes)
  if (text === null) {
    return { ok: false, code: 'PREFLIGHT_RESPONSE_INVALID', message: 'preflight response body is not valid UTF-8' }
  }

  let payload: unknown
  try {
    payload = JSON.parse(text)
  } catch {
    return { ok: false, code: 'PREFLIGHT_RESPONSE_INVALID', message: 'preflight response body is not valid JSON' }
  }
  if (!jsonComplexityWithinBounds(payload)) {
    return { ok: false, code: 'PREFLIGHT_RESPONSE_INVALID', message: 'preflight response body exceeds JSON complexity bounds' }
  }
  if (normalizeRiverClickEnvelope(payload) === null) {
    return { ok: false, code: 'PREFLIGHT_RESPONSE_INVALID', message: 'preflight response must decode as {status:"ok",data:{...}}' }
  }

  return { ok: true, status, contentType: 'identity', bodyBytes: bytes.byteLength, payload }
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function nonEmptyString(value: unknown, maxBytes: number): string | null {
  if (typeof value !== 'string' || value.length === 0) return null
  if (new TextEncoder().encode(value).byteLength > maxBytes) return null
  return value
}

const RFC3339_INSTANT_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$/

function parseInteger(value: string): number {
  return Number.parseInt(value, 10)
}

function fractionalMilliseconds(value: string | undefined): number {
  if (value === undefined || value === '') return 0
  return Number.parseInt(`${value}000`.slice(0, 3), 10)
}

function offsetMinutes(zone: string): number | null {
  if (zone === 'Z') return 0
  const sign = zone[0] === '+' ? 1 : -1
  const hours = parseInteger(zone.slice(1, 3))
  const minutes = parseInteger(zone.slice(4, 6))
  if (hours > 23 || minutes > 59) return null
  return sign * (hours * 60 + minutes)
}

/**
 * Strict RFC3339 UTC/offset instant that is calendar-valid, normalized to the
 * canonical Date#toISOString() ('.000Z') form the shipping frontend sends as
 * issue_time (loadHydroMetRiverForecast -> normalizeHydroMetCycle ->
 * toISOString). Rejects '2026-02-30T...', hour 24, minute 60, and any zone the
 * arithmetic cannot reproduce from the given components.
 */
export function normalizeRiverClickCycleTime(value: string): string | null {
  const match = RFC3339_INSTANT_PATTERN.exec(value)
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
  if (year < 0 || year > 9999) return null
  if (month < 1 || month > 12) return null
  if (hour > 23 || minute > 59 || second > 59) return null
  if (day < 1 || day > 31) return null

  const timestamp = Date.UTC(year, month - 1, day, hour, minute, second, millisecond) - offset * 60_000
  if (!Number.isFinite(timestamp)) return null

  // Round-trip verification against the input's own wall-clock components
  // (offset shifted back): this rejects Feb 30, hour 24, minute 60, etc.
  const local = new Date(timestamp + offset * 60_000)
  if (
    local.getUTCFullYear() !== year ||
    local.getUTCMonth() !== month - 1 ||
    local.getUTCDate() !== day ||
    local.getUTCHours() !== hour ||
    local.getUTCMinutes() !== minute ||
    local.getUTCSeconds() !== second ||
    local.getUTCMilliseconds() !== millisecond
  ) {
    return null
  }
  return new Date(timestamp).toISOString()
}

/**
 * One current identity-only GFS/IFS product: exact source and basin, ready
 * status, availability.ready !== false, valid nonempty run/model/cycle/version
 * identity. Returns only closed fields plus the fixed scenario.
 */
export function parseRiverClickPreflightProduct(
  payload: Record<string, unknown>,
  source: 'GFS' | 'IFS',
  basinId: string,
): RiverClickPreflightProduct {
  // Never interpolate an untrusted product/header value into a failure message:
  // the source/basin mismatch is a fixed closed classification.
  if (payload.status !== 'ready') return { ok: false, reason: 'product status must be ready' }
  const availability = payload.availability as Record<string, unknown> | undefined
  if (availability && availability.ready === false) return { ok: false, reason: 'product availability.ready is false' }
  const sourceId = nonEmptyString(payload.source_id, 64)
  if (sourceId !== source) return { ok: false, reason: 'product source does not match the requested source' }
  const basin = nonEmptyString(payload.basin_id, 256)
  if (basin !== basinId) return { ok: false, reason: 'product basin_id does not match the requested basin' }
  const basinVersionId = nonEmptyString(payload.basin_version_id, 256)
  const riverNetworkVersionId = nonEmptyString(payload.river_network_version_id, 256)
  const runId = nonEmptyString(payload.run_id, 256)
  const modelId = nonEmptyString(payload.model_id, 256)
  const cycleTime = nonEmptyString(payload.cycle_time, 64)
  if (!basinVersionId || !riverNetworkVersionId || !runId || !modelId || !cycleTime) {
    return { ok: false, reason: 'product run/model/cycle/version identity must be nonempty' }
  }
  const canonicalCycleTime = normalizeRiverClickCycleTime(cycleTime)
  if (canonicalCycleTime === null) {
    return { ok: false, reason: 'product cycle_time is not an RFC3339 UTC instant' }
  }

  const scenario = source === 'GFS' ? RIVER_CLICK_GFS_SCENARIO : RIVER_CLICK_IFS_SCENARIO
  return {
    ok: true,
    product: {
      source_id: source,
      basin_id: basinId,
      basin_version_id: basinVersionId,
      river_network_version_id: riverNetworkVersionId,
      run_id: runId,
      model_id: modelId,
      cycle_time: canonicalCycleTime,
      scenario,
    },
  }
}

function sanitizeCoordinate(raw: unknown): { ok: true; coordinate: number[] } | { ok: false } {
  if (!Array.isArray(raw) || raw.length < 2 || raw.length > RIVER_CLICK_SEGMENT_GEOMETRY_MAX_DIMENSIONS) return { ok: false }
  const lon = finiteNumber(raw[0])
  const lat = finiteNumber(raw[1])
  if (lon === null || lat === null) return { ok: false }
  if (lon < -180 || lon > 180 || lat < -90 || lat > 90) return { ok: false }
  const elevation = raw.length >= 3 ? finiteNumber(raw[2]) : null
  // The shipping app budget preserves a FINITE third coordinate (kept in the
  // sanitized geometry and counted in the serialized bytes); a non-finite or
  // non-numeric third coordinate is malformed, and extra dimensions are a
  // dimension-budget refusal below.
  if (raw.length >= 3 && elevation === null) return { ok: false }
  return { ok: true, coordinate: elevation === null ? [lon, lat] : [lon, lat, elevation] }
}

/** Smallest documented bounded WGS84 epsilon used to de-degenerate the bbox. */
export const RIVER_CLICK_BBOX_EPSILON_DEG = 1e-6

function bboxFromCoordinates(coordinates: number[][]): RiverClickPreflightSegmentOk['bbox'] | null {
  if (coordinates.length === 0) return null
  let minLon = Number.POSITIVE_INFINITY
  let minLat = Number.POSITIVE_INFINITY
  let maxLon = Number.NEGATIVE_INFINITY
  let maxLat = Number.NEGATIVE_INFINITY
  for (const [lon, lat] of coordinates) {
    minLon = Math.min(minLon, lon)
    minLat = Math.min(minLat, lat)
    maxLon = Math.max(maxLon, lon)
    maxLat = Math.max(maxLat, lat)
  }
  if (!Number.isFinite(minLon) || !Number.isFinite(minLat) || !Number.isFinite(maxLon) || !Number.isFinite(maxLat)) return null
  if (minLon === maxLon && minLat === maxLat) return null
  // Pad a zero-width or zero-height extent by the bounded WGS84 epsilon so the
  // hook never rejects an otherwise valid axis-aligned segment framing, while
  // still covering every coordinate and clamping at the world edges.
  let outMinLon = minLon
  let outMinLat = minLat
  let outMaxLon = maxLon
  let outMaxLat = maxLat
  const clampLon = (value: number) => Math.min(180, Math.max(-180, value))
  const clampLat = (value: number) => Math.min(90, Math.max(-90, value))
  if (outMinLon === outMaxLon) {
    outMinLon = clampLon(outMinLon - RIVER_CLICK_BBOX_EPSILON_DEG)
    outMaxLon = clampLon(outMaxLon + RIVER_CLICK_BBOX_EPSILON_DEG)
    if (outMinLon === outMaxLon) {
      if (maxLon >= 180) {
        outMinLon = 180 - RIVER_CLICK_BBOX_EPSILON_DEG
        outMaxLon = 180
      } else if (minLon <= -180) {
        outMinLon = -180
        outMaxLon = -180 + RIVER_CLICK_BBOX_EPSILON_DEG
      }
    }
  }
  if (outMinLat === outMaxLat) {
    outMinLat = clampLat(outMinLat - RIVER_CLICK_BBOX_EPSILON_DEG)
    outMaxLat = clampLat(outMaxLat + RIVER_CLICK_BBOX_EPSILON_DEG)
    if (outMinLat === outMaxLat) {
      if (maxLat >= 90) {
        outMinLat = 90 - RIVER_CLICK_BBOX_EPSILON_DEG
        outMaxLat = 90
      } else if (minLat <= -90) {
        outMinLat = -90
        outMaxLat = -90 + RIVER_CLICK_BBOX_EPSILON_DEG
      }
    }
  }
  return [[outMinLon, outMinLat], [outMaxLon, outMaxLat]]
}

function serializedByteLength(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value)).byteLength
}

function sanitizeGeometry(
  geom: Record<string, unknown>,
): {
  ok: true
  geometry: Geometry2D
  coordinateCount: number
  bbox: RiverClickPreflightSegmentOk['bbox']
  /** Every sanitized coordinate in SOURCE order (including parts that are later
   *  skipped by the >=2-point retention rule). The frozen contract indexes the
   *  anchor on the flattened SOURCE coordinate index floor((count-1)/2), so a
   *  skipped one-point part still contributes its coordinate to the anchor
   *  position while the shipping geometry/bbox keep retained parts only. */
  sourceFlat: number[][]
} | { ok: false } {
  if (geom.type === 'LineString' && Array.isArray(geom.coordinates)) {
    const coordinates: number[][] = []
    let count = 0
    for (const raw of geom.coordinates) {
      count += 1
      if (count > RIVER_CLICK_SEGMENT_GEOMETRY_MAX_COORDINATES) return { ok: false }
      const sanitized = sanitizeCoordinate(raw)
      if (!sanitized.ok) return { ok: false }
      coordinates.push(sanitized.coordinate)
    }
    // LineString requires at least two sanitized coordinates (the app owner
    // rejects a shorter line).
    if (coordinates.length < 2) return { ok: false }
    const bbox = bboxFromCoordinates(coordinates)
    if (bbox === null) return { ok: false }
    const geometry: Geometry2D = { type: 'LineString', coordinates }
    // Serialized-byte accounting happens AFTER sanitization and INCLUDES any
    // preserved finite third dimension, exactly like the shipping app owner.
    if (serializedByteLength(geometry) > RIVER_CLICK_SEGMENT_GEOMETRY_MAX_SERIALIZED_BYTES) return { ok: false }
    return { ok: true, geometry, coordinateCount: count, bbox, sourceFlat: coordinates }
  }
  if (geom.type === 'MultiLineString' && Array.isArray(geom.coordinates)) {
    const parts: number[][][] = []
    const sourceFlat: number[][] = []
    let count = 0
    for (const part of geom.coordinates) {
      if (!Array.isArray(part) || part.length === 0) return { ok: false }
      const coordinates: number[][] = []
      for (const raw of part) {
        count += 1
        if (count > RIVER_CLICK_SEGMENT_GEOMETRY_MAX_COORDINATES) return { ok: false }
        const sanitized = sanitizeCoordinate(raw)
        if (!sanitized.ok) return { ok: false }
        coordinates.push(sanitized.coordinate)
      }
      // A part with fewer than two sanitized points is SKIPPED (not a refusal)
      // as long as at least one valid part remains — the app owner behaves the
      // same way (sanitizeSegmentLineCoordinates + length>=2 filter).
      if (coordinates.length >= 2) parts.push(coordinates)
      sourceFlat.push(...coordinates)
    }
    if (parts.length === 0) return { ok: false }
    const flat = parts.flat()
    const bbox = bboxFromCoordinates(flat)
    if (bbox === null) return { ok: false }
    const geometry: Geometry2D = { type: 'MultiLineString', coordinates: parts }
    if (serializedByteLength(geometry) > RIVER_CLICK_SEGMENT_GEOMETRY_MAX_SERIALIZED_BYTES) return { ok: false }
    return { ok: true, geometry, coordinateCount: count, bbox, sourceFlat }
  }
  return { ok: false }
}

/**
 * Segment detail: exact path/network identity and payload-root geom passing
 * the same LineString/MultiLineString budget as the app. Bbox covers all
 * sanitized coordinates; anchor is flattened-source coordinate index
 * floor((coordinateCount - 1) / 2).
 */
export function parseRiverClickPreflightSegment(
  payload: Record<string, unknown>,
  segmentId: string,
  riverNetworkVersionId: string,
  basinVersionId: string,
): RiverClickPreflightSegment {
  if (nonEmptyString(payload.river_segment_id, 256) !== segmentId) {
    return { ok: false, reason: `segment river_segment_id must be ${segmentId}` }
  }
  if (nonEmptyString(payload.river_network_version_id, 256) !== riverNetworkVersionId) {
    return { ok: false, reason: `segment river_network_version_id must be ${riverNetworkVersionId}` }
  }
  const geom = payload.geom
  if (!geom || typeof geom !== 'object' || Array.isArray(geom)) {
    return { ok: false, reason: 'segment geom is unavailable or malformed' }
  }
  const sanitized = sanitizeGeometry(geom as Record<string, unknown>)
  if (!sanitized.ok) {
    return { ok: false, reason: 'segment geom fails the bounded LineString/MultiLineString geometry budget' }
  }
  // The frozen contract: anchor = flattened SOURCE coordinate index
  // floor((sourceCount - 1) / 2). sourceFlat keeps source order even when a
  // one-point MultiLineString part was skipped from the shipment geometry, so
  // the anchor stays on the source line while bbox/geometry stay retained-only.
  const anchorIndex = Math.floor((sanitized.coordinateCount - 1) / 2)
  const anchor = sanitized.sourceFlat[anchorIndex]
  if (!anchor) return { ok: false, reason: 'segment geom has no anchor coordinate' }

  return {
    ok: true,
    segment: {
      segmentId,
      riverNetworkVersionId,
      geometry: sanitized.geometry,
      bbox: sanitized.bbox,
      anchor: [anchor[0], anchor[1]],
      coordinateCount: sanitized.coordinateCount,
    },
  }
}
